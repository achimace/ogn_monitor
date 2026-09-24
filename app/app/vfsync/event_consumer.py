"""Redis PubSub consumer: ``event:{slug}`` -> SyncCoordinator.on_event.

Subscribes with ``psubscribe("event:*")`` and resolves the slug of every
message to a TenantConfig via ``tenants_provider`` (called per message so
config reloads take effect immediately). Messages of unknown / disabled
slugs are dropped. One failing message never stops the consumer.
"""

import asyncio
import inspect
import json
from datetime import datetime
from typing import Any, Callable

import structlog

from app.config import settings
from app.vfsync.models import TenantConfig, utcnow

log = structlog.get_logger()

EVENT_PATTERN = "event:*"


class EventConsumer:
    """Consumes flight events for enabled tenants and feeds the coordinator."""

    def __init__(
        self,
        redis: Any,
        tenants_provider: Callable[[], list[TenantConfig]],
        coordinator: Any,
        on_activity: Callable[[], Any] | None = None,
        pattern: str = EVENT_PATTERN,
        reconnect_delay_s: float | None = None,
    ) -> None:
        self._redis = redis
        self._tenants_provider = tenants_provider
        self._coordinator = coordinator
        self._on_activity = on_activity
        self._pattern = pattern
        self._reconnect_delay_s = (
            settings.vfsync_pubsub_reconnect_s if reconnect_delay_s is None else reconnect_delay_s
        )
        self.last_event_ts: datetime | None = None
        self.events_handled = 0
        self.events_ignored = 0
        self.events_failed = 0

    # ------------------------------------------------------------------

    def _tenant_for(self, slug: str) -> TenantConfig | None:
        for t in self._tenants_provider():
            if t.slug == slug and t.enabled:
                return t
        return None

    async def handle_message(self, channel: str | bytes, raw: str | bytes) -> bool:
        """Process one PubSub message. Returns True if it reached the coordinator."""
        if isinstance(channel, bytes):
            channel = channel.decode("utf-8", "replace")
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", "replace")

        parts = channel.split(":", 1)
        if len(parts) != 2 or parts[0] != "event":
            return False
        slug = parts[1]
        tenant = self._tenant_for(slug)
        if tenant is None:
            self.events_ignored += 1
            return False

        try:
            event = json.loads(raw)
        except json.JSONDecodeError:
            self.events_failed += 1
            log.warning("vfsync_event_invalid_json", slug=slug)
            return False
        if not isinstance(event, dict):
            self.events_failed += 1
            log.warning("vfsync_event_not_an_object", slug=slug)
            return False

        try:
            await self._coordinator.on_event(tenant, event)
        except asyncio.CancelledError:
            raise
        except Exception:
            self.events_failed += 1
            log.exception("vfsync_event_failed", slug=slug,
                          type=event.get("type"), flarm_id=event.get("flarm_id"))
            return False

        self.events_handled += 1
        self.last_event_ts = utcnow()
        if self._on_activity is not None:
            result = self._on_activity()
            if inspect.isawaitable(result):
                await result
        return True

    # ------------------------------------------------------------------

    async def run(self) -> None:
        """Subscribe and consume until cancelled; re-subscribes after Redis errors."""
        while True:
            pubsub = self._redis.pubsub()
            try:
                await pubsub.psubscribe(self._pattern)
                log.info("vfsync_consumer_started", pattern=self._pattern)
                async for message in pubsub.listen():
                    if message.get("type") != "pmessage":
                        continue
                    await self.handle_message(message["channel"], message["data"])
                log.info("vfsync_consumer_stream_ended")
                return
            except asyncio.CancelledError:
                log.info("vfsync_consumer_stopping", handled=self.events_handled)
                raise
            except Exception:
                log.exception("vfsync_consumer_error", retry_in_s=self._reconnect_delay_s)
            finally:
                await self._close(pubsub)
            await asyncio.sleep(self._reconnect_delay_s)

    async def _close(self, pubsub: Any) -> None:
        for step in (lambda: pubsub.punsubscribe(self._pattern), pubsub.close):
            try:
                result = step()
                if inspect.isawaitable(result):
                    await result
            except Exception:  # pragma: no cover - best effort on shutdown
                log.debug("vfsync_consumer_close_failed", exc_info=True)

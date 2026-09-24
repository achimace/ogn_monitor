"""Alerting for the VF-Sync worker (R-12).

Two parts:
- `evaluate()` - pure rule evaluation over a monitoring snapshot
  (feed dead > 30 min, budget > 80 %, session pending > 24 h, write-error
  streak, login forbidden).
- `AlertSink` - delivery via ntfy (HTTP POST), e-mail (SMTP settings of the
  app) or the log. Alerts are de-duplicated per key so a persisting
  condition does not spam the channel (re-sent after `repeat_after`).
"""

import asyncio
import smtplib
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from typing import Callable

import httpx
import structlog

from app.config import settings

log = structlog.get_logger()

FEED_DEAD_AFTER = timedelta(minutes=30)
BUDGET_WARN_RATIO = 0.8
SESSION_PENDING_AFTER = timedelta(hours=24)
WRITE_ERROR_STREAK = 3
DEFAULT_REPEAT_AFTER = timedelta(hours=6)


@dataclass(frozen=True)
class Alert:
    key: str          # de-duplication key, e.g. "feed_dead", "budget:ohlstadt"
    level: str        # info | warning | critical
    title: str
    message: str


@dataclass
class TenantSnapshot:
    slug: str
    budget_used: int
    daily_budget: int
    oldest_pending_age: timedelta | None = None   # oldest open session not completed
    write_error_streak: int = 0
    login_forbidden: bool = False


@dataclass
class MonitoringSnapshot:
    now: datetime
    last_event_ts: datetime | None
    worker_started_at: datetime
    tenants: list[TenantSnapshot] = field(default_factory=list)


def evaluate(snap: MonitoringSnapshot) -> list[Alert]:
    """Rules R-12; pure and deterministic."""
    alerts: list[Alert] = []

    reference = snap.last_event_ts or snap.worker_started_at
    if snap.tenants and snap.now - reference > FEED_DEAD_AFTER:
        minutes = int((snap.now - reference).total_seconds() // 60)
        alerts.append(Alert(
            key="feed_dead", level="critical",
            title="VF-Sync: kein Flugereignis",
            message=f"Seit {minutes} min kein Event vom APRS-Worker (Feed tot?)",
        ))

    for t in snap.tenants:
        if t.daily_budget > 0:
            ratio = t.budget_used / t.daily_budget
            if ratio >= 1.0:
                alerts.append(Alert(
                    key=f"budget_stop:{t.slug}", level="critical",
                    title=f"VF-Sync {t.slug}: Budget erschoepft",
                    message=f"{t.budget_used}/{t.daily_budget} Requests - Hard-Stop, alles wird gequeued",
                ))
            elif ratio >= BUDGET_WARN_RATIO:
                alerts.append(Alert(
                    key=f"budget:{t.slug}", level="warning",
                    title=f"VF-Sync {t.slug}: Budget > 80 %",
                    message=f"{t.budget_used}/{t.daily_budget} Requests heute ({ratio:.0%})",
                ))
        if t.oldest_pending_age is not None and t.oldest_pending_age > SESSION_PENDING_AFTER:
            hours = int(t.oldest_pending_age.total_seconds() // 3600)
            alerts.append(Alert(
                key=f"pending:{t.slug}", level="warning",
                title=f"VF-Sync {t.slug}: Session haengt",
                message=f"Aelteste offene Session seit {hours} h nicht abgeschlossen",
            ))
        if t.write_error_streak >= WRITE_ERROR_STREAK:
            alerts.append(Alert(
                key=f"write_errors:{t.slug}", level="critical",
                title=f"VF-Sync {t.slug}: Schreibfehlerserie",
                message=f"{t.write_error_streak} Schreibversuche in Folge fehlgeschlagen",
            ))
        if t.login_forbidden:
            alerts.append(Alert(
                key=f"login:{t.slug}", level="critical",
                title=f"VF-Sync {t.slug}: Login abgelehnt (403)",
                message="Credentials/2FA pruefen - Mandant pausiert",
            ))
    return alerts


class AlertSink:
    """Delivers alerts to the configured channels with de-duplication."""

    def __init__(
        self,
        ntfy_url: str | None = None,
        email_to: str | None = None,
        *,
        http: httpx.AsyncClient | None = None,
        send_mail: Callable[[EmailMessage], None] | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        repeat_after: timedelta = DEFAULT_REPEAT_AFTER,
    ):
        self._ntfy_url = ntfy_url if ntfy_url is not None else settings.vfsync_alert_ntfy_url
        self._email_to = email_to if email_to is not None else settings.vfsync_alert_email_to
        self._http = http
        self._send_mail = send_mail or _smtp_send
        self._clock = clock
        self._repeat_after = repeat_after
        self._last_sent: dict[str, datetime] = {}
        self.sent: list[Alert] = []   # for tests / health

    @property
    def channels(self) -> list[str]:
        ch = []
        if self._ntfy_url:
            ch.append("ntfy")
        if self._email_to and settings.smtp_host:
            ch.append("email")
        return ch or ["log"]

    def should_send(self, alert: Alert) -> bool:
        last = self._last_sent.get(alert.key)
        return last is None or self._clock() - last >= self._repeat_after

    def clear(self, key: str) -> None:
        """Condition resolved: allow the next occurrence to alert immediately."""
        self._last_sent.pop(key, None)

    async def send(self, alert: Alert) -> bool:
        """Deliver if not recently sent. Returns True if delivered."""
        if not self.should_send(alert):
            return False
        self._last_sent[alert.key] = self._clock()
        self.sent.append(alert)
        log.warning("vfsync_alert", key=alert.key, level=alert.level,
                    title=alert.title, message=alert.message)
        if self._ntfy_url:
            await self._send_ntfy(alert)
        if self._email_to and settings.smtp_host:
            await self._send_email(alert)
        return True

    async def send_all(self, alerts: list[Alert]) -> int:
        active = {a.key for a in alerts}
        # conditions that disappeared may alert again next time
        for key in list(self._last_sent):
            if key not in active:
                self._last_sent.pop(key)
        n = 0
        for a in alerts:
            if await self.send(a):
                n += 1
        return n

    async def _send_ntfy(self, alert: Alert) -> None:
        priority = {"critical": "5", "warning": "4"}.get(alert.level, "3")
        headers = {"Title": alert.title, "Priority": priority,
                   "Tags": "warning" if alert.level != "info" else "information_source"}
        try:
            client = self._http or httpx.AsyncClient(timeout=10.0)
            try:
                resp = await client.post(self._ntfy_url, content=alert.message.encode("utf-8"),
                                         headers=headers)
                resp.raise_for_status()
            finally:
                if self._http is None:
                    await client.aclose()
        except Exception as exc:  # alerting must never crash the worker
            log.error("vfsync_alert_ntfy_failed", key=alert.key, error=str(exc))

    async def _send_email(self, alert: Alert) -> None:
        msg = EmailMessage()
        msg["Subject"] = f"[{alert.level.upper()}] {alert.title}"
        msg["From"] = settings.smtp_user or "vfsync@localhost"
        msg["To"] = self._email_to
        msg.set_content(alert.message)
        try:
            await asyncio.get_running_loop().run_in_executor(None, self._send_mail, msg)
        except Exception as exc:
            log.error("vfsync_alert_email_failed", key=alert.key, error=str(exc))


def _smtp_send(msg: EmailMessage) -> None:
    """Blocking SMTP send with the app's SMTP settings (run in executor)."""
    with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=15) as smtp:
        smtp.starttls()
        if settings.smtp_user:
            smtp.login(settings.smtp_user, settings.smtp_pass)
        smtp.send_message(msg)

"""Resilient APRS-IS client for OGN data streaming.

Maintains a TCP connection to aprs.glidernet.org with:
- Auto-reconnect with exponential backoff
- Keepalive ping every 60 seconds
- Beacon timeout detection (120s no data = dead connection)
- Health status tracking for monitoring endpoint
- Dynamic filter updates when airfields change

IMPORTANT: This is a Singleton - only ONE instance connects to OGN.
"""

import asyncio
import time
from typing import Callable, Awaitable

import structlog

from app.config import settings

log = structlog.get_logger()

# Timing constants
KEEPALIVE_INTERVAL = 60       # Send keepalive every 60s
BEACON_TIMEOUT = 120          # No data for 120s -> connection dead
RECONNECT_BASE_DELAY = 1      # Initial reconnect delay (seconds)
RECONNECT_MAX_DELAY = 300     # Max reconnect delay (5 minutes)

# OGN login format (read-only, pass -1)
_LOGIN_FMT = "user {callsign} pass -1 vers OGNFlightMonitor 2.0 filter {filters}\r\n"


class APRSClient:
    """Resilient APRS-IS TCP client for OGN beacon streaming.

    Args:
        callsign: APRS-IS login callsign.
        on_beacon: Async callback for each received beacon line.
        on_health_change: Async callback when health status changes.
    """

    def __init__(
        self,
        callsign: str,
        on_beacon: Callable[[str], Awaitable[None]],
        on_health_change: Callable[[dict], Awaitable[None]] | None = None,
    ):
        self.callsign = callsign
        self.on_beacon = on_beacon
        self.on_health_change = on_health_change

        self.server = settings.ogn_server
        self.port = settings.ogn_port

        # State
        self.connected = False
        self.filters: list[str] = []
        self._writer: asyncio.StreamWriter | None = None
        self._shutdown = asyncio.Event()

        # Health metrics
        self._start_time = time.monotonic()
        self._last_data_time: float = 0
        self._beacon_count = 0
        self._beacon_count_prev = 0
        self._beacon_rate_time = time.monotonic()
        self._beacons_per_minute = 0
        self._reconnect_count = 0

    async def run(self, filters: list[str]) -> None:
        """Main loop: connect, stream, auto-reconnect on failure.

        Args:
            filters: Initial APRS-IS filter strings.
        """
        self.filters = filters
        delay = RECONNECT_BASE_DELAY

        while not self._shutdown.is_set():
            try:
                await self._connect_and_stream()
                delay = RECONNECT_BASE_DELAY  # Reset on successful connection
            except (ConnectionError, asyncio.TimeoutError, OSError) as e:
                self.connected = False
                self._reconnect_count += 1
                log.warning(
                    "aprs_connection_lost",
                    error=str(e),
                    reconnect_delay=delay,
                    reconnect_count=self._reconnect_count,
                )
                await self._notify_health()

                # Wait before reconnect (interruptible by shutdown)
                try:
                    await asyncio.wait_for(
                        self._shutdown.wait(), timeout=delay
                    )
                    break  # Shutdown requested
                except asyncio.TimeoutError:
                    pass  # Timeout expired, try reconnecting

                delay = min(delay * 2, RECONNECT_MAX_DELAY)
            except asyncio.CancelledError:
                break

        self.connected = False
        await self._close_writer()
        log.info("aprs_client_stopped")

    async def stop(self) -> None:
        """Signal the client to shut down gracefully."""
        self._shutdown.set()
        await self._close_writer()

    async def update_filters(self, filters: list[str]) -> None:
        """Update APRS-IS filters on the fly.

        Sends a new #filter command to the server without reconnecting.
        """
        self.filters = filters
        if self._writer and self.connected:
            filter_str = " ".join(filters)
            cmd = f"#filter {filter_str}\r\n"
            try:
                self._writer.write(cmd.encode("ascii"))
                await self._writer.drain()
                log.info("aprs_filter_updated", filters=filters)
            except Exception as e:
                log.warning("aprs_filter_update_failed", error=str(e))

    def health_status(self) -> dict:
        """Current health status for /api/health/ogn endpoint."""
        now = time.monotonic()
        age = now - self._last_data_time if self._last_data_time > 0 else -1

        # Calculate beacon rate
        elapsed = now - self._beacon_rate_time
        if elapsed >= 60:
            self._beacons_per_minute = int(
                (self._beacon_count - self._beacon_count_prev) * 60 / elapsed
            )
            self._beacon_count_prev = self._beacon_count
            self._beacon_rate_time = now

        return {
            "connected": self.connected,
            "last_beacon_age_s": round(age, 1) if age >= 0 else -1,
            "beacons_per_minute": self._beacons_per_minute,
            "reconnect_count": self._reconnect_count,
            "uptime_s": int(now - self._start_time),
            "total_beacons": self._beacon_count,
        }

    async def _connect_and_stream(self) -> None:
        """Establish connection, login, and stream beacons."""
        log.info(
            "aprs_connecting",
            server=self.server,
            port=self.port,
            filters=self.filters,
        )

        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(self.server, self.port),
            timeout=10,
        )
        self._writer = writer

        # Read server banner
        banner = await asyncio.wait_for(reader.readline(), timeout=10)
        log.info("aprs_banner", banner=banner.decode("utf-8", errors="replace").strip())

        # Send login
        filter_str = " ".join(self.filters)
        login_cmd = _LOGIN_FMT.format(
            callsign=self.callsign,
            filters=filter_str,
        )
        writer.write(login_cmd.encode("ascii"))
        await writer.drain()

        # Read login response
        response = await asyncio.wait_for(reader.readline(), timeout=10)
        response_str = response.decode("utf-8", errors="replace").strip()
        log.info("aprs_login_response", response=response_str)

        if "unverified" not in response_str.lower() and "verified" not in response_str.lower():
            raise ConnectionError(f"Unexpected login response: {response_str}")

        self.connected = True
        self._last_data_time = time.monotonic()
        await self._notify_health()

        log.info("aprs_connected", filters=self.filters)

        # Start keepalive task
        keepalive_task = asyncio.create_task(self._keepalive_loop(writer))

        try:
            while not self._shutdown.is_set():
                try:
                    line_bytes = await asyncio.wait_for(
                        reader.readline(),
                        timeout=BEACON_TIMEOUT,
                    )
                except asyncio.TimeoutError:
                    raise ConnectionError(
                        f"APRS-IS beacon timeout ({BEACON_TIMEOUT}s)"
                    )

                if not line_bytes:
                    raise ConnectionError("APRS-IS connection closed by server")

                self._last_data_time = time.monotonic()

                line = line_bytes.decode("utf-8", errors="replace").strip()
                if not line:
                    continue

                # Skip server comments/keepalives
                if line.startswith("#"):
                    continue

                self._beacon_count += 1
                await self.on_beacon(line)

        finally:
            keepalive_task.cancel()
            try:
                await keepalive_task
            except asyncio.CancelledError:
                pass
            await self._close_writer()

    async def _keepalive_loop(self, writer: asyncio.StreamWriter) -> None:
        """Send periodic keepalive pings to prevent connection timeout."""
        while True:
            await asyncio.sleep(KEEPALIVE_INTERVAL)
            try:
                writer.write(b"#keepalive\r\n")
                await writer.drain()
            except Exception:
                break

    async def _close_writer(self) -> None:
        """Close the TCP writer safely."""
        if self._writer:
            try:
                self._writer.close()
                await self._writer.wait_closed()
            except Exception:
                pass
            self._writer = None

    async def _notify_health(self) -> None:
        """Push health status update."""
        if self.on_health_change:
            await self.on_health_change(self.health_status())

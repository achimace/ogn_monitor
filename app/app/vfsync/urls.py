"""vf_base_url policy (Konzept Kap. 8.4): TLS mandatory, host allow-list.

Used by the API (PUT /api/vfsync/config) and the operator CLI so a tenant
can never point the worker - which sends the technical user's credentials
- at an arbitrary host (SSRF / plaintext).
"""

from urllib.parse import urlsplit

from app.config import settings


def allowed_hosts() -> set[str]:
    return {h.strip().lower() for h in settings.vfsync_allowed_hosts.split(",") if h.strip()}


def validate_base_url(url: str) -> str:
    """Return the normalised URL or raise ValueError with a German message."""
    url = (url or "").strip().rstrip("/")
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if not host or parts.path not in ("", "/") or parts.query or parts.fragment:
        raise ValueError("vf_base_url muss die Form https://host sein")
    if parts.scheme != "https":
        if not (parts.scheme == "http" and settings.vfsync_allow_insecure_base_url):
            raise ValueError("vf_base_url muss https:// verwenden")
    if not settings.vfsync_allow_insecure_base_url and host not in allowed_hosts():
        raise ValueError(f"Host {host} ist nicht freigegeben ({', '.join(sorted(allowed_hosts()))})")
    return f"{parts.scheme}://{parts.netloc}"

"""Redis keys and channels shared by the VF-Sync worker and the API.

Only string constants live here so the API can import them without
pulling worker internals (stores, coordinator, event consumer).

- ``VFSYNC_HEALTH_KEY``      hash written by the worker, read by the status endpoint
- ``VFSYNC_CONFIG_CHANNEL``  PubSub channel; payload = airfield slug whose
  ``vf_sync_config`` row was just written (CLI tools, API PUT). The worker
  reloads its tenant list immediately instead of waiting for the periodic
  ``VFSYNC_CONFIG_RELOAD_S`` poll.
"""

VFSYNC_HEALTH_KEY = "vfsync:health"
VFSYNC_CONFIG_CHANNEL = "vfsync:config"

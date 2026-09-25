"""Redis channels shared by the APRS worker and the API.

Only string constants live here so the API can import them without
pulling worker internals (state machine, tracker).

- ``TRACKER_CONFIG_CHANNEL``  PubSub channel; payload = airfield slug whose
  tracking configuration (currently: the per-airfield ignore list
  ``airfield_ignored_aircraft``) was just changed by the API. The worker
  reloads its airfield configs immediately instead of waiting for the
  periodic reload (``app.worker.CONFIG_RELOAD_INTERVAL_S``).
"""

TRACKER_CONFIG_CHANNEL = "tracker:config"

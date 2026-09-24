"""VF-Sync: Vereinsflieger.de integration worker.

Consumes flight events from the APRS worker (Redis PubSub), matches them
to Vereinsflieger flights and writes times / tow heights / landing counts
under strict write invariants. Specification: docs/konzept-vf-sync.md,
interfaces: docs/dev-guides/vfsync-internals.md.
"""

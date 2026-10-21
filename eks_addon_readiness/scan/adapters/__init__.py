"""Transports that satisfy `scan.ports.ClusterReader`.

Kept out of `scan/__init__.py` on purpose. Stages 3-5 must import `scan` — the
digest-frozen `facts/gaps.py` does `from ..scan import snapshot_digest` — and
nothing about a gap ledger needs an MCP client on the import path.
"""

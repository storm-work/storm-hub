"""Vendored proven browser engine from storm-auto (session.py + solver.py + proxy_bridge.py).

Copied verbatim (2026-08-04) because the clean rewrite could not pass DataDome's bd.js challenge
(A/B: 0/18) while this exact code does (113 confirmed bookings). It's the one module with a
months-long anti-detection moat; the rest of storm-hub stays clean. Refactor later.

The files import each other top-level (`import solver`, `from proxy_bridge import start_bridge`), so
we put this directory on sys.path before importing — keeping the vendored code UNCHANGED.
"""
from __future__ import annotations

import os
import sys

_VENDOR_DIR = os.path.dirname(os.path.abspath(__file__))
if _VENDOR_DIR not in sys.path:
    sys.path.insert(0, _VENDOR_DIR)

from session import PlaywrightSession, get_session  # noqa: E402

__all__ = ["get_session", "PlaywrightSession"]

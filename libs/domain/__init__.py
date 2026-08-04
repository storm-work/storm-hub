"""storm-hub domain layer — pure, dependency-free (spec §3/§4/§6).

Entities, enums, state machines, settings taxonomy + resolver, and invariants. No DB, no network,
no framework — everything here is unit-testable in isolation and is the single source of truth the
persistence layer (migrations) and the API build on.
"""
from __future__ import annotations

from . import enums, rules, state_machines
from .settings import CATALOG, resolve, validate_layer

__all__ = ["enums", "rules", "state_machines", "CATALOG", "resolve", "validate_layer"]

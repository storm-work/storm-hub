"""storm-hub portal engine — clean rewrite of storm-auto's confirmed automation (spec §8).

Pure, offline-testable logic (period parsing, gettime date-window computation, settings→config
mapping, the phase-machine skeleton) lives here and is unit-tested against captured fixtures. The
live browser/login/book layer (Playwright, solver, proxy) is wired in Phase 2b and driven by the
per-identity worker runtime (apps/worker).
"""
from . import discovery
from .config import EngineConfig
from .phases import EngineOps, PhaseMachine, PhaseResult

__all__ = ["discovery", "EngineConfig", "EngineOps", "PhaseMachine", "PhaseResult"]

"""Which reCAPTCHA solver + which key(s) one consumer (a booking worker, a registration account) uses.

This sits ABOVE the vendored engine's race_all (libs/engine/vendor/solver.py), which still races
whatever key lists it is handed. Two operator asks it implements:

  * SELECTION -- use the solver chosen in settings (RECAPTCHA_SOLVER / REGISTRATION_SOLVER), not an
    unconditional race over every provider.
  * LOAD DISTRIBUTION -- when a solver has several keys, hand each worker ONE key (round-robin), so N
    keys spread the load instead of every worker hammering key #1.

Pure + stateless (the index is supplied by the caller), so it is unit-tested with no Hub.
"""
from __future__ import annotations

SOLVERS = ("capsolver", "anticaptcha", "race")


def parse_keys(raw) -> list[str]:
    """Comma-separated setting value -> clean list (blanks dropped)."""
    return [k.strip() for k in str(raw or "").split(",") if k.strip()]


def rr_index(discriminator: str) -> int:
    """A stable non-negative index for round-robin key assignment, derived from a per-consumer string
    (worker id / sub-account id). Stable so the same worker keeps its key across relaunches; spread so
    a fleet divides evenly enough across the key list. Not cryptographic -- distribution, not secrecy."""
    import hashlib
    return int(hashlib.md5(str(discriminator or "").encode()).hexdigest()[:8], 16)


def active_solver_lists(solver: str, capsolver_all, anticaptcha_all) -> tuple[list[str], list[str]]:
    """The FULL key lists active for a selection -- for consumers that DISTRIBUTE INTERNALLY (the
    registrar cycles per account) or are POOL-WIDE (the login-token factory races the whole set).
    Same selection rules as resolve_solver_keys, but without the per-consumer pick:
      capsolver -> (caps, []),  anticaptcha -> ([], ac) (capsolver fallback if no ac),  race -> (caps, ac)."""
    cs = list(capsolver_all or [])
    ac = list(anticaptcha_all or [])
    s = str(solver or "capsolver").strip().lower()
    if s == "anticaptcha":
        return (([] if ac else cs), ac)
    if s == "race":
        return (cs, ac)
    return (cs, [])


def resolve_solver_keys(solver: str, capsolver_all, anticaptcha_all, *,
                        index: int = 0) -> tuple[list[str], list[str]]:
    """(capsolver_keys, anticaptcha_keys) for ONE consumer.

      capsolver   -> one capsolver key, no anticaptcha.
      anticaptcha -> one anticaptcha key; FALLS BACK to one capsolver key if no anticaptcha keys are
                     configured (a misconfigured selection must not brick login).
      race        -> one key from EACH available solver (the engine races them).

    `index` distributes across the chosen list: consumer k gets key[k % len]. Empty lists yield [].
    """
    cs = list(capsolver_all or [])
    ac = list(anticaptcha_all or [])

    def pick(lst: list[str]) -> list[str]:
        return [lst[index % len(lst)]] if lst else []

    s = str(solver or "capsolver").strip().lower()
    if s == "anticaptcha":
        chosen_ac = pick(ac)
        return ([] if chosen_ac else pick(cs)), chosen_ac
    if s == "race":
        return pick(cs), pick(ac)
    return pick(cs), []   # 'capsolver' (default / unknown)

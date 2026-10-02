# storm-hub — Engineering Memory

**Purpose.** This directory is the project's durable engineering knowledge: what we have
verified, what we have decided, what we still do not know, and how the system actually
behaves. It is version-controlled deliberately — before 2026-08-25 every accumulated fact
about this system lived only in an assistant-private memory directory
(`~/.claude/projects/-srv-projects-storm-hub/memory/`), outside the repo, unreviewable and
unversioned. That was itself a failure mode: knowledge that cannot be reviewed cannot be
corrected.

**The repository is the source of truth for implementation. This directory is the source of
truth for accumulated knowledge.** When they disagree, investigate, decide which is right,
and update this directory. Never silently pick one.

## Files

| File | Contents |
|---|---|
| `ARCHITECTURE.md` | Components, runtime flow, orchestration, worker/session lifecycle, observability |
| `PROXY_CONTRACTS.md` | Per-provider proxy contracts — credential grammar, sticky semantics, rotation, TTLs |
| `CONFIRMED_FACTS.md` | Only facts with stated evidence. Each carries its evidence and date. |
| `DECISIONS.md` | Deliberate engineering choices, with the reasoning and what would reverse them |
| `FAILURE_MODES.md` | Known and suspected failure modes in a fixed schema |
| `OPEN_QUESTIONS.md` | What we do not know, and what evidence would settle it |
| `REPORT_<date>_*.md` | Point-in-time analyses, preserved as written — NOT living docs |
| `SYSTEM_MEMORY.md` | This file — purpose, discipline, and the snapshot status |

## Epistemic discipline

Every claim in this directory carries exactly one of these labels:

- **CONFIRMED** — established by repository code, configuration, a passing test, a
  reproducible measurement, a log, or an explicit operator decision. States its evidence.
- **OBSERVED** — seen at least once, not yet reproduced or fully explained.
- **HYPOTHESIS** — a proposed explanation awaiting verification. Never build on one without
  saying so.
- **DECISION** — a choice we made. Not a fact about the world.
- **UNKNOWN** — named explicitly rather than guessed at.

Never silently promote hypothesis → fact, assumption → fact, or workaround → design. When
evidence overturns an entry, record the old statement, the new evidence, and the date; keep
enough history that the change is understandable. An entry that is merely *old* is not
thereby wrong — but an entry whose supporting measurement has been invalidated must be
downgraded immediately, even if its conclusion still seems right.

## Working rule for future sessions

1. Read this directory. 2. Read the relevant code. 3. **Verify the memory still matches the
code** — never skip this. 4. Identify the exact problem. 5. Gather evidence. 6. Minimal fix.
7. Test. 8. Check regressions. 9. Update this directory. 10. Report what changed and what
risk remains.

## Status of this snapshot

Originally written 2026-08-25 from a full read of the repository at branch `apply-revert-proven`,
commit `dc133fb` (883 tests). **Last reconciled 2026-10-02** against branch `request-manual-review`
(base `pt-version`, commit `81d67a2`): **1306 tests across 70 files**. The living line has since
diverged onto `pt-version` — the confirmed apply path is now the PT in-page **XHR submit** (a real
`%PDF` captured), the consecutive-portal-down booking breaker was **removed** (2026-09-23, replaced
by a record-only portal-liveness badge), and discovery, proxy-geo, login-token-pool and
cross-booking-feed subsystems were added. `ARCHITECTURE.md` §12 is the roll-up; `CONFIRMED_FACTS.md`,
`DECISIONS.md` and `FAILURE_MODES.md` carry the dated entries. This directory remains a
reconstruction of current understanding, not a design document — several sections still record that
the system does *not* do what its own comments claim.

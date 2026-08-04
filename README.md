# storm-hub

Control plane for portal-booking automation: a dashboard drives a **Manager Hub** that owns
bookings, identities, proxies, worker templates and settings, and orchestrates **one isolated
worker process per identity**. The proven portal engine (from `storm-auto`) is rewritten clean
behind a stable interface.

Full design: [`../storm-auto`'s approved spec] — see `docs/SPEC.md` (mirrors the plan file).

## Layout

```
apps/       hub (FastAPI REST+WS), worker (per-identity runtime), registrar, dashboard (React+Vite)
libs/
  domain/   PURE: enums, state machines, settings taxonomy + resolver, invariants (no DB/framework)
  db/       SQLAlchemy models (§3 schema) + engine wiring
  engine/   portal automation (session/solver/login/discovery/booking/hold_swap/pool)  [Phase 2]
  bus/      Redis streams (cmd/evt) + signal pubsub                                     [Phase 3]
  proxies/  central proxy-lease service                                                [Phase 3]
  telegram/                                                                            [Phase 6]
migrations/ Alembic (Postgres; portable types bootstrap on SQLite)
tests/      pytest
```

## Design invariants (enforced)

- **Settings layers** — `worker ▶ template ▶ booking ▶ global`; each setting has exactly one home
  layer, overrides only where declared (`libs/domain/settings/catalog.py`). No silent overlap.
- **Roles** — real booking derives role by identity type (test→scout, real→apply); test booking
  assigns roles manually. `libs/domain/rules.py`.
- **Identity exclusivity (C2)** — an identity is allocated to at most one active booking
  (partial unique index `uq_identity_active_allocation`).
- **Single operator (§12c)** — at most one active `operate` session host-wide
  (partial unique index `uq_single_active_operate`).

## Dev

```bash
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]' sqlalchemy alembic
.venv/bin/python -m pytest            # domain + schema smoke tests
DATABASE_URL=postgresql+psycopg://... .venv/bin/alembic upgrade head
```

## Build phases

1. ✅ domain + models + migrations (pure, tested)
2. engine — clean rewrite anchored to storm-auto's confirmed path; one worker books 2032 in isolation
3. bus + supervisor + hub skeleton (REST+Redis, JWT auth)
4. orchestration + scheduler (booking→workers→auto-ops)
5. dashboard SPA (React+Vite)
6. telegram + hardening
```

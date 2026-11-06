# storm-hub

Control plane for portal-booking automation: a dashboard drives a **Manager Hub** that owns
bookings, identities, proxies, worker templates and settings, and orchestrates **one isolated
worker process per identity**. The proven portal engine (from `storm-auto`) is rewritten clean
behind a stable interface.

Full engineering knowledge base: `docs/engineering/` — start with `SYSTEM_MEMORY.md`, then
`ARCHITECTURE.md` (as-built, reconciled 2026-10-02). The live line is branch `pt-version`.

## Layout

```
apps/       hub (FastAPI REST+WS), worker (per-identity runtime), registrar, dashboard (React+Vite)
libs/
  domain/   PURE: enums, state machines, settings taxonomy + resolver, invariants (no DB/framework)
  db/       SQLAlchemy models (§3 schema) + engine wiring
  engine/   phase machine + EngineOps Protocol; payload, discovery, checkpoint,
            orchestration, portal_health; vendor/ = storm-auto's Playwright engine (verbatim)
  bus/      Redis streams (cmd/evt) + signal pubsub, semaphore, portal_liveness, token_pool
  proxies/  central proxy lease/mint/geo/health service
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
uv sync --frozen --extra dev                       # .venv from uv.lock
.venv/bin/python -m playwright install chromium    # the pinned build (1228)
(cd apps/dashboard && npm ci && npm run build)     # dist/ is served by the Hub
.venv/bin/alembic upgrade head                     # DATABASE_URL comes from .env
.venv/bin/python -m pytest -q tests                # full suite, ~15 min
```

## Deploy / move to a new server

`docs/RELOCATION.md`. In short: `infra/relocate/export.sh` on the old host makes one bundle, and
`bootstrap.sh --host <ip>` on a fresh Ubuntu host rebuilds everything from it. That covers code, DB,
`.env`, Donut profiles, units, Caddy and fail2ban. The Hub is never auto-started.

## Status

All build phases are complete and the system is **in production** on a single host
(`storm-hub.service`). It has booked real appointments end-to-end, including on the PT in-page
XHR apply path (a `%PDF` captured — see `docs/engineering/CONFIRMED_FACTS.md`).

1. ✅ domain + models + migrations (pure, tested)
2. ✅ engine — clean rewrite anchored to storm-auto's confirmed path
3. ✅ bus + supervisor + hub (REST + Redis + WS, JWT auth, single-operator seat)
4. ✅ orchestration + scheduler (booking → workers → auto-ops; DB-recomputed tick)
5. ✅ dashboard SPA (React + Vite)
6. ✅ hardening — proxy geo rotation, login token pool, cross-booking slot feed, origin recovery

Current: 94 Hub routes · 24 DB models · 1306 tests / 70 files (branch `request-manual-review`).

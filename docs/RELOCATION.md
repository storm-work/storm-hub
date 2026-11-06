# Moving storm-hub to a new server

You run three commands. One bundle carries everything. Nothing on the new host is configured by hand.

```
OLD host (as storm)   infra/relocate/export.sh                       -> ~/storm-hub-relocate-<stamp>.tgz
OLD -> NEW            scp ~/storm-hub-relocate-*.tgz root@NEW:/root/
NEW host (as root)    tar -xzf ... && bash bootstrap.sh --host NEW   -> ready host, Hub not started
```

Plan for about 30 minutes, most of it package installs. The old host is not changed, so rollback means
starting the Hub there again.

## What moves, and what doesn't

| Item | How | Notes |
|---|---|---|
| Code, all branches | `git bundle` | The new host needs no GitHub key; `origin` is set back to GitHub |
| `.env` | copied, chmod 600 | DB/Redis URLs, JWT secret, proxy passwords |
| Postgres DB (~33 MB) | `pg_dump -Fc` → `pg_restore` | Settings, identities, sub-accounts, bookings, users, solver keys, credential pools |
| Donut state | tar of `~/.local/share/DonutBrowser` | One profile per account (the account's device fingerprint), proxy entries, the app sign-in and Local API token, the Wayfern build. No VNC sign-in, no profile re-create, no fingerprint DRIFT |
| Donut package | the installed `.deb` | Same version (0.35.5 at writing); not "latest" |
| `data/` | tar | Gitignored identity and proxy source files |
| `var/` | **only with `--full`** | ~0.5 GB of worker logs, submits and artifacts: history only |
| Redis | **not moved** | Run-time state only. Moving it replays stale command streams into the next launch |
| Python venv, Playwright Chromium, dashboard `dist/` | rebuilt | From `uv.lock` (frozen), Playwright 1.61 (chromium 1228) and `package-lock.json` |
| systemd units, Caddy, fail2ban, ufw | installed from `infra/` | The Caddy site address comes from `--host` |
| storm-auto (`/srv/projects/storm-auto`, `storm-worker`/`storm-scout`/`storm-backup` units) | **not moved** | Legacy project; move it separately if it is still needed |

The user (`storm`) and the paths (`/srv/projects/storm-hub`, `/home/storm/...`) stay the same on purpose.
Donut profile metadata, the units, the scripts and the DB all name them, so nothing has to be
rewritten.

## New host requirements

- **OS:** Ubuntu 22.04 LTS, the same as the old host. 24.04 also works; uv then fetches Python 3.10.
- **Size:** the old host is 56 cores, 125 GB RAM and uses 26 GB of disk. Each worker is a full browser,
  so size the new host for the fleet you run. Give it at least 40 GB of free disk.
- **Access:** root over SSH. Bootstrap copies root's `authorized_keys` to `storm`.
- **Network:** outbound access to GitHub releases, astral.sh, deb.nodesource.com, cloudsmith (Caddy),
  the Playwright CDN, the proxy providers and the solver APIs.

## 1. Freeze the old host

1. In the dashboard, **stop every running booking**. Wait until the Workers page shows none running.
   A booking live on two hosts means two fleets on the same accounts.
2. Leave the Hub running. Export does not need it stopped, and you can still look at the dashboard.

## 2. Export (old host, as storm)

```bash
cd /srv/projects/storm-hub && git pull --ff-only
infra/relocate/export.sh            # ~3.5 GB bundle in $HOME
# infra/relocate/export.sh --lean   # without the 3.5 GB Wayfern build (Donut must download it again)
# infra/relocate/export.sh --full   # also carry var/ (logs/submits history)
```

The export refuses to run while any worker process is alive, or while tracked files have uncommitted
changes, because those changes would not move. It prints a MANIFEST with the commit, versions and a
sha256 for every file.

**The bundle is secret.** It holds credentials and applicant PII. It is written chmod 600. Copy it
straight from host to host, never through a laptop or a chat.

## 3. Copy

```bash
scp ~/storm-hub-relocate-*.tgz root@NEW_HOST:/root/
```

## 4. Bootstrap (new host, as root)

```bash
mkdir -p /root/reloc && tar -xzf /root/storm-hub-relocate-*.tgz -C /root/reloc
bash /root/reloc/bootstrap.sh --host NEW_HOST_IP_OR_DOMAIN
```

What it does, in order:
1. Checks the bundle checksums.
2. Installs packages: Postgres, Redis, Xvfb, Caddy, Node 24, fail2ban and ufw.
3. Creates the `storm` user and installs uv.
4. Clones the repo from the bundle and checks out the same branch.
5. Installs `.env`, `data/` and, if present, `var/`.
6. Runs `uv sync --frozen`, then installs Playwright Chromium and its system libraries.
7. Runs `npm ci && npm run build` for the dashboard.
8. Creates the DB role and database from `DATABASE_URL`, runs `pg_restore`, then
   `alembic upgrade head`.
9. Installs the pinned Donut `.deb` and restores the Donut state.
10. Installs the units: `storm-hub` and its `DISPLAY=:99` drop-in, `xvfb99`, `xvfb77`, `donut`, and the
    nightly backup timer. Enabled at boot: xvfb99, xvfb77, donut and the backup timer.
    **storm-hub is never enabled.**
11. Renders the Caddyfile for `--host`, then sets up fail2ban and ufw (ports 22, 80, 443).
12. Runs the checks, each printed as `ok` or `FAIL`.

The script is idempotent: if a step fails, fix the cause and run it again. It never restores over an
existing database. To re-import, drop the database first (`sudo -u postgres dropdb <db>`).

Use `--no-firewall` if the provider's firewall already handles ufw's job.

## 5. Verify (new host, as storm)

```bash
sudo systemctl start storm-hub
curl -s http://127.0.0.1:8123/health                       # {"status":"ok","db":"ok",...}
cd /srv/projects/storm-hub
.venv/bin/python -m probes.donut_probe --soax              # steps 1-3 must pass; touches no portal page
.venv/bin/python -m pytest -q tests/hub tests/worker -x    # optional, ~15 min
```

Then open `https://NEW_HOST`. The certificate is self-signed (`tls internal`); accept it once.

1. Log in.
2. Check the Identities, Proxies and Settings pages: the counts should match the old host.
3. Donut page: the profiles should be listed, none running.
4. Start a **small test booking** first, or a registration. The operator does this, because live
   actions book real appointments.

## 6. Cut over

1. On the **old** host, stop everything that could act on its own:
   ```bash
   sudo systemctl stop storm-hub
   sudo systemctl disable --now storm-hub-backup.timer
   pkill -u storm -f donutbrowser; pkill -u storm -f 'Xvfb :77'
   ```
2. If the dashboard is reached by a DNS name, point it at the new IP.
3. Keep the old host untouched for a few days as the rollback.
4. Then `shred -u` the bundle on both hosts and wipe the old host.

## Rollback

Until step 6, the old host is unchanged. Stop the Hub on the new host and run
`sudo systemctl start storm-hub` on the old one. Any bookings made from the new host exist only in
the new host's DB. Copy them back with a `pg_dump` the same way, or re-enter them.

## Things that are tied to the old IP

- **Proxy providers.** Our pools authenticate with user and password, so an IP change is fine.
  If any provider package uses IP whitelisting, add the new IP there first.
- **Solver keys (capsolver / anticaptcha).** Same rule: only matters if IP allowlisting is enabled
  on the account.
- **Donut account.** The sign-in tokens move with `settings/`. If the app asks to sign in again (for
  example, the account is bound to a device), use the one-time VNC procedure in
  `docs/engineering/DONUT_BACKEND.md` (setup step 3), then set `DONUT_API_TOKEN` in Global settings.
- **Caddy.** `tls internal` suits a bare IP. With a domain, delete the `tls internal` line in
  `/etc/caddy/Caddyfile` and reload Caddy to get a Let's Encrypt certificate (ports 80 and 443 open).

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `FAIL donut local API answering` | The app did not start or chose another port. Run `journalctl -u donut -n 50`. If it asks to sign in, use the VNC step above. If the port differs, set `DONUT_API_URL` in Global settings |
| Donut profiles fail to launch, "browser not downloaded" | The bundle was made with `--lean`. Open the app over VNC and let it download Wayfern, or re-export without `--lean` |
| `pg_restore` errors about roles or owners | Harmless with `--no-owner`. The restore continues, and the database is owned by the `.env` role |
| Workers start without a display | Check `systemctl status xvfb99` and that the drop-in `/etc/systemd/system/storm-hub.service.d/display.conf` exists, then run `daemon-reload` |
| Dashboard 502 | The Hub is not started (by design). Run `sudo systemctl start storm-hub` |
| fail2ban fails to start | `/var/log/caddy/storm-hub-access.log` is missing. Bootstrap creates it; run `touch` + `chown caddy` |

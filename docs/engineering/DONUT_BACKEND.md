# Donut Browser backend (`BROWSER_BACKEND=donut`)

Added 2026-10-26 (D50). Status: **built, not yet measured.** No booking has run on it. Every
confirmed booking so far ran on `chromium`.

## What it is

`BROWSER_BACKEND` picks where a session's browser comes from:

- `chromium` (default): the engine launches Playwright's own Chromium. The engine applies its own
  fingerprint pool, version alignment and stealth script, plus `HEADLESS_MODE`.
- `donut`: each session runs in a Donut Browser profile that uses the **Wayfern** engine (a
  Chromium fork). The engine attaches to it over CDP, and Wayfern owns the fingerprint. The
  engine's fingerprint layer is skipped entirely, because two fingerprint layers on one browser
  contradict each other.

Nothing else changes: the same `PlaywrightSession`, worker thread, page, commands, and login,
warm-up, discovery and apply flows. The only difference is `connect_over_cdp` in place of
`chromium.launch`. The code is in `libs/engine/vendor/donut.py` plus the launch/teardown branch in
`libs/engine/vendor/session.py`.

```
Hub ──Redis streams──> worker process (Python: phases + ops_live + PlaywrightSession)
                          │  REST 127.0.0.1:10108   start/stop profile, set its proxy
                          ├──────────────────────────> Donut app (desktop, under Xvfb)
                          │                                   │ launches
                          │  CDP 127.0.0.1:<port>             v
                          └──────────────────────────> Wayfern profile ──proxy──> portal
```

This is unlike the Octo setup, where the bot ran *inside* each profile as an extension and talked
to the Hub over a WebSocket. Here the bot stays in our worker process and drives the browser from
outside. The browser carries no extension and no code of ours. Hub-to-worker messaging is
unchanged.

## Profiles and proxies

`DONUT_PROFILE_SCOPE` controls how profiles are kept:

- `account` (default): one persistent profile per sub-account, named `storm-<sha1(fp_seed)[:12]>`.
  - The account gets the same device on every login, as `DETERMINISTIC_FP` gives it on Chromium.
  - The profile is created once, which keeps us under Donut's hourly limit on new fingerprints.
  - It has `clear_on_close`, so every session still starts with an empty cookie jar.
- `session`: a throwaway profile per session, deleted on release. Every launch is a new fingerprint
  and counts against that limit.

Each profile has one Donut proxy entry. The entry is rewritten with the session's exit on every
launch, always with the **full** settings, because a partial PUT clears the username and password.
If a profile is found already running (left by a crashed worker), it is killed and restarted, never
shared.

Teardown: `browser.close()` over CDP only disconnects, so every teardown path stops the profile
through the API. That covers normal close, a hung close, `close_browser`, and the init timeout.

## Settings

| Key | Layer | Default | |
|---|---|---|---|
| `BROWSER_BACKEND` | global, template | `chromium` | `donut` turns it on; per template, so one booking can A/B both |
| `DONUT_API_URL` | global | `http://127.0.0.1:10108` | the app picks a random port if 10108 is busy |
| `DONUT_API_TOKEN` | global | | bearer token; masked on read, never echoed in worker logs |
| `DONUT_HEADLESS` | global, template | `True` | off = real window inside the app's display |
| `DONUT_PROFILE_OS` | global, template | `windows` | a non-Linux OS needs a plan with cross-OS fingerprints (402 otherwise) |
| `DONUT_PROFILE_SCOPE` | global, template | `account` | see above |

The Hub's own proxy-screening probes always stay on Chromium.

## Known limits, all unmeasured

- **CDP rate.** Donut documents "up to 100 automation (CDP) requests per minute" on Wayfern, and
  does not say whether that is per profile or per account. One login is far more than 100 CDP
  messages. If this limit is enforced, the mode cannot work. `probes/donut_probe.py` step 5
  measures it.
- **Hourly automation limit** on profile runs (HTTP 429 with `Retry-After`). The numbers depend on
  the plan and are not published. Every exit roll is a new run.
- **Scale.** Nothing is documented for about 100 concurrent profiles on one host.
- **Wayfern** is a closed binary with token renewal (see the v0.35.4 release notes): a new way to
  fail that is outside our control.

## Setup runbook (operator, one time)

1. Install the app and its runtime (needs sudo). Download the latest amd64 `.deb` from
   https://github.com/zhom/donutbrowser/releases, then:
   `sudo apt install ./Donut*_amd64.deb xvfb x11vnc`
2. Run the app as **storm**, the user the Hub and workers run as, on a virtual display:
   `Xvfb :77 -screen 0 1920x1080x24 & DISPLAY=:77 donutbrowser &`
   Use the binary name from the package (`dpkg -L <package> | grep bin/`).
3. Sign in once and enable the Local API. On the server, start a VNC server bound to localhost:
   `x11vnc -display :77 -localhost -once -nopw`
   From your machine, open a tunnel with `ssh -L 5900:127.0.0.1:5900 storm@<server>` and connect a
   VNC viewer to `localhost:5900`. Then:
   - sign in to the paid account;
   - go to Integrations > Local API and turn it on;
   - copy the token.
4. In the dashboard, go to Global settings and set `DONUT_API_TOKEN` (and `DONUT_API_URL` if the
   app chose a port other than 10108).
5. Readiness probe. It touches no portal page:
   `.venv/bin/python -m probes.donut_probe --soax`
   It must pass steps 1 to 3. Read step 4: does the timezone match the exit country, and is
   `webdriver` false? Read step 5: the round-trips per minute and any errors.
6. A/B, operator-run, with real logins. Create a template copy with `BROWSER_BACKEND=donut`, then
   run it beside the Chromium template in one booking: same soax pool, same window, about 50 logins
   each. Compare:
   - login success rate;
   - first keepalive survival;
   - warm-up success.

   **Adopt only on a clear gain.**

Steps 2 and 3 do not survive a reboot. If the A/B pays off, wrap them in a user systemd unit.

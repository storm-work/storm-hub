#!/usr/bin/env bash
# NEW HOST: turn a fresh Ubuntu 22.04/24.04 box into a working storm-hub host from ONE bundle made by
# infra/relocate/export.sh. Run as root, from the extracted bundle directory:
#
#   sudo bash bootstrap.sh --host <new IP or domain> [--bundle DIR] [--no-firewall]
#
# Same user (storm), same paths (/srv/projects/storm-hub, ~/.local/share/DonutBrowser) as the old host
# ON PURPOSE: Donut profile metadata, systemd units, scripts and the DB all name them, so keeping them
# means nothing has to be rewritten. Idempotent: a failed run can simply be run again.
#
# It does NOT start the Hub. storm-hub.service has no [Install] and Restart=no by design (a restart
# resumes RUNNING bookings with live workers) -- start it yourself after the checks in
# docs/RELOCATION.md.
set -euo pipefail

HOST=""; BUNDLE=$(cd "$(dirname "$0")" && pwd); FIREWALL=1
while [[ $# -gt 0 ]]; do
    case "$1" in
        --host) HOST=$2; shift 2 ;;
        --bundle) BUNDLE=$(cd "$2" && pwd); shift 2 ;;
        --no-firewall) FIREWALL=0; shift ;;
        *) echo "unknown argument $1" >&2; exit 2 ;;
    esac
done
[[ $EUID -eq 0 ]] || { echo "run as root (sudo)" >&2; exit 1; }
[[ -n "$HOST" ]] || { echo "--host <new IP or domain> is required (Caddy serves the dashboard on it)" >&2; exit 2; }
for f in repo.bundle env db.dump; do
    [[ -f "$BUNDLE/$f" ]] || { echo "bundle is missing $f ($BUNDLE)" >&2; exit 1; }
done

U=storm
HOME_U=/home/$U
APP=/srv/projects/storm-hub
DONUT_DIR=$HOME_U/.local/share/DonutBrowser
export DEBIAN_FRONTEND=noninteractive
say() { printf '\n== %s\n' "$*"; }
as_u() { sudo -u "$U" -H bash -lc "$*"; }

say "verify bundle checksums"
if [[ -f "$BUNDLE/MANIFEST" ]]; then
    (cd "$BUNDLE" && grep -E '^[0-9a-f]{64}  ' MANIFEST | grep -v ' bootstrap.sh$' | sha256sum -c --quiet)
    head -5 "$BUNDLE/MANIFEST"
fi

say "packages"
apt-get update -qq
apt-get install -y -qq git curl ca-certificates gnupg build-essential jq sudo \
    postgresql postgresql-contrib redis-server xvfb x11vnc dbus dbus-x11 \
    fail2ban ufw unattended-upgrades debian-keyring debian-archive-keyring apt-transport-https
if ! command -v caddy >/dev/null; then
    curl -1sLf https://dl.cloudsmith.io/public/caddy/stable/gpg.key \
        | gpg --dearmor --yes -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
    curl -1sLf https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt \
        > /etc/apt/sources.list.d/caddy-stable.list
    apt-get update -qq && apt-get install -y -qq caddy
fi
if ! command -v node >/dev/null || [[ $(node -v | cut -c2- | cut -d. -f1) -lt 20 ]]; then
    curl -fsSL https://deb.nodesource.com/setup_24.x | bash - >/dev/null
    apt-get install -y -qq nodejs
fi

say "user $U"
id "$U" >/dev/null 2>&1 || useradd -m -s /bin/bash -G sudo "$U"
if [[ ! -s $HOME_U/.ssh/authorized_keys && -s /root/.ssh/authorized_keys ]]; then
    install -d -m 700 -o "$U" -g "$U" "$HOME_U/.ssh"
    install -m 600 -o "$U" -g "$U" /root/.ssh/authorized_keys "$HOME_U/.ssh/authorized_keys"
    echo "copied root's authorized_keys to $U (log in as $U from now on)"
fi
[[ -x $HOME_U/.local/bin/uv ]] || as_u 'curl -LsSf https://astral.sh/uv/install.sh | sh >/dev/null'

say "repo -> $APP"
install -d -o "$U" -g "$U" -m 755 /srv/projects
if [[ ! -d $APP/.git ]]; then
    install -m 644 "$BUNDLE/repo.bundle" /tmp/storm-hub.bundle
    as_u "git clone -q /tmp/storm-hub.bundle $APP"
    rm -f /tmp/storm-hub.bundle
fi
BRANCH=$(cat "$BUNDLE/BRANCH" 2>/dev/null || echo main)
as_u "cd $APP && git checkout -q $BRANCH"
[[ -s $BUNDLE/REMOTE ]] && as_u "cd $APP && git remote set-url origin $(cat "$BUNDLE/REMOTE")"
install -m 600 -o "$U" -g "$U" "$BUNDLE/env" "$APP/.env"
if [[ -f $BUNDLE/data.tgz ]]; then as_u "cd $APP && tar -xzf $BUNDLE/data.tgz"; fi
if [[ -f $BUNDLE/var.tgz ]]; then as_u "cd $APP && tar -xzf $BUNDLE/var.tgz"; fi
as_u "mkdir -p $APP/var/worker-logs"

say "python env (uv.lock, frozen)"
as_u "cd $APP && ~/.local/bin/uv sync --frozen --extra dev --python 3.10 -q"
$APP/.venv/bin/python -m playwright install-deps chromium >/dev/null
as_u "cd $APP && .venv/bin/python -m playwright install chromium >/dev/null"

say "dashboard build"
as_u "cd $APP/apps/dashboard && npm ci --silent --no-audit --no-fund && npm run -s build"

say "postgres"
systemctl enable --now postgresql >/dev/null
DBURL=$(grep -m1 '^DATABASE_URL=' "$APP/.env" | cut -d= -f2-)
# postgresql+psycopg://USER:PASS@HOST:PORT/DB
read -r PGU PGP PGDB < <(python3 - "$DBURL" <<'PY'
import sys
from urllib.parse import urlsplit, unquote
u = urlsplit(sys.argv[1].replace("postgresql+psycopg://", "postgresql://"))
print(unquote(u.username or ""), unquote(u.password or ""), u.path.lstrip("/"))
PY
)
sudo -u postgres psql -qtAc "SELECT 1 FROM pg_roles WHERE rolname='$PGU'" | grep -q 1 \
    || sudo -u postgres psql -qc "CREATE ROLE \"$PGU\" LOGIN PASSWORD '$PGP'"
if ! sudo -u postgres psql -qtAc "SELECT 1 FROM pg_database WHERE datname='$PGDB'" | grep -q 1; then
    sudo -u postgres createdb -O "$PGU" "$PGDB"
    install -m 644 "$BUNDLE/db.dump" /tmp/storm_hub.dump
    sudo -u postgres pg_restore --no-owner --role="$PGU" -d "$PGDB" /tmp/storm_hub.dump
    rm -f /tmp/storm_hub.dump
    echo "restored $PGDB"
else
    echo "database $PGDB already exists -- NOT restoring over it (drop it first to re-import)"
fi
as_u "cd $APP && .venv/bin/alembic upgrade head"

say "redis"
systemctl enable --now redis-server >/dev/null
# Starts EMPTY on purpose: Redis holds only run-time state (signal latches, command streams, budgets,
# pulse). Carrying it over replays stale command streams into the next launch.

say "donut"
if [[ -f $BUNDLE/donut.deb ]]; then
    apt-get install -y -qq "$BUNDLE/donut.deb"
elif ! dpkg -s donut >/dev/null 2>&1; then
    V=$(sed -n 's/^donut=//p' "$BUNDLE/MANIFEST" 2>/dev/null)
    if [[ -n "$V" && "$V" != none ]]; then
        curl -fsSL -o /tmp/donut.deb "https://github.com/zhom/donutbrowser/releases/download/v$V/Donut_${V}_amd64.deb"
        apt-get install -y -qq /tmp/donut.deb && rm -f /tmp/donut.deb
    fi
fi
if [[ -f $BUNDLE/donut.tgz && ! -d $DONUT_DIR/profiles ]]; then
    as_u "mkdir -p $DONUT_DIR && tar -xzf $BUNDLE/donut.tgz -C $DONUT_DIR"
fi

say "systemd units"
install -m 644 $APP/infra/storm-hub.service /etc/systemd/system/storm-hub.service
install -d /etc/systemd/system/storm-hub.service.d
install -m 644 $APP/infra/storm-hub.service.d/display.conf /etc/systemd/system/storm-hub.service.d/display.conf
for unit in xvfb99.service xvfb77.service donut.service storm-hub-backup.service storm-hub-backup.timer; do
    install -m 644 "$APP/infra/$unit" "/etc/systemd/system/$unit"
done
systemctl daemon-reload
systemctl enable --now xvfb99.service xvfb77.service storm-hub-backup.timer >/dev/null
if dpkg -s donut >/dev/null 2>&1; then systemctl enable --now donut.service >/dev/null; fi

say "caddy -> https://$HOST"
install -d -o caddy -g caddy /var/log/caddy
[[ -f /var/log/caddy/storm-hub-access.log ]] || install -m 644 -o caddy -g caddy /dev/null /var/log/caddy/storm-hub-access.log
sed "s|{\$STORM_HUB_HOST}|$HOST|" $APP/infra/Caddyfile > /etc/caddy/Caddyfile
caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile >/dev/null
systemctl enable caddy >/dev/null && systemctl reload-or-restart caddy

say "fail2ban"
install -m 644 $APP/infra/fail2ban/storm-hub-auth.filter.conf /etc/fail2ban/filter.d/storm-hub-auth.conf
install -m 644 $APP/infra/fail2ban/storm-hub-auth.jail.local /etc/fail2ban/jail.d/storm-hub-auth.local
systemctl enable fail2ban >/dev/null && systemctl restart fail2ban

if (( FIREWALL )); then
    say "firewall (22, 80, 443)"
    ufw allow OpenSSH >/dev/null; ufw allow 80/tcp >/dev/null; ufw allow 443/tcp >/dev/null
    ufw --force enable >/dev/null
fi

say "checks"
fail=0
ok()  { echo "  ok    $1"; }
bad() { echo "  FAIL  $1"; fail=1; }
if as_u "cd $APP && .venv/bin/python -c 'from apps.hub.db import SessionLocal; from sqlalchemy import text; SessionLocal().execute(text(\"select 1\"))'" >/dev/null 2>&1
    then ok "postgres reachable with the .env credentials"; else bad "postgres reachable with the .env credentials"; fi
if redis-cli ping 2>/dev/null | grep -q PONG; then ok "redis"; else bad "redis"; fi
if [[ -f $APP/apps/dashboard/dist/index.html ]]; then ok "dashboard built"; else bad "dashboard built"; fi
if as_u "cd $APP && .venv/bin/python -c 'import apps.hub.main, apps.worker.ops_live'" >/dev/null 2>&1
    then ok "hub + worker import"; else bad "hub + worker import"; fi
for unit in xvfb99 xvfb77 donut caddy fail2ban; do
    if systemctl is-active --quiet "$unit"; then ok "$unit active"; else bad "$unit active"; fi
done
api=0
for _ in $(seq 30); do
    if curl -s -o /dev/null --max-time 2 http://127.0.0.1:10108/; then api=1; break; fi
    sleep 2
done
if (( api )); then ok "donut local API answering on 10108"; else bad "donut local API answering on 10108 (see Troubleshooting)"; fi
echo
if (( fail )); then echo "some checks failed -- see docs/RELOCATION.md 'Troubleshooting'"; fi
cat <<EOF
Bootstrap done. The Hub is NOT started (by design). Next, as $U:
  sudo systemctl start storm-hub && curl -s http://127.0.0.1:8123/health
  open https://$HOST  (self-signed 'tls internal' cert -- accept it once)
Then follow docs/RELOCATION.md "Verify" and "Cut-over".
EOF

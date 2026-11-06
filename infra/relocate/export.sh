#!/usr/bin/env bash
# OLD HOST: pack everything a new host needs into ONE tarball. Run as `storm`, no sudo needed.
#
#   infra/relocate/export.sh [--full] [--lean] [OUT_DIR]
#
# The bundle carries:
#   repo.bundle   the git repo, all branches (the new host needs no GitHub key to deploy)
#   env           .env (DATABASE_URL, REDIS_URL, JWT_SECRET, proxy passwords)
#   db.dump       pg_dump -Fc of the Hub database (settings, identities, bookings, users, solver keys)
#   donut.tgz     Donut app state: one profile per account (= the account's device fingerprint), its
#                 proxy entries, the app's sign-in and Local API token, and the Wayfern browser build
#                 the profiles were made with (3.5 GB, 2.5 GB of it fingerprint fonts). Carrying it
#                 means no VNC sign-in, no profile re-create, no download. --lean leaves the browser
#                 build out (bundle ~3 GB smaller); the Donut app then has to download it again.
#   donut.deb     the exact Donut package running here (pinned, not "latest")
#   data.tgz      data/ (gitignored identity/proxy source files)
#   var.tgz       --full only: var/ (worker logs, submits, artifacts; ~0.5 GB, history only)
#   MANIFEST      versions + sha256 of every file
#
# THE BUNDLE IS SECRET: credentials + applicant PII. It is written chmod 600. Copy it host-to-host
# (scp), never through a laptop or chat, and `shred -u` it on both ends once the new host is live.
set -euo pipefail

FULL=0; LEAN=0
while [[ "${1:-}" == --* ]]; do
    case "$1" in
        --full) FULL=1 ;;
        --lean) LEAN=1 ;;
        *) echo "unknown flag $1" >&2; exit 2 ;;
    esac
    shift
done
OUT_DIR=${1:-$HOME}
PROJECT_DIR=$(cd "$(dirname "$0")/../.." && pwd)
DONUT_DIR=$HOME/.local/share/DonutBrowser
STAMP=$(date -u +%Y%m%d_%H%M%S)
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT
umask 077
cd "$PROJECT_DIR"

say() { printf '\n== %s\n' "$*"; }

say "pre-flight"
running=$(curl -fsS http://127.0.0.1:8123/health >/dev/null 2>&1 && echo up || echo down)
nworkers=$(pgrep -u "$(id -u)" -fc -- "-m apps\.(worker|registrar)" || true)
echo "hub: $running   worker processes: $nworkers"
if [[ "$nworkers" != "0" ]]; then
    echo "!! workers are running. Stop every booking in the dashboard first (a live booking on two" >&2
    echo "!! hosts = two fleets on the same accounts). Re-run when 0 workers remain." >&2
    exit 1
fi
if ! git diff --quiet || ! git diff --cached --quiet; then
    echo "!! uncommitted tracked changes -- commit them or they will not move:" >&2
    git status --short --untracked-files=no >&2
    exit 1
fi

say "git bundle"
git remote get-url origin > "$WORK/REMOTE" 2>/dev/null || true
git bundle create "$WORK/repo.bundle" --all
git rev-parse --abbrev-ref HEAD > "$WORK/BRANCH"

say ".env"
cp .env "$WORK/env"

say "database"
DATABASE_URL=$(grep -m1 '^DATABASE_URL=' .env | cut -d= -f2-)
pg_dump "${DATABASE_URL/postgresql+psycopg:/postgresql:}" -F custom --no-owner --no-privileges \
    -f "$WORK/db.dump"

say "donut state"
if [[ -d "$DONUT_DIR" ]]; then
    excl=(); (( LEAN )) && excl=(--exclude=./binaries --exclude=./data/downloaded_browsers.json)
    tar -czf "$WORK/donut.tgz" -C "$DONUT_DIR" "${excl[@]}" .
else
    echo "(no Donut state here -- skipped)"
fi
deb=$(ls -1 "$HOME"/Donut_"$(dpkg-query -W -f='${Version}' donut 2>/dev/null)"_amd64.deb 2>/dev/null | head -1 || true)
if [[ -n "$deb" ]]; then cp "$deb" "$WORK/donut.deb"; else echo "(installed Donut .deb not found in \$HOME -- bootstrap will download it)"; fi

say "data/"
[[ -d data ]] && tar -czf "$WORK/data.tgz" data
if (( FULL )); then
    say "var/ (--full)"
    tar -czf "$WORK/var.tgz" --exclude=./var/keepalive var
fi

say "manifest"
{
    echo "created_utc=$STAMP"
    echo "source_host=$(hostname) $(hostname -I | awk '{print $1}')"
    echo "commit=$(git rev-parse HEAD) branch=$(cat "$WORK/BRANCH")"
    echo "python=$(.venv/bin/python -V 2>&1) postgres=$(psql -V | awk '{print $3}') node=$(node -v 2>/dev/null)"
    echo "donut=$(dpkg-query -W -f='${Version}' donut 2>/dev/null || echo none)"
    (cd "$WORK" && sha256sum -- *)
} > "$WORK/MANIFEST.tmp"     # .tmp: sha256 over "*" must not include the manifest being written
mv "$WORK/MANIFEST.tmp" "$WORK/MANIFEST"
cp "$PROJECT_DIR/infra/relocate/bootstrap.sh" "$WORK/bootstrap.sh"

OUT="$OUT_DIR/storm-hub-relocate-$STAMP.tgz"
tar -czf "$OUT" -C "$WORK" .
chmod 600 "$OUT"
say "done"
ls -lh "$OUT"
cat "$WORK/MANIFEST" | head -5
echo
echo "next:  scp $OUT root@NEW_HOST:/root/"
echo "then on NEW_HOST:  mkdir /root/reloc && tar -xzf /root/$(basename "$OUT") -C /root/reloc && \\"
echo "                   sudo bash /root/reloc/bootstrap.sh --host NEW_HOST_IP_OR_DOMAIN"

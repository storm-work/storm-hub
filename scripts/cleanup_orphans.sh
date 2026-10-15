#!/usr/bin/env bash
# storm-hub orphan / leak cleanup.
#
# Finds and (optionally) kills LEAKED processes only -- ones reparented to init (PPID=1), not the
# Hub, not crashpad helpers, older than a guard age. The live Hub and everything it currently owns
# (active workers/registrations + their browser trees) have live, non-init parents, so they are
# NEVER matched. Verified 2026-10-15 against a live registration run: the filter selected only a
# 22-day-old orphaned root-owned Xvfb and left the running registrar's churning browsers untouched.
#
# Usage:
#   scripts/cleanup_orphans.sh              # inspect (read-only) -- the default; safe anytime
#   scripts/cleanup_orphans.sh inspect      # same
#   sudo scripts/cleanup_orphans.sh sweep   # KILL leaked browser/display procs (needs sudo: some are root-owned)
#   scripts/cleanup_orphans.sh restart-hub  # the correct way to clear orphaned WORKERS (see note)
#
# WHY WORKERS ARE NOT KILLED BY `sweep`: a restarted Hub legitimately ADOPTS orphaned worker
# processes by PID (supervisor reconcile() scans /proc), so a PPID=1 python worker may be one the
# Hub is actively managing -- killing it blindly is unsafe. Clear stuck/orphaned workers the right
# way with `restart-hub` (reconcile reaps the ones it no longer tracks; the OS cleans their browser
# trees), then run `sweep` to mop up any browser/Xvfb that outlived even the restart.

set -euo pipefail

AGE_GUARD="${AGE_GUARD:-600}"   # seconds; a leak must be older than this (avoids racing transients like crashpad)
LEAK_RE='Xvfb|chrome|chromium|headless|node'

hub_pid() { systemctl show -p MainPID --value storm-hub.service 2>/dev/null || echo 0; }

list_leaks() {
  local hub; hub="$(hub_pid)"
  # PPID==1 (reparented to init), not the Hub, not a crashpad helper, a browser/display process.
  ps -eo pid=,ppid=,user=,etimes=,comm= | awk -v h="$hub" -v re="$LEAK_RE" -v g="$AGE_GUARD" '
    $2==1 && $1!=h && $5!~/crashpad/ && $5~re && $4>g { printf "%s %s %s %s\n", $1, $3, $5, $4 }'
}

cmd_inspect() {
  local hub; hub="$(hub_pid)"
  echo "Hub MainPID=${hub} ($(systemctl is-active storm-hub.service 2>/dev/null || echo unknown)); age guard=${AGE_GUARD}s"
  echo "Leaked browser/display processes (PPID=1, excl. Hub/crashpad):"
  local any=0
  while read -r pid user comm age; do
    [ -n "${pid:-}" ] || continue
    printf "  LEAK pid=%s user=%s comm=%s age=%ss\n" "$pid" "$user" "$comm" "$age"; any=1
  done < <(list_leaks)
  [ "$any" = 0 ] && echo "  (none -- clean)"
  # Orphaned python app procs are reported but NOT swept (see header note); clear via restart-hub.
  echo "Orphaned python worker/registrar procs (PPID=1) -- clear with 'restart-hub', not sweep:"
  local foundpy=0
  for p in $(ps -eo pid=,ppid= | awk -v h="$hub" '$2==1 && $1!=h{print $1}'); do
    local a; a="$(tr '\0' ' ' </proc/"$p"/cmdline 2>/dev/null || true)"
    case "$a" in *apps.worker*|*apps.registrar*)
      printf "  pid=%s age=%ss :: %.70s\n" "$p" "$(ps -o etimes= -p "$p" 2>/dev/null | tr -d ' ')" "$a"; foundpy=1;;
    esac
  done
  [ "$foundpy" = 0 ] && echo "  (none)"
}

cmd_sweep() {
  local n=0
  while read -r pid user comm age; do
    [ -n "${pid:-}" ] || continue
    echo "killing leaked pid=${pid} user=${user} comm=${comm} age=${age}s"
    kill -9 "$pid" 2>/dev/null && n=$((n+1)) || echo "  -> could not kill ${pid} (owner=${user}; re-run with sudo)"
  done < <(list_leaks)
  echo "swept ${n} leaked process(es)."
}

cmd_restart_hub() {
  echo "restarting storm-hub.service (supervisor reconcile reaps untracked workers)..."
  sudo systemctl restart storm-hub.service
  echo "done. run '$0 sweep' afterwards to mop up any leaked browser/Xvfb."
}

case "${1:-inspect}" in
  inspect|"") cmd_inspect ;;
  sweep)      cmd_sweep ;;
  restart-hub) cmd_restart_hub ;;
  *) echo "usage: $0 [inspect|sweep|restart-hub]" >&2; exit 2 ;;
esac

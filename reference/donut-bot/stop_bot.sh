#!/bin/bash
# Stop every api_bot.py process.
# Two traps this has already fallen into:
#  1. `pgrep -x python3` does not reliably list them, so five runs once accumulated unnoticed,
#     competing for the same accounts and burning proxies in parallel.
#  2. `pgrep -f "python3 api_bot.py"` matches the *launching shell* too, because its own command
#     line contains that string — so stopping the bot killed the shell doing the stopping.
# Hence: match the full command line, then keep only PIDs whose executable is actually python.
found=""
for pid in $(pgrep -f "api_bot\.py" 2>/dev/null); do
  [ "$pid" = "$$" ] && continue
  exe=$(readlink /proc/$pid/exe 2>/dev/null)
  case "$exe" in *python*) found="$found $pid";; esac
done
if [ -z "$found" ]; then echo "no api_bot processes running"; exit 0; fi
kill $found 2>/dev/null; sleep 3
left=""
for pid in $found; do kill -0 $pid 2>/dev/null && left="$left $pid"; done
if [ -n "$left" ]; then kill -9 $left 2>/dev/null; sleep 2; fi
still=""
for pid in $found; do kill -0 $pid 2>/dev/null && still="$still $pid"; done
if [ -n "$still" ]; then echo "STILL RUNNING:$still"; exit 1; fi
echo "stopped:$found"
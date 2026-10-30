#!/usr/bin/env python3
"""
Visa Bot Monitor — Dashboard server (zero-dependency, reads the bot's existing logs).

This server does NOT touch api_bot.py. It only READS the log files the bot already
produces (logs/api_bot.log + logs/users/*.log) and exposes them as JSON + a dashboard
HTML page. Run it alongside the bot on the same server:

    python3 dashboard_server.py

Then open http://SERVER_IP:8080 in your browser.

The bot already writes rich, greppable status lines like:
    [STATUS] username | stage=LOGGING_IN | event=attempt_start | detail=1/3
    [Sentinel] username claimed the SENTINEL role (gen 1)
    [Follower] username is a FOLLOWER (sentinel gen 1)
    [Warm] username: holding session warm for 45.2 min until 11:00 Europe/Lisbon
    [Sentinel] SLOTS FOUND on poll #12 after 25.3 min
    [Sentinel] SIGNALING ALL FOLLOWERS to fire /slots NOW
    [Follower] SENTINEL SIGNAL: slots found! Firing /slots in 0.7s

This dashboard parses those lines and presents them in real time.
"""
import os
import re
import json
import time
import glob
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from datetime import datetime
from urllib.parse import urlparse

# --- Configuration -----------------------------------------------------------
BOT_LOG_DIR = os.environ.get('BOT_LOG_DIR', os.path.join(os.path.dirname(__file__), 'logs'))
MAIN_LOG = os.path.join(BOT_LOG_DIR, 'api_bot.log')
USERS_LOG_DIR = os.path.join(BOT_LOG_DIR, 'users')
DASHBOARD_HTML = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'dashboard.html')
PORT = int(os.environ.get('DASHBOARD_PORT', '8080'))
REFRESH_INTERVAL = 3  # seconds — the dashboard JS polls this fast

# --- Log parsing -------------------------------------------------------------
# [STATUS] username | stage=LOGGING_IN | event=attempt_start | detail=1/3
STATUS_RE = re.compile(
    r'\[STATUS\]\s+(\S+)\s+\|\s+stage=(\S+)\s+\|\s+event=(\S+)(?:\s+\|\s+detail=(.*))?'
)
# [Sentinel] username claimed the SENTINEL role (gen 1)
SENTINEL_CLAIM_RE = re.compile(r'\[Sentinel\]\s+(\S+)\s+claimed the SENTINEL role \(gen (\d+)\)')
# [Follower] username is a FOLLOWER (sentinel gen 1)
FOLLOWER_RE = re.compile(r'\[Follower\]\s+(\S+)\s+is a FOLLOWER \(sentinel gen (\d+)\)')
# [Sentinel] SLOTS FOUND on poll #12 after 25.3 min
SLOTS_FOUND_RE = re.compile(r'\[Sentinel\]\s+SLOTS FOUND on poll #(\d+) after ([\d.]+) min')
# [Sentinel] SIGNALING ALL FOLLOWERS
SIGNAL_RE = re.compile(r'\[Sentinel\]\s+SIGNALING ALL FOLLOWERS')
# [Follower] SENTINEL SIGNAL: slots found! Firing /slots in 0.7s
FOLLOWER_SIGNAL_RE = re.compile(r'\[Follower\]\s+SENTINEL SIGNAL.*Firing /slots in ([\d.]+)s')
# [Warm] username: holding session warm for 45.2 min until 11:00 Europe/Lisbon
WARM_RE = re.compile(r'\[Warm\]\s+(\S+):\s+holding session warm for ([\d.]+) min')
# [Sentinel] poll #5 ... no slots
SENTINEL_POLL_RE = re.compile(r'\[Sentinel\]\s+poll #(\d+).*no slots')
# Booking confirmed / APPOINTMENT BOOKED
BOOKED_RE = re.compile(r'(?:Booking confirmed|APPOINTMENT BOOKED|booking_success)', re.I)
# Booking failed / failed all
FAILED_RE = re.compile(r'(?:failed all|booking_failed|worker_exhausted)', re.I)


def _parse_log_timestamp(line: str) -> float:
    """Try to extract a timestamp from the start of a log line.
    Format: '2026-10-16 10:15:23 | LEVEL | ...' or ISO. Returns time.time() on failure."""
    m = re.match(r'(\d{4}-\d{2}-\d{2}[\sT]\d{2}:\d{2}:\d{2})', line)
    if m:
        try:
            return datetime.strptime(m.group(1), '%Y-%m-%d %H:%M:%S').timestamp()
        except Exception:
            pass
    return time.time()


def parse_logs():
    """Read the main bot log + per-user logs, return aggregated state.

    Returns a dict with:
      - accounts: {username: {stage, event, detail, last_seen, role, proxy, ...}}
      - sentinel: {username, generation} or None
      - counters: {total, logging_in, warming, sentinel, followers, polling, booked, failed, slots_found}
      - events: [list of recent events with timestamp, type, username, text]
      - updated_at: timestamp
    """
    accounts = {}
    events = []
    sentinel = None
    slots_found = False
    slots_found_at = None

    # --- Read the main log for global events + sentinel state ---
    if os.path.exists(MAIN_LOG):
        try:
            with open(MAIN_LOG, 'r', encoding='utf-8', errors='replace') as f:
                # Read the last ~5000 lines for performance
                lines = f.readlines()[-5000:]
        except Exception:
            lines = []
    else:
        lines = []

    for line in lines:
        ts = _parse_log_timestamp(line)

        m = STATUS_RE.search(line)
        if m:
            username, stage, event, detail = m.group(1), m.group(2), m.group(3), (m.group(4) or '')[:100]
            if username not in accounts:
                accounts[username] = {'username': username}
            accounts[username].update({
                'stage': stage, 'event': event, 'detail': detail,
                'last_seen': ts, 'last_seen_ago': time.time() - ts,
            })
            # Classify role/stage for counters
            if event in ('session_warming',):
                accounts[username]['role'] = 'warming'
            elif stage == 'COMPLETED':
                accounts[username]['role'] = 'booked'
            elif stage in ('FAILED', 'STOPPED'):
                accounts[username]['role'] = 'failed'
            elif stage == 'LOGGED_IN':
                accounts[username]['role'] = 'logging_in'
            continue

        m = SENTINEL_CLAIM_RE.search(line)
        if m:
            sentinel = {'username': m.group(1), 'generation': int(m.group(2)), 'at': ts}
            if m.group(1) in accounts:
                accounts[m.group(1)]['role'] = 'sentinel'
            events.append({'ts': ts, 'type': 'sentinel_claim', 'username': m.group(1),
                           'text': f'{m.group(1)} became SENTINEL (gen {m.group(2)})'})
            continue

        m = FOLLOWER_RE.search(line)
        if m:
            if m.group(1) in accounts:
                accounts[m.group(1)]['role'] = 'follower'
            events.append({'ts': ts, 'type': 'follower', 'username': m.group(1),
                           'text': f'{m.group(1)} is a FOLLOWER (gen {m.group(2)})'})
            continue

        m = SLOTS_FOUND_RE.search(line)
        if m:
            slots_found = True
            slots_found_at = ts
            events.append({'ts': ts, 'type': 'slots_found', 'username': sentinel['username'] if sentinel else '?',
                           'text': f'SLOTS FOUND on poll #{m.group(1)} after {m.group(2)} min'})
            continue

        if SIGNAL_RE.search(line):
            events.append({'ts': ts, 'type': 'signal', 'username': '',
                           'text': 'SENTINEL SIGNALING ALL FOLLOWERS to fire /slots NOW'})
            continue

        m = WARM_RE.search(line)
        if m:
            if m.group(1) in accounts:
                accounts[m.group(1)]['role'] = 'warming'
            continue

        m = FOLLOWER_SIGNAL_RE.search(line)
        if m:
            events.append({'ts': ts, 'type': 'follower_fire', 'username': '',
                           'text': f'Follower firing /slots in {m.group(1)}s'})
            continue

        if BOOKED_RE.search(line):
            events.append({'ts': ts, 'type': 'booked', 'username': '',
                           'text': 'Booking confirmed!'})
            continue

        if FAILED_RE.search(line):
            events.append({'ts': ts, 'type': 'failed', 'username': '',
                           'text': line.strip()[:120]})
            continue

    # --- Read per-user logs for the latest stage of each account ---
    if os.path.isdir(USERS_LOG_DIR):
        for userlog in glob.glob(os.path.join(USERS_LOG_DIR, '*.log')):
            username = os.path.basename(userlog).replace('.log', '')
            try:
                with open(userlog, 'r', encoding='utf-8', errors='replace') as f:
                    userlines = f.readlines()[-200:]
            except Exception:
                continue
            if username not in accounts:
                accounts[username] = {'username': username}
            # Find the last [STATUS] line in this user's log
            for ul in reversed(userlines):
                m = STATUS_RE.search(ul)
                if m:
                    ts = _parse_log_timestamp(ul)
                    accounts[username].update({
                        'stage': m.group(2), 'event': m.group(3),
                        'detail': (m.group(4) or '')[:100],
                        'last_seen': ts, 'last_seen_ago': time.time() - ts,
                    })
                    break

    # --- Compute counters ---
    counters = {'total': len(accounts), 'logging_in': 0, 'warming': 0,
                'sentinel': 0, 'followers': 0, 'polling': 0,
                'booked': 0, 'failed': 0, 'slots_found': 1 if slots_found else 0}
    for acc in accounts.values():
        role = acc.get('role', 'logging_in')
        if role in counters:
            counters[role] += 1

    # Sort events by timestamp descending, keep last 50
    events.sort(key=lambda e: e.get('ts', 0), reverse=True)
    events = events[:50]
    for e in events:
        e['ago'] = time.time() - e.get('ts', time.time())

    return {
        'accounts': list(accounts.values()),
        'sentinel': sentinel,
        'counters': counters,
        'events': events,
        'slots_found_at': slots_found_at,
        'updated_at': time.time(),
    }


# --- HTTP server -------------------------------------------------------------
class DashboardHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass  # silence default request logging

    def _send_json(self, data, status=200):
        body = json.dumps(data, default=str).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, path):
        try:
            with open(path, 'rb') as f:
                body = f.read()
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except FileNotFoundError:
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b'dashboard.html not found')

    def do_GET(self):
        path = urlparse(self.path).path
        if path == '/' or path == '/dashboard.html':
            self._send_html(DASHBOARD_HTML)
        elif path == '/api/status':
            self._send_json(parse_logs())
        elif path == '/api/health':
            self._send_json({'ok': True, 'log_dir': BOT_LOG_DIR,
                             'main_log_exists': os.path.exists(MAIN_LOG)})
        else:
            self.send_response(404)
            self.end_headers()


def main():
    print(f"Visa Bot Monitor")
    print(f"  Log dir:  {BOT_LOG_DIR}")
    print(f"  Main log: {MAIN_LOG} ({'exists' if os.path.exists(MAIN_LOG) else 'NOT FOUND'})")
    print(f"  Users:    {USERS_LOG_DIR}")
    print(f"  Dashboard: http://0.0.0.0:{PORT}")
    print(f"  Open in browser: http://YOUR_SERVER_IP:{PORT}")
    print(f"  Auto-refresh every {REFRESH_INTERVAL}s")
    print()
    server = HTTPServer(('0.0.0.0', PORT), DashboardHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
        server.shutdown()


if __name__ == '__main__':
    main()

#!/usr/bin/env bash
# Create a viewer account against a running Hub.
#
# Credentials come from the ENVIRONMENT, never from this file. Both passwords were literals here
# until 2026-08-26 and reached three remote branches in git history -- see the credential incident
# record. Do not reintroduce them: export them for the length of the call instead, e.g.
#
#   read -rs STORM_ADMIN_PASSWORD && export STORM_ADMIN_PASSWORD
#   read -rs VIEWER_PASSWORD      && export VIEWER_PASSWORD
#   ./user.sh
#
# `read -rs` keeps them out of shell history, which a plain `export FOO=...` does not.
set -euo pipefail

: "${STORM_ADMIN_PASSWORD:?set STORM_ADMIN_PASSWORD (do not hardcode it)}"
: "${VIEWER_PASSWORD:?set VIEWER_PASSWORD (do not hardcode it)}"
: "${VIEWER_USERNAME:=saulo2020}"
HUB="${HUB:-http://127.0.0.1:8123}"

# 1. log in as admin (the "storm" account) to get a token
TOKEN=$(curl -s -X POST "$HUB/auth/login" \
  -H 'Content-Type: application/json' \
  --data-binary @<(printf '{"username":"storm","password":"%s","purpose":"operate"}' "$STORM_ADMIN_PASSWORD") \
  | python3 -c 'import sys,json; print(json.load(sys.stdin)["token"])')

# 2. create the viewer
curl -s -X POST "$HUB/auth/users" \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  --data-binary @<(printf '{"username":"%s","password":"%s","role":"viewer"}' "$VIEWER_USERNAME" "$VIEWER_PASSWORD")

#!/bin/bash
# Nightly backup: pg_dump the storm_hub DB + checkpoint directory -> /srv/backups/storm-hub.
# Run by storm-hub-backup.service (systemd timer), not meant to be run manually except for testing.
#
# Why this exists (see plan §Root Cause Analysis / Phase F7): the project's original data loss was a
# manual, unscoped destructive command run under pressure during a live incident, with no backup to
# fall back on. This alone would have made that mistake recoverable in minutes instead of catastrophic.
set -euo pipefail

PROJECT_DIR=/srv/projects/storm-hub
BACKUP_DIR=/srv/backups/storm-hub
DUMPS_DIR="$BACKUP_DIR/dumps"
CKPT_BACKUP_DIR="$BACKUP_DIR/checkpoints"
RETAIN_DAYS=14

mkdir -p "$DUMPS_DIR" "$CKPT_BACKUP_DIR"
cd "$PROJECT_DIR"

# Pull DATABASE_URL out of .env without exporting every other secret in the file.
DATABASE_URL=$(grep -m1 '^DATABASE_URL=' .env | cut -d= -f2-)
# pg_dump doesn't understand the SQLAlchemy "+psycopg" dialect suffix.
PG_DUMP_URL=${DATABASE_URL/postgresql+psycopg:/postgresql:}

STAMP=$(date -u +%Y%m%d_%H%M%S)
TMPFILE=$(mktemp "$DUMPS_DIR/.storm_hub_${STAMP}.XXXXXX.dump")

pg_dump "$PG_DUMP_URL" -F custom -f "$TMPFILE"
mv "$TMPFILE" "$DUMPS_DIR/storm_hub_${STAMP}.dump"

# Checkpoint directory snapshot (Phase F7) — var/checkpoints/ may not exist yet early in the project;
# skip gracefully rather than failing the whole backup run.
if [ -d "$PROJECT_DIR/var/checkpoints" ]; then
    tar -czf "$CKPT_BACKUP_DIR/checkpoints_${STAMP}.tar.gz.tmp" -C "$PROJECT_DIR/var" checkpoints
    mv "$CKPT_BACKUP_DIR/checkpoints_${STAMP}.tar.gz.tmp" "$CKPT_BACKUP_DIR/checkpoints_${STAMP}.tar.gz"
fi

# Retention: keep the last RETAIN_DAYS days of dumps/snapshots.
find "$DUMPS_DIR" -name 'storm_hub_*.dump' -mtime "+${RETAIN_DAYS}" -delete
find "$CKPT_BACKUP_DIR" -name 'checkpoints_*.tar.gz' -mtime "+${RETAIN_DAYS}" -delete

echo "backup complete: $DUMPS_DIR/storm_hub_${STAMP}.dump"

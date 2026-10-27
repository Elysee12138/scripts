#!/usr/bin/env bash
# backup.sh - 数据备份脚本（示例占位）
set -euo pipefail

BACKUP_SRC="${BACKUP_SRC:-/data}"
BACKUP_DST="${BACKUP_DST:-/backup}"
STAMP="$(date +%Y%m%d-%H%M%S)"
KEEP_DAYS="${KEEP_DAYS:-7}"

mkdir -p "$BACKUP_DST"
tar -czf "$BACKUP_DST/backup-$STAMP.tar.gz" -C "$BACKUP_SRC" .
find "$BACKUP_DST" -name 'backup-*.tar.gz' -mtime "+$KEEP_DAYS" -delete

echo "[backup] done: $BACKUP_DST/backup-$STAMP.tar.gz"

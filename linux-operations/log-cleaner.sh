#!/usr/bin/env bash
# log-cleaner.sh - 清理过期日志（示例占位）
set -euo pipefail

LOG_DIR="${LOG_DIR:-/var/log}"
KEEP_DAYS="${KEEP_DAYS:-30}"

echo "[log-cleaner] target=$LOG_DIR keep=${KEEP_DAYS}d"
find "$LOG_DIR" -type f -name '*.log*' -mtime "+$KEEP_DAYS" -print -delete

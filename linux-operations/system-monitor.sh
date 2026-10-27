#!/usr/bin/env bash
# system-monitor.sh - 基础系统监控采集（示例占位）
set -uo pipefail

echo "===== $(date '+%F %T') ====="
echo "--- load ---";      uptime
echo "--- memory ---";    free -h
echo "--- disk ---";      df -h
echo "--- top cpu ---";   ps -eo pid,comm,%cpu,%mem --sort=-%cpu | head -n 11

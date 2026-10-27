#!/usr/bin/env bash
# security-audit.sh - 基础安全巡检（示例占位）
set -uo pipefail

echo "===== security audit ====="
echo "--- 登录失败记录 ---"
lastb 2>/dev/null | head -n 20 || echo "无 lastb 数据"

echo "--- 可登录账户 ---"
awk -F: '$7 !~ /(nologin|false)$/ {print $1":"$3":"$7}' /etc/passwd

echo "--- 监听端口 ---"
ss -tulnp 2>/dev/null || netstat -tulnp 2>/dev/null

echo "--- SUID 文件 ---"
find / -xdev -perm -4000 -type f 2>/dev/null | head -n 20

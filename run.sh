#!/usr/bin/env bash
# 一条命令：重置数据库 → 生成重复投诉样例 → 启动处置台。
set -euo pipefail
cd "$(dirname "$0")"

PORT="${1:-8000}"

echo "==> 重置数据库并生成样例（含同一事件的多次重复投诉）…"
python3 -m app.seed

echo "==> 启动营地噪声投诉处置台： http://127.0.0.1:${PORT}"
echo "    营位图 /map · 事件受理 /incidents · 换位建议 /relocate · 复核统计 /review"
exec python3 -m app.web "${PORT}"

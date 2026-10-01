#!/usr/bin/env bash
# 一条命令启动：重置并生成重复投诉合并等演示样例，然后启动本地服务。
# 用法：./run.sh [端口]   （默认端口 8000）
set -euo pipefail
cd "$(dirname "$0")"

PORT="${1:-8000}"
PYTHON="${PYTHON:-python3}"

echo "▶ 使用 $PYTHON（仅标准库，无需安装依赖）"
$PYTHON -m app.main --reset --host 127.0.0.1 --port "$PORT"

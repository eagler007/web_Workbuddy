#!/bin/sh
# 只渲染聚合报告（从 DATA_DIR/wb_history.json 读），不跑签到。
set -eu

DATA_DIR="${DATA_DIR:-/data}"
PY="$(command -v python3 || command -v python)"
HERE="$(cd "$(dirname "$0")/.." && pwd)"

mkdir -p "$DATA_DIR"
exec "$PY" "$HERE/wb_report.py" --report "$DATA_DIR/report.html" "$@"

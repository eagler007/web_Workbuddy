#!/bin/sh
# 跑一次「用量采集 + 落盘」，供 cron 或手动调用。
# 只读：只调查询接口，不改任何远端状态。
set -eu

DATA_DIR="${DATA_DIR:-/data}"
LOCK="$DATA_DIR/.collect_usage.lock"
PY="$(command -v python3 || command -v python)"
HERE="$(cd "$(dirname "$0")/.." && pwd)"
DAYS="${WB_USAGE_DAYS:-30}"

mkdir -p "$DATA_DIR"

exec 9>"$LOCK"
if ! flock -n 9; then
    echo "[collect_usage] 上一次还在跑，跳过本次 $(date '+%F %T')"
    exit 0
fi

echo "[collect_usage] 开始 $(date '+%F %T %Z') 窗口 ${DAYS} 天"
"$PY" "$HERE/collect_usage.py" \
    --days "$DAYS" \
    --accounts "$DATA_DIR/wb_accounts.json" \
    --history "$DATA_DIR/usage_history.json" \
    --print
RC=$?
echo "[collect_usage] 退出码 $RC（0=有数据 / 2=全部账号失败 / 1=出错）"

# 采完顺手重渲染聚合报告（离线），让报告页的用量区块也是新的
"$PY" "$HERE/wb_report.py" --report "$DATA_DIR/report.html" 2>/dev/null || \
    echo "[collect_usage][warn] 报告重渲染失败（不影响用量数据）"

exit $RC

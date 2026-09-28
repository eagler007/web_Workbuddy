#!/bin/sh
# 跑一次「签到 + 派猫 + 归档 + 出报告」，供 cron 或手动调用。
# 用 flock 防重叠：上一次没跑完时，这次直接退出（不会堆在一起）。
set -eu

DATA_DIR="${DATA_DIR:-/data}"
LOCK="$DATA_DIR/.run_daily.lock"
PY="$(command -v python3 || command -v python)"
HERE="$(cd "$(dirname "$0")/.." && pwd)"

mkdir -p "$DATA_DIR"

exec 9>"$LOCK"
if ! flock -n 9; then
    echo "[run_daily] 上一次还在跑，跳过本次 $(date '+%F %T')"
    exit 0
fi

echo "[run_daily] 开始 $(date '+%F %T %Z')"
"$PY" "$HERE/wb_daily.py" \
    --report "$DATA_DIR/report_single.html" \
    --raw-log "$DATA_DIR/wb_daily_raw.log"
RC=$?
echo "[run_daily] wb_daily 退出码 $RC"

# 归档已经由 wb_daily.py 写好，这里只把它渲染成聚合报告（离线，不连任何外部服务）
"$PY" "$HERE/wb_report.py" --report "$DATA_DIR/report.html" || \
    echo "[run_daily][warn] 聚合报告渲染失败（不影响签到结果）"

echo "[run_daily] 完成 $(date '+%F %T %Z')"
exit $RC

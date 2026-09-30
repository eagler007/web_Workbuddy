#!/bin/sh
# 容器入口：tini 已在 Dockerfile 里当 PID 1，这里负责起 supercronic + Web 服务。
set -eu

DATA_DIR="${DATA_DIR:-/data}"
WEB_PORT="${WEB_PORT:-8080}"
CRON_SCHEDULE="${CRON_SCHEDULE:-30 7,13,17 * * *}"
USAGE_CRON="${USAGE_CRON:-10 8 * * *}"
CRON_FILE="/tmp/wb-crontab"

mkdir -p "$DATA_DIR"
echo "[entry] DATA_DIR=$DATA_DIR  TZ=${TZ:-unset}  本地时间=$(date '+%Y-%m-%d %H:%M:%S %Z')"

# ---- crontab：时间点来自 CRON_SCHEDULE / USAGE_CRON（.env 可改）
cat > "$CRON_FILE" <<EOF
# WorkBuddy 每日签到 + 派猫；时间点由 CRON_SCHEDULE 控制（默认 7:30 / 13:30 / 17:30）
$CRON_SCHEDULE /app/deploy/run_daily.sh >> /proc/1/fd/1 2>&1
# 用量采集（只读）；默认每天早上 8:10 跑一次，窗口 WB_USAGE_DAYS（默认 30 天）
# 采的是「前一天」（WB_USAGE_TARGET=prev）—— 当天数据有 2-3 小时延迟，早上采当天必然为空
$USAGE_CRON /app/deploy/collect_usage.sh >> /proc/1/fd/1 2>&1
EOF
echo "[entry] crontab:"
sed 's/^/        /' "$CRON_FILE"

# ---- 起 supercronic（后台），日志直接进容器 stdout
if command -v supercronic >/dev/null 2>&1; then
    supercronic "$CRON_FILE" &
    echo "[entry] supercronic 已启动 (pid $!)"
else
    echo "[entry][warn] 没找到 supercronic，定时任务不会跑"
fi

# ---- 前台跑 Web（SIGTERM 由 tini 转给全组）
echo "[entry] 启动 Web: 0.0.0.0:$WEB_PORT"
exec python3 /app/app.py

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""运行日志归档 —— 按天保留历史，不覆盖。

每次 cron / 手动跑完签到，run_daily.sh 会把当次的完整输出（last_run.log）
喂进来，这里按「日期」追加到 data/logs/<YYYY-MM-DD>.log，并在每条前面加
`=== <时间戳> ===` 分隔。历史日志就这样累积，绝不互相覆盖。

Web 控制台的「日志」页默认显示当日，并可用 ?date= 查任意一天。

设计要点：
  - **不写凭据**：写入前用正则把 JWT（eyJ…）/ Server 酱 key（SCT…）打码。
  - **保留 180 天**：prune 删掉更早的文件，避免无限膨胀。
  - **纯标准库**，无外部依赖。

用法：
  python runlog.py --ingest /data/last_run.log
  python runlog.py --ingest /data/last_run.log --date 2026-09-29
  python runlog.py --prune            # 默认保留 180 天
  python runlog.py --prune 90
  python runlog.py --list             # 列出有日志的日期
  python runlog.py --read 2026-09-29  # 读出某天日志
"""

import argparse
import datetime
import glob
import os
import re

DATA_DIR = os.environ.get("DATA_DIR", "/data")
LOGS_DIR = os.path.join(DATA_DIR, "logs")
DEFAULT_KEEP = 180

# 二次防线：写入前把凭据形态打码（与 app.py 视图层一致）
_RE_TOKEN = re.compile(r"eyJ[A-Za-z0-9._\-]{20,}")
_RE_SCT = re.compile(r"\bSCT[A-Za-z0-9]{6,}")


def _redact(text):
    t = _RE_TOKEN.sub("***REDACTED***", text)
    t = _RE_SCT.sub("***REDACTED***", t)
    return t


def _ensure():
    os.makedirs(LOGS_DIR, exist_ok=True)


def _path_for(date_str):
    # 只允许 YYYY-MM-DD，避免路径穿越
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", date_str or ""):
        date_str = datetime.date.today().isoformat()
    return os.path.join(LOGS_DIR, date_str + ".log")


def ingest(src_file, date=None, ts=None):
    """读 src_file（当次运行输出），按 date 追加到当日日志文件。返回 (date, 字节数)。"""
    if not os.path.isfile(src_file):
        return None, 0
    try:
        with open(src_file, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
    except Exception:
        return None, 0
    content = content.strip()
    if not content:
        return None, 0
    date = date or datetime.date.today().isoformat()
    ts = ts or datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    _ensure()
    p = _path_for(date)
    block = "\n=== %s ===\n%s\n" % (ts, _redact(content))
    try:
        with open(p, "a", encoding="utf-8") as f:
            f.write(block)
        return date, len(block)
    except Exception:
        return None, 0


def prune(days=DEFAULT_KEEP):
    """删除 days 天之前的日志文件。返回删除的日期列表。"""
    _ensure()
    cutoff = datetime.date.today() - datetime.timedelta(days=days)
    removed = []
    for p in glob.glob(os.path.join(LOGS_DIR, "*.log")):
        base = os.path.basename(p)[:-4]
        try:
            d = datetime.date.fromisoformat(base)
        except Exception:
            continue
        if d < cutoff:
            try:
                os.remove(p)
                removed.append(base)
            except Exception:
                pass
    return removed


def list_dates():
    """返回有日志的日期列表（升序）。"""
    _ensure()
    out = []
    for p in glob.glob(os.path.join(LOGS_DIR, "*.log")):
        base = os.path.basename(p)[:-4]
        if re.match(r"^\d{4}-\d{2}-\d{2}$", base):
            out.append(base)
    return sorted(out)


def read_date(date=None):
    """读某天日志文本；date 默认今天。文件不存在返回空串。"""
    date = date or datetime.date.today().isoformat()
    p = _path_for(date)
    if not os.path.isfile(p):
        return ""
    try:
        with open(p, "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    except Exception:
        return ""


def main(argv=None):
    ap = argparse.ArgumentParser(description="运行日志按天归档")
    ap.add_argument("--ingest", help="把某次运行输出文件追加进当日日志")
    ap.add_argument("--date", help="指定日期 YYYY-MM-DD（默认今天）")
    ap.add_argument("--prune", nargs="?", const=DEFAULT_KEEP, type=int,
                    help="删除 N 天前的日志（默认 %d）" % DEFAULT_KEEP)
    ap.add_argument("--list", action="store_true", help="列出有日志的日期")
    ap.add_argument("--read", help="读出某天日志")
    args = ap.parse_args(argv)

    if args.list:
        for d in list_dates():
            print(d)
        return 0
    if args.read:
        print(read_date(args.read))
        return 0
    if args.prune is not None:
        removed = prune(args.prune)
        print("[runlog] 已清理 %d 个早于 %d 天的日志：%s" %
              (len(removed), args.prune, ", ".join(removed) or "无"))
        return 0
    if args.ingest:
        date, n = ingest(args.ingest, date=args.date)
        if date:
            print("[runlog] 已归档 %d 字节 → %s" % (n, date))
            return 0
        print("[runlog] 无内容可归档（%s 为空或不存在）" % args.ingest)
        return 0
    ap.print_help()
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())

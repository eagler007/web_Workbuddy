#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""用量采集器 —— 自动请求真实用量接口，把结果落到 data/usage_history.json。

供容器里的定时任务（supercronic）每天调用，也可以手动跑：

    python collect_usage.py                 # 用 data/wb_accounts.json 里的账号
    python collect_usage.py --days 30       # 指定窗口
    python collect_usage.py --print         # 同时把摘要打到 stdout（定时任务日志里能看）

设计要点：
  - **只读**：只调查询接口，不写任何远端状态。
  - **隔离**：单个账号失败不影响其它账号；整体失败不影响退出码语义（退出码只表示「有没有落盘」）。
  - **合并历史**：同一天重复跑会**覆盖当天**记录（幂等），不是追加出重复行。
  - **不含 token**：落盘内容只有账号名、掩码 UID、日期与积分，**没有任何凭据**。

数据文件结构（data/usage_history.json）：
    {
      "version": 1,
      "updated": "YYYY-MM-DD HH:MM:SS",
      "days": 30,
      "by_date": {                       # 按「采集日期」归并，重跑覆盖
         "2026-09-28": {
            "ts": "2026-09-28 08:00:12",
            "days": 30,
            "accounts": [
              {"name":"本机","uid_masked":"11111111…","ok":true,
               "sum":123.45,"today":12.3,
               "series":[{"date":"2026-08-30","credit":1.2}, ...]}
            ]
         }
      }
    }
"""

import argparse
import datetime
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import usage as U  # noqa: E402

DATA_DIR = os.environ.get("DATA_DIR", "/data")
ACCOUNTS_FILE = os.path.join(DATA_DIR, "wb_accounts.json")
HISTORY_FILE = os.path.join(DATA_DIR, "usage_history.json")


def _log(msg):
    print("[usage] %s" % msg, flush=True)


def load_accounts(path=None):
    """读账号。兼容 {"accounts":[...]} 与裸数组两种形态。"""
    p = path or ACCOUNTS_FILE
    if not os.path.isfile(p):
        return []
    try:
        with open(p, "r", encoding="utf-8") as f:
            d = json.load(f)
    except Exception as e:
        _log("读账号文件失败：%s" % e)
        return []
    arr = d.get("accounts") if isinstance(d, dict) else d
    if not isinstance(arr, list):
        return []
    out = []
    for a in arr:
        if isinstance(a, dict) and (a.get("token") or a.get("uid")):
            out.append({"name": (a.get("name") or "").strip(),
                        "token": (a.get("token") or "").strip(),
                        "uid": (a.get("uid") or "").strip()})
    return out


def load_history(path=None):
    p = path or HISTORY_FILE
    if not os.path.isfile(p):
        return {"version": 1, "updated": "", "days": 0, "by_date": {}}
    try:
        with open(p, "r", encoding="utf-8") as f:
            d = json.load(f)
        if not isinstance(d, dict) or not isinstance(d.get("by_date"), dict):
            raise ValueError("结构不对")
        return d
    except Exception as e:
        _log("历史文件损坏（%s），将重建" % e)
        return {"version": 1, "updated": "", "days": 0, "by_date": {}}


def save_history(h, path=None):
    """原子写：临时文件 + rename，避免 Web 端读到半截。"""
    p = path or HISTORY_FILE
    os.makedirs(os.path.dirname(p) or ".", exist_ok=True)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(h, f, ensure_ascii=False, indent=1)
        f.write("\n")
    os.replace(tmp, p)
    try:
        os.chmod(p, 0o600)
    except Exception:
        pass


def slim_rows(rows, keep_series=True):
    """把查询结果精简成落盘形态（**去掉一切凭据**，只留展示需要的）。

    2026-09-29 起：用量接口换成 get-user-request-usage（请求级明细），
    聚合出 by_model / by_hour / total。这些只是统计值，不含任何凭据，一并落盘，
    方便历史页直接画「按模型」分布，不必回源重查。
    """
    out = []
    for r in rows or []:
        item = {
            "name": r.get("name") or "-",
            "uid_masked": r.get("uid_masked") or "-",
            "ok": bool(r.get("ok")),
            "err": (str(r.get("err"))[:120] if r.get("err") else None),
            "http": r.get("http"),
            "sum": r.get("sum"),
            "today": r.get("today"),
            "total": r.get("total"),
            "range": r.get("range"),
            "by_model": r.get("by_model") or {},
            "by_hour": r.get("by_hour") or {},
        }
        if keep_series:
            item["series"] = [{"date": (it.get("date") or "")[:10],
                               "credit": it.get("credit")}
                              for it in (r.get("days") or [])
                              if (it.get("date") or "")]
        out.append(item)
    return out


def collect(days=30, accounts=None, history=None, accounts_file=None):
    """跑一轮采集，返回 (rows, 落盘后的 history)。不写盘。"""
    accs = accounts if accounts is not None else load_accounts(accounts_file)
    if not accs:
        _log("没有账号（%s 为空或不含 token/uid）" % ACCOUNTS_FILE)
        return [], history or load_history()
    _log("开始采集 %d 个账号 · 窗口 %d 天" % (len(accs), days))
    rows = U.query_accounts(accs, days=days)
    n_ok = len([r for r in rows if r.get("ok")])
    _log("采集完成：%d/%d 个账号返回数据" % (n_ok, len(rows)))
    for r in rows:
        if r.get("ok"):
            _log("  %-12s 今日 %-8s %d 天合计 %s" % (
                r.get("name"), r.get("today"), days, r.get("sum")))
        else:
            _log("  %-12s 失败：%s" % (r.get("name"), r.get("err")))

    h = history or load_history()
    key = datetime.date.today().isoformat()
    h.setdefault("by_date", {})
    # 幂等：同一天重跑覆盖当天，不追加重复行
    h["by_date"][key] = {
        "ts": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "days": days,
        "accounts": slim_rows(rows),
    }
    # 只留最近 180 天，别让文件无限长
    keys = sorted(h["by_date"].keys())
    if len(keys) > 180:
        for k in keys[:-180]:
            h["by_date"].pop(k, None)
    h["version"] = 1
    h["days"] = days
    h["updated"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return rows, h


def print_summary(rows, days):
    """把摘要打到 stdout —— 定时任务的日志里能直接看到。"""
    n_ok = len([r for r in rows if r.get("ok")])
    print("")
    print("=" * 58)
    print("WorkBuddy 用量采集 · %s · 窗口 %d 天" %
          (datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), days))
    print("=" * 58)
    if not rows:
        print("没有账号可查。")
    for r in rows:
        if r.get("ok"):
            print("  %-12s 今日 %-10s %d 天合计 %s" % (
                r.get("name") or "-",
                r.get("today") if r.get("today") is not None else "—",
                days, r.get("sum") if r.get("sum") is not None else "—"))
        else:
            print("  %-12s 失败：%s" % (r.get("name") or "-", r.get("err")))
    print("-" * 58)
    print("合计 %d/%d 个账号返回数据 · 口径：积分消耗（非 token 数）" %
          (n_ok, len(rows)))
    print("")


def main(argv=None):
    ap = argparse.ArgumentParser(description="WorkBuddy 用量采集（只读）")
    ap.add_argument("--days", type=int,
                    default=int(os.environ.get("WB_USAGE_DAYS") or 30),
                    help="查询窗口天数，默认 30，上限 31")
    ap.add_argument("--accounts", default=ACCOUNTS_FILE, help="账号文件路径")
    ap.add_argument("--history", default=HISTORY_FILE, help="历史文件路径")
    ap.add_argument("--print", dest="do_print", action="store_true",
                    help="把摘要打到 stdout")
    ap.add_argument("--dry-run", action="store_true", help="只查不落盘")
    args = ap.parse_args(argv)

    acc_file = args.accounts
    hist_file = args.history
    days = max(1, min(int(args.days or 30), 31))
    try:
        rows, h = collect(days=days, accounts_file=acc_file, history=load_history(hist_file))
    except Exception as e:
        _log("采集异常：%s: %s" % (type(e).__name__, e))
        return 1

    if not rows:
        _log("没有可采集的账号（%s）" % acc_file)
        return 1

    if args.do_print:
        print_summary(rows, days)

    if args.dry_run:
        _log("dry-run：不落盘")
        return 0

    try:
        save_history(h, hist_file)
        _log("已写入 %s（按日期归并，共 %d 天）" % (hist_file, len(h["by_date"])))
    except Exception as e:
        _log("落盘失败：%s: %s" % (type(e).__name__, e))
        return 1

    # 退出码：只要有账号成功就 0；全失败返回 2（便于 cron 侧区分）
    n_ok = len([r for r in rows if r.get("ok")])
    return 0 if n_ok > 0 else 2


if __name__ == "__main__":
    sys.exit(main())

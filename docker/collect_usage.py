#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""用量采集器 —— 自动请求真实用量接口，把结果落到 data/usage_history.json。

供容器里的定时任务（supercronic）每天调用，也可以手动跑：

    python collect_usage.py                 # 用 data/wb_accounts.json 里的账号
    python collect_usage.py --days 30       # 指定窗口
    python collect_usage.py --target today  # 采当天（默认采「前一天」）
    python collect_usage.py --print         # 同时把摘要打到 stdout（定时任务日志里能看）

设计要点：
  - **只读**：只调查询接口，不写任何远端状态。
  - **隔离**：单个账号失败不影响其它账号；整体失败不影响退出码语义（退出码只表示「有没有落盘」）。
  - **采「前一天」**（`--target prev`，默认）：官方明说用量数据有 **2–3 小时延迟**，
    早上 8:10 采「当天」必然一片空（页面上一排「—」，2026-09-30 老板反馈的正是这个）。
    采昨天 = 一整天完整数据，且离采集时刻至少已过去 8 小时，数据一定落库了。
  - **键 = 数据归属日**（v2 起）：一次请求就拿回整个窗口的按日数据，所以顺手把窗口里
    每一天都落一格 —— 历史表不会因为容器停过几天就断档，也不需要额外请求。
  - **不拿失败盖掉好数据**：某个账号这次查失败时，若它在这天已有成功记录，**保留原值**。
  - **不含 token**：落盘内容只有账号名、掩码 UID、日期与积分，**没有任何凭据**。

数据文件结构（data/usage_history.json，v2）：
    {
      "version": 2,
      "updated": "YYYY-MM-DD HH:MM:SS",
      "days": 30,
      "target_mode": "prev",
      "by_date": {                       # 键 = 数据归属日（不是采集日）
         "2026-09-29": {
            "ts": "2026-09-30 08:10:12",  # 这一格最后一次被采到的时刻
            "v": 2,
            "target": "2026-09-29",
            "accounts": [
              {"name":"本机","uid_masked":"11111111…","ok":true,
               "credit":12.34,"sum":123.45,"total":180,"http":200,"err":null}
            ]
         }
      }
    }

> v1 的键是「采集日」、字段叫 `today`；读取端（app.py / 页面）两种都认。
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


TARGET_PREV = "prev"
TARGET_TODAY = "today"


def resolve_target(mode=None, today=None):
    """把 target 模式解析成「窗口右端日期」。返回 (end_date_iso, mode)。

    prev（默认）：采集日的**前一天** —— 当天数据有 2–3h 延迟，早上采「今天」必空。
    today：采集日当天（想傍晚采当天就用这个）。
    """
    d = today or datetime.date.today()
    m = (mode or os.environ.get("WB_USAGE_TARGET") or TARGET_PREV).strip().lower()
    if m not in (TARGET_PREV, TARGET_TODAY):
        m = TARGET_PREV
    end = d - datetime.timedelta(days=1) if m == TARGET_PREV else d
    return end.isoformat(), m


def window_dates(days, end_iso):
    """窗口内所有日期，升序（含 end_iso）。"""
    try:
        end = datetime.date.fromisoformat(str(end_iso)[:10])
    except Exception:
        end = datetime.date.today()
    days = max(1, min(int(days or 30), 31))
    return [(end - datetime.timedelta(days=i)).isoformat()
            for i in range(days - 1, -1, -1)]


def _credit_on(row, date):
    """某账号在指定日期的积分。

    ⚠️ 窗口内**没有记录 = 0.0**，不是 None —— 那个账号那天确实没调用。
    返回 None 会让页面上又出现一排「—」（就是老板截图里那个问题）。
    """
    for it in (row.get("days") or []):
        if (it.get("date") or "")[:10] == date:
            try:
                return round(float(it.get("credit")), 2)
            except (TypeError, ValueError):
                return 0.0
    return 0.0


def _cell(row, date):
    """把一个账号的结果转成某一天的落盘格子（含成功/失败两种形态）。"""
    c = {"name": row.get("name") or "-",
         "uid_masked": row.get("uid_masked") or "-",
         "ok": bool(row.get("ok")),
         "http": row.get("http"),
         "err": (str(row.get("err"))[:120] if row.get("err") else None)}
    if c["ok"]:
        c["credit"] = _credit_on(row, date)
        c["sum"] = row.get("sum")
        c["total"] = row.get("total")
    return c


def write_window(h, rows, days, end_iso, ts):
    """把这一轮拿到的窗口数据写进历史（**键 = 数据归属日**）。返回写了多少天。

    - 一次请求拿回整个窗口的按日数据 → 窗口里每一天都落一格（历史不会断档）。
    - 某账号这次失败时，若它在这天已有成功记录 → **保留原值**（别用失败盖掉好数据）。
    - 全部账号都失败 → 只写窗口右端一格的失败痕迹，不动其它日期。
    """
    bd = h.setdefault("by_date", {})
    n_ok = len([r for r in rows if r.get("ok")])
    dates = window_dates(days, end_iso) if n_ok > 0 else [end_iso]
    for d in dates:
        prev = bd.get(d) if isinstance(bd.get(d), dict) else {}
        old = {}
        for a in (prev.get("accounts") or []):
            if isinstance(a, dict) and a.get("name"):
                old[a["name"]] = a
        cells = []
        for r in rows:
            nm = r.get("name") or "-"
            keep = old.get(nm)
            if not r.get("ok") and isinstance(keep, dict) and keep.get("ok"):
                cells.append(keep)          # 保留这天已有的好数据
            else:
                cells.append(_cell(r, d))
        bd[d] = {"ts": ts, "v": 2, "target": d, "accounts": cells}
    return len(dates)


def collect(days=30, accounts=None, history=None, accounts_file=None, target=None):
    """跑一轮采集，返回 (rows, 落盘后的 history)。不写盘。"""
    accs = accounts if accounts is not None else load_accounts(accounts_file)
    if not accs:
        _log("没有账号（%s 为空或不含 token/uid）" % ACCOUNTS_FILE)
        return [], history or load_history()
    end_iso, mode = resolve_target(target)
    _log("开始采集 %d 个账号 · 窗口 %d 天 · 目标日 %s（%s）"
         % (len(accs), days, end_iso, "前一日" if mode == TARGET_PREV else "当日"))
    rows = U.query_accounts(accs, days=days, end_date=end_iso)
    n_ok = len([r for r in rows if r.get("ok")])
    _log("采集完成：%d/%d 个账号返回数据" % (n_ok, len(rows)))
    for r in rows:
        if r.get("ok"):
            tc = r.get("target_credit")
            if tc is None:
                tc = _credit_on(r, end_iso)
            _log("  %-12s %s 消耗 %-8s %d 天合计 %s" % (
                r.get("name"), end_iso, tc, days, r.get("sum")))
        else:
            _log("  %-12s 失败：%s" % (r.get("name"), r.get("err")))

    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    h = history or load_history()
    h.setdefault("by_date", {})
    n_written = write_window(h, rows, days=days, end_iso=end_iso, ts=ts)
    if n_ok:
        _log("已落盘：%d 天记录（目标日 %s，键 = 数据归属日）" % (n_written, end_iso))
    else:
        _log("全部账号失败：只在 %s 留一格失败痕迹，不覆盖已有历史" % end_iso)
    # 只留最近 180 天，别让文件无限长
    keys = sorted(h["by_date"].keys())
    if len(keys) > 180:
        for k in keys[:-180]:
            h["by_date"].pop(k, None)
    h["version"] = 2
    h["days"] = days
    h["target_mode"] = mode
    h["updated"] = ts
    return rows, h


def print_summary(rows, days, target=None):
    """把摘要打到 stdout —— 定时任务的日志里能直接看到。"""
    n_ok = len([r for r in rows if r.get("ok")])
    end_iso = next((r.get("target") for r in rows if r.get("target")), None)
    if not end_iso:
        end_iso, _ = resolve_target(target)
    print("")
    print("=" * 58)
    print("WorkBuddy 用量采集 · %s · 窗口 %d 天 · 目标日 %s" %
          (datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), days, end_iso))
    print("=" * 58)
    if not rows:
        print("没有账号可查。")
    for r in rows:
        if r.get("ok"):
            tc = r.get("target_credit")
            if tc is None:
                tc = _credit_on(r, end_iso)
            print("  %-12s %s %-10s %d 天合计 %s" % (
                r.get("name") or "-", end_iso,
                tc if tc is not None else "—",
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
    ap.add_argument("--target", default=os.environ.get("WB_USAGE_TARGET") or TARGET_PREV,
                    choices=[TARGET_PREV, TARGET_TODAY],
                    help="采哪一天：prev=前一天（默认，避开 2–3h 数据延迟），"
                         "today=当天")
    ap.add_argument("--print", dest="do_print", action="store_true",
                    help="把摘要打到 stdout")
    ap.add_argument("--dry-run", action="store_true", help="只查不落盘")
    args = ap.parse_args(argv)

    acc_file = args.accounts
    hist_file = args.history
    days = max(1, min(int(args.days or 30), 31))
    try:
        rows, h = collect(days=days, accounts_file=acc_file,
                          history=load_history(hist_file), target=args.target)
    except Exception as e:
        _log("采集异常：%s: %s" % (type(e).__name__, e))
        return 1

    if not rows:
        _log("没有可采集的账号（%s）" % acc_file)
        return 1

    if args.do_print:
        print_summary(rows, days, target=args.target)

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

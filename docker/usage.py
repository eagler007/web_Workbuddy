#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""用量查询模块 —— 自动请求真实积分消耗接口。

从 wb_daily.py 抽出来，供 Web 控制台直接调用（控制台不该 import 整个签到脚本）。

接口来源：www.workbuddy.cn 主包 index-BTO2lsRd.js（786 KB）里内联的用量页代码，
顺着「用量明细」文案挖出来的。请求体字段名是 **startTime/endTime**（驼峰），
别和 get-user-resource 那套 SlicePeriodStartTime/PackageCodes 混了 —— 那是另一个接口。

    POST /v2/billing/meter/get-user-daily-usage
         body {"startTime":"YYYY-MM-DD 00:00:00",
               "endTime":"YYYY-MM-DD 23:59:59",
               "pageNum":1,"pageSize":N}
         resp data.data.data[] = [{"date":"YYYY-MM-DD","credit":<消耗>}, ...]
              data.data.total  = 总条数
    POST /v2/billing/meter/get-user-request-usage   请求级明细，同上参数

业务要点（前端文案原文）：
  - 「CodeBuddy 插件、IDE、Code 采用积分计费模式，模型调用根据系数自动扣除积分。」
    → 这个 credit 就是**积分消耗**，积分按模型系数折算，不是原始 token 数。
  - 「用量数据存在 2-3 小时的数据延迟」→ 当天数据可能还没落库，别把「今天为 0」当异常。
  - 「用量明细仅展示 {date} 之后的数据」→ 有最早可查日期。
  - 前端的日期区间硬上限是 **31 天**，超了会被前端拦（服务端行为未验证，这里也按 31 天封顶）。

依赖：纯标准库。**不 import wb_daily**（避免控制台被签到脚本的重依赖拖住）。
"""

import datetime
import json
import os
import urllib.error
import urllib.request

HOST_BILLING = "https://www.codebuddy.cn"    # 积分/用量接口
TIMEOUT = 20
USAGE_MAX_DAYS = 31        # 前端 hard limit，照抄
USAGE_DEFAULT_DAYS = 7     # 前端默认窗口

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) WorkBuddy-Usage/1.0")


# ---------------------------------------------------------------- 小工具
def _r2(v):
    """保留 2 位小数；None 原样返回。"""
    if v is None:
        return None
    try:
        return round(float(v) + 0.0, 2)
    except (TypeError, ValueError):
        return None


def _int_or_none(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _call(method, url, token, uid, body=None, timeout=TIMEOUT):
    """返回 (http_status, 原始响应文本)。不抛异常。"""
    data = None if method == "GET" else json.dumps(body if body is not None else {}).encode("utf-8")
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Accept": "application/json",
        "Authorization": "Bearer " + token,
        "X-User-Id": uid,
        "Content-Type": "application/json",
        "User-Agent": UA,
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as e:
        return 0, "URLError: %s" % e


def _jload(text):
    try:
        return json.loads(text)
    except Exception:
        return None


def usage_range(days):
    """返回 (startTime, endTime) 字符串，按前端 li() 的格式。"""
    days = max(1, min(int(days or USAGE_DEFAULT_DAYS), USAGE_MAX_DAYS))
    today = datetime.date.today()
    start = (today - datetime.timedelta(days=days - 1)).isoformat()
    return start + " 00:00:00", today.isoformat() + " 23:59:59"


# ---------------------------------------------------------------- 主查询
def get_daily_usage(token, uid, days=USAGE_DEFAULT_DAYS, timeout=TIMEOUT):
    """读每日积分/用量。返回结构化 dict，取不到就 days=[] 且 ok=False，绝不抛异常。

    返回字段：
      ok/http/err/raw_body/range[起,止]/total/days[{date,credit}]/sum/today
    """
    r = {"ok": False, "http": 0, "err": None, "days": [], "total": None,
         "today": None, "sum": None, "raw_body": "", "range": None}
    try:
        st_t, en_t = usage_range(days)
        r["range"] = [st_t[:10], en_t[:10]]
        body = {"startTime": st_t, "endTime": en_t, "pageNum": 1, "pageSize": 100}
        st, text = _call("POST", HOST_BILLING + "/v2/billing/meter/get-user-daily-usage",
                         token, uid, body, timeout=timeout)
        r["http"] = st
        r["raw_body"] = text
        obj = _jload(text)
        if not isinstance(obj, dict):
            r["err"] = "响应不是 JSON（http=%s）" % st
            return r
        if st != 200 or obj.get("code") not in (0, None):
            r["err"] = "http=%s code=%s msg=%s" % (
                st, obj.get("code"), obj.get("msg") or obj.get("message"))
            return r
        # ⚠️ 双层 data：data.data.data 才是数组（前端写的是 E.data.data.data）。
        #    但网关偶尔会少一层，所以逐层判空、能取到就用。
        d = obj.get("data")
        if isinstance(d, dict) and isinstance(d.get("data"), dict):
            inner = d["data"]
        else:
            inner = d if isinstance(d, dict) else {}
        arr = inner.get("data") if isinstance(inner, dict) else None
        if not isinstance(arr, list):
            r["err"] = "data.data.data 不是列表（可能字段改版）"
            return r
        r["total"] = _int_or_none(inner.get("total"))
        out, tot = [], 0.0
        for it in arr:
            if not isinstance(it, dict):
                continue
            dt = it.get("date") or it.get("Date")
            # 没有日期的记录直接丢：这是「按天」的序列，缺日期就没法画、也没法比对
            if dt in (None, ""):
                continue
            cr = it.get("credit")
            if cr is None:
                cr = it.get("Credit")
            try:
                cr = float(cr)
            except (TypeError, ValueError):
                cr = None
            out.append({"date": str(dt)[:10],
                        "credit": _r2(cr) if cr is not None else None})
            if cr is not None:
                tot += cr
        # 按日期升序，方便报告画走势
        out.sort(key=lambda x: x.get("date") or "")
        r["days"] = out
        r["sum"] = _r2(tot) if out else None
        today_s = datetime.date.today().isoformat()
        for it in out:
            if (it.get("date") or "")[:10] == today_s:
                r["today"] = it.get("credit")
                break
        r["ok"] = True
    except Exception as e:
        r["err"] = "%s: %s" % (type(e).__name__, e)
    return r


def fill_gaps(days, n=None):
    """把稀疏的按天数据补齐成连续序列（缺的填 0），方便画走势。

    days: [{"date":"YYYY-MM-DD","credit":x}, ...]（已升序）
    返回等长连续序列；n=None 时按数据自身的首尾跨度补。
    """
    if not days:
        return []
    try:
        first = datetime.date.fromisoformat(days[0]["date"])
        last = datetime.date.fromisoformat(days[-1]["date"])
    except Exception:
        return list(days)
    if n:                                  # 显式指定长度：从 last 往前推 n 天
        try:
            n = max(1, min(int(n), USAGE_MAX_DAYS))
            first = last - datetime.timedelta(days=n - 1)
        except Exception:
            pass
    got = {}
    for it in days:
        got[(it.get("date") or "")[:10]] = it.get("credit")
    out, cur = [], first
    while cur <= last:
        k = cur.isoformat()
        out.append({"date": k, "credit": got.get(k, 0.0) if k in got else 0.0})
        cur += datetime.timedelta(days=1)
    return out


# ---------------------------------------------------------------- 多账号
def query_accounts(accounts, days=USAGE_DEFAULT_DAYS, timeout=TIMEOUT, workers=4):
    """并发查多个账号的用量。

    accounts: [{"name":..., "token":..., "uid":...}, ...]
    返回 [{"name","uid_masked","ok","err","http","sum","today","days","range"}, ...]

    隔离纪律：任一账号查询失败只影响它自己那一行，**绝不影响其它账号**，
    整体也不抛异常。
    """
    out = [None] * len(accounts or [])

    def _one(i, a):
        name = (a.get("name") or "").strip()
        uid = (a.get("uid") or "").strip()
        token = (a.get("token") or "").strip()
        uid_m = (uid[:8] + "…") if len(uid) > 8 else (uid or "-")
        row = {"name": name or uid_m, "uid_masked": uid_m, "ok": False,
               "err": None, "http": 0, "sum": None, "today": None,
               "days": [], "range": None}
        if not token or not uid:
            row["err"] = "缺 token 或 uid"
            out[i] = row
            return
        try:
            du = get_daily_usage(token, uid, days=days, timeout=timeout)
            row["ok"] = bool(du.get("ok"))
            row["err"] = du.get("err")
            row["http"] = du.get("http")
            row["sum"] = du.get("sum")
            row["today"] = du.get("today")
            row["days"] = du.get("days") or []
            row["range"] = du.get("range")
        except Exception as e:                       # 双保险
            row["err"] = "%s: %s" % (type(e).__name__, e)
        out[i] = row

    try:
        n = len(accounts or [])
        if n == 0:
            return []
        w = max(1, min(int(workers or 1), 8))
        if w == 1 or n == 1:
            for i, a in enumerate(accounts):
                _one(i, a)
        else:
            from concurrent.futures import ThreadPoolExecutor
            with ThreadPoolExecutor(max_workers=w) as ex:
                list(ex.map(lambda p: _one(p[0], p[1]), list(enumerate(accounts))))
    except Exception:
        # 并发框架本身挂了 → 退化成串行，别让整个页面空掉
        for i, a in enumerate(accounts or []):
            if out[i] is None:
                _one(i, a)
    return [x for x in out if x is not None]


def merge_rows(rows):
    """把多账号结果并成「按日期求和」的全局序列，用于画总走势。

    返回 (["YYYY-MM-DD", ...] 升序, {date: 合计}, 总合计)
    """
    acc = {}
    for r in rows or []:
        for it in (r.get("days") or []):
            d = (it.get("date") or "")[:10]
            if not d:
                continue
            v = it.get("credit")
            try:
                v = float(v)
            except (TypeError, ValueError):
                v = 0.0
            acc[d] = acc.get(d, 0.0) + v
    keys = sorted(acc.keys())
    return keys, {k: _r2(acc[k]) for k in keys}, _r2(sum(acc.values())) if acc else None


def _selftest():
    """离线自测：python usage.py 直接跑。不联网。"""
    ok = fail = 0

    def ck(label, cond):
        nonlocal ok, fail
        if cond:
            ok += 1
            print("  [OK] %s" % label)
        else:
            fail += 1
            print("  [!!] %s" % label)

    print("[1] usage_range 边界")
    s, e = usage_range(7)
    ck("7 天窗口右端是今天", e.startswith(datetime.date.today().isoformat()))
    ck("左端 = 今天-6 天",
       s.startswith((datetime.date.today() - datetime.timedelta(days=6)).isoformat()))
    s2, _ = usage_range(999)
    ck("超上限被夹到 31 天",
       s2.startswith((datetime.date.today() - datetime.timedelta(days=30)).isoformat()))
    s3, _ = usage_range(0)
    # 0/None 视为「没传」→ 回落到默认窗口（不是夹到 1 天）
    ck("0 天回落到默认 7 天",
       s3.startswith((datetime.date.today() - datetime.timedelta(days=6)).isoformat()))
    s4, _ = usage_range(-5)
    ck("负数夹到 1 天（只有今天一天）", s4.startswith(datetime.date.today().isoformat()))

    print("[2] get_daily_usage 异常路径（无网络，必失败但不抛）")
    r = get_daily_usage("", "", days=7, timeout=1)
    ck("空凭据不抛异常", isinstance(r, dict))
    ck("空凭据 ok=False", r["ok"] is False)
    ck("空凭据有 err", bool(r["err"]))

    print("[3] fill_gaps 补洞")
    g = fill_gaps([{"date": "2026-09-25", "credit": 1.0},
                   {"date": "2026-09-27", "credit": 3.0}])
    ck("跨 3 天补成 3 条", len(g) == 3)
    ck("中间那天补 0", g[1]["credit"] == 0.0)
    ck("首尾值保留", g[0]["credit"] == 1.0 and g[2]["credit"] == 3.0)
    ck("空输入返回空", fill_gaps([]) == [])

    print("[4] merge_rows 多账号求和")
    rows = [{"days": [{"date": "2026-09-27", "credit": 1.5}]},
            {"days": [{"date": "2026-09-27", "credit": 2.5},
                      {"date": "2026-09-28", "credit": 4.0}]}]
    keys, per, total = merge_rows(rows)
    ck("日期升序两个", keys == ["2026-09-27", "2026-09-28"])
    ck("同日求和 4.0", per["2026-09-27"] == 4.0)
    ck("总计 8.0", total == 8.0)

    print("[5] query_accounts 缺凭据不炸 + 掩码")
    qs = query_accounts([{"name": "a", "token": "", "uid": ""},
                         {"name": "b", "token": "x", "uid": "1234567890abcdef"}],
                        days=7, timeout=1)
    ck("返回条数不变", len(qs) == 2)
    ck("缺凭据那行有 err", bool(qs[0]["err"]))
    ck("uid 掩码为前 8 位", qs[1]["uid_masked"] == "12345678…")
    ck("uid_masked 不含完整 uid", "1234567890abcdef" not in json.dumps(qs, ensure_ascii=False))

    print("\n自测：%d 通过 / %d 失败" % (ok, fail))
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    import sys
    sys.exit(_selftest())

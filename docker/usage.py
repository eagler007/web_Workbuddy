#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""用量查询模块 —— 自动请求真实积分消耗接口。

从 wb_daily.py 抽出来，供 Web 控制台直接调用（控制台不该 import 整个签到脚本）。

⚠️ **接口真相（2026-09-29 实测确认，踩坑纪录）**：
- `get-user-daily-usage` **这个接口根本不存在** —— 任何 body（camelCase / PascalCase /
  带毫秒 / 带 ProductCode）都返回 `{"code":...,"msg":"invalid params"}`，纯属幻觉接口。
- **真正能用的只有 `get-user-request-usage`**（请求级明细）：
      POST https://www.codebuddy.cn/billing/meter/get-user-request-usage
  - **不带 /v2 前缀**（和查余额/签到那条相反 —— 同组 `/billing/meter/*` 前缀不统一，
    带错就是 404：404=路径错，401=路径对但没凭据）。
  - 鉴权：`Authorization: Bearer <token>` + `X-User-Id: <uid>`（和其它接口一致）。
  - 请求体（camelCase，实测通）：
        {"startTime":"YYYY-MM-DD 00:00:00",
         "endTime":"YYYY-MM-DD 23:59:59",
         "pageNum":1, "pageSize":500}
  - 响应（**两层 data**，不是三层）：
        {"code":0,"msg":"OK",
         "data":{"total":N,
                 "data":[{"requestId":..,"credit":0.37,"model":"deepseek-v4.1-flash",
                          "client":"WorkBuddy","requestTime":"2026-09-29 09:38:00", ...}]}}
    `data.data` 是请求数组，`data.total` 是总条数。`pageSize=500` 一般一次拿全。

业务要点（前端文案原文）：
  - 「CodeBuddy 插件、IDE、Code 采用积分计费模式，模型调用根据系数自动扣除积分。」
    → 这个 `credit` 就是**积分消耗**，按模型系数折算，不是原始 token 数。
  - 「用量数据存在 2-3 小时的数据延迟」→ 当天数据可能还没落库，别把「今天为 0」当异常。
  - 前端的日期区间硬上限是 **31 天**，超了会被前端拦（服务端行为未验证，这里也按 31 天封顶）。

聚合（本模块在客户端做）：把请求级明细按 `requestTime` 前 10 位按日求和，
并切出 `by_model` / `by_client` / `by_hour` / `requests` 明细，供看板直接渲染。

依赖：纯标准库。**不 import wb_daily**（避免控制台被签到脚本的重依赖拖住）。
"""

import collections
import datetime
import json
import os
import urllib.error
import urllib.request

HOST_BILLING = "https://www.codebuddy.cn"    # 积分/用量接口
TIMEOUT = 20
USAGE_MAX_DAYS = 31        # 前端 hard limit，照抄
USAGE_DEFAULT_DAYS = 7     # 前端默认窗口

# ⚠️ 唯一真接口：请求级明细（不带 /v2）。见文件头说明。
#    `get-user-daily-usage` 是幻觉接口（任何参数都 invalid params），绝不能用。
PATH_REQUEST_USAGE = "/billing/meter/get-user-request-usage"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) WorkBuddy-Usage/1.0")

# 请求明细最多保留多少条给"明细表"（再多页面也渲染不动）
MAX_REQUEST_ROWS = 300


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


def _call_with_prefix_fallback(path, token, uid, body, timeout=TIMEOUT, caller=None):
    """按 `path` 请求；**若 404 则自动试一次带/不带 /v2 的另一种前缀**。

    2026-09-28 踩过：用量接口不带 /v2，而查余额/签到那条带 /v2 —— 前缀不统一。
    写死一个前缀，服务端一改就全挂（症状是 404）。这里做个自适应：
    首选 `path`，404 时换另一种前缀再试一次；第二次成功就沿用（记住），
    这样即使服务端调整也不会再断。

    caller：可注入的 HTTP 函数 `(method, url, token, uid, body, timeout) -> (status, text)`。
    默认用本模块的 `_call`。传进来是为了让上层（如 wb_daily）的桩函数继续生效。

    返回 (status, text, used_path)。永不抛异常。
    """
    global _PREFIX_PREF
    fn = caller or _call
    cand = [path]
    alt = ("/v2" + path) if not path.startswith("/v2") else path[3:]
    if alt and alt != path:
        cand.append(alt)
    # 之前学到过偏好 → 把它提到最前。
    # ⚠️ 别在 sort 的 key 里调 cand.index()：排序过程中列表会被就地改动，
    #    ValueError: ... is not in list（2026-09-28 踩过）。
    if _PREFIX_PREF in cand:
        cand = [_PREFIX_PREF] + [p for p in cand if p != _PREFIX_PREF]
    last = (0, "", path)
    for i, p in enumerate(cand):
        st, text = fn("POST", HOST_BILLING + p, token, uid, body, timeout)
        if st != 404:                    # 通了（哪怕是 401/500）就是对的路径
            if i > 0:                    # 换了前缀才通 → 记住它
                _PREFIX_PREF = p
            return st, text, p
        last = (st, text, p)
    return last


# 学到过的正确前缀（进程级）。None 表示还不知道。
_PREFIX_PREF = None


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


def _parse_records(text):
    """把响应文本解析成请求级明细列表。返回 (records, total, err)。

    兼容两层 / 三层 data 嵌套。坏响应一律返回 ([], None, err_msg)，绝不抛。
    """
    obj = _jload(text)
    if not isinstance(obj, dict):
        return [], None, "响应不是 JSON"
    # code 非 0（含 invalid params / 401 网关）都算"取不到"
    code = obj.get("code")
    if code not in (0, None):
        msg = obj.get("msg") or obj.get("message") or ""
        return [], None, ("invalid params" if "invalid" in str(msg).lower()
                          else "code=%s msg=%s" % (code, msg))
    d = obj.get("data")
    if not isinstance(d, dict):
        # 有的响应把数组直接放 data 下（单层）
        if isinstance(d, list):
            return d, len(d), None
        return [], None, "data 字段缺失/类型不对"
    arr = d.get("data") if isinstance(d.get("data"), list) else None
    total = _int_or_none(d.get("total"))
    if arr is None and isinstance(d.get("data"), dict):
        # 三层嵌套兜底（理论上本接口不会走到这里）
        inner = d["data"]
        arr = inner.get("data") if isinstance(inner.get("data"), list) else None
        total = total or _int_or_none(inner.get("total"))
    if arr is None:
        arr = []
    return arr, total, None


# ---------------------------------------------------------------- 主查询
def get_daily_usage(token, uid, days=USAGE_DEFAULT_DAYS, timeout=TIMEOUT, caller=None):
    """读真实积分消耗（请求级明细，客户端聚合）。

    返回结构化 dict，取不到就 days=[] 且 ok=False，绝不抛异常。

    返回字段：
      ok/http/err/raw_body/range[起,止]/path
      days      : [{"date","credit"}]           按日求和（升序）
      by_model  : {"model": credit}             按模型（横向条）
      by_client : {"client": credit}            按客户端（WorkBuddy/IDE…）
      by_hour   : {0..23: credit}               0-24 时分布
      requests  : [{"ts","model","client","credit"}]  最多 MAX_REQUEST_ROWS 条（按时间倒序）
      total     : 窗口内请求条数
      sum       : 窗口合计积分
      today     : 今日积分（可能为 0 / None）

    caller：可选，注入的 HTTP 函数（见 _call_with_prefix_fallback）。
    """
    r = {"ok": False, "http": 0, "err": None, "days": [],
         "by_model": {}, "by_client": {}, "by_hour": {}, "requests": [],
         "total": None, "sum": None, "today": None,
         "raw_body": "", "range": None, "path": None}
    try:
        st_t, en_t = usage_range(days)
        r["range"] = [st_t[:10], en_t[:10]]
        body = {"startTime": st_t, "endTime": en_t, "pageNum": 1, "pageSize": 500}
        st, text, used = _call_with_prefix_fallback(
            PATH_REQUEST_USAGE, token, uid, body, timeout=timeout, caller=caller)
        r["http"] = st
        r["raw_body"] = text
        r["path"] = used
        if st != 200:
            r["err"] = "http=%s" % st
            if st == 404:
                r["err"] += "（路径 %s 不存在 —— 检查前缀是否该带/不该带 /v2）" % used
            return r
        recs, total, err = _parse_records(text)
        if err:
            r["err"] = err
            return r
        if not recs:
            # 空数据（可能当天还没落库 / 区间内确实没调用）—— 不算失败，给空壳
            r["total"] = total or 0
            r["sum"] = 0.0
            r["ok"] = True
            return r

        per_day = collections.defaultdict(float)
        per_model = collections.defaultdict(float)
        per_client = collections.defaultdict(float)
        per_hour = collections.defaultdict(float)
        out_reqs = []
        today_s = datetime.date.today().isoformat()
        for it in recs:
            if not isinstance(it, dict):
                continue
            # 真接口用 requestTime；个别形态可能用 date，都兼容
            ts = str(it.get("requestTime") or it.get("request_time")
                     or it.get("date") or "")
            dt = ts[:10]
            cr = _r2(it.get("credit"))
            if cr is None:
                cr = 0.0
            model = str(it.get("model") or it.get("modelName") or "?")
            client = str(it.get("client") or it.get("clientName") or "?")
            if dt:
                per_day[dt] += cr
            per_model[model] += cr
            per_client[client] += cr
            hh = ts[11:13]
            if hh.isdigit():
                per_hour[int(hh)] += cr
            out_reqs.append({"ts": ts, "model": model,
                             "client": client, "credit": cr})

        # 按日升序（方便画走势、和 report 对齐）
        days_out = [{"date": d, "credit": _r2(per_day[d])}
                    for d in sorted(per_day.keys())]
        # 窗口合计 / 今日
        total_credit = round(sum(per_day.values()), 2)
        today_credit = _r2(per_day.get(today_s)) if today_s in per_day else None

        # 明细按时间倒序，截断
        out_reqs.sort(key=lambda x: x.get("ts") or "", reverse=True)
        out_reqs = out_reqs[:MAX_REQUEST_ROWS]

        r["days"] = days_out
        r["by_model"] = {k: _r2(v) for k, v in sorted(
            per_model.items(), key=lambda x: -x[1])}
        r["by_client"] = {k: _r2(v) for k, v in sorted(
            per_client.items(), key=lambda x: -x[1])}
        r["by_hour"] = {h: _r2(per_hour.get(h, 0.0)) for h in range(24)}
        r["requests"] = out_reqs
        r["total"] = _int_or_none(total) if total is not None else len(recs)
        r["sum"] = total_credit
        r["today"] = today_credit
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
def query_accounts(accounts, days=USAGE_DEFAULT_DAYS, timeout=TIMEOUT, workers=4, caller=None):
    """并发查多个账号的用量。

    accounts: [{"name":..., "token":..., "uid":...}, ...]
    caller：可选，注入 HTTP 函数（测试桩用，见 get_daily_usage）。
    返回 [{"name","uid_masked","ok","err","http","sum","today","total",
            "days","by_model","by_hour","requests","range"}, ...]

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
               "total": None, "days": [], "by_model": {}, "by_hour": {},
               "requests": [], "range": None}
        if not token or not uid:
            row["err"] = "缺 token 或 uid"
            out[i] = row
            return
        try:
            du = get_daily_usage(token, uid, days=days, timeout=timeout, caller=caller)
            row["ok"] = bool(du.get("ok"))
            row["err"] = du.get("err")
            row["http"] = du.get("http")
            row["sum"] = du.get("sum")
            row["today"] = du.get("today")
            row["total"] = du.get("total")
            row["days"] = du.get("days") or []
            row["by_model"] = du.get("by_model") or {}
            row["by_hour"] = du.get("by_hour") or {}
            row["requests"] = du.get("requests") or []
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

    # 假 HTTP：返回一段 request-usage 风格的真实结构
    def fake_call(method, url, token, uid, body, timeout=TIMEOUT):
        obj = {"code": 0, "msg": "OK", "data": {"total": 3, "data": [
            {"requestId": "r1", "credit": 1.5, "model": "m-a",
             "client": "WorkBuddy", "requestTime": "2026-09-27 09:00:00"},
            {"requestId": "r2", "credit": 2.5, "model": "m-a",
             "client": "IDE", "requestTime": "2026-09-27 21:30:00"},
            {"requestId": "r3", "credit": 4.0, "model": "m-b",
             "client": "WorkBuddy", "requestTime": "2026-09-28 10:00:00"},
        ]}}
        return 200, json.dumps(obj)

    print("[1] usage_range 边界")
    s, e = usage_range(7)
    ck("7 天窗口右端是今天", e.startswith(datetime.date.today().isoformat()))
    s2, _ = usage_range(999)
    ck("超上限被夹到 31 天",
       s2.startswith((datetime.date.today() - datetime.timedelta(days=30)).isoformat()))
    s3, _ = usage_range(0)
    ck("0 天回落到默认 7 天",
       s3.startswith((datetime.date.today() - datetime.timedelta(days=6)).isoformat()))

    print("[2] get_daily_usage 解析 + 聚合（假接口）")
    r = get_daily_usage("t", "u", days=7, caller=fake_call)
    ck("ok=True", r["ok"] is True)
    ck("窗口合计 8.0", r["sum"] == 8.0)
    ck("请求数 3", r["total"] == 3)
    ck("按日两天", len(r["days"]) == 2)
    ck("09-27 求和 4.0", abs((r["days"][0]["credit"] or 0) - 4.0) < 1e-6)
    ck("by_model m-a=4.0", abs(r["by_model"].get("m-a", 0) - 4.0) < 1e-6)
    ck("by_model m-b=4.0", abs(r["by_model"].get("m-b", 0) - 4.0) < 1e-6)
    ck("by_client WorkBuddy=5.5", abs(r["by_client"].get("WorkBuddy", 0) - 5.5) < 1e-6)
    ck("by_hour 09=1.5", abs(r["by_hour"].get(9, 0) - 1.5) < 1e-6)
    ck("by_hour 21=2.5", abs(r["by_hour"].get(21, 0) - 2.5) < 1e-6)
    ck("requests 截断≤300", len(r["requests"]) <= MAX_REQUEST_ROWS)
    ck("requests 倒序", r["requests"][0]["ts"] >= r["requests"][-1]["ts"])

    print("[3] invalid params / 401 优雅降级")
    def bad_call(method, url, token, uid, body, timeout=TIMEOUT):
        return 200, json.dumps({"code": 10001, "msg": "invalid params"})
    r2 = get_daily_usage("t", "u", days=7, caller=bad_call)
    ck("invalid params → ok=False", r2["ok"] is False)
    ck("invalid params 有 err", bool(r2["err"]))
    ck("invalid params 不抛", isinstance(r2, dict))

    print("[4] fill_gaps 补洞")
    g = fill_gaps([{"date": "2026-09-25", "credit": 1.0},
                   {"date": "2026-09-27", "credit": 3.0}])
    ck("跨 3 天补成 3 条", len(g) == 3)
    ck("中间那天补 0", g[1]["credit"] == 0.0)

    print("[5] merge_rows 多账号求和")
    rows = [{"days": [{"date": "2026-09-27", "credit": 1.5}]},
            {"days": [{"date": "2026-09-27", "credit": 2.5},
                      {"date": "2026-09-28", "credit": 4.0}]}]
    keys, per, total = merge_rows(rows)
    ck("日期升序两个", keys == ["2026-09-27", "2026-09-28"])
    ck("同日求和 4.0", per["2026-09-27"] == 4.0)

    print("[6] query_accounts 缺凭据不炸 + 掩码")
    qs = query_accounts([{"name": "a", "token": "", "uid": ""},
                         {"name": "b", "token": "x", "uid": "1234567890abcdef",
                          "days": 7}],
                        days=7, caller=fake_call)
    ck("返回条数不变", len(qs) == 2)
    ck("缺凭据那行有 err", bool(qs[0]["err"]))
    ck("uid 掩码为前 8 位", qs[1]["uid_masked"] == "12345678…")
    ck("uid_masked 不含完整 uid",
       "1234567890abcdef" not in json.dumps(qs, ensure_ascii=False))
    ck("有数据行聚合到位", qs[1]["sum"] == 8.0)

    print("\n自测：%d 通过 / %d 失败" % (ok, fail))
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    import sys
    sys.exit(_selftest())

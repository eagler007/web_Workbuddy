#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""
WorkBuddy 每日签到 + 派猫猫旅行 一体化脚本（仅标准库，无第三方依赖）
================================================================================

功能
----
1) 多账号签到：对每个账号 POST /v2/billing/meter/daily-checkin
   （签到前/后各查一次余额，算积分变化；空槽位自动跳过）
2) 派猫猫旅行（成长计划 → Buddy → 派猫猫旅行）：每个账号各跑一遍
     GET  /v2/activity/growth/buddy/travel/status   读状态
     POST /v2/activity/growth/buddy/travel/claim    领取"已经到家"的旅行奖励
     POST /v2/activity/growth/buddy/travel/depart   派新的一趟（body: {"location_id": N}）
   顺序固定为「先领后派」，且"领"和"派"是两个独立判断、独立函数：
     - 领：state == arrived 才领
     - 派：领完之后重新读一次状态，只有 state == idle 且 daily_limit_reached == false 才派
   今天已经派过（daily_limit_reached=true）就只记录、不派。

3) 猫猫那部分任何异常都被吞掉并单独记录，绝不影响签到结论（签到成功就算成）。

4) 结果写入本地历史归档 data/wb_history.json，并生成单次运行报告 wb_daily_report.html；
   多账号 × 多日期的聚合报告由 wb_report.py 生成（同一个 HTML 文件名）。

凭据（token 绝不打印、绝不写入报告；只在掩码里出现前 8 位）
--------------------------------------------------------
   多账号（容器里的正规来源）：DATA_DIR/wb_accounts.json（由 Web 界面维护）
   多账号（覆盖用）：环境变量 WB_ACCOUNTS_JSON
   单账号兜底：环境变量 WORKBUDDY_ACCESS_TOKEN + WORKBUDDY_UID

用法
----
   python3 wb_daily.py                     # 跑全部账号（签到 + 派猫），并写入历史归档
   python3 wb_daily.py --dry-run           # 只读，不领取、不派出
   python3 wb_daily.py --no-cat            # 只签到
   python3 wb_daily.py --account 孙        # 只跑名字里含「孙」的账号
   python3 wb_daily.py --location-id 1     # 指定地点（默认咖啡馆 id=1）
   python3 wb_daily.py --report out.html   # 指定报告路径（默认在 DATA_DIR 下）
   python3 wb_daily.py --no-history        # 不写入 wb_history.json

多账号怎么配
------------
  优先级从高到低（第一个能用的就用）：
    1) 环境变量 WB_ACCOUNTS_JSON = {"accounts":[{"name":"本机","token":"…","uid":"…"}, …]}
       ⚠️ 容器里**不要设这个**，否则会遮蔽 Web 界面添加的账号
    2) DATA_DIR/wb_accounts.json（Web 界面维护；含明文 token，别外传）
    3) 单账号兜底：WORKBUDDY_ACCESS_TOKEN / WORKBUDDY_UID
  空槽位（token 或 uid 为空）自动跳过。

数据存在哪（容器里）
--------------------
  DATA_DIR（默认 /data，可用环境变量覆盖）：
    wb_accounts.json   账号（Web 维护）
    wb_history.json    本地归档（永久保留）
    report_single.html 单次运行报告
    wb_daily_raw.log   原始响应（排查用，体积会涨）
  聚合报告（多账号 × 多日期）由 wb_report.py 生成到 report.html。

接口依据
--------
   签到类：/v2/billing/meter/*（青龙脚本 workbuddy_checkin.py 实测）
   派猫类：取自我本机 WorkBuddy 前端包 growthSpace-*.js 里的真实请求常量。
          线上 /v2/activity/*（Bearer 通道）与 /activity/*（Cookie 通道）两条路由
          落到同一个 activity-server；本脚本走 Bearer 通道。
"""

import argparse
import base64
import datetime
import html
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

try:
    # 保留控制台自身编码（Windows 中文环境是 GBK），只把编不出来的字符替换掉，
    # 避免强行改成 utf-8 后在 GBK 控制台里显示成乱码。
    sys.stdout.reconfigure(errors="replace")
except Exception:
    pass

# ---------------------------------------------------------------- 常量
HOST_BILLING = "https://www.codebuddy.cn"    # 积分/签到接口
HOST_ACTIVITY = "https://www.workbuddy.cn"   # 成长体系/派猫接口
RESOURCE_BODY = {"PageNumber": 1, "PageSize": 100, "ProductCode": "p_tcaca",
                 "Status": [0, 3], "OnlyValidPeriod": True}
LOCATION_DEFAULT = 1          # 咖啡馆（travel/config: id=1 code=coffee）
LOCATION_NAMES = {1: "咖啡馆", 2: "商场店铺", 3: "健身房", 4: "古镇客栈"}
UA = "WorkBuddyDaily/1.0"
TIMEOUT = 25
RAW_CAP = 1600                # 单条原始响应打在屏幕上的最大长度（完整版写进 raw log）

# 数据目录：容器里是 /data（挂载卷），本机跑默认用脚本目录下的 data/
_HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("DATA_DIR") or os.path.join(os.path.dirname(_HERE), "data")


def data_path(p):
    """把可能相对的输出路径统一落到 DATA_DIR 下（绝对路径原样用）。"""
    return p if os.path.isabs(p) else os.path.join(DATA_DIR, p)


def mask(s, n=8):
    if not isinstance(s, str) or not s:
        return "-"
    return (s[:n] + "…" + s[-4:]) if len(s) > n + 4 else (s[:n] + "…")


def _env_on(name, default=False):
    """环境开关：未设置用 default；'0'/'false'/'no'/'off'/'' 视为关，其余为开。"""
    v = os.environ.get(name)
    if v is None:
        return default
    return v.strip().lower() not in ("", "0", "false", "no", "off")


# ---------------------------------------------------------------- 凭据
def _creds_from_env():
    t = (os.environ.get("WORKBUDDY_ACCESS_TOKEN") or "").strip()
    u = (os.environ.get("WORKBUDDY_UID") or "").strip()
    if t and u:
        return {"token": t, "uid": u, "src": "<env>", "expiresAt": None, "refreshExpiresAt": None}
    return None



# ---------------------------------------------------------------- 多账号凭据
# 账号在 DATA_DIR/wb_accounts.json（容器里 /data/wb_accounts.json，由 Web 界面维护）
#   {"accounts": [{"name": "本机", "token": "...", "uid": "..."}, ...]}
# ⚠️ 这个文件含明文 token，权限 0600；脚本从不把它读出来打印。
# 优先级：WB_ACCOUNTS_JSON 环境变量 > DATA_DIR/wb_accounts.json > 单账号兜底
ACCOUNTS_FILE = os.path.join(DATA_DIR, "wb_accounts.json")



def _accounts_from_env():
    raw = (os.environ.get("WB_ACCOUNTS_JSON") or "").strip()
    if not raw:
        return None
    try:
        d = json.loads(raw)
    except Exception as e:
        print("[warn] WB_ACCOUNTS_JSON 解析失败：%s" % e)
        return None
    return _norm_accounts(d)


def _norm_accounts(d):
    """兼容 {"accounts":[...]} 和裸 [...] 两种写法；缺 name/uid 的补默认值。"""
    if isinstance(d, dict):
        arr = d.get("accounts") or []
    else:
        arr = d or []
    out = []
    for i, a in enumerate(arr, 1):
        if not isinstance(a, dict):
            continue
        t = (a.get("token") or "").strip()
        u = (a.get("uid") or "").strip()
        if not (t and u):
            continue          # 槽位为空直接跳过，和 QD 模板的 {% if token_N and uid_N %} 一个语义
        out.append({
            "name": (a.get("name") or "").strip() or mask(u),
            "token": t,
            "uid": u,
            "expiresAt": a.get("expiresAt"),
            "refreshExpiresAt": a.get("refreshExpiresAt"),
        })
    return out


def _accounts_from_file():
    if not os.path.isfile(ACCOUNTS_FILE):
        return None
    try:
        with open(ACCOUNTS_FILE, "r", encoding="utf-8") as f:
            d = json.load(f)
    except Exception as e:
        print("[warn] 读 %s 失败：%s" % (ACCOUNTS_FILE, e))
        return None
    accs = _norm_accounts(d)
    return accs or None


def load_accounts():
    """返回账号列表；每一本都保证 token/uid 齐全。"""
    for fn in (_accounts_from_env, _accounts_from_file):
        try:
            accs = fn()
        except Exception as e:
            print("[warn] 账号来源 %s 失败：%s" % (fn.__name__, e))
            continue
        if accs:
            return accs
    c = None
    try:
        c = _creds_from_env()
    except Exception:
        c = None
    if c:
        return [{"name": mask(c["uid"]), "token": c["token"], "uid": c["uid"],
                 "expiresAt": c.get("expiresAt"), "refreshExpiresAt": c.get("refreshExpiresAt")}]
    return []


# ---------------------------------------------------------------- 历史归档
# 与 wb_report.py 共用 DATA_DIR/wb_history.json，谁先跑谁写，按 (task_id, ts) 去重。
# 「打开报告就能看到实时 + 历史」：报告每次刷新都读这里。
HISTORY_FILE = os.path.join(DATA_DIR, "wb_history.json")


def load_history():
    if not os.path.isfile(HISTORY_FILE):
        return {"runs": []}
    try:
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) and "runs" in d else {"runs": []}
    except Exception as e:
        print("[warn] 读历史归档失败（将重建）：%s" % e)
        return {"runs": []}


def save_history(h):
    os.makedirs(os.path.dirname(HISTORY_FILE), exist_ok=True)
    tmp = HISTORY_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(h, f, ensure_ascii=False, indent=2)
    os.replace(tmp, HISTORY_FILE)


def _r2(v):
    """统一 2 位小数；绝对值 < 0.005 归零（消掉 1e-07 这种浮点噪声）。"""
    try:
        v = float(v)
    except (TypeError, ValueError):
        return None
    if abs(v) < 0.005:
        return 0.0
    return round(v, 2)


def make_run(ts_iso, accounts, cat, note, success=True):
    """把一次运行拼成归档记录（结构与 wb_report.py 完全一致）。"""
    a_list = []
    tot_after = 0.0
    tot_before = 0.0
    for a in accounts:
        ck = a["checkin"]
        after = a.get("after")
        before = a.get("before")
        if after is not None:
            tot_after += after
        if before is not None:
            tot_before += before
        gr = a.get("growth") or {}
        sk = gr.get("streak") or {}
        rs = gr.get("redeem") or {}
        a_list.append({
            "name": a["name"],
            "uid": a["uid"],
            "checkin": ck.get("msg") or ("签到成功" if ck.get("ok") else "签到失败"),
            "credit": ck.get("credit"),
            "streak": ck.get("streak"),
            "before": before,
            "after": after,
            "diff": _r2(ck.get("credit")) if ck.get("credit") is not None else None,
            "tc": a.get("tc"),
            "buy": a.get("buy"),
            "rw": a.get("rw"),
            # —— 连登/成长（阶段 A：只读采集）——
            # 连登天数优先用签到响应里的 streak_days（200 成功时才有），
            # 拿不到就退回 growth/streak 接口的值。两者取到的都是同一个数。
            "streak_days": (ck.get("streak") if ck.get("streak") is not None
                            else sk.get("days")),
            "streak_next_tier": sk.get("next_tier"),
            "makeup_cards": sk.get("makeup_cards") if sk.get("ok") else None,
            "makeup_dates": sk.get("makeup_dates") if sk.get("ok") else None,
            "redeem_summary": (rs.get("counts") if rs.get("ok") else None),
            "growth_err": gr.get("err"),
        })
    summary = {
        "before": _r2(tot_before) if tot_before else None,
        "after": _r2(tot_after) if tot_after else None,
        "diff": _r2(tot_after - tot_before) if tot_before else None,
    }
    # 套餐/购买/平台奖励：只统计拿到构成明细的账号
    for key in ("tc", "buy", "rw"):
        vals = [a[key] for a in a_list if a.get(key) is not None]
        summary[key] = _r2(sum(vals)) if vals else None
    return {
        "task_id": note,
        "task_note": note,
        "ts": ts_iso,
        "date": ts_iso[:10],
        "success": bool(success),
        "summary": summary,
        "accounts": a_list,
        "cat": cat,
        "source": "wb_daily.py",
        "raw": "",
    }


def append_history(run):
    """按 (task_id, ts) 去重后追加。返回是否真的写入了。"""
    h = load_history()
    key = (run.get("task_id"), run.get("ts"))
    if any((r.get("task_id"), r.get("ts")) == key for r in h["runs"]):
        return False
    h["runs"].append(run)
    h["runs"].sort(key=lambda r: (r.get("ts") or "", str(r.get("task_id") or "")))
    save_history(h)
    return True


# ---------------------------------------------------------------- HTTP
def call(method, url, token, uid, body=None, timeout=TIMEOUT):
    """返回 (http_status, 原始响应文本)。"""
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


def jload(text):
    try:
        return json.loads(text)
    except Exception:
        return None


def jwt_exp(token):
    """从 JWT 里取 exp（不校验签名，只读 payload）。"""
    try:
        part = token.split(".")[1]
        part += "=" * (-len(part) % 4)
        return json.loads(base64.urlsafe_b64decode(part).decode("utf-8", "replace")).get("exp")
    except Exception:
        return None


def days_left(exp):
    if not exp:
        return None
    try:
        exp = float(exp)
    except (TypeError, ValueError):
        return None
    left = (exp - time.time()) / 86400.0
    return max(0, int(round(left)))


# ---------------------------------------------------------------- 签到
def query_balance(token, uid):
    st, text = call("POST", HOST_BILLING + "/v2/billing/meter/get-user-resource",
                    token, uid, RESOURCE_BODY)
    if st != 200:
        return None, st, text
    obj = jload(text) or {}
    accts = (((obj.get("data") or {}).get("Response") or {}).get("Data") or {}).get("Accounts") or []
    total = 0.0
    for a in accts:
        v = a.get("CycleCapacityRemainPrecise")
        if v is None:
            v = a.get("CycleCapacityRemain")
        try:
            total += float(v)
        except (TypeError, ValueError):
            pass
    return round(total, 2), st, text


def do_checkin(token, uid):
    """签到。ok 表示签到是否成功（这是唯一的"成不成"判据）。"""
    r = {"before": None, "after": None, "credit": None, "streak": None,
         "msg": "", "ok": False, "raw": {}}

    before, st, text = query_balance(token, uid)
    r["raw"]["balance_before"] = {"http": st, "body": text}
    r["before"] = before

    st, text = call("POST", HOST_BILLING + "/v2/billing/meter/daily-checkin", token, uid, {})
    r["raw"]["checkin"] = {"http": st, "body": text}
    obj = jload(text) or {}
    data = obj.get("data") or {}
    code = obj.get("code")
    r["msg"] = obj.get("msg") or obj.get("message") or ""
    if isinstance(data, dict):
        if data.get("credit") is not None:
            try:
                r["credit"] = int(data["credit"])
            except (TypeError, ValueError):
                pass
        if data.get("streak_days") is not None:
            try:
                r["streak"] = int(data["streak_days"])
            except (TypeError, ValueError):
                pass

    # 成功判据：
    #   1) HTTP 200 且业务码 0 —— 正常签到成功
    #   2) 响应里出现"今天已签到" —— 幂等命中，同样算成功。
    #      ⚠️ 实测这时服务端返回的是 **HTTP 400**（body: {"code":10001,"msg":"今天已签到，请明天再来"}），
    #      所以不能只看 HTTP 200，否则把"已签到"误判成失败。
    #   3) 不要单独把 code=10001 当成功——空 body 时网关也会回 10001（msg=EOF）。
    if "已签到" in text or "already" in text.lower():
        r["ok"] = True
        if r["credit"] is None:
            r["credit"] = 0
    elif st == 200 and code in (0, None):
        r["ok"] = True
    else:
        r["ok"] = False

    after, st, text = query_balance(token, uid)
    r["raw"]["balance_after"] = {"http": st, "body": text}
    r["after"] = after

    if r["credit"] is None and r["before"] is not None and r["after"] is not None:
        r["credit"] = round(r["after"] - r["before"], 2)
    return r


# ---------------------------------------------------------------- 派猫猫旅行
def cat_status(token, uid):
    st, text = call("GET", HOST_ACTIVITY + "/v2/activity/growth/buddy/travel/status", token, uid)
    return st, (jload(text) or {}), text


def claim_arrived_reward(token, uid, log):
    """判断一：只有 state == arrived 才领取。返回 (动作描述, 原始响应)。"""
    st, obj, text = cat_status(token, uid)
    log.append(("读状态 travel/status", st, text))
    data = obj.get("data") or {}
    state = data.get("state") or "unknown"
    if st != 200 or obj.get("code") != 0:
        return "状态读取失败，未领取", None
    if state != "arrived":
        return "没有到家的奖励可领（state=%s），不领取" % state, None
    st2, text2 = call("POST", HOST_ACTIVITY + "/v2/activity/growth/buddy/travel/claim",
                      token, uid, {})
    log.append(("领取到家奖励 travel/claim", st2, text2))
    obj2 = jload(text2) or {}
    if st2 == 200 and obj2.get("code") == 0:
        credit = data.get("reward_credit")
        return "已领取到家奖励%s" % ("，+%s 积分" % credit if credit else ""), text2
    return "领取失败（http=%s code=%s msg=%s）" % (st2, obj2.get("code"), obj2.get("msg")), text2


def depart_new_trip(token, uid, location_id, log, dry=False):
    """判断二：领完之后**重新读一次状态**，只有 idle 且未达今日上限才派。"""
    st, obj, text = cat_status(token, uid)
    log.append(("读状态 travel/status", st, text))
    data = obj.get("data") or {}
    state = data.get("state") or "unknown"
    if st != 200 or obj.get("code") != 0:
        return "状态读取失败，未派出"
    if state == "traveling":
        return "猫还在路上，不重复派"
    if state == "arrived":
        return "还有未领取的奖励，先领再派，本轮不派"
    if data.get("daily_limit_reached"):
        return "今天已经派过了，跳过"
    if dry:
        return "[dry-run] 本可派出，已跳过"
    st2, text2 = call("POST", HOST_ACTIVITY + "/v2/activity/growth/buddy/travel/depart",
                      token, uid, {"location_id": location_id})
    log.append(("派出新一趟 travel/depart", st2, text2))
    obj2 = jload(text2) or {}
    if st2 == 200 and obj2.get("code") == 0:
        return "已派出（%s）" % LOCATION_NAMES.get(location_id, "地点%d" % location_id)
    return "派出失败（http=%s code=%s msg=%s）" % (st2, obj2.get("code"), obj2.get("msg"))


def cat_flow(token, uid, location_id, log, dry=False):
    """猫猫整条链路。任何异常都吞掉，返回 ok=False，绝不影响签到结论。"""
    r = {"ok": True, "err": None, "claim": None, "depart": None, "final": None}
    try:
        r["claim"] = claim_arrived_reward(token, uid, log)[0]
        r["depart"] = depart_new_trip(token, uid, location_id, log, dry=dry)
        st, obj, text = cat_status(token, uid)
        log.append(("读状态 travel/status", st, text))
        d = (obj or {}).get("data") or {}
        loc = d.get("location")
        r["final"] = {
            "state": d.get("state"),
            "location": loc.get("name") if isinstance(loc, dict) else None,
            "depart_at": d.get("depart_at"),
            "arrive_at": d.get("arrive_at"),
            "server_now": d.get("server_now"),
            "reward_credit": d.get("reward_credit"),
            "daily_limit_reached": d.get("daily_limit_reached"),
            "duration_hours": d.get("duration_hours"),
        }
    except Exception as e:
        r["ok"] = False
        r["err"] = "%s: %s" % (type(e).__name__, e)
    return r


# ---------------------------------------------------------------- 连续登录（成长中心）
# 接口来源：本机 WorkBuddy 前端包 growthSpace-CCYzF8bt.js 里写死的 API 层，逐字对应：
#   GET  /v2/activity/growth/streak           连续登录状态
#   GET  /v2/activity/growth/redeem/summary   连登奖励兑换汇总（本月各档已兑次数）
#   POST /v2/activity/growth/redeem           body {"tier": <"starter"|"advanced"|"legendary">,
#                                                   "client_token": <客户端幂等键>}
#   POST /v2/activity/growth/makeup-cards/use body {"target_date": <"YYYY-MM-DD">}
#
# 业务规则（页面文案原文）：
#   - 档位：入门档 7 天 / 进阶档 14 天 / 巅峰档 28 天
#   - 「连登天数可多档累计。每档每月限兑 1 次，连登天数每月清零并重新计算。」
#   - 「当前连登天数计算方式为当前日期前连续登录且使用 WorkBuddy 的天数，
#      奖励领取以当月最大连续登录且使用 WorkBuddy 的天数为准。」
#   - 「补登卡为永久持有且上限 4 张，仅可补救当月断登的天数。」
#
# 阶段说明：当前（阶段 A）只做**只读**读取与展示，不发起任何写入。
STREAK_TIERS = [("starter", "入门档", "7d"),
                ("advanced", "进阶档", "14d"),
                ("legendary", "巅峰档", "28d")]


def get_streak(token, uid):
    """读连续登录状态。返回结构化 dict（取不到就全 None，绝不抛异常）。"""
    r = {"ok": False, "http": 0, "err": None, "days": None, "next_tier": None,
         "makeup_dates": [], "makeup_cards": {"balance": None, "max": None},
         "redeem_status": {}, "raw_body": ""}
    try:
        st, text = call("GET", HOST_ACTIVITY + "/v2/activity/growth/streak", token, uid)
        r["http"] = st
        r["raw_body"] = text
        obj = jload(text)
        if not isinstance(obj, dict):
            r["err"] = "响应不是 JSON（http=%s）" % st
            return r
        if st != 200 or obj.get("code") not in (0, None):
            r["err"] = "http=%s code=%s msg=%s" % (st, obj.get("code"), obj.get("msg"))
            return r
        data = obj.get("data")
        if not isinstance(data, dict):
            r["err"] = "data 不是对象"
            return r
        # 前端读的是 data.streak.* 与 data.makeup_cards.*
        sk = data.get("streak") if isinstance(data.get("streak"), dict) else data
        if isinstance(sk, dict):
            if sk.get("days") is not None:
                try:
                    r["days"] = int(sk["days"])
                except (TypeError, ValueError):
                    pass
            nt = sk.get("next_tier")
            r["next_tier"] = nt if isinstance(nt, str) else None
            md = sk.get("makeup_dates")
            r["makeup_dates"] = md if isinstance(md, list) else []
        mc = data.get("makeup_cards")
        if isinstance(mc, dict):
            for key in ("balance", "max"):
                if mc.get(key) is not None:
                    try:
                        r["makeup_cards"][key] = int(mc[key])
                    except (TypeError, ValueError):
                        pass
        rs = data.get("redemption_status")
        if isinstance(rs, dict):
            tiers = rs.get("tiers")
            if isinstance(tiers, list):
                r["redeem_status"] = {"tiers": tiers}
            elif isinstance(tiers, dict):
                r["redeem_status"] = {"tiers": tiers}
        r["ok"] = True
    except Exception as e:
        r["err"] = "%s: %s" % (type(e).__name__, e)
    return r


def get_redeem_summary(token, uid):
    """读连登奖励兑换汇总（本月每档已兑几次）。只读。"""
    r = {"ok": False, "http": 0, "err": None, "counts": {}, "raw_body": ""}
    try:
        st, text = call("GET", HOST_ACTIVITY + "/v2/activity/growth/redeem/summary", token, uid)
        r["http"] = st
        r["raw_body"] = text
        obj = jload(text)
        if not isinstance(obj, dict):
            r["err"] = "响应不是 JSON（http=%s）" % st
            return r
        if st != 200 or obj.get("code") not in (0, None):
            r["err"] = "http=%s code=%s msg=%s" % (st, obj.get("code"), obj.get("msg"))
            return r
        data = obj.get("data") if isinstance(obj.get("data"), dict) else {}
        # 前端字段：starter_count / advanced_count / legendary_count
        for key, _name, _tier in STREAK_TIERS:
            v = data.get(key + "_count")
            if v is not None:
                try:
                    r["counts"][key] = int(v)
                except (TypeError, ValueError):
                    pass
        r["ok"] = True
    except Exception as e:
        r["err"] = "%s: %s" % (type(e).__name__, e)
    return r


def growth_flow(token, uid, log, enabled=True):
    """连登/成长只读采集。任何异常都吞掉，绝不影响签到结论（与派猫同样的隔离纪律）。

    阶段 A 只读：GET status + GET redeem/summary，不发起任何写操作。
    """
    r = {"ok": True, "err": None, "enabled": bool(enabled),
         "streak": None, "redeem": None}
    if not enabled:
        r["err"] = "已按 WB_GROWTH=0 跳过"
        return r
    try:
        sk = get_streak(token, uid)
        log.append(("读连续登录 growth/streak", sk.get("http"), sk.get("raw_body") or ""))
        r["streak"] = sk
        rs = get_redeem_summary(token, uid)
        log.append(("读兑换汇总 growth/redeem/summary", rs.get("http"), rs.get("raw_body") or ""))
        r["redeem"] = rs
        if not sk.get("ok") and not rs.get("ok"):
            r["ok"] = False
            r["err"] = "streak=%s / redeem=%s" % (sk.get("err"), rs.get("err"))
    except Exception as e:
        r["ok"] = False
        r["err"] = "%s: %s" % (type(e).__name__, e)
    return r


# ---------------------------------------------------------------- 报告
def fmt_ts(ts):
    try:
        ts = int(ts)
        if ts <= 0:
            return "-"
        return datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return "-"


TPL = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>WorkBuddy 每日签到</title>
<style>
 *{box-sizing:border-box}
 body{margin:0;background:#f2f3f7;color:#1f2329;font:15px/1.6 -apple-system,"PingFang SC","Microsoft YaHei",sans-serif}
 .wrap{max-width:720px;margin:0 auto;padding:20px 16px 40px}
 h1{font-size:26px;margin:6px 0 2px}
 .meta{color:#8b8f99;font-size:13px;margin-bottom:14px}
 .hero{background:linear-gradient(135deg,#6a5cf5,#8f7bff);border-radius:16px;color:#fff;padding:22px 20px;margin-bottom:14px}
 .hero h2{margin:0;font-size:20px;font-weight:600}
 .hero .t{opacity:.9;font-size:13px;margin-top:8px;white-space:pre-line}
 .cards{display:flex;gap:10px;margin-bottom:14px;flex-wrap:wrap}
 .card{flex:1;min-width:130px;background:#fff;border-radius:14px;padding:16px 8px;text-align:center}
 .card .v{font-size:24px;font-weight:700}
 .card .k{color:#8b8f99;font-size:12px;margin-top:4px}
 .panel{background:#fff;border-radius:14px;margin-bottom:14px;overflow:hidden}
 .panel>h3{margin:0;padding:15px 16px 6px;font-size:16px}
 table{width:100%;border-collapse:collapse;font-size:14px}
 th,td{padding:12px 10px;text-align:center;border-bottom:1px solid #f0f1f4}
 th{color:#8b8f99;font-weight:500;font-size:13px}
 td.acc{color:#4b5058;font-family:ui-monospace,Consolas,monospace;font-size:12px}
 td.ok{color:#17a673;font-weight:600}
 td.fail{color:#e34d59;font-weight:600}
 td.credit{color:#17a673;font-weight:600}
 td.credit.zero{color:#8b8f99}
 .foot{padding:12px 10px;color:#8b8f99;font-size:12.5px;display:flex;justify-content:space-between;flex-wrap:wrap;gap:6px}
 .sec{background:#fff;border-radius:14px;padding:16px;margin-bottom:14px}
 .sec h3{margin:0 0 12px;font-size:16px}
 .kv{display:flex;justify-content:space-between;gap:12px;padding:8px 0;border-bottom:1px dashed #f0f1f4;font-size:14px}
 .kv:last-child{border-bottom:0}
 .kv span{color:#8b8f99;white-space:nowrap}
 .kv b{font-weight:600;text-align:right}
 .err{margin-top:10px;background:#fff5f5;color:#c0392b;border-radius:10px;padding:10px 12px;font-size:13px}
 .note{color:#9aa0aa;font-size:12px;margin-top:14px;line-height:1.8}
 .acct{border:1px solid #eef0f4;border-radius:12px;padding:14px;margin-bottom:10px}
 .acct h4{margin:0 0 4px;font-size:15px}
 .acct .uid{color:#9aa0aa;font-size:11.5px;font-family:ui-monospace,Consolas,monospace;word-break:break-all;margin-bottom:8px}
 .tag{display:inline-block;padding:1px 7px;border-radius:8px;font-size:11.5px;margin-left:6px}
</style></head>
<body><div class="wrap">
 <h1>WorkBuddy 每日签到</h1>
 <div class="meta">__TS__</div>
 <div class="hero"><h2>WorkBuddy 每日签到</h2><div class="t">__VERDICT__</div></div>
 <div class="cards">
   <div class="card"><div class="v">__TODAY__</div><div class="k">今日获取</div></div>
   <div class="card"><div class="v">__BALANCE__</div><div class="k">账户总余额</div></div>
   <div class="card"><div class="v">__N__</div><div class="k">账号数</div></div>
   <div class="card"><div class="v">__OKN__</div><div class="k">签到成功</div></div>
 </div>
 <div class="panel">
   <table><thead><tr><th>账号</th><th>状态</th><th>本次积分</th><th>连续</th><th>余额</th></tr></thead>
   <tbody>__ROWS__</tbody></table>
   <div class="foot"><span>共 __N__ 个账号</span><span>耗时 __COST__ 秒</span><span>__HISTHINT__</span></div>
 </div>
 <div class="sec">
   <h3>各账号明细</h3>
   __ACCT_CARDS__
 </div>
 <div class="sec">
   <h3>派猫猫旅行（先领后派）</h3>
   __CAT_ROWS__
   __CAT_ERR__
 </div>
 <div class="note">签到结论：__VERDICT__。猫猫部分失败不影响签到结论。<br>
 完整历史（多账号 × 多日期）请看 <code>wb_daily_report.html</code>，由 <code>wb_report.py</code> 生成；本页数据也已写入 <code>data/wb_history.json</code>。</div>
</div></body></html>
"""


def render_html(ctx):
    """生成单次运行的报告页：头部 + 指标卡 + 账号表 + 账号明细 + 猫猫区块。"""
    rows = []
    for a in ctx["accounts"]:
        ck = a["checkin"]
        ok = ck["ok"]
        credit = _r2(ck.get("credit"))
        rows.append(
            "<tr>"
            "<td class='acc' title='{uid}'>{acc}</td>"
            "<td class='{cls}'>{status}</td>"
            "<td class='{ccls}'>{credit}</td>"
            "<td>{streak}</td>"
            "<td>{bal}</td>"
            "</tr>".format(
                uid=html.escape(a["uid"]),
                acc=html.escape(a["name"]),
                cls="ok" if ok else "fail",
                status="已领取" if ok else "失败",
                ccls="credit" if credit else "credit zero",
                credit=("+%g" % credit) if credit else "+0",
                streak=("%s天" % ck["streak"]) if ck.get("streak") is not None else "—",
                bal=("%.2f" % a["after"]) if a.get("after") is not None else "—"))

    cards = []
    for a in ctx["accounts"]:
        ck = a["checkin"]
        ok = ck["ok"]
        rows_kv = [
            ("uid", a["uid"]),
            ("RT", ("%s 天" % a["rt_days"]) if a.get("rt_days") is not None else "—"),
            ("AT", ("%s 天" % a.get("at_days")) if a.get("at_days") is not None else "—"),
            ("签到结果", ck.get("msg") or ("成功" if ok else "失败")),
            ("本次积分", ("+%g" % _r2(ck.get("credit"))) if ck.get("credit") else "+0"),
            ("连续天数", ("%s 天" % ck["streak"]) if ck.get("streak") is not None else "—"),
            ("签到前 → 签到后", "%s → %s" % (a.get("before"), a.get("after"))),
            ("套餐 / 购买 / 平台奖励",
             "%s / %s / %s" % (a.get("tc"), a.get("buy"), a.get("rw"))),
        ]
        cards.append(
            '<div class="acct"><h4>%s<span class="tag" style="background:%s">%s</span></h4>'
            '<div class="uid">%s</div>%s</div>' % (
                html.escape(a["name"]),
                "#e8f7f0" if ok else "#fdecee",
                "已签到" if ok else "未成功",
                html.escape(a["uid"]),
                "".join('<div class="kv"><span>%s</span><b>%s</b></div>'
                        % (html.escape(k), html.escape(str(v))) for k, v in rows_kv)))

    cat = ctx["cat"] or {}
    f = (cat.get("final") or {}) if cat.get("ok") else {}
    state_map = {"idle": "空闲", "traveling": "旅行中", "arrived": "已到家"}
    cat_lines = [
        ("领取（先做）", cat.get("claim") or "-"),
        ("派出（后做）", cat.get("depart") or "-"),
        ("当前状态", state_map.get(f.get("state"), f.get("state") or "-")),
        ("地点", f.get("location") or "-"),
        ("出发 → 到家", "%s → %s" % (fmt_ts(f.get("depart_at")), fmt_ts(f.get("arrive_at")))
         if f.get("arrive_at") else "-"),
        ("今日已派", "是" if f.get("daily_limit_reached") else "否"),
    ]
    cat_rows = "".join(
        "<div class='kv'><span>%s</span><b>%s</b></div>" % (html.escape(k), html.escape(str(v)))
        for k, v in cat_lines)
    cat_err = ("<div class='err'>猫猫部分异常（不影响签到结论）：%s</div>" % html.escape(cat["err"])) \
        if not cat.get("ok") else ""

    repl = {
        "__TS__": ctx["ts_text"],
        "__TODAY__": ("+%g" % ctx["total_credit"]) if ctx["total_credit"] else "+0",
        "__BALANCE__": ctx["balance_text"],
        "__N__": str(len(ctx["accounts"])),
        "__OKN__": str(ctx["ok_count"]),
        "__ROWS__": "".join(rows),
        "__ACCT_CARDS__": "".join(cards),
        "__CAT_ROWS__": cat_rows,
        "__CAT_ERR__": cat_err,
        "__COST__": str(ctx["cost"]),
        "__HISTHINT__": ctx["hist_hint"],
        "__VERDICT__": html.escape(ctx["verdict"]),
    }
    out = TPL
    for k, v in repl.items():
        out = out.replace(k, v)
    return out


# ---------------------------------------------------------------- main
def do_account(acc, args, idx, total):
    """把一个账号的签到 + 派猫做完，返回结构化结果。"""
    token, uid = acc["token"], acc["uid"]
    print("\n===== [%d/%d] %s  uid=%s  token=%s ====="
          % (idx, total, acc["name"], mask(uid), mask(token)))

    at_days = days_left(acc.get("expiresAt") or jwt_exp(token))
    rt_days = days_left(acc.get("refreshExpiresAt"))

    ck = do_checkin(token, uid)
    print("签到: %s%s" % ("成功" if ck["ok"] else "失败",
                        ("（%s）" % ck["msg"]) if ck["msg"] else ""))
    print("积分: 前 %s -> 后 %s (本次 %s)" % (ck["before"], ck["after"], ck["credit"]))

    # 余额构成：套餐 / 购买 / 平台奖励
    # ⚠️ _split_balance 内部自己会 jload，这里必须传**原始文本**，不能传已解析的 dict
    tc = buy = rw = None
    raw_body = ((ck["raw"].get("balance_before") or {}).get("body") or "")
    if raw_body:
        tc, buy, rw = _split_balance(raw_body)
    print("构成: 套餐 %s + 购买 %s + 平台奖励 %s" % (tc, buy, rw))

    log = []
    cat = {"ok": True, "err": None, "claim": None, "depart": None, "final": None}
    if not args.no_cat:
        cat = cat_flow(token, uid, args.location_id, log, dry=args.dry_run)
        print("猫猫: 领 = %s" % cat.get("claim"))
        print("      派 = %s" % cat.get("depart"))
        if not cat.get("ok"):
            print("猫猫异常（不影响签到结论）: %s" % cat.get("err"))

    # 连登/成长：放在最后，纯只读。即使整段出错也绝不影响签到与派猫的既有结论。
    growth = growth_flow(token, uid, log, enabled=args.growth)
    if args.growth:
        sk = growth.get("streak") or {}
        rs = growth.get("redeem") or {}
        print("连登: 当前 %s 天，下一档 %s，补登卡 %s/%s"
              % (sk.get("days"), sk.get("next_tier") or "已达最高档",
                 sk.get("makeup_cards", {}).get("balance"),
                 sk.get("makeup_cards", {}).get("max")))
        print("      本月已兑: %s" % (rs.get("counts") or "—"))
        if not growth.get("ok"):
            print("连登读取异常（不影响签到结论）: %s" % growth.get("err"))

    return {
        "name": acc["name"],
        "uid": uid,
        "rt_days": rt_days,
        "at_days": at_days,
        "checkin": ck,
        "before": ck["before"],
        "after": ck["after"],
        "tc": tc,
        "buy": buy,
        "rw": rw,
        "cat": cat,
        "growth": growth,
        "log": log,
    }


def _split_balance(body):
    """从 get-user-resource 响应里拆出「套餐 / 购买 / 平台奖励」三部分结余。

    口径（实测 2026-09-28，Accounts[] 共 36 条，按 PackageCode 分类）：
      - TCACA_code_007_*  PackageName 含「运营裂变」→ 平台奖励积分
      - TCACA_code_*      PackageName 含「拉新权益」→ 购买积分
      - 其余（含正式套餐/赠送额度）          → 套餐积分
    合计一定等于 CycleCapacityRemainPrecise 的全量求和，分类只是把同一份数拆开。
    取不到就返回 None，不猜。
    """
    obj = jload(body) or {}
    accts = (((obj.get("data") or {}).get("Response") or {}).get("Data") or {}).get("Accounts") or []
    if not accts:
        return None, None, None
    tc = buy = rw = 0.0
    for a in accts:
        v = a.get("CycleCapacityRemainPrecise")
        if v is None:
            v = a.get("CycleCapacityRemain")
        try:
            v = float(v)
        except (TypeError, ValueError):
            continue
        code = (a.get("PackageCode") or "")
        name = (a.get("PackageName") or "")
        # 「平台奖励」类
        if ("运营裂变" in name) or ("_007_" in code) or ("裂变" in name):
            rw += v
        # 「购买」类（拉新权益包 / 付费购买）
        elif ("拉新权益" in name) or ("拉新" in name) or ("权益包" in name):
            buy += v
        else:
            tc += v
    return _r2(tc), _r2(buy), _r2(rw)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="只读，不领取/不派出")
    ap.add_argument("--no-cat", action="store_true", help="跳过派猫")
    ap.add_argument("--no-growth", dest="growth", action="store_false",
                    default=_env_on("WB_GROWTH", True),
                    help="跳过连登/成长采集（等价于 WB_GROWTH=0）")
    ap.add_argument("--location-id", type=int, default=LOCATION_DEFAULT)
    ap.add_argument("--report", default="report_single.html",
                    help="单次运行报告路径（默认在 DATA_DIR 下）")
    ap.add_argument("--raw-log", default="wb_daily_raw.log",
                    help="原始响应落盘路径（默认在 DATA_DIR 下）")
    ap.add_argument("--no-history", action="store_true", help="不写入 wb_history.json")
    ap.add_argument("--account", default="", help="只跑指定账号（按名字/uid 前缀匹配）")
    args = ap.parse_args()

    t0 = time.time()
    accounts = load_accounts()
    if args.account:
        k = args.account.lower()
        accounts = [a for a in accounts
                    if k in (a["name"] or "").lower() or (a["uid"] or "").lower().startswith(k)]
    if not accounts:
        print("没找到任何可用账号。按以下任一方式提供：\n"
              "  1) Web 界面添加（推荐，写到 %s）\n"
              "  2) 环境变量 WB_ACCOUNTS_JSON='{\"accounts\":[{\"name\":\"本机\",\"token\":\"...\",\"uid\":\"...\"}]}'\n"
              "  3) 环境变量 WORKBUDDY_ACCESS_TOKEN / WORKBUDDY_UID（单账号）" % ACCOUNTS_FILE)
        return 2

    print("共 %d 个账号：%s" % (len(accounts), "、".join(a["name"] for a in accounts)))

    results = []
    full_log = []
    for i, acc in enumerate(accounts, 1):
        try:
            r = do_account(acc, args, i, len(accounts))
        except Exception as e:
            print("[warn] 账号 %s 处理异常：%s: %s" % (acc["name"], type(e).__name__, e))
            r = {"name": acc["name"], "uid": acc["uid"], "rt_days": None, "at_days": None,
                 "checkin": {"ok": False, "msg": "%s: %s" % (type(e).__name__, e),
                             "credit": None, "streak": None, "before": None, "after": None,
                             "raw": {}},
                 "before": None, "after": None, "tc": None, "buy": None, "rw": None,
                 "cat": {"ok": False, "err": str(e), "claim": None, "depart": None, "final": None},
                 "log": []}
        results.append(r)
        for k in ("balance_before", "checkin", "balance_after"):
            v = r["checkin"]["raw"].get(k)
            if v:
                full_log.append("\n--- [%s][签到] %s  HTTP %s -----\n%s"
                                % (r["name"], k, v["http"], v["body"]))
        for nm, st, body in r["log"]:
            full_log.append("\n--- [%s][猫猫] %s  HTTP %s -----\n%s" % (r["name"], nm, st, body))

    with open(data_path(args.raw_log), "w", encoding="utf-8") as f:
        f.write("\n".join(full_log))

    # ---- 汇总
    ok_count = sum(1 for r in results if r["checkin"]["ok"])
    total_credit = sum((_r2(r["checkin"].get("credit")) or 0) for r in results)
    total_after = sum(r["after"] for r in results if r.get("after") is not None)
    verdict = "签到成功 %d/%d 个账号，本次共 +%g 积分" % (
        ok_count, len(results), total_credit)

    cost = round(time.time() - t0, 1)
    ts_now = datetime.datetime.now()
    ts_text = ts_now.strftime("%Y-%m-%d %H:%M:%S")

    # ---- 写入本地历史归档（与 wb_report.py 共用一份）
    hist_hint = "未写历史"
    if not args.no_history:
        cat_combined = {"ok": True, "err": None, "claim": None, "depart": None, "final": None}
        try:
            run = make_run(ts_text, results, cat_combined, note=Qd_note(), success=ok_count > 0)
            if append_history(run):
                n = len(load_history()["runs"])
                hist_hint = "已归档（共 %d 条）" % n
            else:
                hist_hint = "本次已存在，未重复归档"
        except Exception as e:
            print("[warn] 写历史归档失败（不影响签到结论）：%s" % e)
            hist_hint = "归档失败"

    ctx = {
        "ts_text": ts_text,
        "accounts": results,
        "ok_count": ok_count,
        "total_credit": total_credit,
        "balance_text": ("%.2f" % total_after) if results else "—",
        "cost": cost,
        "hist_hint": hist_hint,
        "cat": results[0]["cat"] if len(results) == 1 else _cat_combined(results),
        "verdict": verdict,
    }
    out = data_path(args.report)
    with open(out, "w", encoding="utf-8") as f:
        f.write(render_html(ctx))

    print("\n===== 汇总 =====")
    print(verdict)
    print("总余额: %s" % ctx["balance_text"])
    print("报告: %s" % out)
    print("原始返回完整版: %s" % data_path(args.raw_log))
    print("历史归档: %s（%s）" % (HISTORY_FILE, hist_hint))
    print("多账号历史报告: 跑 wb_report.py --no-sync 生成")
    print("耗时: %s 秒" % cost)

    # 退出码：只要有账号签到成功就算成（"签到成功就算成"）
    return 0 if ok_count > 0 else 1


def Qd_note():
    """给归档记录一个稳定的 task_id 标签（容器里默认 docker，手动单跑可设 manual）。"""
    return os.environ.get("WB_TASK_NOTE", "docker")


def _cat_combined(results):
    """多账号时把各账号的猫猫结论合起来展示。"""
    lines = []
    ok_all = True
    for r in results:
        c = r["cat"] or {}
        if not c.get("ok"):
            ok_all = False
        lines.append("%s：领 = %s；派 = %s" % (
            r["name"], c.get("claim") or "-", c.get("depart") or "-"))
    return {"ok": ok_all, "err": None, "claim": "；".join(lines), "depart": None, "final": None}


if __name__ == "__main__":
    sys.exit(main())

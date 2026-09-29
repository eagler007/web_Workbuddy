#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""
通知推送模块（Server 酱 / 通用 Webhook）
================================================================================

设计纪律（跟派猫、连登、用量一个套路）
--------------------------------------
1. **纯只读副作用**：推送失败绝不影响签到结论，也绝不改退出码。
2. **零第三方依赖**：只用标准库 urllib。
3. **绝不打印 / 落盘 key**：key 只出现在请求 URL 里，日志里掩码。
4. **可离线单测**：`render_text()` 是纯函数，(summary dict) -> 文本，
   不碰网络，测试直接断言字符串。

支持三通道（互不排斥，配了就发）
--------------------------------
  Server 酱 Turbo  https://sctapi.ftqq.com/<SENDKEY>.send     (表单: title / desp)
  Server 酱³(SC3)   https://<uid>.push.ft07.com/send/<KEY>.send  (同上)
  通用 Webhook     用户自定义 URL，POST JSON（钉钉/飞书/企业微信/Bark 等）
                   用 WB_NOTIFY_WEBHOOK_TEMPLATE 控制 JSON 形状。

怎么用
------
  from notify import build_summary, notify, render_text
  summ = build_summary(results, ok_count, total_credit, total_after, cost, args)
  txt  = render_text(summ)                       # 纯函数
  notify(summ, log)                              # 发（内部自动 try/except）

环境变量
--------
  WB_NOTIFY=1                     总开关（默认开；没配任何通道时自动静默跳过）
  WB_NOTIFY_ON=always|fail|change 推送时机（默认 always）
  WB_SENDKEY=...                  Server 酱 Turbo 的 SENDKEY（SCT 开头）
  WB_SENDKEY3=...                 Server 酱³ 的完整 sendkey 形如 <uid>t<key>
  WB_NOTIFY_TITLE=...             自定义标题前缀
  WB_NOTIFY_WEBHOOK=...           通用 Webhook 地址
  WB_NOTIFY_WEBHOOK_TEMPLATE=...  Webhook JSON 模板（用 {title} / {desp} 占位）
  WB_NOTIFY_TIMEOUT=8             超时秒数
"""

import html
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

try:
    sys.stdout.reconfigure(errors="replace")
except Exception:
    pass

UA = "WorkBuddyDaily/1.0"
DEFAULT_TIMEOUT = 8

# Server 酱官方两个域（Turbo 直连 sctapi；³ 走个人子域）
SCTAPI = "https://sctapi.ftqq.com/%s.send"
SC3 = "https://%s.push.ft07.com/send/%s.send"   # 只有 ³ 用；这里留作文档


def _env(name, default=""):
    v = os.environ.get(name)
    return default if v is None else v.strip()


def _on(name, default=False):
    v = os.environ.get(name)
    if v is None:
        return default
    return v.strip().lower() not in ("", "0", "false", "no", "off")


def mask(s, keep=4):
    """掩码：只留头尾，够辨认不泄露。"""
    if not isinstance(s, str) or not s:
        return "-"
    if len(s) <= keep * 2:
        return s[0] + "*" * (len(s) - 1)
    return s[:keep] + "*" * (len(s) - keep * 2) + s[-keep:]


# ---------------------------------------------------------------- 配置
class Config:
    def __init__(self):
        self.enabled = _on("WB_NOTIFY", True)
        self.on = (_env("WB_NOTIFY_ON", "always") or "always").lower()
        self.sendkey = _env("WB_SENDKEY")          # Turbo: SCT xxxx
        self.sendkey3 = _env("WB_SENDKEY3")        # ³: 形如 xxxxxxtxxxxx
        self.title_prefix = _env("WB_NOTIFY_TITLE", "WorkBuddy 签到")
        self.webhook = _env("WB_NOTIFY_WEBHOOK")
        self.webhook_tpl = _env("WB_NOTIFY_WEBHOOK_TEMPLATE")
        try:
            self.timeout = max(2, min(60, int(_env("WB_NOTIFY_TIMEOUT", str(DEFAULT_TIMEOUT)) or 8)))
        except Exception:
            self.timeout = DEFAULT_TIMEOUT

    def channels(self):
        """返回可用通道名列表（用于日志与「没配就静默」判断）。"""
        out = []
        if self.sendkey:
            out.append("serverchan")
        if self.sendkey3:
            out.append("serverchan3")
        if self.webhook:
            out.append("webhook")
        return out

    def describe(self):
        """给日志/页面看的安全描述（key 一律掩码）。"""
        bits = []
        if self.sendkey:
            bits.append("Server酱 Turbo(%s)" % mask(self.sendkey))
        if self.sendkey3:
            bits.append("Server酱³(%s)" % mask(self.sendkey3))
        if self.webhook:
            try:
                host = urllib.parse.urlparse(self.webhook).netloc or "?"
            except Exception:
                host = "?"
            bits.append("Webhook(%s)" % host)
        return "、".join(bits) if bits else "未配置任何通道"


# ---------------------------------------------------------------- 摘要（纯函数）
def at_warn_days():
    """Token 到期提醒阈值（天），WB_TOKEN_EXPIRE_WARN_DAYS 可调，默认 7。"""
    try:
        return max(0, min(60, int(_env("WB_TOKEN_EXPIRE_WARN_DAYS", "7") or 7)))
    except Exception:
        return 7


def build_summary(results, ok_count, total_credit, total_after, cost, args=None):
    """把一次运行的结果压成一个扁平 dict。

    这是 notify 与 wb_daily 之间的**唯一**契约；不含 token，只含 uid 掩码。
    纯数据拼装，不碰网络。
    """
    accs = []
    for r in results or []:
        ck = r.get("checkin") or {}
        gr = r.get("growth") or {}
        sk = gr.get("streak") or {}
        act = gr.get("redeem_action") or {}
        usg = r.get("usage") or {}
        du = usg.get("daily") or {}
        cat = r.get("cat") or {}

        # 兑换人话
        redeem = None
        if act:
            if act.get("skipped"):
                redeem = "跳过（%s）" % act["skipped"]
            elif act.get("results"):
                redeem = "；".join(
                    "%s%s%s" % (x.get("name"), "已兑" if x.get("ok") else "失败",
                                ("+%s" % x["credit"]) if x.get("credit") else "")
                    for x in act["results"])
            elif act.get("notes"):
                redeem = "；".join(act["notes"])

        accs.append({
            "name": r.get("name") or "-",
            "uid": (r.get("uid") or "")[:8],
            "ok": bool(ck.get("ok")),
            "msg": ck.get("msg") or ("签到成功" if ck.get("ok") else "签到失败"),
            "credit": ck.get("credit"),
            "streak": ck.get("streak") if ck.get("streak") is not None else sk.get("days"),
            "after": r.get("after"),
            "cat_claim": cat.get("claim"),
            "cat_depart": cat.get("depart"),
            "redeem": redeem,
            "usage_today": du.get("today") if du.get("ok") else None,
            "at_left": r.get("at_days"),
        })

    tiers = []
    for r in results or []:
        gr = r.get("growth") or {}
        rs = gr.get("redeem") or {}
        if rs.get("ok") and rs.get("counts"):
            tiers.append((r.get("name") or "-", rs["counts"]))

    return {
        "ts": _now(),
        "n_total": len(results or []),
        "n_ok": int(ok_count or 0),
        "total_credit": total_credit,
        "total_after": total_after,
        "cost": cost,
        "accounts": accs,
        "redeem_counts": tiers,
        "at_warn_days": at_warn_days(),
        "dry": bool(getattr(args, "dry_run", False)) if args else False,
    }


def _now():
    import datetime
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def verdict_of(s):
    """一句话结论（标题用）。"""
    n, ok = s.get("n_total", 0), s.get("n_ok", 0)
    cr = s.get("total_credit") or 0
    if n == 0:
        return "没有账号"
    if ok == n:
        return "全部成功 %d/%d · +%g 积分" % (ok, n, cr)
    if ok == 0:
        return "全部失败 0/%d" % n
    return "部分成功 %d/%d · +%g 积分" % (ok, n, cr)


def expiring_accounts(s):
    """摘要里「Token 快到期」的账号（at_left ≤ 阈值），按剩余天数升序。"""
    warn = s.get("at_warn_days")
    if warn is None:
        return []
    return sorted((a for a in (s.get("accounts") or [])
                   if a.get("at_left") is not None and a["at_left"] <= warn),
                  key=lambda a: a["at_left"])


def render_text(s):
    """把摘要渲染成 Server 酱的 Markdown 正文（纯函数，方便单测）。

    返回 (title, desp)。desp 是 Markdown，Server 酱支持。
    """
    title = verdict_of(s)
    exps = expiring_accounts(s)
    if exps:
        mn = exps[0]["at_left"]
        title += (" · ⚠️Token最快%d天到期" % mn) + ("，该换了" if mn <= 3 else "")
    L = []
    L.append("**时间**：%s" % s.get("ts", "-"))
    L.append("**结果**：%s" % title)
    L.append("**账户总余额**：%s" % (_fmt(s.get("total_after"))))
    L.append("**耗时**：%s 秒" % s.get("cost", "-"))
    if s.get("dry"):
        L.append("> 本轮是 dry-run，未做任何写入。")
    L.append("")
    L.append("### 账号明细")
    L.append("")
    L.append("| 账号 | 状态 | 本次 | 连续 | 余额 |")
    L.append("| --- | --- | --- | --- | --- |")
    for a in s.get("accounts") or []:
        L.append("| %s | %s | %s | %s | %s |" % (
            _esc(a.get("name")),
            "✅" if a.get("ok") else "❌",
            _plus(a.get("credit")),
            (("%s 天" % a["streak"]) if a.get("streak") is not None else "—"),
            _fmt(a.get("after")),
        ))
    # 派猫
    cats = [a for a in (s.get("accounts") or []) if a.get("cat_claim") or a.get("cat_depart")]
    if cats:
        L.append("")
        L.append("### 派猫猫")
        L.append("")
        for a in cats:
            L.append("- **%s** 领：%s ｜ 派：%s" % (
                _esc(a.get("name")), _esc(a.get("cat_claim") or "-"),
                _esc(a.get("cat_depart") or "-")))
    # 连登兑换
    reds = [a for a in (s.get("accounts") or []) if a.get("redeem")]
    if reds:
        L.append("")
        L.append("### 连登兑换")
        L.append("")
        for a in reds:
            L.append("- **%s**：%s" % (_esc(a.get("name")), _esc(a.get("redeem"))))
    if s.get("redeem_counts"):
        L.append("")
        L.append("本月已兑次数：")
        for nm, counts in s["redeem_counts"]:
            L.append("- %s：入门 %s / 进阶 %s / 巅峰 %s" % (
                _esc(nm), counts.get("starter", 0), counts.get("advanced", 0),
                counts.get("legendary", 0)))
    # 用量
    us = [a for a in (s.get("accounts") or []) if a.get("usage_today") is not None]
    if us:
        L.append("")
        L.append("### 今日积分消耗")
        L.append("")
        for a in us:
            L.append("- **%s**：%s" % (_esc(a.get("name")), _fmt(a.get("usage_today"))))
        L.append("")
        L.append("> 口径：积分消耗（模型调用按系数扣积分，非原始 token）。官方提示有 2-3 小时延迟。")
    # Token 到期提醒（≤ WB_TOKEN_EXPIRE_WARN_DAYS 天才出现；≤3 天红牌）
    if exps:
        L.append("")
        L.append("### ⚠️ Token 即将到期")
        L.append("")
        for a in exps:
            lvl = "🔴" if a["at_left"] <= 3 else "🟡"
            L.append("- %s **%s**：还剩 %d 天，尽快到网页「账号」页用同一 UID 换新" % (
                lvl, _esc(a.get("name")), a["at_left"]))
    L.append("")
    L.append("---")
    L.append("由 WorkBuddy 每日助手自动推送")
    return title, "\n".join(L)


def _fmt(v):
    if v is None:
        return "—"
    try:
        return ("%.2f" % float(v)).rstrip("0").rstrip(".")
    except Exception:
        return str(v)


def _plus(v):
    if v is None:
        return "—"
    try:
        f = float(v)
    except Exception:
        return str(v)
    return ("+%g" % f) if f else "+0"


def _esc(s):
    return str(s if s is not None else "-").replace("|", "\\|").replace("\n", " ")


# ---------------------------------------------------------------- 发送
def _post_form(url, data, timeout):
    body = urllib.parse.urlencode(data).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "Content-Type": "application/x-www-form-urlencoded",
        "User-Agent": UA,
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, r.read().decode("utf-8", "replace")


def _post_json(url, obj, timeout):
    body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "Content-Type": "application/json",
        "User-Agent": UA,
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, r.read().decode("utf-8", "replace")


def _send_serverchan(cfg, title, desp, log):
    """Server 酱 Turbo：POST https://sctapi.ftqq.com/<SENDKEY>.send  (title, desp)。"""
    url = SCTAPI % cfg.sendkey
    try:
        st, text = _post_form(url, {"title": title, "desp": desp}, cfg.timeout)
    except Exception as e:
        return False, "%s: %s" % (type(e).__name__, e)
    obj = _jload(text)
    # 成功：code == 0（Turbo）。有的返回 pushid。
    ok = (st == 200) and (isinstance(obj, dict) and obj.get("code") in (0, None))
    if not ok and st == 200 and not isinstance(obj, dict):
        ok = True     # 非 JSON 但 200，保守当成功
    return ok, "http=%s %s" % (st, (text or "")[:200])


def _send_serverchan3(cfg, title, desp, log):
    """Server 酱³：sendkey 形如 <uid>t<key>，域名 https://<uid>.push.ft07.com。
    简化处理：用官方兼容域 sctapi 兜底（³ 的 key 在 Turbo 域上不一定认），
    所以这里显式按 ³ 的域名规则拼。
    """
    key = cfg.sendkey3
    m = re.match(r"^([A-Za-z0-9]+)t([A-Za-z0-9]+)$", key)
    if not m:
        return False, "WB_SENDKEY3 格式不对（应形如 xxxxxxtxxxxxx）"
    uid, real = m.group(1), m.group(2)
    url = "https://%s.push.ft07.com/send/%s.send" % (uid, real)
    try:
        st, text = _post_form(url, {"title": title, "desp": desp}, cfg.timeout)
    except Exception as e:
        return False, "%s: %s" % (type(e).__name__, e)
    obj = _jload(text)
    ok = (st == 200) and (isinstance(obj, dict) and obj.get("code") in (0, None))
    return ok, "http=%s %s" % (st, (text or "")[:200])


def _send_webhook(cfg, title, desp, log):
    """通用 Webhook。默认发 {"title":..., "text":..., "markdown":...} 三套字段，
    兼容钉钉/飞书/企业微信里最常见的几种形状；用模板可完全自定义。"""
    if cfg.webhook_tpl:
        raw = (cfg.webhook_tpl
               .replace("{title}", json.dumps(title, ensure_ascii=False)[1:-1])
               .replace("{desp}", json.dumps(desp, ensure_ascii=False)[1:-1])
               .replace("{text}", json.dumps(title + "\n" + desp, ensure_ascii=False)[1:-1]))
        try:
            payload = json.loads(raw)
        except Exception as e:
            return False, "WB_NOTIFY_WEBHOOK_TEMPLATE 不是合法 JSON：%s" % e
    else:
        payload = {"title": title, "text": title, "desp": desp,
                   "markdown": desp, "content": title + "\n\n" + desp}
    try:
        st, text = _post_json(cfg.webhook, payload, cfg.timeout)
    except urllib.error.HTTPError as e:
        return False, "HTTP %s %s" % (e.code, (e.read() or b"")[:120].decode("utf-8", "replace"))
    except Exception as e:
        return False, "%s: %s" % (type(e).__name__, e)
    return (200 <= st < 300), "http=%s %s" % (st, (text or "")[:200])


def _jload(text):
    try:
        return json.loads(text)
    except Exception:
        return None


def should_notify(cfg, s):
    """按 WB_NOTIFY_ON 判断这次该不该发。"""
    if not cfg.enabled:
        return False, "已按 WB_NOTIFY=0 关闭"
    if not cfg.channels():
        return False, "没配置任何推送通道"
    mode = cfg.on
    n, ok = s.get("n_total", 0), s.get("n_ok", 0)
    if mode in ("fail", "failure", "only-fail", "onlyfail"):
        if n and ok == n:
            return False, "WB_NOTIFY_ON=fail：本次全部成功，不推送"
    elif mode in ("change", "diff"):
        if s.get("total_credit") in (0, None) and ok == n:
            return False, "WB_NOTIFY_ON=change：本次无变化，不推送"
    return True, ""


def notify(s, log=None, cfg=None):
    """发送通知。**永不抛异常**，返回结构化结果供调用方记录。

    返回 {"ok":bool, "sent":[通道], "results":{通道:(ok,msg)}, "skipped":str|None, "err":str|None}
    """
    r = {"ok": True, "sent": [], "results": {}, "skipped": None, "err": None}
    try:
        cfg = cfg or Config()
        go, why = should_notify(cfg, s)
        if not go:
            r["skipped"] = why
            return r
        title, desp = render_text(s)
        title = "%s ｜ %s" % (cfg.title_prefix, title)
        senders = []
        if cfg.sendkey:
            senders.append(("serverchan", _send_serverchan))
        if cfg.sendkey3:
            senders.append(("serverchan3", _send_serverchan3))
        if cfg.webhook:
            senders.append(("webhook", _send_webhook))
        for name, fn in senders:
            try:
                ok, msg = fn(cfg, title, desp, log)
            except Exception as e:
                ok, msg = False, "%s: %s" % (type(e).__name__, e)
            r["results"][name] = (ok, msg)
            if ok:
                r["sent"].append(name)
            else:
                r["ok"] = False
        if not r["sent"]:
            r["err"] = "；".join("%s 失败：%s" % (k, v[1]) for k, v in r["results"].items())
    except Exception as e:
        r["ok"] = False
        r["err"] = "%s: %s" % (type(e).__name__, e)
    return r


def describe_channels():
    """给 Web 页面「运行信息」用：返回安全的通道描述（不含 key 明文）。"""
    cfg = Config()
    return {
        "enabled": cfg.enabled,
        "on": cfg.on,
        "channels": cfg.channels(),
        "desc": cfg.describe(),
    }


# ---------------------------------------------------------------- 自测
def _selftest():
    """离线自测：render_text 是纯函数，断言正文关键形态。"""
    s = build_summary([
        {"name": "本机", "uid": "12345678-abcd", "after": 1234.5,
         "checkin": {"ok": True, "msg": "签到成功", "credit": 5, "streak": 3},
         "growth": {"streak": {"days": 3},
                    "redeem": {"ok": True, "counts": {"starter": 1, "advanced": 0, "legendary": 0}},
                    "redeem_action": {"results": [{"name": "入门档", "ok": True, "credit": 100}],
                                      "notes": []}},
         "usage": {"daily": {"ok": True, "today": 46}},
         "cat": {"ok": True, "claim": "已领取到家奖励，+8 积分", "depart": "已派出（咖啡馆）"}},
        {"name": "公司号", "uid": "87654321-efgh", "after": 200.0,
         "checkin": {"ok": False, "msg": "今天已签到", "credit": 0, "streak": 2},
         "growth": {}, "usage": {}, "cat": {}},
    ], ok_count=1, total_credit=5, total_after=1434.5, cost=3.2)
    t, d = render_text(s)
    assert "部分成功 1/2" in t, t
    assert "| 本机 | ✅ | +5 | 3 天 | 1234.5 |" in d, d
    assert "派猫猫" in d
    assert "连登兑换" in d and "入门档已兑+100" in d
    assert "本月已兑次数" in d
    assert "今日积分消耗" in d and "46" in d
    assert "|公司号" not in d  # 名字不含竖线
    # 全部成功
    s2 = dict(s, n_ok=2)
    assert verdict_of(s2).startswith("全部成功")
    s3 = dict(s, n_ok=0)
    assert verdict_of(s3).startswith("全部失败")
    print("[ok] notify 自测通过：")
    print("  title =", t)
    print("  desp 行数 =", len(d.split("\n")))
    return 0


if __name__ == "__main__":
    sys.exit(_selftest())

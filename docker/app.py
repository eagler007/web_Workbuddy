#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
WorkBuddy 每日助手 —— Web 控制台（零第三方依赖，纯标准库）

功能
----
  1. 账号管理：网页上增 / 删 / 改 WorkBuddy 账号（名称 + Token + UID）
  2. 测活：保存后立刻调一次查余额接口，验证 token 是否有效并显示余额
  3. 手动跑：全量跑 / 只跑某一个账号（后台异步，页面轮询进度和日志）
  4. 报告：动态渲染（读 /data/wb_history.json 实时出图）+ 静态文件兜底
  5. 日志：看最近几次运行的原始输出

安全
----
  - 登录：.env 的 WEB_PASSWORD 做校验，密码本体不落任何文件；
    签名会话 cookie（HMAC-SHA256，密钥来自 WEB_SECRET），httponly + samesite=lax
  - CSRF：所有 POST 校验 Origin/Referer 同源，并校验会话里的一次性 token
  - 账号列表里 token 一律掩码，编辑框 value 留空；任何页面都不吐完整 token
  - 路径穿越：静态/报告文件只允许白名单名字，且强制 realpath 落在 DATA_DIR 内
  - 请求体大小上限、并发写 wb_accounts.json 加锁

环境变量
--------
  DATA_DIR       数据目录，默认 /data
  WEB_PORT       监听端口，默认 8080
  WEB_HOST       监听地址，默认 0.0.0.0
  WEB_PASSWORD   登录密码，必须设置（否则启动即退出）
  WEB_SECRET     会话签名密钥，必须设置（否则启动即退出）
  WB_PYTHON      调子脚本用的解释器，默认自动探测
"""

import base64
import hashlib
import hmac
import html
import http.cookies
import json
import os
import re
import secrets
import shutil
import signal
import subprocess
import sys
import threading
import time
import datetime
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

try:
    sys.stdout.reconfigure(errors="replace")
except Exception:
    pass

# ---------------------------------------------------------------- 常量
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)              # 让 `import runlog` / `import usage` 不受 cwd 影响
DATA_DIR = os.environ.get("DATA_DIR") or "/data"
WEB_HOST = os.environ.get("WEB_HOST", "0.0.0.0")
WEB_PORT = int(os.environ.get("WEB_PORT", "8080"))
WEB_PASSWORD = os.environ.get("WEB_PASSWORD", "")
WEB_SECRET = os.environ.get("WEB_SECRET", "")
SESSION_TTL = 7 * 24 * 3600          # 会话有效期 7 天
MAX_BODY = 256 * 1024                # 请求体上限

ACCOUNTS_FILE = os.path.join(DATA_DIR, "wb_accounts.json")
HISTORY_FILE = os.path.join(DATA_DIR, "wb_history.json")
USAGE_HISTORY_FILE = os.path.join(DATA_DIR, "usage_history.json")
REPORT_FILE = os.path.join(DATA_DIR, "report.html")
SINGLE_REPORT = os.path.join(DATA_DIR, "report_single.html")
RAW_LOG = os.path.join(DATA_DIR, "wb_daily_raw.log")
RUN_LOG = os.path.join(DATA_DIR, "last_run.log")

HOST_BILLING = "https://www.codebuddy.cn"
RESOURCE_BODY = {"PageNumber": 1, "PageSize": 100, "ProductCode": "p_tcaca",
                 "Status": [0, 3], "OnlyValidPeriod": True}
UA = "WorkBuddyConsole/1.0"
TIMEOUT = 25

ACC_LOCK = threading.RLock()          # 保护 wb_accounts.json 的读写
RUN_LOCK = threading.Lock()           # 同时只允许一个手动任务在跑

# 用量查询短缓存：{天数: {"ts":.., "rows":.., "err":..}}。
# 目的是「同一窗口内多次刷新不打爆接口」，不是长期存储。
# TTL 可调：WB_USAGE_CACHE_TTL（秒），最小 5。
try:
    USAGE_CACHE_TTL = max(5, int(os.environ.get("WB_USAGE_CACHE_TTL") or 60))
except Exception:
    USAGE_CACHE_TTL = 60
_USAGE_CACHE = {}
_USAGE_LOCK = threading.Lock()

# 手动跑的进度状态（内存态，重启丢失，够用）
JOB = {"running": False, "kind": "", "started": 0.0, "ended": 0.0,
       "rc": None, "account": "", "log": ""}


def py_bin():
    """解释器：优先显式环境变量，其次本进程，最后 PATH 里找。"""
    p = os.environ.get("WB_PYTHON")
    if p and os.path.isfile(p):
        return p
    if sys.executable and os.path.isfile(sys.executable):
        return sys.executable
    return shutil.which("python3") or shutil.which("python") or "python3"


def mask(s, n=8):
    if not isinstance(s, str) or not s:
        return "-"
    return (s[:n] + "…" + s[-4:]) if len(s) > n + 4 else (s[:n] + "…")


# ---------------------------------------------------------------- 账号存储
def load_accounts():
    with ACC_LOCK:
        if not os.path.isfile(ACCOUNTS_FILE):
            return []
        try:
            with open(ACCOUNTS_FILE, "r", encoding="utf-8") as f:
                d = json.load(f)
        except Exception:
            return []
    arr = d.get("accounts") if isinstance(d, dict) else d
    if not isinstance(arr, list):
        return []
    out = []
    for a in arr:
        if isinstance(a, dict) and (a.get("token") or a.get("uid")):
            out.append({"name": (a.get("name") or "").strip() or mask(a.get("uid")),
                        "token": (a.get("token") or "").strip(),
                        "uid": (a.get("uid") or "").strip()})
    return out


def save_accounts(accs):
    """原子写：先写临时文件再 rename，避免并发读半截。"""
    with ACC_LOCK:
        os.makedirs(DATA_DIR, exist_ok=True)
        tmp = ACCOUNTS_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"accounts": accs}, f, ensure_ascii=False, indent=2)
            f.write("\n")
        os.replace(tmp, ACCOUNTS_FILE)
        try:
            os.chmod(ACCOUNTS_FILE, 0o600)
        except Exception:
            pass


def upsert_account(name, token, uid):
    """按 uid 判断新增还是更新；token/uid 为空表示不改动。"""
    accs = load_accounts()
    if not uid:
        return False, "UID 不能为空"
    for a in accs:
        if a["uid"] == uid:
            if name:
                a["name"] = name
            if token:
                a["token"] = token
            save_accounts(accs)
            return True, "已更新"
    if not token:
        return False, "新增账号时 Token 不能为空"
    accs.append({"name": name or mask(uid), "token": token, "uid": uid})
    save_accounts(accs)
    return True, "已添加"


def delete_account(uid):
    accs = load_accounts()
    left = [a for a in accs if a["uid"] != uid]
    if len(left) == len(accs):
        return False, "没找到这个账号"
    save_accounts(left)
    return True, "已删除"


# ---------------------------------------------------------------- 测活
def _token_days(token, acc=None):
    """accessToken 还剩多少天。优先用账号里存的 expiresAt，退回解 JWT 的 exp。"""
    exp = None
    if isinstance(acc, dict) and acc.get("expiresAt"):
        exp = acc["expiresAt"]
    if not exp and token:
        try:
            part = token.split(".")[1]
            part += "=" * (-len(part) % 4)
            exp = json.loads(base64.urlsafe_b64decode(part).decode("utf-8", "replace")).get("exp")
        except Exception:
            exp = None
    if not exp:
        return None
    try:
        left = (float(exp) - time.time()) / 86400.0
    except Exception:
        return None
    return max(0, int(round(left)))


def check_account(token, uid):
    """调一次查余额接口。返回 (ok, 摘要文本)。token 不进日志。"""
    if not token or not uid:
        return False, "Token / UID 为空"
    req = urllib.request.Request(
        HOST_BILLING + "/v2/billing/meter/get-user-resource",
        data=json.dumps(RESOURCE_BODY).encode(),
        headers={"User-Agent": UA, "Content-Type": "application/json",
                 "Authorization": "Bearer " + token, "X-User-Id": uid},
        method="POST")
    try:
        body = urllib.request.urlopen(req, timeout=TIMEOUT).read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        raw = ""
        try:
            raw = e.read().decode("utf-8", "replace")[:200]
        except Exception:
            pass
        if e.code == 401:
            return False, "401 未授权 —— Token 无效或已过期"
        return False, "HTTP %d %s" % (e.code, raw)
    except Exception as e:
        return False, "网络错误：%s" % e
    try:
        d = json.loads(body)
    except Exception:
        return False, "返回不是 JSON"
    accounts = ((d.get("data") or {}).get("Accounts")) or []
    if not accounts:
        code = (d.get("data") or {}).get("CycleCapacityRemainPrecise") \
            if isinstance(d.get("data"), dict) else None
        if d.get("code") not in (0, None) and not accounts:
            return False, "接口返回 code=%s msg=%s" % (d.get("code"), d.get("msg") or "")
    total = 0.0
    for a in accounts:
        try:
            total += float(a.get("CycleCapacityRemainPrecise") or 0)
        except Exception:
            pass
    return True, "有效 · 有效积分合计 %.2f · 明细 %d 条" % (total, len(accounts))


# ---------------------------------------------------------------- 跑脚本
def _run_script(args, note):
    """在后台线程里跑 wb_daily.py，输出实时追加到 JOB['log']。"""
    cmd = [py_bin(), os.path.join(HERE, "wb_daily.py")] + args
    env = dict(os.environ)
    env["DATA_DIR"] = DATA_DIR
    env["WB_TASK_NOTE"] = note
    env["PYTHONIOENCODING"] = "utf-8"
    JOB.update({"running": True, "kind": note, "started": time.time(),
                "ended": 0.0, "rc": None, "log": ""})
    try:
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             cwd=HERE, env=env, text=True,
                             encoding="utf-8", errors="replace", bufsize=1)
        for line in p.stdout:
            JOB["log"] += line
            if len(JOB["log"]) > 400_000:        # 内存兜底，防爆
                JOB["log"] = JOB["log"][-300_000:]
        p.wait()
        JOB["rc"] = p.returncode
    except Exception as e:
        JOB["log"] += "\n[error] %s\n" % e
        JOB["rc"] = -1
    finally:
        JOB["ended"] = time.time()
        JOB["running"] = False
        try:
            with open(RUN_LOG, "w", encoding="utf-8") as f:
                f.write(JOB["log"])
        except Exception:
            pass


def start_run(account=""):
    """启动一次手动运行（全量或单账号）。返回 (是否启动, 提示)。"""
    if JOB["running"]:
        return False, "已经有一次运行在进行中"
    if not RUN_LOCK.acquire(blocking=False):
        return False, "已经有一次运行在进行中"
    accs = load_accounts()
    if not accs:
        RUN_LOCK.release()
        return False, "还没有账号，先去「账号」页添加"
    if account and not any(a["name"] == account or a["uid"] == account for a in accs):
        RUN_LOCK.release()
        return False, "没找到这个账号"
    args = []
    if account:
        args += ["--account", account]
    note = "manual:" + (account or "all")

    def _wrap():
        try:
            _run_script(args, note)
        finally:
            RUN_LOCK.release()
            # 跑完顺手刷一次聚合报告（失败不影响主流程）
            try:
                subprocess.run([py_bin(), os.path.join(HERE, "wb_report.py"),
                                "--report", os.path.basename(REPORT_FILE)],
                               cwd=HERE, env={**os.environ, "DATA_DIR": DATA_DIR},
                               capture_output=True, timeout=120)
            except Exception:
                pass

    threading.Thread(target=_wrap, daemon=True).start()
    return True, "已启动"


def run_sync_report():
    """同步渲染聚合报告（进程内直接调渲染函数，不跑子进程）。"""
    try:
        import wb_report
        h = wb_report.load_history()
    except Exception as e:
        return None, "读归档失败：%s" % e
    try:
        return wb_report.render_report(h, days=30), ""
    except Exception as e:
        return None, "渲染失败：%s" % e


# ---------------------------------------------------------------- 会话
def make_session():
    ts = str(int(time.time()))
    nonce = secrets.token_hex(8)
    payload = "%s.%s" % (ts, nonce)
    sig = hmac.new(WEB_SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return payload + "." + sig


def check_session(val):
    if not val or val.count(".") != 2:
        return False, ""
    ts, nonce, sig = val.split(".")
    want = hmac.new(WEB_SECRET.encode(), ("%s.%s" % (ts, nonce)).encode(),
                    hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, want):
        return False, ""
    try:
        if time.time() - int(ts) > SESSION_TTL:
            return False, nonce
    except Exception:
        return False, ""
    return True, nonce


def check_password(pw):
    if not WEB_PASSWORD:
        return False
    return hmac.compare_digest(hashlib.sha256(pw.encode()).hexdigest(),
                               hashlib.sha256(WEB_PASSWORD.encode()).hexdigest())


# ---------------------------------------------------------------- 页面
# 样式统一走 static/theme.css（浅色 + 深色两套变量），这里读进来内联，
# 好处：单文件、无外链、离线可用，且和报告页共用同一份主题定义，风格必然一致。
STATIC_DIR = os.path.join(HERE, "static")


def _read_static(name):
    try:
        with open(os.path.join(STATIC_DIR, name), "r", encoding="utf-8") as f:
            return f.read()
    except Exception:
        return ""


def _strip_css_comments(css):
    """内联前剥掉 CSS 注释：注释是给人看的，内联进每个页面纯属浪费字节。"""
    import re as _re
    out = _re.sub(r"/\*.*?\*/", "", css, flags=_re.S)
    out = _re.sub(r"\n{3,}", "\n\n", out)
    return out.strip()


def _theme_css():
    return _strip_css_comments(_read_static("theme.css")) or _FALLBACK_CSS


def _theme_js():
    return _read_static("theme.js") or ""


# 万一 static/ 丢了（挂载错误等），至少别变成无样式裸页
_FALLBACK_CSS = """
html[data-theme=light]{--bg:#f2f3f7;--card:#fff;--line:#eceef3;--fg:#1f2329;
--mut:#8b8f99;--acc:#6a5cf5;--acc-fg:#fff;--ok:#17a673;--bad:#e34d59;--warn:#d98600;}
html[data-theme=dark]{--bg:#0f1115;--card:#171a21;--line:#262b36;--fg:#e6e9ef;
--mut:#8b93a7;--acc:#7c6cff;--acc-fg:#fff;--ok:#3fb950;--bad:#f85149;--warn:#d29922;}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);
font:15px/1.6 -apple-system,"PingFang SC","Microsoft YaHei",sans-serif}
.wrap{max-width:1080px;margin:0 auto;padding:20px 16px 60px}
"""

# 防闪白：主题必须在 <head> 里、样式之前定下来，否则先渲染浅色再跳深色。
# 这段和 static/theme.js 的逻辑保持一致（那边负责按钮交互）。
THEME_BOOT_JS = """
(function(){try{var k='wb-theme',v=localStorage.getItem(k);
if(!v){v=(window.matchMedia&&window.matchMedia('(prefers-color-scheme: dark)').matches)?'dark':'light';}
document.documentElement.setAttribute('data-theme',v);
document.documentElement.style.colorScheme=v;}catch(e){
document.documentElement.setAttribute('data-theme','light');}})();
"""

THEME_BTN = ('<button type="button" class="themebtn" id="wb-theme-btn" '
             'onclick="wbToggleTheme()" title="切换主题">'
             '<span class="ico" id="wb-theme-ico">☾</span>'
             '<span id="wb-theme-label">夜晚</span></button>')


def page(title, body, on="", msg="", nonce="", extra_head="", extra_body=""):
    nav = [("/", "总览"), ("/accounts", "账号"), ("/usage", "用量"),
           ("/report", "报告"), ("/logs", "日志"), ("/update", "更新")]
    links = "".join('<a href="%s"%s>%s</a>' %
                    (h, ' class="on"' if h == on else "", t) for h, t in nav)
    m = ""
    if msg:
        kind = "ok" if msg[0] else "bad"
        m = '<div class="msg %s">%s</div>' % (kind, html.escape(msg[1]))
    return """<!DOCTYPE html><html lang="zh-CN" data-theme="light"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>%s · WorkBuddy 助手</title>
<script>%s</script><style>%s</style>%s</head><body><div class="wrap">
<header><h1><a href="/" style="color:inherit;text-decoration:none">WorkBuddy 每日助手</a></h1>
<nav>%s%s<a href="/logout">退出</a></nav></header>
%s%s
<footer class="hint" style="margin-top:28px">数据目录 %s · <span class="mono">%s</span></footer>
</div><script>%s</script>%s</body></html>""" % (
        html.escape(title), THEME_BOOT_JS, _theme_css(), extra_head,
        links, THEME_BTN, m, body,
        html.escape(DATA_DIR), time.strftime("%Y-%m-%d %H:%M:%S"),
        _theme_js(), extra_body)


def _source_label(note):
    """把归档里的 task_note（manual:all / manual:<uid> / docker）翻成人话。"""
    n = (note or "").strip()
    if n == "docker":
        return "定时 · docker"
    if n.startswith("manual:"):
        who = n[7:] or "all"
        if who == "all":
            return "手动 · 全部账号"
        for a in load_accounts():
            if a["uid"] == who or a["name"] == who:
                return "手动 · " + (a["name"] or who)
        return "手动 · " + who
    return n or "—"


def view_home(msg="", nonce=""):
    accs = load_accounts()
    runs = []
    try:
        import wb_report
        h = wb_report.load_history()
        runs = [r for r in h.get("runs", []) if r.get("accounts")][-5:]
    except Exception:
        pass
    last = runs[-1] if runs else None

    cards = [
        ("账号数", str(len(accs)), ""),
        ("最近一次", (last or {}).get("date", "—"), "总余额 %s" %
         ((last or {}).get("summary", {}) or {}).get("after", "—")),
        ("归档记录", str(len(runs)), "最近 5 次" if runs else "还没有数据"),
    ]
    card_html = "".join(
        '<div class="mcard"><div class="v">%s</div><div class="k">%s</div>'
        '<div class="s">%s</div></div>' % (html.escape(c[1]), html.escape(c[0]),
                                           html.escape(c[2])) for c in cards)

    body = '<div class="cards">%s</div>' % card_html

    # 手动运行区
    opts = "".join('<option value="%s">%s</option>' %
                   (html.escape(a["uid"]), html.escape(a["name"])) for a in accs)
    body += """<div class="card"><h2>立即运行</h2>
<div class="actions">
<form method="post" action="/run" data-csrf="%s">
<input type="hidden" name="csrf" value="%s"><input type="hidden" name="mode" value="all">
<button type="submit">全部账号跑一次</button></form>
<form method="post" action="/run" data-csrf="%s" class="row" style="flex:1">
<input type="hidden" name="csrf" value="%s"><input type="hidden" name="mode" value="one">
<div style="min-width:200px"><select name="account">%s</select></div>
<button type="submit" class="ghost">只跑这一个</button></form>
</div>
<div id="job" class="hint"></div></div>""" % (nonce, nonce, nonce, nonce, opts)

    # 最近记录
    if runs:
        rows = ""
        for r in reversed(runs):
            s = r.get("summary", {}) or {}
            rows += ("<tr><td class='acc'>%s</td><td>%s</td><td>%s</td><td>%s</td>"
                     "<td>%s</td></tr>") % (
                html.escape(str(r.get("date", ""))),
                html.escape(_source_label(r.get("task_note", ""))),
                html.escape(str(len(r.get("accounts", [])))),
                html.escape(str(s.get("diff", "—"))),
                html.escape(str(s.get("after", "—"))))
        body += ("<div class='panel'><h3>最近几次运行</h3><div class='scroll'><table>"
                 "<tr><th>日期</th><th>来源</th><th>账号</th><th>当日积分</th>"
                 "<th>总余额</th></tr>%s</table></div></div>") % rows
    else:
        body += ("<div class='panel'><h3>最近几次运行</h3>"
                 "<p class='trim'>还没有归档数据。先加账号，再点「全部账号跑一次」。</p></div>")

    body += """<script>
async function poll(){try{const r=await fetch('/job',{headers:{'X-Requested-With':'fetch'}});
const j=await r.json();const el=document.getElementById('job');if(!el)return;
if(j.running){el.innerHTML='<span class="pill warn">运行中</span> '+j.kind+
' · '+Math.round(j.elapsed)+'s &nbsp; <a href="/logs">看日志</a>';setTimeout(poll,2000);}
else if(j.rc!==null){el.innerHTML='<span class="pill '+(j.rc===0?'ok':'bad')+'">'+
(j.rc===0?'完成':'退出码 '+j.rc)+'</span> 用时 '+Math.round(j.elapsed)+
's &nbsp; <a href="/logs">看日志</a> · <a href="/report">看报告</a>';}}
catch(e){setTimeout(poll,3000);}}
poll();
</script>"""
    return page("总览", body, on="/", msg=msg, nonce=nonce)


def view_accounts(msg="", nonce=""):
    accs = load_accounts()
    rows = ""
    for i, a in enumerate(accs, 1):
        exp = _token_days(a["token"], a)
        if exp is None:
            exp_txt = '<span class="mut">—</span>'
        elif exp <= 3:
            exp_txt = '<span class="st bad">%d 天</span>' % exp
        elif exp <= 14:
            exp_txt = '<span class="st warn">%d 天</span>' % exp
        else:
            exp_txt = '<span class="st ok">%d 天</span>' % exp
        rows += """<tr>
<td>%d</td>
<td class="acc">%s<br><span class="mono mutd">%s</span></td>
<td class="mono">%s</td>
<td>%s</td>
<td><div class="actions">
<form method="post" action="/check"><input type="hidden" name="csrf" value="%s">
<input type="hidden" name="uid" value="%s"><button class="ghost">测活</button></form>
<form method="post" action="/delete" onsubmit="return confirm('删除账号「%s」？')">
<input type="hidden" name="csrf" value="%s"><input type="hidden" name="uid" value="%s">
<button class="danger">删除</button></form></div></td></tr>""" % (
            i, html.escape(a["name"]), html.escape(a["uid"]),
            html.escape(mask(a["token"])), exp_txt,
            nonce, html.escape(a["uid"]),
            html.escape(a["name"]), nonce, html.escape(a["uid"]))

    table = ("<div class='scroll'><table><tr><th>#</th><th>名称 / UID</th><th>Token</th>"
             "<th>AT 剩余</th><th>操作</th></tr>%s</table></div>" % rows) if accs else \
        "<p class='trim'>还没有账号。在下面填入 Token 和 UID 添加第一个。</p>"

    # 推送通道状态：让老板一眼看到「配没配、会不会发」
    try:
        import notify as _nt
        _nd = _nt.describe_channels()
        if _nd["enabled"] and _nd["channels"]:
            push_html = ('<div class="msg ok">推送已开启：%s（时机 %s）</div>'
                         % (html.escape(_nd["desc"]), html.escape(_nd["on"])))
        elif _nd["enabled"]:
            push_html = ('<div class="msg bad">推送开关是开的，但<b>没配置任何通道</b> —— '
                         '会静默跳过。在 .env 里填 <code>WB_SENDKEY</code>（Server 酱）'
                         '或 <code>WB_NOTIFY_WEBHOOK</code> 后重启容器。</div>')
        else:
            push_html = '<div class="msg bad">推送已关闭（WB_NOTIFY=0）。</div>'
    except Exception as e:
        push_html = '<div class="msg bad">读推送配置失败：%s</div>' % html.escape(str(e))

    body = """<div class="panel"><h3>已有账号（%d）</h3>
<div class="sub">Token 只显示前 8 位掩码，完整值不经过页面。要换 Token 就用同一个 UID
重新提交一次。AT 剩余 = accessToken 有效期，<b>≤3 天会标红</b>，来不及就换新的。</div>
%s
</div>

<div class="card"><h2>添加 / 更新账号</h2>
<form method="post" action="/save" class="row">
<input type="hidden" name="csrf" value="%s">
<div><label>名称（随便起，便于区分）</label>
<input name="name" placeholder="本机 / 公司号" autocomplete="off"></div>
<div><label>UID</label>
<input name="uid" placeholder="账号 UID" autocomplete="off" required></div>
<div style="flex:2"><label>Token（accessToken）</label>
<input name="token" type="password" placeholder="粘贴 accessToken，不回显"
autocomplete="off" spellcheck="false" required></div>
<button type="submit">保存并测活</button>
</form>
<p class="hint">保存后会自动调一次查余额接口验证 Token 是否有效。
  怎么拿到 Token / UID，见 README「获取凭据」。</p></div>

<div class="card"><h2>推送通知（Server 酱 / Webhook）</h2>
%s
<p class="hint">
  在 .env 里配置（改完 <code>docker compose up -d</code> 生效）：<br>
  · Server 酱 Turbo：<code>WB_SENDKEY=SCTxxxxxxxxx</code>（密钥在 sct.ftqq.com 拿）<br>
  · Server 酱³：<code>WB_SENDKEY3=你的sendkey</code><br>
  · 通用 Webhook：<code>WB_NOTIFY_WEBHOOK=https://...</code>（钉钉/飞书/Bark 等）<br>
  · 推送时机：<code>WB_NOTIFY_ON=always|fail|change</code>（默认每次跑完都发）<br>
  推送内容和「日志」页看到的一致：每账号签到结果 + 派猫 + 连登兑换 + 当日积分消耗。
</p></div>""" % (len(accs), table, nonce, push_html)
    return page("账号", body, on="/accounts", msg=msg, nonce=nonce)


def view_report(msg=""):
    """动态渲染聚合报告；渲染不出来就退回静态文件。

    报告自带完整样式（和本页共用 static/theme.css），所以整段直出、不再套
    控制台外壳 —— 这样两个页面在视觉上必然一致。只在最前面插一条返回横幅。
    """
    body = ""
    try:
        htm, err = run_sync_report()
        if htm:
            banner = (
                '<div class="wrap" style="padding-bottom:0"><div class="card" '
                'style="display:flex;gap:10px;align-items:center;flex-wrap:wrap;'
                'padding:10px 14px;margin-bottom:0">'
                '<span class="hint" style="margin:0">实时渲染自 %s</span>'
                '<span style="margin-left:auto;display:flex;gap:12px;align-items:center">'
                '<a href="/">← 返回控制台</a>'
                '<a href="/report?raw=1" target="_blank" rel="noopener">单独打开</a>'
                '%s</span></div></div>') % (html.escape(HISTORY_FILE), THEME_BTN)
            marker = '<body><div class="wrap">'
            htm = (htm.replace(marker, '<body>' + banner + '<div class="wrap">', 1)
                   if marker in htm else banner + htm)
            return htm
        body = "<div class='card'><p class='mut'>动态渲染失败：%s</p></div>" % html.escape(err)
    except Exception as e:
        body = "<div class='card'><p class='mut'>异常：%s</p></div>" % html.escape(str(e))

    if os.path.isfile(REPORT_FILE):
        try:
            with open(REPORT_FILE, "r", encoding="utf-8") as f:
                return f.read()
        except Exception:
            pass
    return page("报告", body, on="/report", msg=msg, nonce="")


# ---- 日志分块渲染（折叠 + 单行截断）----------------------------------------
# 为什么要在**展示层**再兜一道：老版本归档进 data/logs/ 的日志里，balance
# 响应体是几十条套餐的完整 JSON（单行几万字符）。那种历史块没法回炉重写
# （改用户数据风险高），但渲染时必须限制，否则一打开日志页就被刷屏。
_LOG_TS_RE = re.compile(r"^===\s*(.+?)\s*===\s*$", re.M)
LOG_LINE_CLIP = int(os.environ.get("WB_LOG_LINE_CLIP") or 480)


def _fmt_size(n):
    if n >= 1024 * 1024:
        return "%.1f MB" % (n / 1024.0 / 1024.0)
    if n >= 1024:
        return "%.1f KB" % (n / 1024.0)
    return "%d B" % n


def _clip_line(line, limit=LOG_LINE_CLIP):
    """单行过长就截断（保留行首，够看清是哪个请求/哪条报错）。"""
    if len(line) <= limit:
        return line
    return line[:limit] + "  …[本行共 %s，已截断]" % _fmt_size(len(line))


def _render_log_blocks(raw):
    """把按天归档的文本渲染成可折叠的块列表；**最新一块默认展开**，其余折叠。"""
    txt = (raw or "").strip()
    if not txt:
        return "<div class='sub' style='margin:0 16px 16px'>（这一天还没有运行日志）</div>"
    body_lines = lambda body: "\n".join(_clip_line(l) for l in body.split("\n"))
    parts = _LOG_TS_RE.split(txt)
    rest = parts[1:]
    blocks = [(rest[i].strip(), rest[i + 1].strip())
              for i in range(0, len(rest) - 1, 2)]
    if not blocks:
        # 没有 === 分隔：迁移前的 last_run.log，或单次原始输出
        return ("<details class='logblk new' open><summary><b>本次运行</b>"
                "<span class='mutd'>%s</span></summary><pre>%s</pre></details>"
                % (html.escape(_fmt_size(len(txt))), html.escape(body_lines(txt))))
    out = []
    last = len(blocks) - 1
    for idx, (ts, body) in enumerate(blocks):
        new = idx == last
        out.append(
            "<details class='logblk%s'%s><summary><b>%s</b>"
            "<span class='mutd'>%d 行 · %s%s</span></summary><pre>%s</pre></details>"
            % (" new" if new else "", " open" if new else "",
               html.escape(ts), len(body.split("\n")), _fmt_size(len(body)),
               " · 最新" if new else "", html.escape(body_lines(body))))
    return "".join(out)


def view_logs(msg="", nonce="", date=None):
    """运行日志页：历史按天保留，默认显示当日，可 ?date=YYYY-MM-DD 查任意一天。"""
    try:
        import runlog as RL
    except Exception:
        RL = None
    today = datetime.date.today().isoformat()
    date = date or today
    dates = RL.list_dates() if RL else []
    # 读出当日（或指定日）的归档文本
    raw = RL.read_date(date) if RL else ""
    if not raw and date == today and os.path.isfile(RUN_LOG):
        # 兼容迁移前最后一次的 last_run.log 单文件
        try:
            with open(RUN_LOG, "r", encoding="utf-8", errors="replace") as f:
                raw = f.read()
        except Exception:
            raw = ""
    # 凭据打码（二次防线）
    txt = re.sub(r"eyJ[A-Za-z0-9._\-]{20,}", "***REDACTED***", raw or "")
    txt = re.sub(r"\bSCT[A-Za-z0-9]{6,}", "***REDACTED***", txt)
    # 日期选择器
    chips = ""
    if dates:
        for d in dates:
            on = " on" if d == date else ""
            chips += ('<a class="chip%s" href="/logs?date=%s">%s</a>'
                      % (on, html.escape(d), html.escape(d)))
    sel = ('<div class="chips" style="margin:0 16px 12px">%s</div>' % chips) if chips else ""
    body = """<div class="panel"><h3>运行日志</h3>
<div class="sub">历史日志按天保留 180 天，默认显示当日；点上面的日期查任意一天。
每次运行单独成块，<b>最新一次默认展开</b>，点标题可展开/收起。</div>
%s
<div class="sub" style="margin:10px 16px 4px">当前：<b>%s</b>（共 %d 天有记录）</div>
%s</div>""" % (sel, html.escape(date), len(dates), _render_log_blocks(txt))
    return page("日志", body, on="/logs", msg=msg, nonce=nonce)


# ---------------------------------------------------------------- 更新检查
GITHUB_REPO = os.environ.get("WB_GITHUB_REPO", "eagler007/web_workbuddy")
GHCR_IMAGE = "ghcr.io/" + GITHUB_REPO.lower() + ":latest"


def _http_json(url, timeout=10, token=""):
    """读 JSON；返回 (ok, 数据 或 错误文本, http)。"""
    hdr = {"User-Agent": UA, "Accept": "application/vnd.github+json"}
    if token:
        hdr["Authorization"] = "Bearer " + token
    req = urllib.request.Request(url, headers=hdr)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return True, json.loads(r.read().decode("utf-8", "replace")), r.status
    except urllib.error.HTTPError as e:
        try:
            raw = e.read().decode("utf-8", "replace")[:300]
        except Exception:
            raw = ""
        return False, "HTTP %s %s" % (e.code, raw), e.code
    except Exception as e:
        return False, "%s: %s" % (type(e).__name__, e), 0


def _local_revision():
    """本地镜像带进来的 commit sha（Dockerfile 构建时写进 /app/WB_REVISION）。"""
    for p in (os.path.join(HERE, "WB_REVISION"), "/app/WB_REVISION"):
        try:
            if os.path.isfile(p):
                with open(p, "r", encoding="utf-8") as f:
                    v = f.read().strip()
                if v:
                    return v
        except Exception:
            pass
    return ""


# ---- GHCR 匿名兜底 ---------------------------------------------------------
# 仓库是私有的：GitHub API 匿名访问必 404（跟额度无关）。但镜像包是**公开**的
# （飞牛不登录也能 pull），而且工作流给每次构建都打了 <commit-sha> tag ——
# 所以「:latest 的 manifest digest == 哪个 sha tag 的 digest」，那个 tag 就
# 是远端最新构建，全程匿名、零 token。
GHCR_ACCEPT = ", ".join([
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.v2+json",
])
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


def _ghcr_repo():
    return GITHUB_REPO.lower()


def _ghcr_token(timeout=10):
    """匿名拿 GHCR 的 pull token（registry 网关的匿名 token，非 GitHub 账号）。"""
    ok, data, _ = _http_json(
        "https://ghcr.io/token?scope=repository:%s:pull" % _ghcr_repo(),
        timeout=timeout)
    if ok and isinstance(data, dict):
        return data.get("token") or None
    return None


def _ghcr_manifest(repo, ref, token, timeout=10, method="GET"):
    """取 manifest；返回 (Docker-Content-Digest 头, 响应体 dict 或 None)。"""
    url = "https://ghcr.io/v2/%s/manifests/%s" % (repo, ref)
    req = urllib.request.Request(url, headers={
        "User-Agent": UA, "Accept": GHCR_ACCEPT,
        "Authorization": "Bearer " + token}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            dig = resp.headers.get("Docker-Content-Digest") or None
            body = None
            if method != "HEAD":
                try:
                    body = json.loads(resp.read().decode("utf-8", "replace"))
                except Exception:
                    body = None
            return dig, body
    except Exception:
        return None, None


def _ghcr_created(repo, token, index_body, timeout=10):
    """从镜像 config blob 里拿构建时间（拿不到就算了，不影响主流程）。"""
    try:
        for m in (index_body or {}).get("manifests") or []:
            p = m.get("platform") or {}
            if p.get("os") == "linux" and p.get("architecture") == "amd64":
                _, mbody = _ghcr_manifest(repo, m.get("digest"), token, timeout)
                cfg = (mbody or {}).get("config") or {}
                if cfg.get("digest"):
                    ok, blob, _ = _http_json(
                        "https://ghcr.io/v2/%s/blobs/%s" % (repo, cfg["digest"]),
                        timeout=timeout, token=token)
                    if ok and isinstance(blob, dict):
                        return blob.get("created")
    except Exception:
        pass
    return None


def _ghcr_update_info(local, timeout=10):
    """匿名走 GHCR 判远端最新构建。拿到远端 sha 返回 dict，拿不到返回 None。"""
    try:
        repo = _ghcr_repo()
        token = _ghcr_token(timeout)
        if not token:
            return None
        latest_dig, index_body = _ghcr_manifest(repo, "latest", token, timeout)
        if not latest_dig:
            return None
        remote = None
        # 快路径：本地 revision 本身就是镜像 tag 之一，HEAD 比一下 digest 即可
        if local and _SHA_RE.match(local):
            ldig, _ = _ghcr_manifest(repo, local, token, timeout, method="HEAD")
            if ldig and ldig == latest_dig:
                remote = local
        # 慢路径：扫全部 sha tag，找 digest 与 latest 相同的那个
        if not remote:
            ok, tags, _ = _http_json(
                "https://ghcr.io/v2/%s/tags/list" % repo, token=token, timeout=timeout)
            for t in (tags or {}).get("tags") or []:
                if not _SHA_RE.match(t or ""):
                    continue
                tdig, _ = _ghcr_manifest(repo, t, token, timeout, method="HEAD")
                if tdig and tdig == latest_dig:
                    remote = t
                    break
        if not remote:
            return None
        return {"remote": remote,
                "remote_date": _ghcr_created(repo, token, index_body, timeout),
                "remote_msg": None, "remote_url": None, "via": "ghcr"}
    except Exception:
        return None


def _to_cn_time(raw):
    """UTC ISO 时间 → 北京时间字符串。解析失败原样返回，绝不抛异常。

    来源两种格式：GitHub API `2026-09-29T02:58:29Z`、
    GHCR 镜像构建时间 `2026-09-29T02:58:29.283413737Z`（纳秒精度，
    fromisoformat 只吃 6 位小数，需先截断）。
    """
    s = str(raw or "").strip()
    if not s:
        return ""
    try:
        txt = s[:-1] + "+00:00" if s.endswith("Z") else s
        m = re.match(r"^(.+?\d)\.(\d+)([+-]\d{2}:?\d{2}|Z)?$", txt)
        if m:
            txt = "%s.%s%s" % (m.group(1), m.group(2)[:6].ljust(6, "0"),
                               m.group(3) or "")
        dt = datetime.datetime.fromisoformat(txt)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        cn = dt.astimezone(datetime.timezone(datetime.timedelta(hours=8)))
        return cn.strftime("%Y-%m-%d %H:%M:%S") + "（北京时间）"
    except Exception:
        return s


def check_update():
    """查远端最新 commit，和本地比对。

    为什么得自己查：飞牛的镜像管理只比 **tag**，而本项目所有构建都用
    :latest —— tag 永远不变，它自然不提示「有新版本」。真正变的是 commit。

    两条路：① GitHub API 的 main 分支 HEAD（仓库私有，匿名 404，需 token）；
    ② GHCR 匿名兜底（默认路，零 token）：比对 :latest 与各 sha tag 的 digest。
    """
    token = os.environ.get("WB_GITHUB_TOKEN", "").strip()
    ok, data, code = _http_json(
        "https://api.github.com/repos/%s/commits/main" % GITHUB_REPO, token=token)
    local = _local_revision()
    r = {"ok": ok, "err": None, "local": local, "remote": None, "remote_date": None,
         "remote_msg": None, "remote_url": None, "behind": None, "repo": GITHUB_REPO,
         "image": GHCR_IMAGE, "has_token": bool(token), "via": None, "checked": True}
    if ok and isinstance(data, dict):
        r["via"] = "github"
        r["remote"] = data.get("sha") or ""
        c = data.get("commit") or {}
        r["remote_date"] = (c.get("committer") or {}).get("date") or ""
        r["remote_msg"] = (c.get("message") or "").split("\n")[0][:120]
        r["remote_url"] = data.get("html_url")
    else:
        g = _ghcr_update_info(local)
        if g:
            r["ok"] = True
            r["err"] = None
            r.update(g)
        else:
            if code == 404:
                why = "仓库是私有的，匿名访问必 404（不是额度问题）"
            elif code == 403:
                why = "匿名额度用尽或被限流"
            else:
                why = "HTTP %s" % (code or "网络异常")
            r["err"] = ("查不到远端版本：%s。GHCR 匿名兜底也失败——正常情况无需任何 "
                        "token 就能比对（镜像包是公开的），走到这步多半是容器出网问题；"
                        "也可在 .env 设 WB_GITHUB_TOKEN=ghp_…（只读）改走 GitHub API。" % why)
            return r
    if local and r["remote"]:
        r["behind"] = (local[:12] != r["remote"][:12])
    elif not local:
        r["err"] = ("镜像里没有 WB_REVISION 文件 —— 这个镜像是在本功能之前构建的，"
                    "先更新一次之后就能自动比对了。")
    return r


def view_update(msg="", nonce="", info=None):
    """更新页。

    设计：页面加载**不**自动查远端（避免每次进页面都打 GitHub/GHCR）。
    只渲染本地信息和「检查更新」按钮；点按钮 POST /update/check 才真正查。
    info=None 表示「尚未检查」，按占位状态渲染。
    """
    if info is None:
        info = {"ok": False, "checked": False, "err": None,
                "local": _local_revision(), "remote": None, "remote_date": None,
                "remote_msg": None, "remote_url": None, "behind": None,
                "repo": GITHUB_REPO, "image": GHCR_IMAGE, "via": None,
                "has_token": bool(os.environ.get("WB_GITHUB_TOKEN", "").strip())}

    def _s(v):
        return html.escape(str(v)) if v else "<span class='mut'>—</span>"

    if info.get("checked") is False:
        status = ('<div class="msg">尚未检查远端版本。点下方「检查更新」，比对本机 commit '
                  '与远端最新构建。</div>')
    elif info.get("behind") is True:
        status = ('<div class="msg bad">发现新版本：远端 commit 与本地不一致，建议更新。</div>')
    elif info.get("behind") is False:
        status = '<div class="msg ok">已是最新版本（本地 commit 与远端一致）。</div>'
    else:
        status = ('<div class="msg bad">%s</div>'
                  % html.escape(info.get("err") or "状态未知"))

    def _row(k, v):
        return ('<div class="kv"><span>%s</span>'
                '<b class="mono" style="font-size:12px">%s</b></div>'
                % (html.escape(k), v))

    # 未检查时只显示本地信息；查过之后再补上需要远端才有的行
    ver = [_row("仓库", _s(info.get("repo"))),
           _row("镜像", _s(info.get("image"))),
           _row("本地 commit", _s((info.get("local") or "")[:12]))]
    if info.get("checked"):
        ver.append(_row("检查方式", {"github": "GitHub API",
                                     "ghcr": "GHCR 兜底（匿名，无需 token）",
                                     }.get(info.get("via"))))
        ver.append(_row("远端 commit", _s((info.get("remote") or "")[:12])))
        ver.append(_row("远端提交时间", _s(_to_cn_time(info.get("remote_date")))))
        ver.append(_row("远端最新提交", _s(info.get("remote_msg"))))
        if info.get("remote_url"):
            ver.append('<div class="kv"><span>查看提交</span><b><a href="%s" target="_blank" '
                       'rel="noopener">在 GitHub 打开 ↗</a></b></div>'
                       % html.escape(info["remote_url"]))

    cmd = ("cd /vol1/docker/workbuddy\n"
           "docker compose pull\n"
           "docker compose up -d")

    body = """<div class="panel">
<h3>版本状态</h3>
<div class="sub">飞牛的「镜像管理」只比对 <strong>tag</strong>，而本项目的构建全部用
<code>:latest</code> —— tag 永远不变，所以它<strong>不会提示更新</strong>。
真正变化的是 commit，这里直接和 GitHub 上 main 分支的 HEAD 比对。</div>
%s
%s
</div>
<div class="panel">
<h3>怎么更新</h3>
<div class="sub">在飞牛的终端（或 SSH）里执行下面三条命令。数据都在
<code>./data</code> 卷里，更新镜像不会动它。</div>
<pre style="margin:0 16px 16px">%s</pre>
<form method="post" action="/update/check" class="actions" style="padding:0 16px 16px">
<input type="hidden" name="csrf" value="%s">
<button type="submit">检查更新</button>
</form>
<p class="hint" style="padding:0 16px 16px">
  本页默认<strong>不</strong>自动查远端；点「检查更新」才比对本地与远端。<br>
  仓库是私有的，GitHub API 匿名访问 404 属正常 —— 会自动改走
  <strong>GHCR 匿名兜底</strong>（镜像包是公开的，无需任何 token）。<br>
  若想显示提交说明等详情，可在 <code>.env</code> 里加
  <code>WB_GITHUB_TOKEN=ghp_…</code>（只读权限就够）。
</p>
</div>""" % (status, "".join(ver), html.escape(cmd), nonce)
    return page("更新", body, on="/update", msg=msg, nonce=nonce)


# ---------------------------------------------------------------- 用量看板
# 视觉语言参考本机「Token 消耗看板」技能（KPI 横排 + 卡片区块 + 深浅色），
# 但数据源不同：那边读本机请求级日志，这里走**真实接口**查账号级积分消耗。
USAGE_DEFAULT_VIEW_DAYS = 7


def _usage_rows_cached(days, force=False):
    """带短缓存的用量查询，避免每次刷新都打接口。

    缓存键含天数；TTL 内直接复用。整体 try/except，失败返回 (rows, err)。
    """
    days = max(1, min(int(days or USAGE_DEFAULT_VIEW_DAYS), 31))
    now = time.time()
    with _USAGE_LOCK:
        c = _USAGE_CACHE.get(days)
        if c and not force and (now - c["ts"]) < USAGE_CACHE_TTL:
            return c["rows"], c["err"], c["ts"], True
    rows, err = [], None
    try:
        import usage as _u
        accs = load_accounts()
        rows = _u.query_accounts(accs, days=days)
    except Exception as e:
        err = "%s: %s" % (type(e).__name__, e)
    with _USAGE_LOCK:
        _USAGE_CACHE[days] = {"ts": now, "rows": rows, "err": err}
    return rows, err, now, False


def load_usage_history():
    """读定时采集落下的用量归档。读不到就返回空壳，绝不抛异常。

    结构见 collect_usage.py 头部说明。**文件里不含任何凭据**。
    """
    if not os.path.isfile(USAGE_HISTORY_FILE):
        return {"version": 1, "updated": "", "days": 0, "by_date": {}}
    try:
        with open(USAGE_HISTORY_FILE, "r", encoding="utf-8") as f:
            d = json.load(f)
        if not isinstance(d, dict) or not isinstance(d.get("by_date"), dict):
            return {"version": 1, "updated": "", "days": 0, "by_date": {}}
        return d
    except Exception:
        return {"version": 1, "updated": "", "days": 0, "by_date": {}}


def _bars_svg(pairs, w=560, h=120, gap=4):
    """轻量柱状图：pairs = [(label, value), ...]。色值全走主题变量。"""
    vals = [float(v or 0) for _, v in pairs]
    if not vals:
        return ""
    mx = max(vals) or 1.0
    n = len(pairs)
    bw = max(2.0, (w - gap * (n - 1)) / float(n))
    parts = []
    for i, (lab, v) in enumerate(pairs):
        v = float(v or 0)
        bh = (v / mx) * (h - 18)
        if bh < 1 and v > 0:
            bh = 1
        x = i * (bw + gap)
        y = h - 16 - bh
        parts.append(
            '<rect x="%.2f" y="%.2f" width="%.2f" height="%.2f" rx="2" '
            'fill="var(--chart-bar)"><title>%s：%.2f</title></rect>'
            % (x, y, bw, bh, html.escape(str(lab)), v))
    return ('<svg class="spark" viewBox="0 0 %d %d" preserveAspectRatio="none" '
            'style="width:100%%;height:%dpx">%s</svg>' % (w, h, h, "".join(parts)))


def _line_svg(dates, per_day, w=560, h=150):
    """轻量折线图（含面积）。全走主题变量。"""
    if not dates:
        return ""
    vals = [float(per_day.get(d) or 0) for d in dates]
    mx = max(vals) or 1.0
    n = len(dates)
    pad_l, pad_r, pad_t, pad_b = 6, 6, 10, 20
    iw = w - pad_l - pad_r
    ih = h - pad_t - pad_b
    pts = []
    for i, v in enumerate(vals):
        x = pad_l + (iw * i / (n - 1) if n > 1 else iw / 2.0)
        y = pad_t + ih - (v / mx) * ih
        pts.append((x, y))
    line = " ".join("%.2f,%.2f" % p for p in pts)
    area = ("%.2f,%.2f " % (pad_l, pad_t + ih)) + line + \
           (" %.2f,%.2f" % (pad_l + iw, pad_t + ih))
    # 只标首、中、末三个日期，避免挤在一起
    marks = ""
    if n:
        idxs = sorted(set([0, n // 2, n - 1]))
        for i in idxs:
            x = pts[i][0]
            marks += ('<text x="%.2f" y="%d" font-size="9" fill="var(--mut2)" '
                      'text-anchor="middle">%s</text>'
                      % (x, h - 6, html.escape(dates[i][5:])))
    return ('<svg viewBox="0 0 %d %d" style="width:100%%;height:%dpx">'
            '<polygon points="%s" fill="var(--spark-lo)" opacity=".35"/>'
            '<polyline points="%s" fill="none" stroke="var(--chart-line)" '
            'stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>'
            '%s</svg>' % (w, h, h, area, line, marks))


def _bars_h(pairs, maxn=12):
    """横向条形图：pairs = [(label, value), ...]（按值降序传入）。色走主题变量。"""
    if not pairs:
        return ""
    vals = [float(v or 0) for _, v in pairs]
    mx = max(vals) or 1.0
    parts = []
    for lab, v in pairs[:maxn]:
        v = float(v or 0)
        w = max((v / mx) * 100.0, 0.6)     # 百分比宽度（最小可见）
        parts.append(
            '<div class="hbar"><span class="hl">%s</span>'
            '<span class="htrack"><span class="hfill" style="width:%.1f%%">'
            '<title>%s：%.2f</title></span></span>'
            '<span class="hv">%.2f</span></div>'
            % (html.escape(str(lab)), w, html.escape(str(lab)), v, v))
    return '<div class="hbars">%s</div>' % "".join(parts)


def _heat_grid(dates, per_day):
    """日历热力矩阵：dates 升序，per_day 是同日期的积分。色阶走主题变量（只变透明度）。"""
    if not dates:
        return ""
    vals = [float(per_day.get(d) or 0) for d in dates]
    mx = max(vals) or 1.0
    cells = []
    for d in dates:
        v = float(per_day.get(d) or 0)
        op = 0.12 + 0.88 * (v / mx) if mx else 0.12
        cells.append(
            '<div class="heatcell" title="%s：%.2f">'
            '<div class="hblock" style="opacity:%.2f"></div>'
            '<div class="hdate">%s</div><div class="hval">%.2f</div></div>'
            % (html.escape(d), v, op, html.escape(d[5:]), v))
    return '<div class="heat">%s</div>' % "".join(cells)


def view_usage(msg="", nonce="", days=None, rows=None, err=None, ts=None, cached=False):
    """用量看板：自动请求真实接口，展示每个账号的积分消耗。"""
    try:
        days = max(1, min(int(days or os.environ.get("WB_USAGE_DAYS")
                              or USAGE_DEFAULT_VIEW_DAYS), 31))
    except Exception:
        days = USAGE_DEFAULT_VIEW_DAYS
    if rows is None:
        rows, err, ts, cached = _usage_rows_cached(days)

    ok_rows = [r for r in rows if r.get("ok")]
    n_ok, n_all = len(ok_rows), len(rows)
    total_sum = None
    try:
        import usage as _u
        _k, _per, total_sum = _u.merge_rows(ok_rows)
    except Exception:
        _k, _per = [], {}

    today_sum = 0.0
    have_today = False
    for r in ok_rows:
        if r.get("today") is not None:
            today_sum += float(r["today"])
            have_today = True

    # ---- KPI 横排（照 token-dashboard 的版式）
    def _kpi(k, v, s=""):
        return ('<div class="kpi"><div class="k">%s</div><div class="v">%s</div>'
                '<div class="s">%s</div></div>'
                % (html.escape(k), v, html.escape(s)))

    req_total = 0
    for r in ok_rows:
        if r.get("total") is not None:
            req_total += int(r["total"])
    kpis = [
        _kpi("今日消耗", ("%.2f" % today_sum) if have_today else "—", "积分"),
        _kpi("%d 天合计" % days, ("%.2f" % total_sum) if total_sum is not None else "—",
             "积分"),
        _kpi("日均", ("%.2f" % (total_sum / days)) if total_sum else "0.00", "积分/天"),
        _kpi("请求数", "%d" % req_total if req_total else "—", "窗口内调用次数"),
        _kpi("账号数", "%d / %d" % (n_ok, n_all), "可用 / 总数"),
    ]
    body = '<div class="kpis">%s</div>' % "".join(kpis)

    # ---- 窗口切换 + 刷新
    chips = ""
    for d in (1, 3, 7, 14, 30):
        chips += ('<a class="chip%s" href="/usage?days=%d">%d 天</a>'
                  % (" on" if d == days else "", d, d))
    body += """<div class="panel"><h3>查询窗口</h3>
<div class="actions" style="padding:6px 16px 14px">
<div class="chips">%s</div>
<form method="post" action="/usage/refresh" style="margin-left:auto">
<input type="hidden" name="csrf" value="%s"><input type="hidden" name="days" value="%d">
<button type="submit" class="ghost">重新查询</button></form>
</div>
<div class="sub" style="padding:0 16px 14px">每次打开本页会<strong>自动请求真实接口</strong>拉取账号级积分消耗
（同一窗口 %d 秒内复用缓存）。点「重新查询」强制刷新。</div></div>""" % (
        chips, nonce, days, USAGE_CACHE_TTL)

    if err:
        body += '<div class="msg bad">查询出错：%s</div>' % html.escape(str(err))

    if not rows:
        body += ('<div class="panel"><h3>还没有账号</h3>'
                 '<p class="trim">先去「账号」页添加账号，本页才能查到消耗。</p></div>')
        return page("用量", body, on="/usage", msg=msg, nonce=nonce)

    # ---- 总走势
    try:
        import usage as _u2
        dates = sorted(_per.keys())
        per_day = _per
    except Exception:
        dates, per_day = [], {}
    if dates:
        body += ('<div class="panel"><h3>每日总消耗走势</h3>'
                 '<div style="padding:4px 16px 16px">%s</div></div>'
                 % _line_svg(dates, per_day))
        # 日历热力矩阵（参考 token-dashboard 版式）
        body += ('<div class="panel"><h3>每日消耗热力</h3>'
                 '<div class="sub">色越深 = 当天消耗越高；当天为空 / 0 可能是 2–3h 数据延迟。</div>'
                 '<div style="padding:8px 16px 16px">%s</div></div>'
                 % _heat_grid(dates, per_day))
    else:
        body += ('<div class="panel"><h3>每日总消耗走势</h3>'
                 '<p class="trim">接口没返回带日期的记录。'
                 '用量数据有 2–3 小时延迟，当天为空属正常。</p></div>')

    # ---- 按模型 / 按时段（跨账号汇总）
    _agg_model, _agg_hour, _all_reqs = {}, {}, []
    for r in ok_rows:
        for m, v in (r.get("by_model") or {}).items():
            _agg_model[m] = _agg_model.get(m, 0.0) + float(v or 0)
        for h, v in (r.get("by_hour") or {}).items():
            _agg_hour[int(h)] = _agg_hour.get(int(h), 0.0) + float(v or 0)
        for rq in (r.get("requests") or []):
            _all_reqs.append(rq)
    if _agg_model:
        _mp = sorted(_agg_model.items(), key=lambda x: -x[1])
        body += ('<div class="panel"><h3>按模型消耗</h3>'
                 '<div style="padding:8px 16px 16px">%s</div></div>' % _bars_h(_mp))
    if _agg_hour:
        _hp = [("%02d" % h, _agg_hour.get(h, 0.0)) for h in range(24)]
        body += ('<div class="panel"><h3>0–24 时分布</h3>'
                 '<div class="sub">按请求发生小时聚合的积分消耗。</div>'
                 '<div style="padding:8px 16px 16px">%s</div></div>' % _bars_h(_hp))

    # ---- 账号明细
    trs = ""
    for r in rows:
        if not r.get("ok"):
            trs += ('<tr><td class="acc">%s</td>'
                    '<td><span class="st bad">失败</span></td>'
                    '<td class="mut">—</td><td class="mut">—</td>'
                    '<td class="mono" style="font-size:12px">%s</td>'
                    '<td class="uerr" style="padding:0;font-size:11.5px">%s</td></tr>'
                    % (html.escape(r.get("name") or "-"),
                       html.escape(r.get("uid_masked") or "-"),
                       html.escape(str(r.get("err") or "查询失败")[:80])))
            continue
        t = r.get("today")
        t_html = ('<b>%.2f</b>' % float(t)) if t is not None else '<span class="mut">—</span>'
        s = r.get("sum")
        s_html = ('%.2f' % float(s)) if s is not None else '<span class="mut">—</span>'
        rng = r.get("range") or []
        trs += ('<tr><td class="acc">%s</td>'
                '<td><span class="st ok">正常</span></td>'
                '<td class="credit">%s</td><td>%s</td>'
                '<td class="mono" style="font-size:12px">%s</td>'
                '<td class="mut" style="font-size:12px">%s</td></tr>'
                % (html.escape(r.get("name") or "-"), t_html, s_html,
                   html.escape("%s → %s" % (rng[0], rng[1]) if len(rng) == 2 else "—"),
                   html.escape(r.get("uid_masked") or "-")))
    body += ('<div class="panel"><h3>账号明细</h3><div class="scroll"><table>'
             '<tr><th>账号</th><th>状态</th><th>今日</th><th>%d 天合计</th>'
             '<th>窗口</th><th>UID</th></tr>%s</table></div></div>' % (days, trs))

    # ---- 每个可用账号的迷你走势
    mini = ""
    for r in ok_rows:
        try:
            import usage as _u3
            g = _u3.fill_gaps(r.get("days") or [], n=days)
        except Exception:
            g = []
        if not g:
            continue
        pairs = [(it["date"], it.get("credit") or 0) for it in g]
        mini += ('<div class="upanel"><div class="uhead">%s'
                 '<span class="usum">%s 天合计 %s</span></div>%s</div>'
                 % (html.escape(r.get("name") or "-"),
                    days,
                    ("%.2f" % float(r["sum"])) if r.get("sum") is not None else "—",
                    _bars_svg(pairs)))
    if mini:
        body += '<div class="panel"><h3>各账号每日消耗</h3>%s</div>' % mini

    # ---- 请求级明细（跨账号，按时间倒序）
    if _all_reqs:
        _all_reqs.sort(key=lambda x: (x.get("ts") or ""), reverse=True)
        _rtrs = ""
        for rq in _all_reqs[:100]:
            cr = rq.get("credit")
            _rtrs += ('<tr><td class="mono" style="font-size:12px">%s</td>'
                      '<td>%s</td><td>%s</td>'
                      '<td class="credit">%s</td></tr>'
                      % (html.escape(str(rq.get("ts") or "")),
                         html.escape(str(rq.get("model") or "-")),
                         html.escape(str(rq.get("client") or "-")),
                         ("%.2f" % float(cr)) if cr is not None else "—"))
        body += ('<div class="panel"><h3>请求级明细（最近 %d 条）</h3>'
                 '<div class="scroll"><table>'
                 '<tr><th>时间</th><th>模型</th><th>客户端</th><th>积分</th></tr>'
                 '%s</table></div>'
                 '<p class="hint" style="padding:0 16px 14px">明细来自 '
                 '<code>get-user-request-usage</code> 请求级接口，'
                 '只含模型 / 客户端 / 积分，不含任何凭据。</p></div>'
                 % (len(_all_reqs[:100]), _rtrs))

    # ---- 定时采集历史（每天早上跑的那一轮）
    hist = load_usage_history()
    hbd = hist.get("by_date") or {}
    if hbd:
        hkeys = sorted(hbd.keys())[-14:]
        # 名称集合（按采集记录里出现过的账号）
        names, seen = [], set()
        for k in reversed(hkeys):
            for a in (hbd[k].get("accounts") or []):
                nm = a.get("name") or "-"
                if nm not in seen:
                    seen.add(nm)
                    names.append(nm)
        ths = "".join("<th>%s</th>" % html.escape(n) for n in names)
        trs2 = ""
        for k in reversed(hkeys):
            rec = hbd[k]
            cellmap = {}
            for a in (rec.get("accounts") or []):
                cellmap[a.get("name") or "-"] = a
            tds = ""
            for n in names:
                a = cellmap.get(n)
                if a is None:
                    tds += '<td class="na">·</td>'
                elif not a.get("ok"):
                    tds += '<td><span class="st bad" title="%s">失败</span></td>' % \
                        html.escape(str(a.get("err") or "")[:60])
                else:
                    t = a.get("today")
                    tds += ('<td class="credit">%s</td>'
                            % (("%.2f" % float(t)) if t is not None
                               else '<span class="mut">—</span>'))
            trs2 += ('<tr><td class="acc">%s</td><td class="mut" '
                     'style="font-size:11.5px">%s</td>%s</tr>'
                     % (html.escape(k), html.escape(str(rec.get("ts") or "")[11:16]),
                        tds))
        body += ('<div class="panel"><h3>定时采集历史</h3>'
                 '<div class="sub">容器里每天 <code>%s</code> 自动跑一次用量采集'
                 '（cron：<code>%s</code>），下表是每次采到的「当日消耗」。'
                 '格子里是积分，<span class="st bad">失败</span>表示那次没查通。</div>'
                 '<div class="scroll"><table><tr><th>日期</th><th>时间</th>%s</tr>%s'
                 '</table></div>'
                 '<p class="hint" style="padding:0 16px 14px">最近更新 %s · 共 %d 天记录'
                 '（保留最近 180 天）</p></div>'
                 % (html.escape(os.environ.get("USAGE_CRON") or "10 8 * * *"),
                    html.escape(os.environ.get("USAGE_CRON") or "10 8 * * *"),
                    ths, trs2,
                    html.escape(str(hist.get("updated") or "—")), len(hbd)))
    else:
        body += ('<div class="panel"><h3>定时采集历史</h3>'
                 '<p class="trim">还没有采集记录。容器里每天早上会自动跑一次'
                 '（cron <code>%s</code>）；也可以手动执行 '
                 '<code>docker exec &lt;容器&gt; /app/deploy/collect_usage.sh</code> '
                 '立刻采一轮。</p></div>'
                 % html.escape(os.environ.get("USAGE_CRON") or "10 8 * * *"))

    body += """<div class="panel"><h3>口径说明</h3><div class="sub" style="padding:0 16px 16px">
<strong>这里查的是「积分消耗」，不是原始 token 数。</strong>CodeBuddy 采用积分计费，
模型调用按系数自动扣除积分。<br>
接口：<code>POST /billing/meter/get-user-request-usage</code>（注意**不带 /v2**，
查余额 / 签到那条才带 —— 写错前缀会 404）；
<code>get-user-daily-usage</code> 是幻觉接口（任何参数都 <code>invalid params</code>，
已于 2026-09-29 弃用）。数据存在 <strong>2–3 小时延迟</strong>，当天为 0 或为空是正常的。<br>
窗口上限 31 天（前端硬限制）。可在「设置」区调默认窗口
<code>WB_USAGE_DAYS</code>。
</div></div>"""

    if ts:
        body += ('<p class="hint">数据时间 %s%s</p>'
                 % (time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts)),
                    "（缓存）" if cached else ""))
    return page("用量", body, on="/usage", msg=msg, nonce=nonce)


def view_login(msg=""):
    body = """<div class="card" style="max-width:420px;margin:60px auto">
<h2>登录</h2>
<form method="post" action="/login">
<div><label>密码</label><input name="password" type="password" autofocus required></div>
<div style="margin-top:14px"><button type="submit">进入</button></div>
</form><p class="hint">密码来自 .env 的 WEB_PASSWORD。</p></div>"""
    m = ""
    if msg:
        m = '<div class="msg bad">%s</div>' % html.escape(msg[1])
    return """<!DOCTYPE html><html lang="zh-CN" data-theme="light"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>登录 · WorkBuddy 助手</title>
<script>%s</script><style>%s</style></head><body><div class="wrap">
%s%s</div><script>%s</script></body></html>""" % (
        THEME_BOOT_JS, _theme_css(), m, body, _theme_js())


# ---------------------------------------------------------------- HTTP
class Handler(BaseHTTPRequestHandler):
    server_version = "WBC/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        sys.stdout.write("[web] %s - %s\n" % (self.address_string(), fmt % args))
        sys.stdout.flush()

    # ---- 工具
    def _send(self, code, body, ctype="text/html; charset=utf-8", extra=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "same-origin")
        for k, v in (extra or []):
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _redirect(self, to, cookie=None):
        extra = [("Location", to)]
        if cookie:
            extra.append(("Set-Cookie", cookie))
        self._send(303, b"", "text/plain", extra)

    def _cookie(self, name):
        c = http.cookies.SimpleCookie(self.headers.get("Cookie") or "")
        return c[name].value if name in c else ""

    def _auth(self):
        ok, nonce = check_session(self._cookie("wb_sess"))
        return ok, nonce

    def _same_origin(self):
        host = self.headers.get("Host") or ""
        for h in ("Origin", "Referer"):
            v = self.headers.get(h)
            if not v:
                continue
            m = re.match(r"^https?://([^/]+)", v)
            if not m or m.group(1) != host:
                return False
        return True

    def _read_form(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except Exception:
            n = 0
        if n <= 0 or n > MAX_BODY:
            return {}
        raw = self.rfile.read(n).decode("utf-8", "replace")
        out = {}
        for kv in raw.split("&"):
            if "=" in kv:
                k, v = kv.split("=", 1)
                out[urllib.parse.unquote_plus(k)] = urllib.parse.unquote_plus(v)
        return out

    def _csrf_ok(self, form, nonce):
        return bool(form.get("csrf")) and bool(nonce) and \
            hmac.compare_digest(form.get("csrf", ""), nonce)

    # ---- 路由
    def _guard(self, fn):
        """统一兜底：任何视图异常都返回 500 文本，不把栈甩给客户端。"""
        try:
            return fn()
        except BrokenPipeError:
            return
        except Exception as e:
            import traceback
            traceback.print_exc()
            try:
                self._send(500, "内部错误：%s" % e, "text/plain; charset=utf-8")
            except Exception:
                pass

    def do_GET(self):
        return self._guard(self._do_GET)

    def _do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        if path == "/healthz":
            return self._send(200, json.dumps({"ok": True, "accounts": len(load_accounts()),
                                               "job": JOB["running"]}),
                              "application/json")
        if path == "/login":
            return self._send(200, view_login())

        ok, nonce = self._auth()
        if not ok:
            return self._redirect("/login")

        if path == "/":
            return self._send(200, view_home(nonce=nonce))
        if path == "/accounts":
            return self._send(200, view_accounts(nonce=nonce))
        if path == "/logs":
            _ld = (qs.get("date") or [""])[0] or None
            return self._send(200, view_logs(nonce=nonce, date=_ld))
        if path == "/update":
            return self._send(200, view_update(nonce=nonce))
        if path == "/usage":
            try:
                _d = int((qs.get("days") or [""])[0])
            except Exception:
                _d = None
            return self._send(200, view_usage(nonce=nonce, days=_d))
        if path == "/usage/data":
            # JSON 接口：给页面做「自动定时刷新」用，也方便你自己 curl
            try:
                _d = int((qs.get("days") or [""])[0])
            except Exception:
                _d = None
            _d = max(1, min(_d or USAGE_DEFAULT_VIEW_DAYS, 31))
            _rows, _err, _ts, _cached = _usage_rows_cached(
                _d, force=bool(qs.get("force")))
            return self._send(200, json.dumps({
                "ok": not _err, "err": _err, "days": _d,
                "cached": _cached, "ts": _ts,
                "rows": _rows}, ensure_ascii=False), "application/json")
        if path == "/job":
            return self._send(200, json.dumps({
                "running": JOB["running"], "kind": JOB["kind"], "rc": JOB["rc"],
                "elapsed": (time.time() - JOB["started"]) if JOB["started"] else 0,
                "tail": JOB["log"][-4000:]}), "application/json")
        if path == "/report":
            if qs.get("raw"):
                htm, err = run_sync_report()
                if htm:
                    return self._send(200, htm)
                return self._send(500, err or "渲染失败", "text/plain; charset=utf-8")
            return self._send(200, view_report())
        return self._send(404, page("找不到", "<div class='card'>页面不存在</div>",
                                    nonce=nonce))

    def do_HEAD(self):
        return self.do_GET()

    def do_POST(self):
        return self._guard(self._do_POST)

    def _do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        form = self._read_form()

        if path == "/login":
            if not check_password(form.get("password", "")):
                time.sleep(0.6)          # 轻微延时，挡暴力猜
                return self._send(401, view_login((False, "密码不对")))
            ck = "wb_sess=%s; Path=/; HttpOnly; SameSite=Lax; Max-Age=%d" % (
                make_session(), SESSION_TTL)
            return self._redirect("/", ck)

        ok, nonce = self._auth()
        if not ok:
            return self._redirect("/login")
        if not self._same_origin() or not self._csrf_ok(form, nonce):
            return self._send(403, page("拒绝", "<div class='card'>请求校验失败（CSRF）</div>",
                                        nonce=nonce))

        if path == "/logout":
            return self._redirect("/login",
                                  "wb_sess=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0")
        if path == "/save":
            name = (form.get("name") or "").strip()
            uid = (form.get("uid") or "").strip()
            token = (form.get("token") or "").strip()
            act, tip = upsert_account(name, token, uid)
            if act:
                okk, res = check_account(token or _find_token(uid), uid)
                msg = (okk, "%s；测活：%s" % (tip, res))
            else:
                msg = (False, tip)
            return self._send(200, view_accounts(msg=msg, nonce=nonce))
        if path == "/delete":
            act, tip = delete_account((form.get("uid") or "").strip())
            return self._send(200, view_accounts(msg=(act, tip), nonce=nonce))
        if path == "/check":
            uid = (form.get("uid") or "").strip()
            okk, res = check_account(_find_token(uid), uid)
            return self._send(200, view_accounts(msg=(okk, res), nonce=nonce))
        if path == "/usage/refresh":
            try:
                _d = int(form.get("days") or USAGE_DEFAULT_VIEW_DAYS)
            except Exception:
                _d = USAGE_DEFAULT_VIEW_DAYS
            _rows, _err, _ts, _ = _usage_rows_cached(_d, force=True)
            _n_ok = len([r for r in _rows if r.get("ok")])
            if _err:
                _msg = (False, "查询出错：%s" % _err)
            elif _rows and _n_ok == 0:
                _msg = (False, "查到 %d 个账号，全部失败。点下面「账号」页检查凭据。" % len(_rows))
            else:
                _msg = (True, "已重新查询：%d 天窗口，%d/%d 个账号返回数据。"
                        % (_d, _n_ok, len(_rows)))
            return self._send(200, view_usage(msg=_msg, nonce=nonce, days=_d,
                                              rows=_rows, err=_err, ts=_ts))
        if path == "/update/check":
            info = check_update()
            okc = info.get("behind")
            msg = (okc is not True,
                   ("已是最新版本（%s）" % (info.get("local") or "")[:12]) if okc is False
                   else ("发现新版本：%s → %s" % ((info.get("local") or "")[:12],
                                                  (info.get("remote") or "")[:12])
                         if okc is True else (info.get("err") or "检查失败")))
            return self._send(200, view_update(msg=msg, nonce=nonce, info=info))
        if path == "/run":
            mode = form.get("mode") or "all"
            who = (form.get("account") or "").strip() if mode == "one" else ""
            act, tip = start_run(who)
            return self._send(200, view_home(msg=(act, tip), nonce=nonce))
        return self._send(404, page("找不到", "<div class='card'>页面不存在</div>",
                                    nonce=nonce))


def _find_token(uid):
    for a in load_accounts():
        if a["uid"] == uid:
            return a["token"]
    return ""


def main():
    if not WEB_PASSWORD:
        print("[fatal] 没设置 WEB_PASSWORD，拒绝启动。请在 .env 里填一个强密码。")
        return 1
    if not WEB_SECRET or len(WEB_SECRET) < 16:
        print("[fatal] WEB_SECRET 缺失或太短（>=16 字符），拒绝启动。")
        print('        生成：python3 -c "import secrets;print(secrets.token_hex(32))"')
        return 1
    os.makedirs(DATA_DIR, exist_ok=True)
    print("[boot] DATA_DIR=%s" % DATA_DIR)
    print("[boot] 账号 %d 个，解释器 %s" % (len(load_accounts()), py_bin()))
    srv = ThreadingHTTPServer((WEB_HOST, WEB_PORT), Handler)
    srv.daemon_threads = True
    signal.signal(signal.SIGTERM, lambda *a: (print("[exit] SIGTERM"), sys.exit(0)))
    print("[boot] 监听 http://%s:%d/" % (WEB_HOST, WEB_PORT))
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("[exit] 收到中断")
    return 0


if __name__ == "__main__":
    sys.exit(main())

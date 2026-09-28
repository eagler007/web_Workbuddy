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
DATA_DIR = os.environ.get("DATA_DIR") or "/data"
WEB_HOST = os.environ.get("WEB_HOST", "0.0.0.0")
WEB_PORT = int(os.environ.get("WEB_PORT", "8080"))
WEB_PASSWORD = os.environ.get("WEB_PASSWORD", "")
WEB_SECRET = os.environ.get("WEB_SECRET", "")
SESSION_TTL = 7 * 24 * 3600          # 会话有效期 7 天
MAX_BODY = 256 * 1024                # 请求体上限

ACCOUNTS_FILE = os.path.join(DATA_DIR, "wb_accounts.json")
HISTORY_FILE = os.path.join(DATA_DIR, "wb_history.json")
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
CSS = """
:root{--bg:#0f1115;--card:#171a21;--line:#262b36;--fg:#e6e9ef;--mut:#8b93a7;
--ok:#3fb950;--bad:#f85149;--warn:#d29922;--acc:#4493f8;}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);
font:14px/1.6 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif}
a{color:var(--acc);text-decoration:none}
a:hover{text-decoration:underline}
.wrap{max-width:1080px;margin:0 auto;padding:20px}
header{display:flex;align-items:center;gap:16px;border-bottom:1px solid var(--line);
padding:14px 0;margin-bottom:20px;flex-wrap:wrap}
header h1{font-size:17px;margin:0;font-weight:600}
nav{display:flex;gap:14px;margin-left:auto;flex-wrap:wrap}
nav a{color:var(--mut)}
nav a.on{color:var(--fg);font-weight:600}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;
padding:16px 18px;margin-bottom:16px}
.card h2{font-size:15px;margin:0 0 12px;font-weight:600}
table{width:100%;border-collapse:collapse}
th,td{text-align:left;padding:9px 10px;border-bottom:1px solid var(--line);font-size:13px}
th{color:var(--mut);font-weight:500}
code,.mono{font-family:ui-monospace,Consolas,monospace;font-size:12px}
input,select{background:#0d1014;border:1px solid var(--line);color:var(--fg);
padding:8px 10px;border-radius:7px;font-size:13px;width:100%}
label{display:block;color:var(--mut);font-size:12px;margin:10px 0 4px}
button{background:var(--acc);color:#fff;border:0;padding:8px 14px;border-radius:7px;
font-size:13px;cursor:pointer}
button.ghost{background:transparent;border:1px solid var(--line);color:var(--fg)}
button.danger{background:transparent;border:1px solid var(--bad);color:var(--bad)}
button:disabled{opacity:.45;cursor:default}
.row{display:flex;gap:12px;flex-wrap:wrap;align-items:flex-end}
.row>div{flex:1;min-width:160px}
.pill{display:inline-block;padding:2px 8px;border-radius:999px;font-size:12px;
border:1px solid var(--line);color:var(--mut)}
.pill.ok{color:var(--ok);border-color:#1f6f3a;background:#0d2818}
.pill.bad{color:var(--bad);border-color:#7d2b26;background:#2b1310}
.pill.warn{color:var(--warn);border-color:#7a5b12;background:#251c07}
pre{background:#0d1014;border:1px solid var(--line);border-radius:8px;padding:12px;
overflow:auto;max-height:460px;font-size:12px;line-height:1.5;white-space:pre-wrap;
word-break:break-all}
.mut{color:var(--mut)}
.msg{padding:9px 12px;border-radius:7px;margin-bottom:14px;font-size:13px}
.msg.ok{background:#0d2818;border:1px solid #1f6f3a;color:#7ee2a8}
.msg.bad{background:#2b1310;border:1px solid #7d2b26;color:#ff9c94}
.hint{color:var(--mut);font-size:12px;margin-top:8px}
.actions{display:flex;gap:8px;flex-wrap:wrap}
"""


def page(title, body, on="", msg="", nonce=""):
    nav = [("/", "总览"), ("/accounts", "账号"), ("/report", "报告"),
           ("/logs", "日志")]
    links = "".join('<a href="%s"%s>%s</a>' %
                    (h, ' class="on"' if h == on else "", t) for h, t in nav)
    links += '<a href="/logout">退出</a>'
    m = ""
    if msg:
        kind = "ok" if msg[0] else "bad"
        m = '<div class="msg %s">%s</div>' % (kind, html.escape(msg[1]))
    return """<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>%s · WorkBuddy 助手</title><style>%s</style></head><body><div class="wrap">
<header><h1>WorkBuddy 每日助手</h1><nav>%s</nav></header>
%s%s
<footer class="hint" style="margin-top:28px">数据目录 %s · <span class="mono">%s</span></footer>
</div></body></html>""" % (html.escape(title), CSS, links, m, body,
                           html.escape(DATA_DIR), time.strftime("%Y-%m-%d %H:%M:%S"))


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
        '<div class="card" style="flex:1;min-width:180px"><div class="mut">%s</div>'
        '<div style="font-size:26px;font-weight:600;margin:4px 0">%s</div>'
        '<div class="mut" style="font-size:12px">%s</div></div>' % c for c in cards)

    body = '<div class="row" style="gap:16px;margin-bottom:16px">%s</div>' % card_html

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
            rows += ("<tr><td class=mono>%s</td><td>%s</td><td>%s</td><td>%s</td>"
                     "<td>%s</td></tr>") % (
                html.escape(str(r.get("date", ""))),
                html.escape(str(r.get("task_note", ""))),
                html.escape(str(len(r.get("accounts", [])))),
                html.escape(str(s.get("diff", "—"))),
                html.escape(str(s.get("after", "—"))))
        body += ("<div class='card'><h2>最近几次运行</h2><table>"
                 "<tr><th>日期</th><th>来源</th><th>账号</th><th>当日积分</th>"
                 "<th>总余额</th></tr>%s</table></div>") % rows
    else:
        body += ("<div class='card'><h2>最近几次运行</h2>"
                 "<p class='mut'>还没有归档数据。先加账号，再点「全部账号跑一次」。</p></div>")

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
        rows += """<tr>
<td>%d</td>
<td>%s<br><span class="mono mut">%s</span></td>
<td class="mono">%s</td>
<td class="mono mut">%s</td>
<td><div class="actions">
<form method="post" action="/check"><input type="hidden" name="csrf" value="%s">
<input type="hidden" name="uid" value="%s"><button class="ghost">测活</button></form>
<form method="post" action="/delete" onsubmit="return confirm('删除账号「%s」？')">
<input type="hidden" name="csrf" value="%s"><input type="hidden" name="uid" value="%s">
<button class="danger">删除</button></form></div></td></tr>""" % (
            i, html.escape(a["name"]), html.escape(a["uid"]),
            html.escape(mask(a["token"])), html.escape(mask(a["uid"])),
            nonce, html.escape(a["uid"]),
            html.escape(a["name"]), nonce, html.escape(a["uid"]))

    table = ("<table><tr><th>#</th><th>名称 / UID</th><th>Token</th><th>UID</th>"
             "<th>操作</th></tr>%s</table>" % rows) if accs else \
        "<p class='mut'>还没有账号。在下面填入 Token 和 UID 添加第一个。</p>"

    body = """<div class="card"><h2>已有账号（%d）</h2>%s
<p class="hint">Token 只显示前 8 位掩码，完整值不经过页面。要换 Token 就用同一个 UID
重新提交一次。</p></div>

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
  怎么拿到 Token / UID，见 README「获取凭据」。</p></div>""" % (
        len(accs), table, nonce)
    return page("账号", body, on="/accounts", msg=msg, nonce=nonce)


def view_report(msg=""):
    """动态渲染聚合报告；渲染不出来就退回静态文件。"""
    body = ""
    try:
        htm, err = run_sync_report()
        if htm:
            body = ("<div class='card' style='padding:8px'>"
                    "<p class='hint' style='margin:6px 10px'>实时渲染自 %s"
                    " &nbsp;<a href=\"/report?raw=1\" target=\"_blank\">"
                    "单独打开</a></p></div>%s") % (html.escape(HISTORY_FILE), htm)
            return page("报告", body, on="/report", msg=msg, nonce="")
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


def view_logs(msg=""):
    txt = ""
    if os.path.isfile(RUN_LOG):
        try:
            with open(RUN_LOG, "r", encoding="utf-8", errors="replace") as f:
                txt = f.read()
        except Exception as e:
            txt = "[读不到] %s" % e
    if not txt:
        txt = "（还没有运行日志）"
    # 二次防线：万一有 token 形态的串，替换掉
    txt = re.sub(r"eyJ[A-Za-z0-9._\-]{20,}", "***REDACTED***", txt)
    body = """<div class="card"><h2>最近一次运行的输出</h2>
<pre>%s</pre>
<p class="hint">这里只保留最后一次（%s）。历史日志随运行覆盖。</p></div>""" % (
        html.escape(txt), html.escape(RUN_LOG))
    return page("日志", body, on="/logs", msg=msg, nonce="")


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
    return """<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>登录 · WorkBuddy 助手</title><style>%s</style></head><body><div class="wrap">
%s%s</div></body></html>""" % (CSS, m, body)


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
            return self._send(200, view_logs())
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

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""
WorkBuddy 多账号签到报告生成器
================================================================================

数据从哪来
----------
唯一的来源是本地归档 DATA_DIR/wb_history.json（容器里 /data/wb_history.json）：

  - 由 wb_daily.py 每次跑完写入（签到 + 派猫的结果）。
  - 按 (task_id, ts) 去重，永久保留。
  - 文件是纯 JSON，想手工看/改/备份都行。

容器版**不再连 QD**（QD 已下线），报告纯离线渲染。历史数据迁移方式：
把老的 wb_history.json 拷到宿主卷 /vol1/docker/workbuddy/data/ 即可。

报告的形态
----------
  - 单文件 HTML（默认在 DATA_DIR 下叫 report.html），双击就能看，手机也能看。
  - 顶部：最近一次运行的三张指标卡（今日积分 / 总余额 / 账号数）。
  - 账号卡：每个账号一张，显示最近状态 + 余额构成（套餐/购买/平台奖励）。
  - 历史区：
      · 「账号 × 日期」矩阵 —— 一眼看出谁哪天签到成功、拿了多少分。
      · 每日合计折线（用内联 SVG 画，不引外部 JS）。
      · 明细表：最近 N 次运行的逐账号记录。
  - 页面内不写任何 token，只有 uid。

用法
----
  python3 wb_report.py                    # 读本地归档生成报告
  python3 wb_report.py --days 30          # 只展示最近 30 天
  python3 wb_report.py --report out.html  # 指定输出路径（默认 DATA_DIR/report.html）
  python3 wb_report.py --no-sync          # 兼容旧调用（容器版恒为离线渲染）
"""

import argparse
import datetime
import html
import json
import os
import re
import sys

try:
    sys.stdout.reconfigure(errors="replace")
except Exception:
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
# 数据目录：容器里是 /data（挂载卷），本机跑默认用脚本父目录下的 data/
DATA_DIR = os.environ.get("DATA_DIR") or os.path.join(os.path.dirname(HERE), "data")
HISTORY = os.path.join(DATA_DIR, "wb_history.json")


def data_path(p):
    """把可能相对的输出路径统一落到 DATA_DIR 下（绝对路径原样用）。"""
    return p if os.path.isabs(p) else os.path.join(DATA_DIR, p)


def mask(s, n=8):
    if not isinstance(s, str) or not s:
        return "-"
    return (s[:n] + "…" + s[-4:]) if len(s) > n + 4 else (s[:n] + "…")


# ---------------------------------------------------------------- 本地归档
def load_history():
    if not os.path.isfile(HISTORY):
        return {"runs": []}
    try:
        with open(HISTORY, "r", encoding="utf-8") as f:
            d = json.load(f)
        if not isinstance(d, dict) or "runs" not in d:
            return {"runs": []}
        return d
    except Exception as e:
        print("[warn] 读历史归档失败（将重建）：%s" % e)
        return {"runs": []}


def save_history(h):
    os.makedirs(os.path.dirname(HISTORY), exist_ok=True)
    tmp = HISTORY + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(h, f, ensure_ascii=False, indent=2)
    os.replace(tmp, HISTORY)


def merge_history(h, new_runs):
    """按 (task_id, ts) 去重后并入。返回新增条数。"""
    seen = {(r.get("task_id"), r.get("ts")) for r in h["runs"]}
    added = 0
    for r in new_runs:
        k = (r.get("task_id"), r.get("ts"))
        if k in seen:
            continue
        seen.add(k)
        h["runs"].append(r)
        added += 1
    h["runs"].sort(key=lambda r: (r.get("ts") or "", r.get("task_id") or 0))
    return added


# ---------------------------------------------------------------- 日志解析
# QD 已下线，报告只读本地归档 wb_history.json。
# 下面这组解析函数保留着，用于解析「历史归档里 raw 字段」或以后从别处导入的 QD 格式日志。
ROW_RE = re.compile(
    r'<tr>\s*'
    r'<td style="white-space:nowrap">([^<]+)</td>\s*'
    r'<td style="white-space:nowrap">\s*(?:<span class="text-\w+">([^<]+)</span>)?\s*</td>\s*'
    r'<td class="autowrap showbut" id="log\d+">(.*?)'
    r'<button class="btn hljs-button"',
    re.S)


def strip_html(s):
    s = s.replace("<br>", "\n")
    s = re.sub(r"<[^>]+>", "", s)
    s = html.unescape(s)
    # 去掉每行行首尾多余空白（QD 模板缩进带进来的）
    return "\n".join(ln.strip() for ln in s.split("\n")).strip()


def is_wb_log(text):
    """判断一段 QD 日志是不是「新版 WorkBuddy 签到」的正文。

    新版特征（只有新版模板才有的字段）：
      - 含「积分(套餐+购买+平台奖励)」这一行（分类汇总，老版没有）
      - 或至少含「【WorkBuddy 每日签到】」且含「套餐积分:」
    老版本模板（msg 变量名对不上那版）日志里是「账号: xxx」单账号格式，不含上述字段，
    会被这里挡掉，避免脏数据进归档。QD 的失败日志（Failed at N/M request）也挡掉。
    """
    if not text:
        return False
    if "Failed at" in text and "__log__" not in text and "【WorkBuddy" not in text:
        return False
    if "积分(套餐+购买+平台奖励)" in text:
        return True
    return ("【WorkBuddy 每日签到】" in text) and ("套餐积分:" in text)


def parse_qd_log_page(page_html):
    """把 /task/<id>/log 页面解析成 [{ts, success, text}]。"""
    out = []
    for m in ROW_RE.finditer(page_html):
        ts_text, verdict, body = m.group(1).strip(), (m.group(2) or "").strip(), m.group(3)
        text = strip_html(body)
        out.append({
            "ts_text": ts_text,
            "success": ("成功" in verdict) if verdict else True,
            "text": text,
        })
    return out


# ---------------------------------------------------------------- 日志正文解析
TS_HEAD_RE = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}")
ACCT_HEAD_RE = re.compile(r"^【(.+?)】\s*$")

SUMMARY_RE = {
    "before_after": re.compile(
        r"积分变化:\s*([\d.]+)\s*->\s*([\d.]+)\s*\(本次\s*([-\d.eE+]+)\)"),
    "tc": re.compile(r"套餐积分:\s*([\d.]+)"),
    "rw": re.compile(r"平台奖励积分:\s*([\d.]+)"),
    "buy": re.compile(r"购买积分:\s*([\d.]+)"),
}
ACCT_FIELD_RE = {
    "uid": re.compile(r"^\s*uid:\s*(\S+)"),
    "checkin": re.compile(r"^\s*签到:\s*(.+?)\s*$"),
    "credit_streak": re.compile(r"^\s*本次:\s*(\S+)\s*积分\s+连续:\s*(\S+)\s*天"),
    "before_after": re.compile(r"^\s*签到前\s*([\d.]+)\s*->\s*签到后\s*([\d.]+)\s*\(本次\s*([-\d.eE+]+)\)"),
    "breakdown": re.compile(r"^\s*套餐\s*([\d.]+)\s*\+\s*购买\s*([\d.]+)\s*\+\s*平台奖励\s*([\d.]+)"),
}


def _f(x, d=0.0):
    """转 float 并规整到 2 位小数（余额接口给的是 256.16000009 这种浮点尾数）。"""
    try:
        v = float(x)
    except (TypeError, ValueError):
        return d
    # 绝对值 < 0.005 一律归零（签到 0 积分时余额只差 1e-07 这种噪声）
    if abs(v) < 0.005:
        return 0.0
    return round(v, 2)


def parse_log_text(text):
    """把一段 __log__ 正文解析成 {summary:{...}, accounts:[{...}]}。

    正文形态（由签到脚本的 __log__ 产生）：
        【WorkBuddy 每日签到】
        积分变化: 1000.00 -> 1000.50 (本次 0.5)

        积分(套餐+购买+平台奖励):
          套餐积分: 100.0
          平台奖励积分: 800.0
          购买积分: 100.0
          合计(签到前): 1000.0
          合计(签到后): 1000.5
        【示例账号-1a2b3c4d】
          uid: 1a2b3c4d-...
          签到: 签到成功
          本次: 0.5 积分   连续: 3 天
          签到前 1000.0 -> 签到后 1000.5 (本次 0.5)
            套餐 100.0 + 购买 100.0 + 平台奖励 800.0
    """
    res = {"summary": {}, "accounts": []}
    cur = None
    for raw in text.split("\n"):
        ln = raw.rstrip()
        s = ln.strip()
        if not s:
            continue

        # 账号块开头
        m = ACCT_HEAD_RE.match(s)
        if m and not m.group(1).startswith("WorkBuddy"):
            cur = {"name": m.group(1), "uid": None, "checkin": None,
                   "credit": None, "streak": None,
                   "before": None, "after": None, "diff": None,
                   "tc": None, "buy": None, "rw": None}
            res["accounts"].append(cur)
            continue

        # 头部合计
        if cur is None:
            for k, rx in SUMMARY_RE.items():
                mm = rx.search(s)
                if mm:
                    if k == "before_after":
                        res["summary"]["before"] = _f(mm.group(1))
                        res["summary"]["after"] = _f(mm.group(2))
                        res["summary"]["diff"] = _f(mm.group(3))
                    else:
                        res["summary"][k] = _f(mm.group(1))
            continue

        # 账号字段
        mm = ACCT_FIELD_RE["uid"].match(ln)
        if mm:
            cur["uid"] = mm.group(1)
            continue
        mm = ACCT_FIELD_RE["checkin"].match(ln)
        if mm:
            cur["checkin"] = mm.group(1)
            continue
        mm = ACCT_FIELD_RE["credit_streak"].match(ln)
        if mm:
            cur["credit"] = mm.group(1)
            cur["streak"] = mm.group(2)
            continue
        mm = ACCT_FIELD_RE["before_after"].match(ln)
        if mm:
            cur["before"] = _f(mm.group(1))
            cur["after"] = _f(mm.group(2))
            cur["diff"] = _f(mm.group(3))
            continue
        mm = ACCT_FIELD_RE["breakdown"].match(ln)
        if mm:
            cur["tc"] = _f(mm.group(1))
            cur["buy"] = _f(mm.group(2))
            cur["rw"] = _f(mm.group(3))
            continue

    # 丢掉空壳账号（老版本模板在空槽位留下的「【账号3】uid: -」占位）
    res["accounts"] = [
        a for a in res["accounts"]
        if a.get("uid") not in (None, "-") and (a.get("after") or 0) > 0
    ]
    return res


def run_from_entry(task_id, note, entry):
    """把 QD 一条日志转成本地归档的一条 run。"""
    parsed = parse_log_text(entry["text"])
    ts_text = entry.get("ts_text") or ""
    # 归一成 ISO（QD 给的是 2026-9-28 11:07:21）
    iso = ts_text
    m = re.match(r"(\d{4})-(\d{1,2})-(\d{1,2}) (\d{1,2}:\d{2}:\d{2})", ts_text)
    if m:
        iso = "%04d-%02d-%02d %s" % (int(m.group(1)), int(m.group(2)), int(m.group(3)), m.group(4))
    return {
        "task_id": task_id,
        "task_note": note,
        "ts": iso,
        "date": iso[:10] if len(iso) >= 10 else "",
        "success": entry.get("success", True),
        "summary": parsed["summary"],
        "accounts": parsed["accounts"],
        "raw": entry["text"],
    }


# ---------------------------------------------------------------- 聚合
def sorted_dates(runs):
    return sorted({r["date"] for r in runs if r.get("date")})


def acct_key(a):
    return a.get("uid") or a.get("name") or "?"


def build_matrix(runs):
    """返回 (dates, {acct_key: {date: account_dict}}, {acct_key: 名字})。"""
    dates = sorted_dates(runs)
    cells, names = {}, {}
    for r in runs:
        d = r.get("date")
        for a in r.get("accounts") or []:
            k = acct_key(a)
            names[k] = a.get("name") or names.get(k) or k
            cells.setdefault(k, {})[d] = a
    return dates, cells, names


# ---------------------------------------------------------------- 报告模板
# ---------------------------------------------------------------- 主题
# 样式统一走 static/theme.css（和 Web 控制台共用同一份定义，风格必然一致）。
# 报告要能「单独打开、离线可看」，所以必须把 CSS/JS **内联**进来，不能外链。
HERE = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(HERE, "static")


def _read_static(name):
    for p in (os.path.join(STATIC_DIR, name), os.path.join(HERE, name)):
        try:
            with open(p, "r", encoding="utf-8") as f:
                return f.read()
        except Exception:
            continue
    return ""


# 万一 static/ 丢了，报告至少还是能看的两套配色
_FALLBACK_CSS = """
html[data-theme=light]{--bg:#f2f3f7;--card:#fff;--line:#eceef3;--fg:#1f2329;
--mut:#8b8f99;--mut2:#a7abb3;--acc:#6a5cf5;--acc-fg:#fff;--ok:#17a673;--bad:#e34d59;
--warn:#d98600;--hero1:#6a5cf5;--hero2:#8f7bff;--card2:#f8f8fc;--grid:#eef0f4;
--chart-bar:#17a673;--chart-line:#6a5cf5;--spark-hi:#6a5cf5;--spark-lo:#c9c3ff;}
html[data-theme=dark]{--bg:#0f1115;--card:#171a21;--line:#262b36;--fg:#e6e9ef;
--mut:#8b93a7;--mut2:#6d7488;--acc:#7c6cff;--acc-fg:#fff;--ok:#3fb950;--bad:#f85149;
--warn:#d29922;--hero1:#4c3fd6;--hero2:#6a5cf5;--card2:#1b1f27;--grid:#232833;
--chart-bar:#3fb950;--chart-line:#7c6cff;--spark-hi:#7c6cff;--spark-lo:#3a3550;}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.6 -apple-system,"PingFang SC",sans-serif}
.wrap{max-width:1080px;margin:0 auto;padding:20px 16px 60px}
"""

# 防闪白：主题要在 <head> 里、样式之前定下来（和 static/theme.js 逻辑一致）
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


def _strip_css_comments(css):
    """内联前剥掉 CSS 注释：注释是给人看的，内联进每个页面纯属浪费字节。"""
    import re as _re
    out = _re.sub(r"/\*.*?\*/", "", css, flags=_re.S)
    out = _re.sub(r"\n{3,}", "\n\n", out)
    return out.strip()


def _theme_css():
    return _strip_css_comments(_read_static("theme.css")) or _FALLBACK_CSS


def _theme_js():
    return _read_static("theme.js")


# 图表用色统一从主题变量取，写死颜色会导致深色模式下看不见
C_GRID = "var(--grid)"
C_LINE = "var(--chart-line)"
C_BAR = "var(--chart-bar)"
C_SPARK_HI = "var(--spark-hi)"
C_SPARK_LO = "var(--spark-lo)"
C_MUT = "var(--mut2)"
C_AX_L = "var(--acc)"
C_AX_R = "var(--ok)"


def _is_ok(a):
    """判断某账号这一次是否签到成功。

    成败口径（2026-09-29 修正）：
      1) **优先用显式 `ok` 布尔** —— 自 2026-09-29 起 `wb_daily.make_run` 已写入
         `ok`，这是唯一权威判据。
      2) 老记录没有 `ok` 字段 → 退回文案判：
         - 服务端成功标志是英文 **"OK"**（和 get-user-resource 一个风格），直接判成功；
         - 含「已签到」或「成功」也判成功；
         - 空 / 「无返回」→ 未知（不算成功也不标失败）。

    ⚠️ 之前只靠文案，导致服务端返回 `msg="OK"` 的真·成功记录被误判成「未成功/失败」。
    """
    if isinstance(a, dict) and "ok" in a and a["ok"] is not None:
        return bool(a["ok"])
    ck = (a.get("checkin") or "").strip()
    if not ck or ck == "无返回":
        return None
    if ck == "OK":                      # 服务端成功标志
        return True
    return ("已签到" in ck) or ("成功" in ck)


def _credit_cell(a):
    """签到积分格：成功 → +N / 已签；失败 → 失败；未知 → ·。"""
    if a is None:
        return '<td class="credit zero">·</td>'
    ok = _is_ok(a)
    if ok is True:
        c = a.get("diff") or 0.0
        if abs(c) < 0.005:
            return '<td class="credit zero">已签</td>'
        return '<td class="credit">+%g</td>' % round(c, 2)
    if ok is False:
        return '<td class="fail">失败</td>'
    return '<td class="credit zero">·</td>'


def dedupe_by_day(runs):
    """把同一天里同一账号的多次运行压成「当天最后一次」。

    为什么需要：调试/手动重跑一天里会跑很多次，直接把所有 run 都铺进矩阵会让
    「一行一天」的表出现重复列，且合计失真。归档 data/wb_history.json 保留全部原始
    记录（审计用），报告层只取每天每账号的**最后一次**结果。

    返回结构与 runs 相同（只含被选中的记录），并按时间升序。
    """
    latest = {}   # (date, acct_key) -> (ts, account_dict, task_id, task_note)
    for r in runs:
        d = r.get("date") or ""
        ts = r.get("ts") or ""
        for a in r.get("accounts") or []:
            k = (d, acct_key(a))
            if k not in latest or ts >= latest[k][0]:
                latest[k] = (ts, a, r.get("task_id"), r.get("task_note"))

    # 按 (ts, task_id) 重组
    by_ts = {}
    for (d, k), (ts, a, tid, note) in latest.items():
        key = (ts, tid)
        by_ts.setdefault(key, {"ts": ts, "date": d, "task_id": tid, "task_note": note,
                               "accounts": [],
                               "summary": {"before": 0.0, "after": 0.0, "diff": 0.0,
                                           "tc": 0.0, "buy": 0.0, "rw": 0.0}})
        by_ts[key]["accounts"].append(a)
    out = []
    for key in sorted(by_ts, key=lambda k: (k[0], k[1] or 0)):
        r = by_ts[key]
        sm = r["summary"]
        for a in r["accounts"]:
            for key in ("before", "after", "tc", "buy", "rw"):
                v = a.get(key)
                if v is not None:
                    sm[key] = round(sm[key] + v, 2)
            if a.get("diff"):
                sm["diff"] = round(sm["diff"] + a["diff"], 2)
        out.append(r)
    return out


def _streak_text(a):
    """账号卡里的「连续天数」。优先用新版 streak_days，退回旧版 streak 字段。"""
    v = a.get("streak_days")
    if v is None:
        v = a.get("streak")
    if v in (None, "-", ""):
        return "-"
    return "%s 天" % v


def _streak_today(runs, name, cur_days):
    """判断「今天这一次运行有没有把连登天数推进」。

    做法：在同一账号的历史里找上一次记录的连登天数，和当前值比。
      cur > prev  → 已计入（+N 天）
      cur == prev → 今天还没计入（连登天数没动）—— 这是要提醒老板的信号
      cur < prev  → 跨月清零 / 断登过，单独标注
    返回 (状态文案, 类型)，类型 ∈ {"ok","warn","bad","na"}。
    """
    if cur_days is None:
        return "—", "na"
    prev = None
    # runs 是按时间升序的，最后一个就是「当前」这一次，从倒数第二个往前找
    for r in reversed(runs[:-1]):
        for a in (r.get("accounts") or []):
            if (a.get("name") or "") == name:
                v = a.get("streak_days")
                if v is None:
                    v = a.get("streak")
                if v is not None:
                    prev = int(v)
                    break
        if prev is not None:
            break
    if prev is None:
        return "首次记录", "na"
    if cur_days > prev:
        return "已计入（+%d）" % (cur_days - prev), "ok"
    if cur_days == prev:
        return "尚未计入", "warn"
    return "已清零（上次 %d）" % prev, "warn"


def render_streak_block(last, runs=None):
    """「连登状态」区块。纯离线：只读归档字段，不发任何网络请求。

    数据来自 wb_daily.py 采集并写入 accounts[] 的：
      streak_days / streak_next_tier / makeup_cards / makeup_dates
      redeem_summary / redeem_text / redeem_todo
    旧归档没有这些字段，一律降级显示 "—"。
    """
    if not last:
        return '<div class="trim">暂无连登数据。等下一次运行后即可看到。</div>'
    accts = last.get("accounts") or []
    if not accts:
        return '<div class="trim">暂无连登数据。</div>'
    has_any = any(a.get("streak_days") is not None or a.get("makeup_cards") or
                  a.get("redeem_summary") for a in accts)
    if not has_any:
        return ('<div class="trim">最近一次运行还没有连登数据'
                '（接口未返回或已按 WB_GROWTH=0 跳过）。</div>')

    runs = runs or [last]

    # 档位中文名：前端定义 starter/advanced/legendary → 7d/14d/28d
    tier_name = {"7d": "入门档", "14d": "进阶档", "28d": "巅峰档"}

    rows = ['<div class="scroll"><table><thead><tr>'
            '<th style="text-align:left">账号</th><th>当前连登</th>'
            '<th>今日是否计入</th><th>下一档</th>'
            '<th>补登卡</th><th>本月已兑</th><th>本次兑换</th>'
            '</tr></thead><tbody>']
    for a in accts:
        nm = a.get("name") or mask(a.get("uid"))
        d = a.get("streak_days")
        nt = a.get("streak_next_tier")
        d_txt = "—" if d is None else "%s 天" % d
        if nt:
            nxt_txt = "%s（%s）" % (tier_name.get(nt, nt), nt)
        else:
            nxt_txt = "已达最高档" if d is not None else "—"
        mc = a.get("makeup_cards") or {}
        if mc.get("balance") is not None:
            cards_txt = "%s / %s 张" % (mc.get("balance"), mc.get("max", 4))
        else:
            cards_txt = "—"
        rs = a.get("redeem_summary") or {}
        if rs:
            got = []
            for tk, cn in (("starter", "入门"), ("advanced", "进阶"),
                           ("legendary", "巅峰")):
                if rs.get(tk):
                    got.append("%s×%s" % (cn, rs[tk]))
            rs_txt = "、".join(got) if got else "均未兑"
        else:
            rs_txt = "—"

        # 本次兑换结果（阶段 B）
        rt = a.get("redeem_text")
        r_ok = a.get("redeem_ok")
        if rt is None:
            act_txt, act_cls = "—", "na"
        elif r_ok is False:
            act_txt, act_cls = rt, "bad"
        elif rt.startswith("跳过"):
            act_txt, act_cls = rt, "na"
        else:
            act_txt, act_cls = rt, "ok"

        # 今日是否计入（跟历史对比）
        today_txt, today_cls = _streak_today(runs, nm, d)

        rows.append(
            "<tr><td class='acc'>%s</td><td>%s</td>"
            "<td><span class='st %s'>%s</span></td>"
            "<td>%s</td><td>%s</td><td>%s</td><td class='%s'>%s</td></tr>"
            % (html.escape(nm), html.escape(d_txt),
               today_cls, html.escape(today_txt),
               html.escape(nxt_txt), html.escape(cards_txt),
               html.escape(rs_txt), act_cls, html.escape(act_txt)))
    rows.append("</tbody></table></div>")
    return "".join(rows)


def _mini_bars(days, width=260, height=46):
    """把 [{date,credit}] 画成一条自包含的迷你柱状图（纯 SVG 内联，无外部依赖）。

    柱高按窗口内最大值归一；无数据的日期留空格子。
    返回 SVG 字符串；数据为空时返回空串。
    """
    vals = [d for d in (days or []) if d.get("credit") is not None]
    if not vals:
        return ""
    mx = max(v["credit"] for v in vals) or 1
    n = len(vals)
    gap = 2
    bw = max(3.0, (width - gap * (n - 1)) / float(n))
    bars = []
    for i, v in enumerate(vals):
        h = max(1.0, (v["credit"] / mx) * (height - 12))
        x = i * (bw + gap)
        y = height - h - 10
        # 最高的一天用主色，其余浅色（都走主题变量，深色模式下自动换色）
        fill = C_SPARK_HI if v["credit"] == mx else C_SPARK_LO
        bars.append(
            '<rect x="%.1f" y="%.1f" width="%.1f" height="%.1f" rx="2" fill="%s">'
            '<title>%s · %s</title></rect>' % (x, y, bw, h, fill,
                                               v.get("date") or "", v["credit"]))
    first = (vals[0].get("date") or "")[5:]
    last = (vals[-1].get("date") or "")[5:]
    return ('<svg class="spark" viewBox="0 0 %d %d" width="%d" height="%d" '
            'preserveAspectRatio="none" role="img">%s'
            '<text x="0" y="%d" font-size="9" fill="%s">%s</text>'
            '<text x="%d" y="%d" font-size="9" fill="%s" text-anchor="end">%s</text>'
            '</svg>') % (width, height, width, height, "".join(bars),
                         height, C_MUT, first, width, height, C_MUT, last)


def render_usage_block(last):
    """「Token / 积分消耗」区块。纯离线：只读归档字段。

    字段来自 wb_daily.py 的 usage_flow() 写入 accounts[] 的：
      usage_today / usage_days / usage_sum / usage_range / usage_err
    口径提醒（前端文案原文）：
      - 这里的 credit 是**积分消耗**（模型调用按系数扣积分），不是原始 token 数。
      - 「用量数据存在 2-3 小时的数据延迟」→ 当天为 0 / 空是正常的。
    旧归档没有这些字段，一律降级显示 "—"。
    """
    if not last:
        return '<div class="trim">暂无用量数据。等下一次运行后即可看到。</div>'
    accts = last.get("accounts") or []
    if not accts:
        return '<div class="trim">暂无用量数据。</div>'
    has_any = any(a.get("usage_days") or a.get("usage_today") is not None
                  for a in accts)
    if not has_any:
        return ('<div class="trim">最近一次运行还没有用量数据'
                '（接口未返回或已按 WB_USAGE=0 跳过）。</div>')

    # ---- 顶部 KPI（多账号求和）
    tot_today, tot_sum, n_today = 0.0, 0.0, 0
    rng = None
    for a in accts:
        if a.get("usage_today") is not None:
            tot_today += float(a["usage_today"])
            n_today += 1
        if a.get("usage_sum") is not None:
            tot_sum += float(a["usage_sum"])
        if not rng and a.get("usage_range"):
            rng = a["usage_range"]
    n_days = 0
    for a in accts:
        ds = a.get("usage_days")
        if isinstance(ds, list):
            n_days = max(n_days, len(ds))
    avg = round(tot_sum / n_days, 2) if n_days else None

    def _f(v):
        if v is None:
            return "—"
        return ("%g" % round(float(v), 2))

    kpi = [
        ("今日消耗", _f(tot_today) if n_today else "—",
         "今日暂无数据（延迟 2-3 小时）" if not n_today else "%d 个账号" % n_today),
        ("窗口合计", _f(tot_sum), ("%s ~ %s" % (rng[0], rng[1])) if rng else "—"),
        ("日均", _f(avg), "%d 天" % n_days if n_days else "—"),
    ]
    kpi_html = "".join(
        '<div class="ucard"><div class="uv">%s</div><div class="uk">%s</div>'
        '<div class="us">%s</div></div>' % (html.escape(v), html.escape(k), html.escape(s))
        for k, v, s in kpi)

    # ---- 走势（取第一个有数据的账号画）
    spark_src = None
    for a in accts:
        if isinstance(a.get("usage_days"), list) and a["usage_days"]:
            spark_src = a
            break
    spark = ""
    if spark_src:
        svg = _mini_bars(spark_src["usage_days"])
        if svg:
            spark = ('<div class="uspark"><div class="uslabel">%s 的每日消耗</div>%s</div>'
                     % (html.escape(spark_src.get("name") or "—"), svg))

    # ---- 明细表
    rows = ['<div class="scroll"><table><thead><tr>'
            '<th style="text-align:left">账号</th><th>今日</th>'
            '<th>窗口合计</th><th>有数据天数</th><th style="text-align:left">走势</th>'
            '</tr></thead><tbody>']
    for a in accts:
        ds = a.get("usage_days")
        nd = len(ds) if isinstance(ds, list) else 0
        cell_ok = bool(ds) or a.get("usage_today") is not None
        cls = "" if cell_ok else "na"
        svg = _mini_bars(ds, width=150, height=30) if isinstance(ds, list) else ""
        td_today = ("<span class='st ok'>%s</span>" % html.escape(_f(a.get("usage_today")))
                    if a.get("usage_today") is not None else "<span class='st na'>—</span>")
        rows.append(
            "<tr><td class='acc'>%s</td><td>%s</td><td class='%s'>%s</td>"
            "<td class='%s'>%s</td><td style='text-align:left'>%s</td></tr>"
            % (html.escape(a.get("name") or mask(a.get("uid"))),
               td_today,
               cls, html.escape(_f(a.get("usage_sum"))),
               cls, html.escape(str(nd)),
               svg or "<span class='mutd'>—</span>"))
    rows.append("</tbody></table></div>")

    err = ""
    for a in accts:
        if a.get("usage_err"):
            err = ("<div class='uerr'>%s 读取失败：%s</div>"
                   % (html.escape(a.get("name") or "—"), html.escape(str(a["usage_err"]))))
            break

    note = ('<div class="unote">口径：这里统计的是<strong>积分消耗</strong>'
            '（模型调用按系数扣积分，非原始 token 数）。'
            '官方提示「用量数据存在 2-3 小时延迟」，当天数字偏小或为空属正常。</div>')
    return kpi_html + spark + "".join(rows) + err + note


def render_report(h, days=30, tasks=None, daily=True):
    runs = [r for r in h["runs"] if r.get("accounts")]
    if tasks:
        runs = [r for r in runs if r.get("task_id") in tasks]
    if daily:
        runs = dedupe_by_day(runs)
    if days and days > 0:
        cut = (datetime.date.today() - datetime.timedelta(days=days - 1)).isoformat()
        runs = [r for r in runs if (r.get("date") or "") >= cut]

    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    last = runs[-1] if runs else None

    # ---- 指标卡
    if last:
        sm = last.get("summary") or {}
        n_ok = sum(1 for a in (last.get("accounts") or []) if _is_ok(a) is True)
        n_tot = len(last.get("accounts") or [])
        cards = [
            ("今日获取", ("+%g" % round(sm.get("diff") or 0, 2)) if sm.get("diff") else "+0"),
            ("账户总余额", ("%.2f" % sm["after"]) if sm.get("after") is not None else "—"),
            ("账号数", "%d（成功 %d）" % (n_tot, n_ok)),
        ]
        verdict_line = "签到成功 %d/%d 个账号" % (n_ok, n_tot)
    else:
        cards = [("今日获取", "—"), ("账户总余额", "—"), ("账号数", "—")]
        verdict_line = "暂无运行记录"

    # ---- 账号 × 日期矩阵（日期在行、账号在列，更符合「看历史」的直觉）
    dates, cells, names = build_matrix(runs)
    show_dates = dates[-days:] if days and days > 0 else dates
    mt = ['<div class="scroll"><table><thead><tr><th style="text-align:left">日期</th>']
    acct_cols = sorted(cells.items(), key=lambda kv: names.get(kv[0], kv[0]))
    for k, _ in acct_cols:
        mt.append("<th>%s</th>" % html.escape(names.get(k) or mask(k)))
    mt.append("<th>当日合计</th></tr></thead><tbody>")
    for d in show_dates:
        tot = 0.0
        tds = []
        for k, by_date in acct_cols:
            a = by_date.get(d)
            tds.append(_credit_cell(a))
            if a and a.get("diff"):
                tot += a["diff"]
        mt.append('<tr><td class="acc">%s</td>%s<td class="credit%s">%s</td></tr>' % (
            html.escape(d), "".join(tds),
            "" if abs(tot) >= 0.005 else " zero",
            ("+%g" % round(tot, 2)) if abs(tot) >= 0.005 else "0"))
    mt.append("</tbody></table></div>")
    matrix_html = "".join(mt)

    # ---- 每日合计折线（内联 SVG）
    daily = []
    for d in show_dates:
        s = 0.0
        bal = None
        for k, by_date in acct_cols:
            a = by_date.get(d)
            if a:
                if a.get("diff"):
                    s += a["diff"]
                if a.get("after") is not None:
                    bal = (bal or 0) + a["after"]
        daily.append((d, s, bal))
    chart_html = render_chart(daily)

    # ---- 明细表（最近 40 条）
    det = ['<div class="scroll"><table><thead><tr>'
           '<th>时间</th><th>任务</th><th>账号</th><th>状态</th><th>积分</th>'
           '<th>余额</th><th>套餐</th><th>购买</th><th>平台奖励</th>'
           "</tr></thead><tbody>"]
    n = 0
    for r in reversed(runs):
        for a in r.get("accounts") or []:
            if n >= 40:
                break
            n += 1
            ok = _is_ok(a)
            st_cls = "ok" if ok is True else ("fail" if ok is False else "")
            st_txt = "已签到" if ok is True else ("未成功" if ok is False else "未知")
            det.append(
                "<tr><td>%s</td><td>#%s</td><td class='acc'>%s</td>"
                "<td class='%s'>%s</td><td class='credit%s'>%s</td>"
                "<td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>" % (
                    html.escape(r.get("ts") or ""), r.get("task_id"),
                    html.escape(a.get("name") or mask(a.get("uid"))),
                    st_cls, st_txt,
                    "" if (a.get("diff") or 0) else " zero",
                    ("+%g" % round(a.get("diff") or 0, 2)) if a.get("diff") else "+0",
                    ("%.2f" % a["after"]) if a.get("after") is not None else "-",
                    ("%.2f" % a["tc"]) if a.get("tc") is not None else "-",
                    ("%.2f" % a["buy"]) if a.get("buy") is not None else "-",
                    ("%.2f" % a["rw"]) if a.get("rw") is not None else "-"))
        if n >= 40:
            break
    det.append("</tbody></table></div>")
    detail_html = "".join(det)

    # ---- 连登状态（阶段 A 采集 + 阶段 B 兑换结果，纯离线渲染）
    streak_html = render_streak_block(last, runs)

    # ---- Token / 积分消耗（只读采集，纯离线渲染）
    usage_html = render_usage_block(last)

    # 账号卡状态标签：用主题变量，别写死颜色
    def _tag(ok):
        if ok is True:
            return "var(--ok-bg)", "var(--ok)", "已签到"
        if ok is False:
            return "var(--bad-bg)", "var(--bad)", "未成功"
        return "var(--card2)", "var(--mut2)", "状态未知"

    acct_cards = []
    if last:
        for a in last.get("accounts") or []:
            ok = _is_ok(a)
            kv = [
                ("签到状态", a.get("checkin") or "-"),
                ("本次积分", ("+%g" % round(a.get("diff") or 0, 2)) if a.get("diff") else "+0"),
                ("连续天数", _streak_text(a)),
                ("余额", ("%.2f" % a["after"]) if a.get("after") is not None else "-"),
                ("套餐", ("%.2f" % a["tc"]) if a.get("tc") is not None else "-"),
                ("购买", ("%.2f" % a["buy"]) if a.get("buy") is not None else "-"),
                ("平台奖励", ("%.2f" % a["rw"]) if a.get("rw") is not None else "-"),
            ]
            tag_bg, tag_fg, tag_txt = _tag(ok)
            acct_cards.append(
                '<div class="acct"><h4>%s <span class="tag" style="background:%s;color:%s">%s</span></h4>'
                '<div class="uid">%s</div>%s</div>' % (
                    html.escape(a.get("name") or "账号"),
                    tag_bg, tag_fg, tag_txt,
                    html.escape(a.get("uid") or "-"),
                    "".join('<div class="kv"><span>%s</span><b>%s</b></div>'
                            % (html.escape(k), html.escape(str(v))) for k, v in kv)))
    acct_html = ('<div class="acct-grid">%s</div>' % "".join(acct_cards)) if acct_cards \
        else '<div class="trim">暂无记录。先在控制台跑一次，再回来看。</div>'


    n_acct = len(acct_cols)
    n_run = len(runs)
    span = ("%s ~ %s" % (show_dates[0], show_dates[-1])) if show_dates else "—"

    return """<!DOCTYPE html>
<html lang="zh-CN" data-theme="light"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>WorkBuddy 多账号签到报告</title>
<script>%s</script>
<style>%s</style></head>
<body><div class="wrap">
  <h1>WorkBuddy 多账号签到报告</h1>
  <div class="meta">生成于 %s ｜ 数据区间 %s ｜ %d 天 %d 次运行 ｜ %d 个账号</div>
  <div class="hero">
    <h2>最近一次运行 <span style="float:right;font-size:13px;font-weight:400;opacity:.9">%s</span></h2>
    <div class="t">%s</div>
  </div>
  <div class="cards">%s</div>
  <div class="panel">
    <h3>账号概览<span class="sub" style="padding:0 0 0 6px">最近一次</span></h3>
    %s
  </div>
  <div class="panel">
    <h3>连登状态<span class="sub" style="padding:0 0 0 6px">最近一次</span></h3>
    <div class="sub">「连续登录」= 连续登录<b>且使用</b> WorkBuddy 的天数 —— 所以它需要你本人发一次对话才会推进。
      档位：入门 7 天 / 进阶 14 天 / 巅峰 28 天，每档每月限兑 1 次。</div>
    %s
  </div>
  <div class="panel">
    <h3>Token / 积分消耗<span class="sub" style="padding:0 0 0 6px">最近一次</span></h3>
    %s
  </div>
  <div class="panel">
    <h3>历史签到矩阵</h3>
    <div class="sub">行 = 日期，列 = 账号；「已签」表示当天签到成功但积分变化 &lt; 0.01；「·」表示当天没有这个账号的记录。</div>
    %s
  </div>
  <div class="panel">
    <h3>每日走势</h3>
    <div class="sub">折线 = 当日账户总余额（左轴）；柱 = 当日积分增量合计（右轴）</div>
    %s
  </div>
  <div class="panel">
    <h3>明细（最近 40 条）</h3>
    %s
  </div>
  <div class="foot">
    数据来源：本地归档 <code>data/wb_history.json</code>。<br>
    本页由 <code>wb_report.py</code> 生成，纯静态、无外部依赖、无网络调用。页面内不含任何 token。
  </div>
</div><script>%s</script></body></html>
""" % (THEME_BOOT_JS, _theme_css(), now, span, len(show_dates), n_run, n_acct,
       html.escape(last.get("ts") if last else "暂无运行记录"),
       html.escape(verdict_line),
       "".join('<div class="mcard"><div class="v">%s</div><div class="k">%s</div></div>'
               % (html.escape(v), html.escape(k)) for k, v in cards),
       acct_html, streak_html, usage_html, matrix_html, chart_html, detail_html,
       _theme_js())


def render_chart(daily):
    """纯内联 SVG 双序列图（不引 Chart.js，离线可用）。

    序列 1：每日「账户总余额」—— 折线（左轴）。这是真正有趋势意义的量。
    序列 2：每日「积分增量合计」—— 柱形（右轴）。签到 0 分时是平的，正好看出"没变化"。
    单看积分增量会把图压成一条 0 基线（因为每天通常就 0~10 分），所以余额当主线。
    """
    if not daily:
        return '<div class="trim">暂无足够数据画图。</div>'
    W, H, PL, PR, PT, PB = 1000, 260, 56, 56, 20, 40
    iw, ih = W - PL - PR, H - PT - PB
    n = len(daily)
    dx = iw / max(1, n - 1) if n > 1 else 0

    def X(i):
        return PL + (i * dx if n > 1 else iw / 2)

    bals = [b for _, _, b in daily if b is not None]
    credits = [c for _, c, _ in daily if c is not None] or [0.0]

    def scale(vals, pad_ratio=0.12):
        lo, hi = min(vals), max(vals)
        if hi - lo < 1e-9:
            lo, hi = lo - 1, hi + 1
        pad = (hi - lo) * pad_ratio
        return lo - pad, hi + pad

    blo, bhi = scale(bals) if bals else (0.0, 1.0)
    clo, chi = 0.0, max(1.0, max(credits) * 1.35)

    def YB(v):
        return PT + ih - (v - blo) / (bhi - blo) * ih

    def YC(v):
        return PT + ih - (v - clo) / (chi - clo) * ih

    parts = []
    # 左轴（余额）3 条网格 + 刻度
    for k in range(3):
        v = blo + (bhi - blo) * k / 2.0
        y = YB(v)
        parts.append('<line x1="%d" y1="%.1f" x2="%d" y2="%.1f" stroke="%s" stroke-width="1"/>'
                     % (PL, y, W - PR, y, C_GRID))
        parts.append('<text x="%d" y="%.1f" font-size="11" fill="%s" text-anchor="end">%.2f</text>'
                     % (PL - 8, y + 4, C_AX_L, v))
    # 右轴（积分）刻度
    for k in range(3):
        v = clo + (chi - clo) * k / 2.0
        y = YC(v)
        parts.append('<text x="%d" y="%.1f" font-size="11" fill="%s" text-anchor="start">%g</text>'
                     % (W - PR + 8, y + 4, C_AX_R, v))

    # 柱：当日积分
    bw = max(3.0, min(26.0, dx * 0.45)) if n > 1 else 18.0
    for i, (_, c, _) in enumerate(daily):
        c = c or 0.0
        y = YC(c)
        y0 = YC(clo)
        if y0 - y < 1.4:
            y = y0 - 1.4   # 让 0 也有一个可见的小方块，避免"什么都没有"
        parts.append('<rect x="%.1f" y="%.1f" width="%.1f" height="%.1f" rx="2" fill="%s" opacity="0.75"/>'
                     % (X(i) - bw / 2, y, bw, max(1.4, y0 - y), C_BAR))

    # 折线：余额
    if bals:
        poly = " ".join("%.1f,%.1f" % (X(i), YB(b))
                        for i, (_, _, b) in enumerate(daily) if b is not None)
        parts.append('<path d="M %s" fill="none" stroke="%s" stroke-width="2.2" '
                     'stroke-linejoin="round"/>' % (poly, C_LINE))
        for i, (_, _, b) in enumerate(daily):
            if b is None:
                continue
            parts.append('<circle cx="%.1f" cy="%.1f" r="%.1f" fill="var(--card)" stroke="%s" '
                         'stroke-width="2"/>' % (X(i), YB(b), 3.2 if n <= 40 else 2.2, C_LINE))

    # x 轴标签（最多 10 个）
    step = max(1, (n + 9) // 10)
    for i in range(0, n, step):
        parts.append('<text x="%.1f" y="%d" font-size="11" fill="%s" text-anchor="middle">%s</text>'
                     % (X(i), H - 14, C_MUT, html.escape((daily[i][0] or "")[5:])))

    legend = (
        '<div style="display:flex;gap:18px;padding:0 16px 10px;font-size:12.5px;color:var(--mut)">'
        '<span><span style="display:inline-block;width:18px;height:3px;background:var(--chart-line);'
        'vertical-align:middle;margin-right:6px"></span>账户总余额（左轴）</span>'
        '<span><span style="display:inline-block;width:11px;height:11px;background:var(--chart-bar);'
        'opacity:.75;vertical-align:middle;margin-right:6px;border-radius:2px"></span>'
        '当日积分合计（右轴）</span></div>')
    return (legend + '<div style="overflow:auto"><svg viewBox="0 0 %d %d" width="100%%" '
            'style="min-width:700px;display:block">%s</svg></div>'
            % (W, H, "".join(parts)))


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-sync", action="store_true",
                    help="兼容旧调用；容器版已无 QD，恒为离线渲染")
    ap.add_argument("--tasks", default="", help="只展示这些 task_id 的记录（逗号分隔），默认全部")
    ap.add_argument("--days", type=int, default=30, help="报告展示最近多少天（0=全部）")
    ap.add_argument("--report", default="report.html",
                    help="聚合报告路径（默认在 DATA_DIR 下）")
    args = ap.parse_args()

    h = load_history()
    tasks = None
    if args.tasks:
        tasks = {t.strip() for t in re.split(r"[,\s]+", args.tasks) if t.strip()}
    out = data_path(args.report)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        f.write(render_report(h, days=args.days, tasks=tasks))
    runs = [r for r in h["runs"] if r.get("accounts")]
    print("报告已生成：%s" % out)
    print("归档记录 %d 条，覆盖 %s" % (
        len(runs), ", ".join(sorted({r["date"] for r in runs if r.get("date")})) or "—"))
    return 0


if __name__ == "__main__":
    sys.exit(main())

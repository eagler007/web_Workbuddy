#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""local_token.py —— 本机 WorkBuddy 凭据读取/续期工具（只在装了客户端的机器上跑）

为什么需要它
------------
WorkBuddy 5.6.2 起，登录态里的 ``auth.accessToken`` / ``auth.refreshToken`` /
``account.nickname`` / ``account.phoneNumber`` 四项，从明文换成了 AES-256-GCM
信封（``{"$wbEncrypted":1,"envelope":"..."}``）。解密用的主密钥只在客户端
主进程内存里 —— 不落盘、不写钥匙串、也不发给子进程。

**后果**：加密之后，人已经没法从凭据文件里手工复制 accessToken 了。
容器里的签到一旦等到 token 过期（accessToken 约 30 天），就只能靠本工具
重新导出一次。

它怎么做
--------
借客户端自带的 Electron 运行时：同一个可执行文件，带上
``ELECTRON_RUN_AS_NODE=1`` 就不再以 GUI 启动，而是一个普通 Node 进程 ——
它天然带着客户端注册过的原生模块（``electron_browser_workbuddy_storage``），
在这个子进程里问出主密钥、就地解密，明文 token 只经内存管道回到本进程。

这不是「破解」：调的是客户端自己注册的接口，跟客户端自己读登录态走同一条路。
只要客户端还能正常登录，这条路就成立；哪天客户端把这个模块的对外接口收了，
它才会失效。密钥全程不落盘、不出子进程。

子命令
------
  check                      看到期天数（**不打印 token**），用于决定要不要续
  export [--out FILE]        导出 {"name","uid","token","refresh_token",...} JSON
  push --url U --password P  直接推送到容器 Web 的「账号」页（同网段时可用）

安全约定
--------
* 默认不落盘；``export`` 不带 ``--out`` 就只打到 stdout，方便你自己接管道。
* ``--out`` 写的文件权限 600，并且会警告「别放到 git 仓库里」。
* ``check`` 只打印到期天数，任何时候都不打印 token。
* 出错信息里不含任何凭据内容。
"""
from __future__ import annotations

import argparse
import base64
import glob
import http.cookiejar
import json
import os
import platform
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request

IS_WIN = platform.system() == "Windows"
IS_MAC = platform.system() == "Darwin"

CRED_WIN = os.path.join(os.environ.get("LOCALAPPDATA", ""), "CodeBuddyExtension",
                        "Data", "Public", "auth", "workbuddy-desktop.info")
CRED_MAC = os.path.expanduser(
    "~/Library/Application Support/CodeBuddyExtension/Data/Public/auth/workbuddy-desktop.info")

# 要取的字段（account.uid 是明文，其余是信封）
FIELDS = ["account.uid", "account.nickname", "account.phoneNumber",
          "auth.accessToken", "auth.refreshToken"]

REASON_CN = {
    "RUNTIME_UNAVAILABLE": "客户端运行时没有所需的原生存储接口（客户端版本不匹配，或不是 WorkBuddy）",
    "KEY_MISMATCH": "凭据与客户端主密钥不匹配（这份凭据不是这台机器上的客户端写下的）",
    "DECRYPT_FAILED": "加密凭据认证失败（凭据不完整，或客户端把加密格式改了）",
    "UNSUPPORTED_ENVELOPE": "不认识的加密信封（suite 不是 1 / sym-v1），需要更新本工具",
    "CRED_UNREADABLE": "读不到登录态文件（客户端没登录，或路径不对）",
    "INVALID_FORMAT": "凭据格式无效",
    "HELPER_PROTOCOL": "凭据助手通信异常",
}

# 跑在客户端 Electron 运行时里的 helper。
# 注意：'WB-AAD\0' 里的 \0 必须是转义序列，AAD 才拼得对（r-string 保证原样传给 JS）。
HELPER_JS = r"""
'use strict';
const crypto = require('crypto');
const fs = require('fs');
const MAXIN = 1 << 20;

const reply = (o) => process.stdout.write(JSON.stringify(o), () => process.exit(o.ok ? 0 : 1));
const fail = (r) => { throw { reason: r }; };
const lp = (s) => {
  const b = Buffer.from(s, 'utf8');
  const L = Buffer.alloc(4); L.writeUInt32BE(b.length);
  return Buffer.concat([L, b]);
};

function readStdin() {
  return new Promise((res, rej) => {
    const c = []; let n = 0;
    process.stdin.on('data', (d) => { n += d.length; if (n > MAXIN) fail('HELPER_PROTOCOL'); c.push(d); });
    process.stdin.on('end', () => res(Buffer.concat(c)));
    process.stdin.on('error', rej);
  });
}

function decryptField(secret, field) {
  if (!field || typeof field !== 'object' || field.$wbEncrypted !== 1) return field;
  let env;
  try { env = JSON.parse(Buffer.from(field.envelope, 'base64').toString('utf8')); }
  catch (e) { fail('INVALID_FORMAT'); }
  if (!env || env.suite !== 1) fail('UNSUPPORTED_ENVELOPE');
  const key = crypto.createHash('sha256').update(secret, 'utf8').digest();
  if (crypto.createHash('sha256').update(key).digest('hex').slice(0, 16) !== env.keyId) fail('KEY_MISMATCH');
  const aad = Buffer.concat([
    Buffer.from('WB-AAD\0', 'ascii'), Buffer.from([1]),
    lp('WBEV1'), lp('sym-v1'), Buffer.from([0, 0, 0, 1]),
    lp(env.keyId), Buffer.from([2, 0, 0])
  ]);
  let pt;
  try {
    const d = crypto.createDecipheriv('aes-256-gcm', key,
      Buffer.from(env.nonce, 'base64'), { authTagLength: 16 });
    d.setAAD(aad);
    d.setAuthTag(Buffer.from(env.authTag, 'base64'));
    pt = Buffer.concat([d.update(Buffer.from(env.ciphertext, 'base64')), d.final()]).toString('utf8');
  } catch (e) { fail('DECRYPT_FAILED'); } finally { key.fill(0); }
  return pt;
}

(async () => {
  try {
    const req = JSON.parse((await readStdin()).toString('utf8') || '{}');
    const storage = process._linkedBinding('electron_browser_workbuddy_storage');
    if (typeof storage.loggerGet !== 'function') fail('RUNTIME_UNAVAILABLE');
    let payload;
    try { payload = JSON.parse(storage.loggerGet()); } catch (e) { fail('RUNTIME_UNAVAILABLE'); }
    if (!payload || payload.version !== 1) fail('RUNTIME_UNAVAILABLE');
    const secret = payload.atRestSecretKey;
    let cred;
    try { cred = JSON.parse(fs.readFileSync(req.credPath, 'utf8')); } catch (e) { fail('CRED_UNREADABLE'); }
    const values = {};
    for (const path of (req.paths || [])) {
      let cur = cred;
      for (const k of path.split('.')) cur = (cur === null || cur === undefined) ? undefined : cur[k];
      if (cur === undefined || cur === null) { values[path] = null; continue; }
      values[path] = decryptField(secret, cur);
    }
    reply({ ok: true, version: 1, electron: process.versions.electron || null, values: values });
  } catch (e) {
    reply({ ok: false, reason: (e && e.reason) || 'HELPER_PROTOCOL' });
  }
})();
"""


# ---------------------------------------------------------------- 运行时定位

def find_runtime():
    """定位 WorkBuddy 可执行文件；可用 WORKBUDDY_EXE 覆盖。"""
    env = (os.environ.get("WORKBUDDY_EXE") or "").strip()
    if env:
        return env if os.path.isfile(env) else None
    if IS_WIN:
        for c in (os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "WorkBuddy", "WorkBuddy.exe"),
                  r"C:\Program Files\WorkBuddy\WorkBuddy.exe",
                  r"C:\Program Files (x86)\WorkBuddy\WorkBuddy.exe"):
            if c and os.path.isfile(c):
                return c
        return _find_runtime_registry()
    if IS_MAC:
        for p in sorted(glob.glob("/Applications/WorkBuddy.app/Contents/MacOS/*")):
            if os.path.isfile(p) and os.access(p, os.X_OK):
                return p
    return None


def _find_runtime_registry():
    """Windows：扫注册表卸载条目里的 InstallLocation（覆盖自定义安装目录）。"""
    try:
        import winreg
    except Exception:
        return None
    roots = [(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Uninstall"),
             (winreg.HKEY_LOCAL_MACHINE, r"Software\Microsoft\Windows\CurrentVersion\Uninstall"),
             (winreg.HKEY_LOCAL_MACHINE, r"Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall")]
    for root, sub in roots:
        try:
            key = winreg.OpenKey(root, sub)
        except OSError:
            continue
        try:
            n = winreg.QueryInfoKey(key)[0]
        except OSError:
            continue
        for i in range(n):
            try:
                sk = winreg.OpenKey(key, winreg.EnumKey(key, i))
                disp = str(winreg.QueryValueEx(sk, "DisplayName")[0])
                if "WorkBuddy" not in disp:
                    continue
                loc = str(winreg.QueryValueEx(sk, "InstallLocation")[0])
                exe = os.path.join(loc, "WorkBuddy.exe")
                if os.path.isfile(exe):
                    return exe
            except OSError:
                continue
    return None


def cred_path():
    p = (os.environ.get("WORKBUDDY_CRED") or "").strip()
    if p:
        return p
    return CRED_MAC if IS_MAC else CRED_WIN


# ---------------------------------------------------------------- 解密

def call_helper(exe, cred, paths, timeout=25):
    """在客户端运行时子进程里解密；明文只走内存管道。"""
    req = json.dumps({"version": 1, "operation": "decrypt",
                      "credPath": cred, "paths": paths}, ensure_ascii=True).encode("ascii")
    env = {k: v for k, v in os.environ.items()
           if not k.upper().startswith(("NODE_", "ELECTRON_", "WORKBUDDY_"))}
    env["ELECTRON_RUN_AS_NODE"] = "1"
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if IS_WIN else 0
    try:
        p = subprocess.run([exe, "-e", HELPER_JS], input=req, stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, env=env, timeout=timeout,
                           creationflags=flags)
    except subprocess.TimeoutExpired:
        raise RuntimeError("凭据助手超时：客户端运行时没有响应")
    except OSError as e:
        raise RuntimeError("启动客户端运行时失败：%s" % e)
    raw = (p.stdout or b"").decode("utf-8", "replace").strip()
    try:
        res = json.loads(raw)
    except Exception:
        tail = (p.stderr or b"").decode("utf-8", "replace").strip()[:200]
        raise RuntimeError("凭据助手返回无法解析%s" % (("：" + tail) if tail else ""))
    if not res.get("ok"):
        r = res.get("reason") or "HELPER_PROTOCOL"
        raise RuntimeError("%s（%s）" % (REASON_CN.get(r, "未知原因"), r))
    return res


def jwt_exp(tok):
    try:
        parts = str(tok).split(".")
        if len(parts) != 3:
            return None
        seg = parts[1] + "=" * (-len(parts[1]) % 4)
        obj = json.loads(base64.urlsafe_b64decode(seg.replace("-", "+").replace("_", "/")).decode("utf-8"))
        exp = obj.get("exp")
        return int(exp) if exp else None
    except Exception:
        return None


def days_left(exp):
    return None if not exp else int(round((exp - time.time()) / 86400.0))


def read_creds(timeout=25):
    """返回 (values, exe, electron_version)。"""
    exe = find_runtime()
    if not exe:
        raise RuntimeError("没找到 WorkBuddy 客户端；用 WORKBUDDY_EXE 指定它的可执行文件")
    cp = cred_path()
    if not os.path.isfile(cp):
        raise RuntimeError("登录态文件不存在：%s（客户端没登录？）" % cp)
    res = call_helper(exe, cp, FIELDS, timeout=timeout)
    return res.get("values") or {}, exe, res.get("electron")


def mask_phone(p):
    return re.sub(r"\d(?=\d{4})", "*", str(p)) if p else ""


# ---------------------------------------------------------------- 子命令

def cmd_check(args):
    vals, exe, el = read_creds()
    at = vals.get("auth.accessToken")
    rt = vals.get("auth.refreshToken")
    nick = vals.get("account.nickname") or "(无昵称)"
    uid = vals.get("account.uid") or "(无 UID)"
    at_d, rt_d = days_left(jwt_exp(at)), days_left(jwt_exp(rt))
    print("客户端   : %s" % exe)
    print("运行时   : Electron %s" % (el or "?"))
    print("账号     : %s" % nick)
    print("UID      : %s" % uid)
    print("手机号   : %s" % (mask_phone(vals.get("account.phoneNumber")) or "—"))
    print("accessToken  : %s" % ("有效，剩 %d 天" % at_d if at_d is not None else "解不出有效期"))
    print("refreshToken : %s" % ("有效，剩 %d 天" % rt_d if rt_d is not None else "解不出有效期"))
    warn = int(os.environ.get("WB_TOKEN_EXPIRE_WARN_DAYS") or 7)
    if at_d is not None and at_d <= warn:
        print("\n⚠️  accessToken 只剩 %d 天（阈值 %d）——该把新 token 填进容器了。"
              % (at_d, warn))
    return 0


def cmd_export(args):
    vals, exe, el = read_creds()
    # 只导出容器真正要用的四个值；手机号不在其中，少一份明文就少一个泄露面
    out = {
        "name": args.name or vals.get("account.nickname") or "本机",
        "uid": vals.get("account.uid"),
        "token": vals.get("auth.accessToken"),
        "refresh_token": vals.get("auth.refreshToken"),
        "at_exp": jwt_exp(vals.get("auth.accessToken")),
        "rt_exp": jwt_exp(vals.get("auth.refreshToken")),
        "exported_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "source_runtime": exe,
    }
    if out["at_exp"]:
        out["at_days_left"] = days_left(out["at_exp"])
    if not args.out:
        print(json.dumps(out, ensure_ascii=False, indent=2))
        print("\n# 上面这个 JSON 可以直接粘贴到容器 Web 的「账号」页；"
              "想落盘加 --out 文件路径（会设 600 权限）。", file=sys.stderr)
        return 0
    fp = os.path.abspath(args.out)
    with open(fp, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    try:
        os.chmod(fp, 0o600)
    except Exception:
        pass
    print("已写出：%s（权限 600）" % fp)
    if os.path.isdir(os.path.join(os.path.dirname(fp), ".git")) or ".git" in fp:
        print("⚠️  这个路径看起来在 git 仓库里 —— 里面是明文凭据，别提交！")
    return 0


def cmd_push(args):
    vals, exe, el = read_creds()
    token = vals.get("auth.accessToken")
    uid = vals.get("account.uid")
    name = args.name or vals.get("account.nickname") or "本机"
    if not token or not uid:
        print("解密结果缺少 token 或 uid，放弃推送。", file=sys.stderr)
        return 1
    url = args.url.rstrip("/")
    cj = http.cookiejar.CookieJar()
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj),
                                     urllib.request.ProxyHandler({}))
    origin = {"Origin": url, "Referer": url + "/login"}
    try:
        data = urllib.parse.urlencode({"password": args.password}).encode()
        req = urllib.request.Request(url + "/login", data=data, headers=dict(
            origin, **{"Content-Type": "application/x-www-form-urlencoded"}))
        op.open(req, timeout=rate(args.timeout))
    except Exception as e:
        print("登录失败：%s" % e, file=sys.stderr)
        return 1
    sess = next((c.value for c in cj if c.name == "wb_sess"), "")
    # CSRF 就是会话 cookie 的中间段（ts.nonce.sig）
    if not sess or sess.count(".") != 2:
        print("登录没拿到会话（密码不对？）", file=sys.stderr)
        return 1
    nonce = sess.split(".")[1]
    try:
        data = urllib.parse.urlencode({"csrf": nonce, "name": name, "uid": uid,
                                       "token": token}).encode()
        req = urllib.request.Request(url + "/save", data=data, headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Origin": url, "Referer": url + "/accounts"})
        body = op.open(req, timeout=rate(args.timeout)).read().decode("utf-8", "replace")
    except Exception as e:
        print("保存失败：%s" % e, file=sys.stderr)
        return 1
    good = 'class="msg ok"' in body
    m = re.search(r'<div class="msg [^"]*">(.*?)</div>', body, re.S)
    tip = re.sub(r"<[^>]+>", "", m.group(1)).strip() if m else ""
    print("%s %s" % ("✅ 已推送：" if good else "❌ 推送返回异常：", tip or "(无提示)"))
    if good:
        print("   账号「%s」UID %s 已更新，accessToken 剩 %s 天。"
              % (name, uid, days_left(jwt_exp(token))))
    return 0 if good else 1


def rate(t):
    return max(3, int(t or 15))


# ---------------------------------------------------------------- 入口

def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="local_token.py",
        description="本机 WorkBuddy 凭据读取/续期（借客户端 Electron 运行时解密，密钥不落盘）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="示例：\n"
               "  python local_token.py check\n"
               "  python local_token.py export\n"
               "  python local_token.py export --out D:/wb_creds.json\n"
               "  python local_token.py push --url http://192.168.2.12:18080 --password 你的密码\n")
    sub = ap.add_subparsers(dest="cmd")

    sub.add_parser("check", help="看到期天数（不打印 token）")

    pe = sub.add_parser("export", help="导出 JSON（默认打到 stdout）")
    pe.add_argument("--out", help="写到文件（600 权限）；不填则只打 stdout")
    pe.add_argument("--name", help="账号名（默认用昵称）")

    pp = sub.add_parser("push", help="推送到容器 Web 的「账号」页")
    pp.add_argument("--url", required=True, help="容器地址，如 http://192.168.2.12:18080")
    pp.add_argument("--password", required=True, help="容器 Web 的 WEB_PASSWORD")
    pp.add_argument("--name", help="账号名（默认用昵称）")
    pp.add_argument("--timeout", type=int, default=15, help="单步超时秒数（默认 15）")

    args = ap.parse_args(argv)
    if not args.cmd:
        ap.print_help()
        return 2
    try:
        if args.cmd == "check":
            return cmd_check(args)
        if args.cmd == "export":
            return cmd_export(args)
        if args.cmd == "push":
            return cmd_push(args)
    except RuntimeError as e:
        print("✗ %s" % e, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    return 2


if __name__ == "__main__":
    sys.exit(main())

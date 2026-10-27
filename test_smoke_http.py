# -*- coding: utf-8 -*-
"""真实 HTTP 冒烟测试：启动服务子进程，验证登录/鉴权/泄露检查。密码不经过 shell。"""
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

PY = sys.executable
HERE = os.path.dirname(os.path.abspath(__file__))
tmp = tempfile.mkdtemp()
PORT = 18899

env = dict(os.environ, DATA_DIR=tmp, PYTHONUNBUFFERED="1")
proc = subprocess.Popen([PY, os.path.join(HERE, "tv_control.py"), "--port", str(PORT)],
                        stdout=open(os.path.join(tmp, "server.log"), "w"),
                        stderr=subprocess.STDOUT, env=env, cwd=HERE)
try:
    # 等服务起来
    base = f"http://127.0.0.1:{PORT}"
    for _ in range(40):
        try:
            urllib.request.urlopen(base + "/login", timeout=1)
            break
        except urllib.error.HTTPError:
            break
        except Exception:
            time.sleep(0.25)
    time.sleep(0.5)

    log = open(os.path.join(tmp, "server.log"), encoding="utf-8").read()
    m = re.search(r"随机管理密码: (\S+)", log)
    assert m, "日志中未找到随机密码: " + log[:300]
    pw = m.group(1)

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **k):
            return None

    def req(path, data=None, cookie=None):
        r = urllib.request.Request(base + path, data=data,
                                   headers={"Content-Type": "application/json",
                                            **({"Cookie": cookie} if cookie else {})})
        opener = urllib.request.build_opener(NoRedirect)
        try:
            resp = opener.open(r, timeout=3)
            return resp.status, dict(resp.headers), resp.read()
        except urllib.error.HTTPError as e:
            return e.code, dict(e.headers), e.read()

    # 1) 未登录主页 302 -> /login
    code, hdrs, _ = req("/")
    assert code == 302 and hdrs.get("Location") == "/login", (code, hdrs.get("Location"))
    # 2) 未登录 API 401
    code, _, _ = req("/status")
    assert code == 401
    # 3) 登录
    code, hdrs, body = req("/login", json.dumps({"password": pw}).encode())
    assert code == 200, (code, body)
    setc = hdrs.get("Set-Cookie", "")
    # SameSite 必须是 Lax：Strict 下 PWA（安装到桌面）跨站启动不携带 cookie，
    # 手机端每次启动都会弹密码框
    assert "HttpOnly" in setc and "SameSite=Lax" in setc
    assert "SameSite=Strict" not in setc
    token = setc.split(";")[0].split("=", 1)[1]
    # 4) 登录后访问
    code, _, body = req("/status", cookie="session=" + token)
    assert code == 200 and b"mode" in body
    code, _, _ = req("/", cookie="session=" + token)
    assert code == 200
    # 5) /config 不泄露认证键
    code, _, body = req("/config", cookie="session=" + token)
    d = json.loads(body)
    assert "auth_password_hash" not in d and "auth_secret" not in d and "auth_salt" not in d
    # 6) 错误密码 401
    code, _, _ = req("/login", json.dumps({"password": "nope"}).encode())
    assert code == 401
    print("SMOKE: 302/401/login/authed/no-leak/401 全部 OK")
finally:
    proc.terminate()

# -*- coding: utf-8 -*-
"""认证功能端到端验证（mock handler，临时目录，不落真实配置）"""
import io
import json
import os
import tempfile

import tv_control as M

tmp = tempfile.mkdtemp()
M.STATE_PATH = os.path.join(tmp, "state.json")
M.CONFIG_PATH = os.path.join(tmp, "config.json")
M._login_fails.clear()
M.CONFIG.update(auth_secret="", auth_salt="", auth_password_hash="")
M._ensure_auth()
M._set_password("testpass123")   # 测试用已知口令

GOOD_PW = "testpass123"
BAD_PW = "x" * 20


class H(M.Handler):
    def __init__(self, body=b"", cookie=None, path="/status", method="GET"):
        self.rfile = io.BytesIO(body)
        self.wfile = io.BytesIO()
        self.headers = {"Content-Type": "application/json", "Content-Length": str(len(body))}
        if cookie:
            self.headers["Cookie"] = cookie
        self.request_version = "HTTP/1.1"
        self.command = method
        self.client_address = ("1.2.3.4", 0)
        self.path = path

    def send_response(self, code, *a):
        self.code = code

    def send_header(self, *a):
        k = a[0]
        v = a[1] if len(a) > 1 else ""
        setattr(self, "_h_" + k.replace("-", "_"), v)

    def end_headers(self):
        pass


# 1) 未登录：页面 302 -> /login；API 401
h = H(path="/")
h.do_GET()
assert h.code == 302 and h._h_Location == "/login", h.code
h = H(path="/status")
h.do_GET()
assert h.code == 401
h = H(body=b"{}", path="/config", method="POST")
h.do_POST()
assert h.code == 401
print("1) unauth: 302->/login, API 401 OK")

# 2) 错误口令 401；正确口令 200 + 安全 Cookie
h = H(json.dumps({"password": "wrong"}).encode(), path="/login", method="POST")
h.do_POST()
assert h.code == 401
h = H(json.dumps({"password": GOOD_PW}).encode(), path="/login", method="POST")
h.do_POST()
assert h.code == 200, h.code
cookie_hdr = getattr(h, "_h_Set_Cookie", "")
token = cookie_hdr.split(";")[0].split("=", 1)[1]
assert token and "HttpOnly" in cookie_hdr and "SameSite=Lax" in cookie_hdr
# SameSite 必须是 Lax：Strict 下 PWA（安装到桌面）跨站启动的导航不携带 cookie，
# 导致手机端每次启动都被判未登录
assert "SameSite=Strict" not in cookie_hdr
print("2) login OK (wrong->401, right->200 + HttpOnly/SameSite=Lax cookie)")

# 3) 登录后 API 正常
h = H(cookie="session=" + token)
h.do_GET()
assert h.code == 200
h = H(cookie="session=" + token, body=json.dumps({"max_watch_min": 25}).encode(),
      path="/config", method="POST")
h.do_POST()
assert h.code == 200 and M.CONFIG["max_watch_min"] == 25
print("3) authed API OK")

# 4) 篡改 token 被拒
h = H(cookie="session=" + token[:-4] + "aaaa", path="/status")
h.do_GET()
assert h.code == 401
print("4) tampered token rejected OK")

# 5) 连续失败 5 次 -> 429 锁定
M._login_fails.clear()
for i in range(5):
    h = H(json.dumps({"password": "bad"}).encode(), path="/login", method="POST")
    h.do_POST()
    assert h.code == 401, (i, h.code)
h = H(json.dumps({"password": "bad"}).encode(), path="/login", method="POST")
h.do_POST()
assert h.code == 429, h.code
print("5) 5 fails -> 429 lock OK")

# 6) 改密码：短口令拒绝；改后旧口令失效
h = H(cookie="session=" + token, body=json.dumps({"new_password": "123"}).encode(),
      path="/set_password", method="POST")
h.do_POST()
assert h.code == 400
h = H(cookie="session=" + token, body=json.dumps({"new_password": "newpass1"}).encode(),
      path="/set_password", method="POST")
h.do_POST()
assert h.code == 200
assert not M._verify_password(BAD_PW) and not M._verify_password(GOOD_PW)
assert M._verify_password("newpass1")
print("6) change password OK (old invalid, short rejected)")

# 7) 登出清除 Cookie（/logout 为 GET）
h = H(cookie="session=" + token, path="/logout", method="GET")
h.do_GET()
assert h.code == 200 and "Max-Age=0" in getattr(h, "_h_Set_Cookie", "")
print("7) logout clears cookie OK")

# 8) 认证键落盘但不通过 GET /config 泄露
saved = json.load(open(M.CONFIG_PATH))
assert "auth_password_hash" in saved and "auth_secret" in saved
h = H(cookie="session=" + token)
h.path = "/config"
h.do_GET()
resp = json.loads(h.wfile.getvalue())
assert "auth_password_hash" not in resp and "auth_secret" not in resp
print("8) auth keys persisted, not leaked via GET /config OK")

# 9) PWA 免验证回归：静态资源公开 + SW 不缓存 /login + 记住本机 token 续期
#    （这些是「手机安装到桌面后每次都弹密码」的根因，必须持续守住）
h = H(path="/sw.js")
h.do_GET()
assert h.code == 200, h.code
sw = h.wfile.getvalue().decode()
assert 'CACHE = "tvtimer-v3"' in sw
assert 'url.pathname === "/login"' not in sw   # 绝不能再缓存登录页
for p in ("/manifest.webmanifest", "/icon.svg"):
    h = H(path=p)
    h.do_GET()
    assert h.code == 200, (p, h.code)          # 未登录也要能取，否则 SW 装不上
print("9) SW/manifest/icon public, /login not cached OK")

# 记住本机：登录返回 token，Bearer 可续期并重发 Lax cookie
M._login_fails.clear()   # 清掉第 5 步「连续失败 5 次」的 IP 锁定，否则本次登录会被 429 拦下
h = H(json.dumps({"password": "newpass1", "remember": True}).encode(),
      path="/login", method="POST")
h.do_POST()
assert h.code == 200, h.code
resp = json.loads(h.wfile.getvalue())
remember_token = resp.get("token")
assert remember_token, "remember 模式应返回 token"
h = H(body=b"", path="/auto_login", method="POST")
h.headers["Authorization"] = "Bearer " + remember_token
h.do_POST()
assert h.code == 200, h.code
assert "SameSite=Lax" in getattr(h, "_h_Set_Cookie", "")
# 伪造 token 仍被拒
h = H(body=b"", path="/auto_login", method="POST")
h.headers["Authorization"] = "Bearer 1.deadbeef"
h.do_POST()
assert h.code == 401, h.code
print("10) remember-me token: issue/renew/forged-reject OK")

print("AUTH CHECKS PASSED")

# -*- coding: utf-8 -*-
"""真实 HTTP 冒烟测试：启动服务子进程，验证「无登录认证 + 简化后的接口」。

覆盖：无登录直访、旧认证/重置休息路由 404、配置入口硬顶 30、非法时段不半更新、
手动调整结余（余额滚存）、PWA 静态资源与 SW 版本、启动日志不泄露密码。
"""
import json
import os
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
log_path = os.path.join(tmp, "server.log")
log_f = open(log_path, "w")
proc = subprocess.Popen([PY, os.path.join(HERE, "tv_control.py"), "--port", str(PORT)],
                        stdout=log_f, stderr=subprocess.STDOUT, env=env, cwd=HERE)
try:
    base = f"http://127.0.0.1:{PORT}"
    for _ in range(40):
        try:
            urllib.request.urlopen(base + "/status", timeout=1)
            break
        except urllib.error.HTTPError:
            break
        except Exception:
            time.sleep(0.25)
    time.sleep(0.3)

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **k):
            return None

    def req(path, data=None):
        r = urllib.request.Request(base + path, data=data,
                                   headers={"Content-Type": "application/json"})
        opener = urllib.request.build_opener(NoRedirect)
        try:
            resp = opener.open(r, timeout=3)
            return resp.status, dict(resp.headers), resp.read()
        except urllib.error.HTTPError as e:
            return e.code, dict(e.headers), e.read()

    # 1) 无登录直接访问主页 / 状态 / 配置
    code, _, body = req("/")
    assert code == 200 and "电视管控" in body.decode("utf-8"), code
    code, _, body = req("/status")
    s = json.loads(body)
    assert code == 200 and "mode" in s, (code, body)
    assert "rest_remaining_sec" not in s, "不应再有强制休息倒计时"
    code, _, body = req("/config")
    c = json.loads(body)
    assert code == 200 and "max_watch_min" in c, c
    for k in ("auth_secret", "auth_salt", "auth_password_hash"):
        assert k not in c, k
    print("1) 无登录直访 / /status /config，快照无倒计时，配置不泄露认证键 OK")

    # 2) 旧登录认证与「重置休息」路由全部 404
    for path in ("/login", "/logout", "/set_password", "/auto_login", "/reset_rest"):
        code, _, _ = req(path)
        assert code == 404, (path, code)
        code, _, _ = req(path, b"{}")
        assert code == 404, (path, code)
    print("2) 旧登录/记住本机/重置休息路由均 404 OK")

    # 3) 配置入口：单次上限 999 -> 30，休息基准 0 -> 1，并落盘
    code, _, body = req("/config", json.dumps({
        "max_watch_min": 999, "rest_min": 0, "weekday_total_min": 45
    }).encode())
    j = json.loads(body)
    assert code == 200 and j["ok"], j
    assert j["config"]["max_watch_min"] == 30, j["config"]
    assert j["config"]["rest_min"] == 1, j["config"]
    assert j["config"]["weekday_total_min"] == 45
    with open(os.path.join(tmp, "tv_control_config.json"), encoding="utf-8") as f:
        saved = json.load(f)
    assert saved["max_watch_min"] == 30 and saved["rest_min"] == 1, saved
    assert not any(k.startswith("auth") for k in saved), saved
    print("3) 单次上限收敛 30 / 休息基准下限 1 / 无认证键落盘 OK")

    # 4) 非法禁用时段返回 400，且不产生「半更新」
    code, _, body = req("/config", json.dumps({
        "tv_ip": "10.0.0.99", "disabled_windows": [["25:99", "07:00"]]
    }).encode())
    assert code == 400, (code, body)
    code, _, body = req("/config")
    assert json.loads(body)["tv_ip"] != "10.0.0.99", "非法时段不应半更新 tv_ip"
    print("4) 非法时段 400 且不半更新 OK")

    # 5) 手动调整结余（远端余额滚存功能保留生效）
    code, _, body = req("/status")
    base_min = json.loads(body)["daily_base_min"]
    code, _, body = req("/carry", json.dumps({"set_min": 20}).encode())
    j = json.loads(body)
    assert code == 200 and j["ok"] and j["carry_min"] == 20, j
    assert abs(j["daily_cap_min"] - (base_min + 20)) < 1e-6, (base_min, j)
    print("5) /carry 手动调整结余，今日额度 = 基准 + 结余 OK")

    # 6) PWA 静态资源公开 + SW 缓存版本升级
    for p in ("/manifest.webmanifest", "/icon.svg", "/sw.js"):
        code, _, _ = req(p)
        assert code == 200, (p, code)
    code, _, body = req("/sw.js")
    assert 'CACHE = "tvtimer-v5"' in body.decode(), "SW 缓存版本应升级"
    print("6) PWA 静态资源公开 + SW tvtimer-v5 OK")

    # 7) 启动日志不再生成/打印随机密码
    with open(log_path, encoding="utf-8") as f:
        log = f.read()
    assert "随机管理密码" not in log and "[认证]" not in log, log[:300]
    assert "无登录认证" in log, log[:300]
    print("7) 启动日志无密码、明确标注无登录认证 OK")

    print("SMOKE ALL PASSED")
finally:
    proc.terminate()
    log_f.close()

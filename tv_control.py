#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
小米电视 管控脚本 (家长控制 / 观看时限) + 配置网页服务
========================================================
功能:
  1) 禁用时段：多个时间段，时段内电视若开机则自动关机（发现开机即关）。
  2) 净观看时长按「观看−休息抵扣」累计：净已看 = 累计观看 − 累计休息×抵扣比例。
     抵扣比例 = 单次最长观看 / 单次休息（休息满「单次休息」分钟即抵扣回满额度）。
     例（默认30/40）：看20休10 -> 净已看 20−10*0.75=12.5 分钟；若二者相等则 1:1（20−10=10）。
     跨「关一下再开」不会清零，因此「看20→关1→又看」无法凭空多放时间（防卡 BUG）。
  3) 强制休息时长：强制关机后须休息指定时长（如 40 分钟）才能再开机，
     休息期间被发现开机同样自动关机。

网页:
  脚本同时提供 HTTP 服务，浏览器打开 http://<本机IP>:<port>/ 即可：
    - 可视化设置以上参数（手机适配）
    - 实时查看电视状态（禁用中 / 观看剩余 / 休息剩余）
  配置保存在同目录 tv_control_config.json，重启后自动加载。

配置（顶部 CONFIG 为默认值；网页保存后写入 json 覆盖）：
  - tv_ip            电视局域网 IP
  - max_watch_min    单次最长观看（分钟）
  - rest_min         强制休息（分钟）
  - disabled_windows 禁用时段列表，每项 ["开始HH:MM","结束HH:MM"]（支持跨午夜）
  - poll_interval    轮询间隔（秒）

部署（常开机器，如 NAS）:
    python tv_control.py
    python tv_control.py --port 9090
    # 或容器（镜像见 Dockerfile / GitHub Actions 自动推送到 ghcr.io）:
    #   docker run -d --restart=unless-stopped -p 8080:8080 \
    #     -v $PWD/data:/app/data ghcr.io/<你的用户名>/tv-control:latest
    # 配置网页: http://<本机IP>:8080/
"""

import argparse
import json
import os
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

# ============================ 默认配置 ============================
CONFIG = {
    "tv_ip": "192.168.80.181",
    "tv_port": 6095,
    "poll_interval": 5,
    "max_watch_min": 30,
    "rest_min": 40,
    "off_cooldown_sec": 10,
    "disabled_windows": [
        ["23:00", "07:00"],
        # ["12:00", "13:00"],
    ],
    "http_host": "0.0.0.0",
    "http_port": 8080,
}
# ================================================================

# 配置落盘目录：容器场景可用环境变量 DATA_DIR 指向挂载卷（如 /app/data），实现配置持久化
DATA_DIR = os.environ.get("DATA_DIR", os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(DATA_DIR, "tv_control_config.json")
HTML_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tv_control.html")
config_lock = threading.Lock()

ALLOWED_KEYS = {"tv_ip", "tv_port", "poll_interval", "max_watch_min", "rest_min", "disabled_windows"}


def load_config():
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            with config_lock:
                CONFIG.update({k: v for k, v in data.items() if k in ALLOWED_KEYS})
            print(f"[config] 已从 {CONFIG_PATH} 加载配置")
        except Exception as e:
            print(f"[config] 读取 {CONFIG_PATH} 失败，使用默认: {e}")


def save_config():
    with config_lock:
        data = {k: CONFIG[k] for k in ALLOWED_KEYS}
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        return True
    except Exception as e:
        print(f"[config] 保存失败: {e}")
        return False


def refresh_urls():
    globals()["ISALIVE_URL"] = f"http://{CONFIG['tv_ip']}:{CONFIG['tv_port']}/request?action=isalive"
    globals()["POWER_URL"] = f"http://{CONFIG['tv_ip']}:{CONFIG['tv_port']}/controller?action=keyevent&keycode=power"


ISALIVE_URL = f"http://{CONFIG['tv_ip']}:{CONFIG['tv_port']}/request?action=isalive"
POWER_URL = f"http://{CONFIG['tv_ip']}:{CONFIG['tv_port']}/controller?action=keyevent&keycode=power"


# ============================ 工具函数 ============================
def _http_get(url):
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=3) as resp:
            return True, resp.read().decode("utf-8", "ignore")
    except Exception:
        return False, None


def tv_is_alive():
    ok, body = _http_get(ISALIVE_URL)
    if not ok or not body:
        return False
    try:
        return json.loads(body).get("status", -1) == 0
    except Exception:
        return False


def power_off():
    if tv_is_alive():
        _http_get(POWER_URL)
        return True
    return False


def hhmm_to_min(s):
    h, m = str(s).split(":")
    return int(h) * 60 + int(m)


def in_disabled_window(now_ts):
    lt = time.localtime(now_ts)
    now_min = lt.tm_hour * 60 + lt.tm_min
    with config_lock:
        windows = [w for w in CONFIG["disabled_windows"]]
    for w in windows:
        try:
            s = hhmm_to_min(w[0])
            e = hhmm_to_min(w[1])
        except Exception:
            continue
        if s <= e:
            if s <= now_min < e:
                return True
        else:
            if now_min >= s or now_min < e:
                return True
    return False


# ============================ 状态机 ============================
class TvGuard:
    """
    观看时长「按净时长累计」模型（防卡 BUG + 休息按比例抵扣）：
      - watched_gross_min : 今日累计观看分钟（毛，只增不减，跨天重置）
      - rest_credit_min  : 今日累计「休息抵扣」分钟 = 离线时长 × 抵扣比例
      - 抵扣比例 RATIO = max_watch_min / rest_min
          （休息 rest_min 分钟，抵扣 max_watch_min 分钟观看；即「完整休息一次 = 额度回满」）
      - 净已看 net = clamp(watched_gross_min - rest_credit_min, 0, max_watch_min)
      - 电视开机时，已看就按 net 计；net 达到上限即强制关机。
      - 例（默认 30/40，RATIO=0.75）：看 20 分钟、休息 10 分钟 -> 净已看 20-10*0.75=12.5 分钟，
        再开即视为已看 12.5 分钟。（若 max_watch==rest，则 1:1，即 20-10=10）
      - 防卡 BUG：看 20 分钟、关 1 分钟、又看，关的 1 分钟只抵扣 0.75 分钟，无法凭空多放时间。
      - 禁用时段内强制关机，且禁用时段不计休息抵扣（属惩罚，不算恢复）。
      - 每天本地零点重置（day_key 变更）。
    """

    OFFLINE_CONFIRM = 2           # 连续离线达到此次数才正式计为「离线/休息」（抗网络抖动）

    def __init__(self):
        self.watched_gross_min = 0.0   # 今日累计观看（毛）
        self.rest_credit_min = 0.0     # 今日累计休息抵扣
        self.last_on_ts = None        # 上一次确认电视在线且可观看的时间戳（累加观看用）
        self.last_off_ts = None       # 上一次确认电视离线的时间戳（累加抵扣用）
        self.cooldown_until = 0.0     # 关机指令冷却（避免反复发 power 触发开关抖动）
        self.offline_count = 0
        self.cycle_day_key = time.strftime("%Y-%m-%d")
        self.lock = threading.Lock()
        self.last_action = "idle"

    @staticmethod
    def _ratio():
        rest = CONFIG["rest_min"]
        return (CONFIG["max_watch_min"] / rest) if rest > 0 else 0.0

    def _net(self):
        mx = CONFIG["max_watch_min"]
        return max(0.0, min(mx, self.watched_gross_min - self.rest_credit_min))

    def _try_off(self, reason):
        now = time.time()
        if now < self.cooldown_until:
            return
        if power_off():
            self.cooldown_until = now + CONFIG["off_cooldown_sec"]
            self.last_action = reason
            print(f"[{logts()}] 关机 -> {reason}")

    def tick(self):
        now = time.time()
        disabled = in_disabled_window(now)
        alive = tv_is_alive()

        # 跨天：重置累计
        day_key = time.strftime("%Y-%m-%d")
        if day_key != self.cycle_day_key:
            self.cycle_day_key = day_key
            self.watched_gross_min = 0.0
            self.rest_credit_min = 0.0
            self.last_on_ts = None
            self.last_off_ts = None
            print(f"[{logts()}] 跨天，观看/休息累计已重置")

        with self.lock:
            if disabled:
                if alive:
                    self._try_off("禁用时段(电视不应开机)")
                self.last_on_ts = None
                self.last_off_ts = None   # 禁用时段不计休息抵扣
                return

            if alive:
                self.offline_count = 0
                # 先累加本次观看时长，再判定净已看是否达上限（避免多放一拍）
                if self.last_on_ts is None:
                    self.last_on_ts = now
                else:
                    delta = (now - self.last_on_ts) / 60.0
                    if delta > 0:
                        self.watched_gross_min += delta
                    self.last_on_ts = now
                if self._net() >= CONFIG["max_watch_min"]:
                    # 净已看已达上限：强制关机，须靠休息抵扣降下来才能再开
                    self._try_off(f"净观看已达 {CONFIG['max_watch_min']} 分钟")
                    self.last_on_ts = None
                    return
                self.last_off_ts = None
            else:
                # 离线 = 休息，按比例累积抵扣（需连续多次确认，抗抖动）
                self.offline_count += 1
                if self.offline_count >= self.OFFLINE_CONFIRM:
                    if self.last_off_ts is None:
                        self.last_off_ts = now
                    else:
                        delta = (now - self.last_off_ts) / 60.0
                        if delta > 0:
                            self.rest_credit_min += delta * self._ratio()
                        self.last_off_ts = now
                    # 休息抵扣不能超过已观看量：否则长期离线会把净已看一直压在 0，
                    # 导致恢复观看时「净已看/还可看」长时间不动（看似不计时）。
                    if self.rest_credit_min > self.watched_gross_min:
                        self.rest_credit_min = self.watched_gross_min
                    self.last_on_ts = None

    def snapshot(self):
        now = time.time()
        disabled = in_disabled_window(now)
        with self.lock:
            net = self._net()
            mx = CONFIG["max_watch_min"]
            ratio = self._ratio()
            # 还需休息多久才能再开机（net 已到上限时才有意义）
            if net >= mx and ratio > 0:
                need_rest_min = max(0.0, (net - mx + 0.01) / ratio)
            else:
                need_rest_min = 0.0
            if disabled:
                mode = "disabled"
            elif self.last_on_ts is not None:
                mode = "watching"
            elif net > 0:
                mode = "resting"      # 离线且在恢复抵扣
            else:
                mode = "idle"
            remaining = max(0.0, mx - net)
        return {
            "tv_ip": CONFIG["tv_ip"],
            "disabled_now": disabled,
            "mode": mode,
            "net_watched_min": round(net, 1),
            "watched_gross_min": round(self.watched_gross_min, 1),
            "rest_credit_min": round(self.rest_credit_min, 1),
            "ratio": round(ratio, 3),
            "cycle_remaining_min": round(remaining, 1),
            "watch_remaining_sec": int(remaining * 60),
            "rest_remaining_sec": int(need_rest_min * 60),
            "max_watch_min": CONFIG["max_watch_min"],
            "rest_min": CONFIG["rest_min"],
            "disabled_windows": CONFIG["disabled_windows"],
            "last_action": self.last_action,
        }


def logts():
    return time.strftime("%Y-%m-%d %H:%M:%S")


guard = TvGuard()


# ============================ HTTP 服务 ============================
class Handler(BaseHTTPRequestHandler):
    def _send_json(self, obj, code=200):
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(obj, ensure_ascii=False).encode("utf-8"))

    def _send_html(self):
        try:
            with open(HTML_PATH, "r", encoding="utf-8") as f:
                html = f.read()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(html.encode("utf-8"))
        except Exception:
            self._send_json({"error": "tv_control.html not found"}, 404)

    def do_GET(self):
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            self._send_html()
        elif path == "/status":
            self._send_json(guard.snapshot())
        elif path == "/config":
            with config_lock:
                self._send_json({k: CONFIG[k] for k in ALLOWED_KEYS})
        else:
            self._send_json({"error": "unknown path"}, 404)

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/config":
            try:
                length = int(self.headers.get("Content-Length", 0))
                raw = self.rfile.read(length) if length else b"{}"
                data = json.loads(raw.decode("utf-8"))
            except Exception as e:
                self._send_json({"ok": False, "error": f"bad body: {e}"}, 400)
                return

            errs = []
            with config_lock:
                if "tv_ip" in data:
                    CONFIG["tv_ip"] = str(data["tv_ip"]).strip()
                if "tv_port" in data:
                    CONFIG["tv_port"] = int(data["tv_port"])
                if "poll_interval" in data:
                    CONFIG["poll_interval"] = max(1, int(data["poll_interval"]))
                if "max_watch_min" in data:
                    CONFIG["max_watch_min"] = max(1, int(data["max_watch_min"]))
                if "rest_min" in data:
                    CONFIG["rest_min"] = max(0, int(data["rest_min"]))
                if "disabled_windows" in data:
                    wins = []
                    for w in data["disabled_windows"]:
                        if isinstance(w, (list, tuple)) and len(w) == 2:
                            hhmm_to_min(w[0]); hhmm_to_min(w[1])  # 校验格式
                            wins.append([str(w[0]), str(w[1])])
                        else:
                            errs.append(f"invalid window: {w}")
                    if not errs:
                        CONFIG["disabled_windows"] = wins

            if errs:
                self._send_json({"ok": False, "error": "; ".join(errs)}, 400)
                return

            refresh_urls()
            ok = save_config()
            self._send_json({"ok": ok, "config": {k: CONFIG[k] for k in ALLOWED_KEYS}})
        elif path == "/power_off":
            # 手动关闭电视：确认在线后发 power 指令；仅关电视，不计入休息/统计逻辑
            try:
                if power_off():
                    self._send_json({"ok": True, "message": "已发送关机指令"})
                else:
                    self._send_json({"ok": False, "message": "电视当前不在线或未响应"}, 400)
            except Exception as e:
                self._send_json({"ok": False, "error": str(e)}, 500)
        else:
            self._send_json({"error": "unknown path"}, 404)

    def log_message(self, *a):
        pass


def monitor_loop():
    print(f"[{logts()}] 监控启动: TV={CONFIG['tv_ip']}:{CONFIG['tv_port']} "
          f"最长观看={CONFIG['max_watch_min']}min 休息={CONFIG['rest_min']}min "
          f"禁用时段={CONFIG['disabled_windows']} 轮询={CONFIG['poll_interval']}s")
    while True:
        try:
            guard.tick()
        except Exception as e:
            print(f"[{logts()}] tick error: {e}")
        time.sleep(CONFIG["poll_interval"])


def main():
    load_config()
    refresh_urls()
    ap = argparse.ArgumentParser(description="小米电视管控脚本 + 配置网页")
    ap.add_argument("--host", default=CONFIG["http_host"])
    ap.add_argument("--port", type=int, default=CONFIG["http_port"])
    args = ap.parse_args()

    threading.Thread(target=monitor_loop, daemon=True).start()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"[{logts()}] 网页/接口: http://{args.host}:{args.port}/  (手机浏览器打开即可)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print(f"\n[{logts()}] 已停止")


if __name__ == "__main__":
    main()

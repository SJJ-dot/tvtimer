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
     休息期间被发现开机同样自动关机。可手动「重置休息」立即清零抵扣、立即可看。
  4) 每日总时长：工作日/休息日分别设「每天可看总时长（分钟）」，只按实际观看时间计
     （与开关机次数无关），达额度当天封禁，次日自动恢复；0 = 不限制。
     工作日/休息日按法定节假日判定（holiday-cn 数据，每月自动更新，离线回退星期几）。

网页:
  脚本同时提供 HTTP 服务，浏览器打开 http://<本机IP>:<port>/ 即可：
    - 可视化设置以上参数（手机适配）
    - 实时查看电视状态（禁用中 / 观看剩余 / 休息剩余 / 今日已看时长）
    - 支持「安装到桌面」（PWA：manifest + service worker）
  配置保存在同目录 tv_control_config.json（容器用 DATA_DIR 指向挂载卷），重启后自动加载。

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
    "weekday_total_min": 60,   # 工作日（周一~五）每天可看总时长（毛分钟）；0 = 不限制
    "weekend_total_min": 120,  # 周末（六、日）每天可看总时长（毛分钟）；0 = 不限制
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

ALLOWED_KEYS = {"tv_ip", "tv_port", "poll_interval", "max_watch_min", "rest_min",
                "weekday_total_min", "weekend_total_min", "disabled_windows"}


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
    h, m = int(h), int(m)
    # 严格校验范围，拒绝 "25:99" 之类静默失效的垃圾时段
    if not (0 <= h <= 24) or not (0 <= m <= 59) or h * 60 + m > 1440:
        raise ValueError(f"invalid time: {s!r}")
    return h * 60 + m


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


def is_weekday(now_ts):
    """今天是否按「工作日」额度计（法定节假日/调休感知）。

    数据源：holiday-cn（github.com/NateScarlet/holiday-cn，国务院放假安排整理），
    每月拉取一次缓存到 DATA_DIR/holiday_cn_<年>.json；拉取失败用旧缓存；
    无任何数据时回退为「周一~周五=工作日」的星期几判断。
    """
    d = time.strftime("%Y-%m-%d", time.localtime(now_ts))
    with _holiday_lock:
        off = _holiday_offdays.get(d)
    if off is not None:
        return not off          # 法定假日(OffDay)=休息日额度；调休补班的周末=工作日额度
    return time.localtime(now_ts).tm_wday < 5


# ---------- holiday-cn 节假日数据：每月拉取 + 本地缓存 ----------
HOLIDAY_REFRESH_SEC = 30 * 86400   # 每 30 天拉取一次
_holiday_lock = threading.Lock()
_holiday_offdays = {}              # "YYYY-MM-DD" -> True(休息日) / False(调休补班日)；普通日期不在表内
_holiday_last_fetch = 0.0


def _holiday_load_file(year):
    """把已下载的年度 JSON 读进内存表，返回是否成功。"""
    path = os.path.join(DATA_DIR, f"holiday_cn_{year}.json")
    if not os.path.exists(path):
        return False
    try:
        with open(path, "r", encoding="utf-8") as f:
            days = json.load(f)["days"]
        with _holiday_lock:
            for it in days:
                # isOffDay=True 休息日；False 调休补班日
                _holiday_offdays[it["date"]] = bool(it["isOffDay"])
        return True
    except Exception as e:
        print(f"[holiday] 读取 {path} 失败: {e}")
        return False


def _holiday_refresh():
    """按需拉取当年（12 月顺带预取次年）节假日数据，成功后写缓存文件。"""
    global _holiday_last_fetch
    year = time.localtime().tm_year
    years = [year] + ([year + 1] if time.localtime().tm_mon == 12 else [])
    got_any = False
    for y in years:
        if _holiday_load_file(y) and os.path.getmtime(os.path.join(DATA_DIR, f"holiday_cn_{y}.json")) > time.time() - HOLIDAY_REFRESH_SEC:
            got_any = True          # 本地缓存足够新，不联网
            continue
        url = f"https://raw.githubusercontent.com/NateScarlet/holiday-cn/master/{y}.json"
        ok, body = _http_get(url)
        if ok and body:
            try:
                days = json.loads(body)["days"]
                path = os.path.join(DATA_DIR, f"holiday_cn_{y}.json")
                with open(path, "w", encoding="utf-8") as f:
                    json.dump({"days": days}, f, ensure_ascii=False)
                _holiday_load_file(y)
                got_any = True
                print(f"[holiday] 已更新 {y} 年节假日数据 ({len(days)} 天)")
            except Exception as e:
                print(f"[holiday] 解析/保存失败 ({url}): {e}")
        else:
            print(f"[holiday] 拉取失败 ({url})，沿用现有数据")
    if got_any:
        _holiday_last_fetch = time.time()


def holiday_refresh_loop():
    # 启动先刷一次（有新缓存就秒回），之后每小时检查是否到 30 天刷新点
    _holiday_refresh()
    while True:
        time.sleep(3600)
        if time.time() - _holiday_last_fetch >= HOLIDAY_REFRESH_SEC:
            _holiday_refresh()


# ============================ 状态机 ============================
class TvGuard:
    """
    观看时长「按净时长累计 + 每日总时长」模型：
      - watched_gross_min : 今日累计观看分钟（毛，只增不减，跨天重置）
      - rest_credit_min  : 今日累计「休息抵扣」分钟 = 离线时长 × 抵扣比例
      - 抵扣比例 RATIO = max_watch_min / rest_min
          （休息 rest_min 分钟，抵扣 max_watch_min 分钟观看；即「完整休息一次 = 额度回满」）
      - 净已看 net = clamp(watched_gross_min - rest_credit_min, 0, max_watch_min)
      - 电视开机时，已看就按 net 计；net 达到上限即强制关机并进入休息。
      - 防卡 BUG：看 20 分钟、关 1 分钟、又看，关的 1 分钟只抵扣 0.75 分钟，无法凭空多放时间。
      - 每日总时长：工作日 weekday_total_min / 休息日 weekend_total_min（毛分钟，0=不限）。
        只按实际观看时间计，与开关机次数无关（网络抖动/探测误判不会误扣额度）。
        达额度当天封禁（total_blocked），次日重置。
      - 禁用时段内强制关机，且禁用时段不计休息抵扣（属惩罚，不算恢复）。
      - 「重置休息」：把休息抵扣顶满（net→0）、清掉强制休息闸门，立即可看；
        不解除「当日总时长封禁」（当天额度是硬上限，次日自动恢复）。
      - 每天本地零点重置（day_key 变更）。
    """

    OFFLINE_CONFIRM = 2           # 连续离线达到此次数才正式计为「离线/休息」（抗网络抖动）
    MIN_SESSION_GAP = 30.0        # 两次开机间隔小于此秒数视为「同一片段短暂中断」，不重新起算

    def _min_session_gap(self):
        # 轮询间隔较大时按 3 倍轮询间隔放宽，避免短暂开关机被误判为新片段
        return max(self.MIN_SESSION_GAP, 3.0 * CONFIG["poll_interval"])

    def __init__(self):
        self.watched_gross_min = 0.0   # 今日累计观看（毛）
        self.rest_credit_min = 0.0     # 今日累计休息抵扣
        self.total_blocked = False     # 今日总时长已用完（重置不解除，次日自动恢复）
        self.last_on_ts = None        # 上一次确认电视在线且可观看的时间戳（累加观看用）
        self.last_view_end_ts = 0.0    # 上一次观看结束（电视离线）时间戳，用于判定是否为新一次
        self.last_off_ts = None       # 上一次确认电视离线的时间戳（累加抵扣用）
        self.rest_until = 0.0         # 强制休息截止时间戳
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

    def _daily_cap(self, now_ts):
        # 当日可看总时长（毛分钟）：工作日/休息日分别设置（法定节假日/调休感知，见 is_weekday）；0 = 不限
        cap = CONFIG["weekday_total_min"] if is_weekday(now_ts) else CONFIG["weekend_total_min"]
        return cap if cap > 0 else 0

    def _try_off(self, reason):
        now = time.time()
        if now < self.cooldown_until:
            return
        if power_off():
            self.cooldown_until = now + CONFIG["off_cooldown_sec"]
            self.last_action = reason
            print(f"[{logts()}] 关机 -> {reason}")

    def reset_rest(self):
        # 把休息抵扣顶满 -> 净已看归零；清掉强制休息闸门，立即可看。
        # 不解除「当日总时长封禁」（total_blocked）：当天额度是硬上限，次日自动恢复。
        with self.lock:
            self.rest_credit_min = self.watched_gross_min
            self.rest_until = 0.0
            self.last_on_ts = None
            self.last_view_end_ts = 0.0
            self.last_off_ts = None
            self.offline_count = 0
            self.last_action = "手动重置(休息)"
        print(f"[{logts()}] 已重置（净已看归零，可立即可看；当日总时长仍累计）")

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
            self.total_blocked = False
            self.last_on_ts = None
            self.last_view_end_ts = 0.0
            self.last_off_ts = None
            self.rest_until = 0.0
            self.offline_count = 0
            print(f"[{logts()}] 跨天，今日观看/总时长已重置")

        with self.lock:
            if disabled:
                if alive:
                    self._try_off("禁用时段(电视不应开机)")
                self.last_on_ts = None
                self.last_off_ts = None   # 禁用时段不计休息抵扣
                return

            if alive:
                self.offline_count = 0
                # 强制休息未到 -> 仍关机，不开始新观看
                if now < self.rest_until:
                    self._try_off("强制休息中(未到休息时间)")
                    self.last_on_ts = None
                    return

                starting = self.last_on_ts is None
                if starting:
                    is_new = (self.last_view_end_ts == 0.0) or (now - self.last_view_end_ts >= self._min_session_gap())
                    if is_new:
                        # 当日总时长检查（先于净已看检查，额度是更严的上限）：
                        # 毛观看达上限 -> 当天封禁（只按时间计，与开关机次数无关）
                        cap = self._daily_cap(now)
                        if cap > 0 and self.watched_gross_min >= cap:
                            self.total_blocked = True
                            self._try_off(f"今日{cap}分钟已看完")
                            self.last_on_ts = None
                            return
                        # 净已看仍顶格（如强制休息期间电视一直没真正关掉、抵扣为零）
                        # -> 直接再关并重置休息（避免秒关体验）
                        if self._net() >= CONFIG["max_watch_min"]:
                            self._try_off("净观看仍满(未恢复)")
                            self.rest_until = now + CONFIG["rest_min"] * 60
                            self.last_on_ts = None
                            return
                        print(f"[{logts()}] 开始观看 (今日额度 {self._daily_cap(now) or '不限'} 分钟)")
                        self.rest_until = 0.0
                        self.last_on_ts = now
                    else:
                        # 同一观看片段的短暂中断（<最小间隔），继续累计
                        self.rest_until = 0.0
                        self.last_on_ts = now
                else:
                    # 累加本次观看时长
                    delta = (now - self.last_on_ts) / 60.0
                    if delta > 0:
                        self.watched_gross_min += delta
                    self.last_on_ts = now

                # 当日总时长达上限 -> 关且今日封禁（先于净已看检查）
                cap = self._daily_cap(now)
                if cap > 0 and self.watched_gross_min >= cap:
                    self.total_blocked = True
                    self._try_off(f"今日{cap}分钟已看完")
                    self.last_on_ts = None
                    return
                # 本次净已看达上限 -> 强制关机并进入休息
                if self._net() >= CONFIG["max_watch_min"]:
                    self._try_off(f"净观看已达 {CONFIG['max_watch_min']} 分钟")
                    self.rest_until = now + CONFIG["rest_min"] * 60
                    self.last_on_ts = None
                    return
                self.last_off_ts = None
            else:
                # 离线 = 休息，按比例累积抵扣（需连续多次确认，抗抖动）
                self.offline_count += 1
                if self.offline_count == 1:
                    # 记录实际离线起点（第一次探测失败时刻，而非确认时刻），
                    # 让「新片段」间隔判定更贴近真实开关机时间
                    self.last_view_end_ts = now
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
            # 还需休息多久（net 已到上限时按抵扣比例反推：净已看/比例=回满所需分钟；rest_until 闸门优先）
            if now < self.rest_until:
                rest_rem = self.rest_until - now
            elif net >= mx and ratio > 0:
                rest_rem = net / ratio * 60
            else:
                rest_rem = 0.0
            if disabled:
                mode = "disabled"
            elif self.total_blocked:
                mode = "blocked"
            elif now < self.rest_until:
                mode = "resting"
            elif self.last_on_ts is not None:
                mode = "watching"
            elif net > 0:
                mode = "resting"      # 离线且在恢复抵扣
            else:
                mode = "idle"
            daily_cap = self._daily_cap(now)
            remaining_cycle = max(0.0, mx - net)
        return {
            "tv_ip": CONFIG["tv_ip"],
            "disabled_now": disabled,
            "workday_today": is_weekday(now),
            "mode": mode,
            "net_watched_min": round(net, 1),
            "watched_gross_min": round(self.watched_gross_min, 1),
            "rest_credit_min": round(self.rest_credit_min, 1),
            "ratio": round(ratio, 3),
            "cycle_remaining_min": round(remaining_cycle, 1),
            "watch_remaining_sec": int(remaining_cycle * 60),
            "rest_remaining_sec": int(rest_rem),
            "max_watch_min": CONFIG["max_watch_min"],
            "rest_min": CONFIG["rest_min"],
            "disabled_windows": CONFIG["disabled_windows"],
            "last_action": self.last_action,
            "daily_cap_min": daily_cap,
            "daily_gross_min": round(self.watched_gross_min, 1),
            "daily_remaining_min": (round(max(0.0, daily_cap - self.watched_gross_min), 1)
                                    if daily_cap > 0 else None),
            "daily_blocked": self.total_blocked,
        }


def logts():
    return time.strftime("%Y-%m-%d %H:%M:%S")


guard = TvGuard()


# ============================ PWA 资源 ============================
MANIFEST = {
    "name": "电视管控",
    "short_name": "电视管控",
    "description": "小米电视家长控制：观看时长、禁用时段、每日总时长",
    "start_url": "/",
    "scope": "/",
    "display": "standalone",
    "background_color": "#f5f7fa",
    "theme_color": "#1f6feb",
    "icons": [
        {"src": "/icon.svg", "sizes": "any", "type": "image/svg+xml", "purpose": "any"},
        {"src": "/icon.svg", "sizes": "any", "type": "image/svg+xml", "purpose": "maskable"},
    ],
}

SW_JS = """\
const CACHE = "tvtimer-v1";
const SHELL = ["/", "/tv_control.html", "/manifest.webmanifest", "/icon.svg"];
self.addEventListener("install", e => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(SHELL).catch(() => {})).then(() => self.skipWaiting()));
});
self.addEventListener("activate", e => {
  e.waitUntil(caches.keys().then(ks => Promise.all(ks.filter(k => k !== CACHE).map(k => caches.delete(k)))).then(() => self.clients.claim()));
});
self.addEventListener("fetch", e => {
  const url = new URL(e.request.url);
  if (url.origin !== self.location.origin) return;
  // 实时数据走网络，不参与缓存
  if (url.pathname === "/status" || url.pathname === "/config") return;
  e.respondWith(
    caches.match(e.request).then(r =>
      r || fetch(e.request).then(resp => {
        const cp = resp.clone();
        caches.open(CACHE).then(c => c.put(e.request, cp));
        return resp;
      }).catch(() => r)
    )
  );
});
"""

ICON_SVG = """\
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512">
  <rect width="512" height="512" rx="96" fill="#1f6feb"/>
  <g fill="none" stroke="#ffffff" stroke-width="26" stroke-linecap="round" stroke-linejoin="round">
    <rect x="138" y="186" width="236" height="150" rx="20"/>
    <path d="M214 350 h84"/>
    <path d="M256 332 v18"/>
    <path d="M196 120 a60 60 0 0 1 120 0"/>
    <path d="M256 120 v44"/>
  </g>
</svg>
"""


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
        elif path == "/manifest.webmanifest":
            self.send_response(200)
            self.send_header("Content-Type", "application/manifest+json; charset=utf-8")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(json.dumps(MANIFEST, ensure_ascii=False).encode("utf-8"))
        elif path == "/sw.js":
            self.send_response(200)
            self.send_header("Content-Type", "application/javascript; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(SW_JS.encode("utf-8"))
        elif path == "/icon.svg":
            self.send_response(200)
            self.send_header("Content-Type", "image/svg+xml; charset=utf-8")
            self.end_headers()
            self.wfile.write(ICON_SVG.encode("utf-8"))
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

            # 先完整解析+校验到 vals，全部通过后才统一应用，避免「半更新」（部分字段生效但不落盘）
            errs = []
            vals = {}
            try:
                if "tv_ip" in data:
                    vals["tv_ip"] = str(data["tv_ip"]).strip()
                if "tv_port" in data:
                    vals["tv_port"] = int(data["tv_port"])
                if "poll_interval" in data:
                    vals["poll_interval"] = max(1, int(data["poll_interval"]))
                if "max_watch_min" in data:
                    vals["max_watch_min"] = max(1, int(data["max_watch_min"]))
                if "rest_min" in data:
                    vals["rest_min"] = max(0, int(data["rest_min"]))
                if "weekday_total_min" in data:
                    vals["weekday_total_min"] = max(0, int(data["weekday_total_min"]))
                if "weekend_total_min" in data:
                    vals["weekend_total_min"] = max(0, int(data["weekend_total_min"]))
                if "disabled_windows" in data:
                    wins = []
                    for w in data["disabled_windows"]:
                        if isinstance(w, (list, tuple)) and len(w) == 2:
                            s_min = hhmm_to_min(w[0])   # 格式/范围非法会抛异常
                            e_min = hhmm_to_min(w[1])
                            if s_min == e_min:
                                raise ValueError(f"时段起止相同(不会生效): {w[0]}")
                            wins.append([str(w[0]), str(w[1])])
                        else:
                            errs.append(f"invalid window: {w}")
                    vals["disabled_windows"] = wins
            except (ValueError, TypeError) as e:
                self._send_json({"ok": False, "error": f"invalid value: {e}"}, 400)
                return

            if errs:
                self._send_json({"ok": False, "error": "; ".join(errs)}, 400)
                return

            with config_lock:
                CONFIG.update(vals)

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
        elif path == "/reset_rest":
            # 手动重置休息：净已看归零、清强制休息闸门，立即可看；不解除当日总时长封禁
            guard.reset_rest()
            self._send_json({"ok": True, "message": "已重置休息，可立即可看"})
        else:
            self._send_json({"error": "unknown path"}, 404)

    def log_message(self, *a):
        pass


def monitor_loop():
    print(f"[{logts()}] 监控启动: TV={CONFIG['tv_ip']}:{CONFIG['tv_port']} "
          f"最长观看={CONFIG['max_watch_min']}min 休息={CONFIG['rest_min']}min "
          f"每日总时长(工作日/休息日)={CONFIG['weekday_total_min']}/{CONFIG['weekend_total_min']}min "
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
    threading.Thread(target=holiday_refresh_loop, daemon=True).start()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"[{logts()}] 网页/接口: http://{args.host}:{args.port}/  (手机浏览器打开即可，可安装到桌面)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print(f"\n[{logts()}] 已停止")


if __name__ == "__main__":
    main()

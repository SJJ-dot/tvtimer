#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
小米电视 管控脚本 (家长控制 / 观看时限) + 配置网页服务
========================================================
规则:
  1) 单次观看不能超过 30 分钟（硬上限，配置值收敛到 1..30）：
     净已看 = clamp(今日累计观看 − 休息抵扣, 0, 单次上限)。
     净已看达到上限即自动关机，必须靠真实关机休息把净已看降下来才能再看。
  2) 休息按比例刷新观看时长：
     刷新速度 = 单次观看上限 / 休息基准时长 (max_watch_min / rest_min)。
     例：30/30 = 1:1（休 10 分钟刷新 10 分钟）；30/60 = 1:2（休 2 分钟刷新 1 分钟）。
     不要求休息满某个时长，随时可以开机，只放行实际折算出的额度；
     跨「关一下再开」不会清零，因此无法凭空多放时间（防卡 BUG）。
     rest_min 只是换算比例的基准，不是强制休息闸门。
  3) 每日总时长：工作日/非工作日分别设基准总时长（毛分钟），只按实际观看时间计
     （与开关机次数无关），达额度当天封禁，次日自动恢复；0 = 不限制。
     工作日/非工作日按法定节假日判定（holiday-cn 数据，每月自动更新，离线回退星期几）。
     当天没看完的额度结余到次日，余额滚存、可无限累计，网页可手动调整。
  4) 禁用时段：多个时间段，时段内电视若开机则自动关机；禁用时段不计休息抵扣。
  5) 无登录认证：打开即用。若要暴露公网，请在反向代理 / Cloudflare Access 层做访问控制。

网页:
  脚本同时提供 HTTP 服务，浏览器打开 http://<本机IP>:<port>/ 即可：
    - 可视化设置以上参数（手机适配）
    - 实时查看电视状态（禁用中 / 净已看 / 还可看 / 今日已看时长）
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
    "rest_min": 30,            # 休息基准时长：与 max_watch_min 共同决定刷新比例（30/30=1:1）
    "weekday_total_min": 60,   # 工作日每天可看总时长（毛分钟）；0 = 不限制
    "weekend_total_min": 120,  # 非工作日每天可看总时长（毛分钟）；0 = 不限制
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
STATE_PATH = os.path.join(DATA_DIR, "guard_state.json")
HTML_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tv_control.html")
config_lock = threading.Lock()

# 状态文件里 carry_min 的口径版本：
#   1（或缺失）= 旧公式 prev_base + carry_old - watched，结余会叠加结余，值可能被放大
#   2          = 余额滚存模型 prev_base + carry_old - watched（可无限累计，不设上限）
# 读到版本 < 2 的存量状态时，把脏结余收敛到一个合法上界（见 load_state）。
CARRY_STATE_VERSION = 2

ALLOWED_KEYS = {"tv_ip", "tv_port", "poll_interval", "max_watch_min", "rest_min",
                "weekday_total_min", "weekend_total_min", "disabled_windows"}
MAX_SESSION_MIN = 30           # 单次观看硬上限：配置入口一律收敛到 1..30
MIN_REST_MIN = 1               # 休息基准下限，保证抵扣比例不会除零或为 0


def _clamp_session_cap(v):
    """把单次观看上限收敛到 1..MAX_SESSION_MIN，非法值回退到默认 30。"""
    try:
        return max(1, min(MAX_SESSION_MIN, int(v)))
    except (ValueError, TypeError):
        return MAX_SESSION_MIN


def _clamp_rest_base(v):
    try:
        return max(MIN_REST_MIN, min(600, int(v)))
    except (ValueError, TypeError):
        return 30


def load_config():
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            with config_lock:
                # 只接受白名单键：旧版本的认证键（auth_*）会被自动丢弃
                CONFIG.update({k: v for k, v in data.items() if k in ALLOWED_KEYS})
                # 旧配置里可能有大于 30 的单次上限 / 非法的休息基准，加载时收敛
                CONFIG["max_watch_min"] = _clamp_session_cap(CONFIG["max_watch_min"])
                CONFIG["rest_min"] = _clamp_rest_base(CONFIG["rest_min"])
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


def _round_min_int(minutes):
    """四舍五入到整数分钟（非负数，四舍五入而非银行家舍入）。"""
    x = max(0.0, float(minutes))
    return float(int(x + 0.5))


def _round_sec(minutes):
    """把分钟数归整到「秒」精度，消除浮点累加误差。

    watched_gross_min 由逐次 (now-last)/60.0 累加而来，带 IEEE754 误差；
    若结余直接继承这些脏值，会逐日沉淀放大成 149.9190133213997 分这类数字。
    归整到秒（1/60 分）既消除误差，又保留亚分钟精度。
    """
    return round(float(minutes) * 60) / 60.0


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
          （休息 rest_min 分钟刷新 max_watch_min 分钟观看；rest_min 仅作比例基准）
      - 净已看 net = clamp(watched_gross_min - rest_credit_min, 0, max_watch_min)
      - net 达到上限即强制关机；之后只要电视仍在线就持续要求关机、不再计时。
      - 休息不要求满时长：真实离线 N 分钟就按 N×RATIO 刷新额度，随时可开机，
        只放行折算出来的剩余额度；没有强制休息闸门，也没有「重置休息」后门。
      - 防卡 BUG：看 20 分钟、关 1 分钟、又看，关的 1 分钟只抵扣 0.75 分钟，无法凭空多放时间。
      - 每日总时长：工作日 weekday_total_min / 非工作日 weekend_total_min（毛分钟，0=不限）。
        只按实际观看时间计，与开关机次数无关（网络抖动/探测误判不会误扣额度）。
        达额度当天封禁（total_blocked），次日重置。
      - 结余余额滚存：今日结余 = 昨日(基准+结余) − 昨日已看，可无限累计，网页可手动调整。
      - 禁用时段内强制关机，且禁用时段不计休息抵扣（属惩罚，不算恢复）。
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
        self.carry_min = 0.0           # 结余时长：昨日基准未用完的部分（可手动增减）
        self.total_blocked = False     # 今日总时长已用完（重置不解除，次日自动恢复）
        self.last_on_ts = None        # 上一次确认电视在线且可观看的时间戳（累加观看用）
        self.last_view_end_ts = 0.0    # 上一次观看结束（电视离线）时间戳，用于判定是否为新一次
        self.last_off_ts = None       # 上一次确认电视离线的时间戳（累加抵扣用）
        self.cooldown_until = 0.0     # 关机指令冷却（避免反复发 power 触发开关抖动）
        self.offline_count = 0
        self.cycle_day_key = time.strftime("%Y-%m-%d")
        self.lock = threading.Lock()
        self.last_action = "idle"
        self._last_save = 0.0

    # ---------- 观看状态持久化（升级/重启不丢当天观看时长） ----------
    def save_state(self):
        try:
            with self.lock:
                st = {
                    "cycle_day_key": self.cycle_day_key,
                    "watched_gross_min": self.watched_gross_min,
                    "rest_credit_min": self.rest_credit_min,
                    "carry_min": self.carry_min,
                    "carry_v": CARRY_STATE_VERSION,
                    "total_blocked": self.total_blocked,
                    "last_view_end_ts": self.last_view_end_ts,
                }
            tmp = STATE_PATH + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(st, f)
            os.replace(tmp, STATE_PATH)   # 原子替换，避免写一半损坏
        except Exception as e:
            print(f"[state] 保存观看状态失败: {e}")

    def maybe_save_state(self, min_interval=30.0):
        # 节流：至少间隔 min_interval 秒才写一次盘，减少闪存磨损
        if time.time() - self._last_save >= min_interval:
            self._last_save = time.time()
            self.save_state()

    def load_state(self):
        if not os.path.exists(STATE_PATH):
            return
        try:
            with open(STATE_PATH, "r", encoding="utf-8") as f:
                st = json.load(f)
            if st.get("cycle_day_key") != time.strftime("%Y-%m-%d"):
                print("[state] 状态文件是旧一天的，忽略")
                return
            with self.lock:
                self.cycle_day_key = st["cycle_day_key"]
                self.watched_gross_min = max(0.0, float(st.get("watched_gross_min", 0.0)))
                self.rest_credit_min = max(0.0, float(st.get("rest_credit_min", 0.0)))
                self.carry_min = _round_min_int(max(0.0, float(st.get("carry_min", 0.0))))
                carry_ver = int(st.get("carry_v", 1))
                self.total_blocked = bool(st.get("total_blocked", False))
                self.last_view_end_ts = float(st.get("last_view_end_ts", 0.0))
            # 存量脏结余清理：carry_v < 2 的状态文件由旧公式写入，
            # 结余里叠了上一轮的结余（如工作日 60、昨天只看了 30，却存成 90）。
            # 旧口径下结余最多也只能是「昨日基准未用完」的量，故收敛到昨日基准；
            # 想要精确值可在网页里手动调整结余（只影响今天）。
            if carry_ver < CARRY_STATE_VERSION:
                before = self.carry_min
                clamped = self._clamp_carry(time.time() - 86400)
                if clamped != before:
                    print(f"[state] 旧版结余已收敛: {before:.0f} -> {clamped:.0f} 分"
                          f"（可在网页手动调整结余）")
            print(f"[state] 已恢复今日观看状态: 毛观看 {self.watched_gross_min:.1f} 分, "
                  f"抵扣 {self.rest_credit_min:.1f} 分, 结余 {self.carry_min:.0f} 分")
        except Exception as e:
            print(f"[state] 读取观看状态失败(忽略): {e}")

    @staticmethod
    def _ratio():
        rest = CONFIG["rest_min"]
        return (CONFIG["max_watch_min"] / rest) if rest > 0 else 0.0

    def _net(self):
        mx = CONFIG["max_watch_min"]
        return max(0.0, min(mx, self.watched_gross_min - self.rest_credit_min))

    @staticmethod
    def _carry_from(prev_base, prev_carry, watched_prev):
        """跨天结转（余额滚存模型，可无限累计）：

        昨日可用额度 = 昨日基准 + 昨日结余，昨天看不完的部分整体结转为今日结余，
        不设上限、不把「额度」和「结余」拆开算，故结余可不断累计：
            第1天 基准60、看30      -> 结余 60-30 = 30
            第2天 基准60+结余30=90、看30 -> 结余 90-30 = 60
            第3天 基准60+结余60=120、看30 -> 结余 120-30 = 90
            ……结余随每天「少看」持续累积。
        watched_prev 为昨日毛观看分钟，先四舍五入到整数；prev_carry 为昨日带入的结余。
        """
        if prev_base <= 0:
            return _round_min_int(max(0.0, float(prev_carry) - _round_min_int(watched_prev)))
        carry = max(0.0, float(prev_base) + float(prev_carry) - _round_min_int(watched_prev))
        return _round_min_int(carry)

    def _daily_cap(self, now_ts):
        # 当日可看总时长（毛分钟）= 当日基准额度 + 结余。
        # 基准为 0 表示不限额；结余 ≤ 一个基准量，故额度上限 = 2×基准。
        # 注意：snapshot() 已持有 self.lock（非重入锁），这里不能再取锁。
        base = CONFIG["weekday_total_min"] if is_weekday(now_ts) else CONFIG["weekend_total_min"]
        if base <= 0:
            return 0
        return base + max(0.0, self.carry_min)

    def _clamp_carry(self, day_ts):
        """把结余收敛到不超过指定日期的基准量（用于清理旧版脏结余）。"""
        base = (CONFIG["weekday_total_min"] if is_weekday(day_ts)
                else CONFIG["weekend_total_min"])
        if base > 0 and self.carry_min > base:
            self.carry_min = float(base)
        return self.carry_min

    def set_carry(self, val):
        """手动设定结余时长（分钟，非负整数）；返回实际生效值。"""
        v = _round_min_int(max(0.0, float(val)))
        with self.lock:
            self.carry_min = v
        self.save_state()
        print(f"[{logts()}] 手动设定结余时长 = {v:.0f} 分钟")
        return v

    def adjust_carry(self, delta):
        """在当前结余上增加/减少若干分钟（负数即缩短）。"""
        with self.lock:
            cur = self.carry_min
        return self.set_carry(cur + float(delta))

    def daily_cap_now(self):
        return self._daily_cap(time.time())

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

        # 跨天：先把昨日未用完的额度结转（不断累计、不设上限），再重置今日累计
        day_key = time.strftime("%Y-%m-%d")
        if day_key != self.cycle_day_key:
            prev_base = (CONFIG["weekday_total_min"] if is_weekday(now - 86400)
                         else CONFIG["weekend_total_min"])
            # 余额滚存：今日结余 = 昨日(基准+结余) - 昨日已看，可无限累计。
            # self.carry_min 此刻仍是昨日带入的结余，作为 prev_carry 参与结转。
            self.carry_min = self._carry_from(prev_base, self.carry_min, self.watched_gross_min)
            if prev_base > 0:
                print(f"[{logts()}] 跨天结转: 昨日(基准 {prev_base} + 结余 "
                      f"{_round_min_int(self.carry_min):.0f}) - 已看 "
                      f"{_round_min_int(self.watched_gross_min):.0f} 分 "
                      f"= 结余 {self.carry_min:.0f} 分")
            self.cycle_day_key = day_key
            self.watched_gross_min = 0.0
            self.rest_credit_min = 0.0
            self.total_blocked = False
            self.last_on_ts = None
            self.last_view_end_ts = 0.0
            self.last_off_ts = None
            self.offline_count = 0
            print(f"[{logts()}] 跨天，今日观看已重置；结余时长 {self.carry_min:.0f} 分钟")

        with self.lock:
            if disabled:
                if alive:
                    self._try_off("禁用时段(电视不应开机)")
                self.last_on_ts = None
                self.last_off_ts = None   # 禁用时段不计休息抵扣
                return

            if alive:
                self.offline_count = 0
                # 两个硬上限在每次在线探测的最前面统一拦截，短暂中断（<最小片段间隔）
                # 也绕不过去。注意这里只判断「净已看是否顶格」，不设任何休息时长闸门：
                # 只要真实离线让抵扣涨了一点，净已看降下来就能立刻按剩余额度继续看。
                cap = self._daily_cap(now)
                if cap > 0 and self.watched_gross_min >= cap:
                    self.total_blocked = True
                    self._try_off(f"今日{cap}分钟已看完")
                    self.last_on_ts = None
                    return
                if self._net() >= CONFIG["max_watch_min"]:
                    self._try_off("净已看已达上限，请关机休息")
                    self.last_on_ts = None
                    return

                if self.last_on_ts is None:
                    is_new = (self.last_view_end_ts == 0.0) or (now - self.last_view_end_ts >= self._min_session_gap())
                    if is_new:
                        print(f"[{logts()}] 开始观看 (今日额度 {cap or '不限'} 分钟)")
                    # 同一观看片段的短暂中断（<最小间隔）继续累计，不另起日志
                    self.last_on_ts = now
                else:
                    delta = (now - self.last_on_ts) / 60.0
                    if delta > 0:
                        self.watched_gross_min += delta
                    self.last_on_ts = now

                # 累加完立刻复检，到点即关机（今日额度优先，它是当天硬上限）
                if cap > 0 and self.watched_gross_min >= cap:
                    self.total_blocked = True
                    self._try_off(f"今日{cap}分钟已看完")
                    self.last_on_ts = None
                    return
                if self._net() >= CONFIG["max_watch_min"]:
                    self._try_off(f"净观看已达 {CONFIG['max_watch_min']} 分钟，请休息")
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
            if disabled:
                mode = "disabled"
            elif self.total_blocked:
                mode = "blocked"
            elif net >= mx:
                # 净已看顶格：继续要求关机，但没有「必须休息满 X 分钟」的闸门，
                # 离线抵扣一旦把 net 降下来就随时可开（只放行折算出的额度）
                mode = "resting"
            elif self.last_on_ts is not None:
                mode = "watching"
            elif net > 0:
                mode = "recovering"   # 离线休息抵扣中，随时可打开电视
            else:
                mode = "idle"
            daily_cap = self._daily_cap(now)
            base_auto = (CONFIG["weekday_total_min"] if is_weekday(now)
                         else CONFIG["weekend_total_min"])
            remaining_cycle = max(0.0, mx - net)
        return {
            "tv_ip": CONFIG["tv_ip"],
            "disabled_now": disabled,
            "workday_today": is_weekday(now),
            "mode": mode,
            # 统计口径：关机后四舍五入为整数分钟，前端不再出现 29.999999999999844
            "net_watched_min": _round_min_int(net),
            "watched_gross_min": _round_min_int(self.watched_gross_min),
            "rest_credit_min": _round_min_int(self.rest_credit_min),
            "ratio": round(ratio, 3),
            "cycle_remaining_min": round(remaining_cycle, 1),
            "watch_remaining_sec": int(remaining_cycle * 60),
            "max_watch_min": CONFIG["max_watch_min"],
            "rest_min": CONFIG["rest_min"],
            "disabled_windows": CONFIG["disabled_windows"],
            "last_action": self.last_action,
            "daily_cap_min": _round_sec(daily_cap) if daily_cap > 0 else daily_cap,
            "daily_cap_auto": (round(base_auto + max(0.0, self.carry_min), 1)
                               if base_auto > 0 else 0),
            "daily_base_min": (CONFIG["weekday_total_min"] if is_weekday(now)
                               else CONFIG["weekend_total_min"]),
            # 结余按整数分钟展示/编辑，避免 89.9190133213997 这类浮点尾数
            "carry_min": _round_min_int(self.carry_min),
            "daily_gross_min": _round_min_int(self.watched_gross_min),
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
const CACHE = "tvtimer-v5";
const SHELL = ["/manifest.webmanifest", "/icon.svg"];
self.addEventListener("install", e => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(SHELL).catch(() => {})).then(() => self.skipWaiting()));
});
self.addEventListener("activate", e => {
  e.waitUntil(caches.keys().then(ks => Promise.all(ks.filter(k => k !== CACHE).map(k => caches.delete(k)))).then(() => self.clients.claim()));
});
self.addEventListener("fetch", e => {
  const url = new URL(e.request.url);
  if (url.origin !== self.location.origin) return;
  // 只缓存静态外壳资源（manifest / icon）。
  // 页面、状态与配置接口一律直连网络，避免 PWA 缓存导致普通刷新看不到新版。
  if (url.pathname !== "/manifest.webmanifest" && url.pathname !== "/icon.svg") return;
  e.respondWith(
    caches.match(e.request).then(r =>
      r || fetch(e.request).then(resp => {
        if (resp && resp.ok) {
          const cp = resp.clone();
          caches.open(CACHE).then(c => c.put(e.request, cp));
        }
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
    def _send_json(self, obj, code=200, extra_headers=None):
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        for k, v in (extra_headers or []):
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(json.dumps(obj, ensure_ascii=False).encode("utf-8"))

    def _send_html(self, html=None):
        try:
            if html is None:
                with open(HTML_PATH, "r", encoding="utf-8") as f:
                    html = f.read()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(html.encode("utf-8"))
        except Exception:
            self._send_json({"error": "tv_control.html not found"}, 404)

    def do_GET(self):
        path = urlparse(self.path).path

        # ---- PWA 静态资源 ----
        # 页面、状态与配置接口一律直连网络（no-store / no-cache），
        # 避免 PWA 缓存导致普通刷新看不到新版页面。
        if path == "/manifest.webmanifest":
            self.send_response(200)
            self.send_header("Content-Type", "application/manifest+json; charset=utf-8")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(json.dumps(MANIFEST, ensure_ascii=False).encode("utf-8"))
            return
        if path == "/sw.js":
            self.send_response(200)
            self.send_header("Content-Type", "application/javascript; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(SW_JS.encode("utf-8"))
            return
        if path == "/icon.svg":
            self.send_response(200)
            self.send_header("Content-Type", "image/svg+xml; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(ICON_SVG.encode("utf-8"))
            return

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
                    vals["max_watch_min"] = _clamp_session_cap(data["max_watch_min"])
                if "rest_min" in data:
                    vals["rest_min"] = _clamp_rest_base(data["rest_min"])
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
        elif path == "/carry":
            # 手动调整「结余时长」（昨日基准没看完的部分）：
            #   {"set_min": 30}   直接设定为 30 分钟
            #   {"delta_min": -10} 在当前值上增加/减少（负数=缩短）
            try:
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
            except Exception:
                body = {}
            if not isinstance(body, dict) or ("set_min" not in body and "delta_min" not in body):
                self._send_json({"ok": False, "error": "缺少 set_min 或 delta_min"}, 400)
                return
            try:
                if body.get("set_min") is not None:
                    val = guard.set_carry(body["set_min"])
                else:
                    val = guard.adjust_carry(body.get("delta_min", 0))
            except (TypeError, ValueError):
                self._send_json({"ok": False, "error": "数值无效"}, 400)
                return
            self._send_json({"ok": True, "message": f"结余已设为 {val:.0f} 分钟",
                             "carry_min": val,
                             "daily_cap_min": guard.daily_cap_now()})
            return
        else:
            self._send_json({"error": "unknown path"}, 404)

    def log_message(self, *a):
        pass


def monitor_loop():
    print(f"[{logts()}] 监控启动: TV={CONFIG['tv_ip']}:{CONFIG['tv_port']} "
          f"单次上限={CONFIG['max_watch_min']}min(硬顶{MAX_SESSION_MIN}) "
          f"休息基准={CONFIG['rest_min']}min(刷新比例 {CONFIG['max_watch_min']}/{CONFIG['rest_min']}) "
          f"无登录认证 "
          f"每日总时长(工作日/非工作日)={CONFIG['weekday_total_min']}/{CONFIG['weekend_total_min']}min "
          f"禁用时段={CONFIG['disabled_windows']} 轮询={CONFIG['poll_interval']}s")
    while True:
        try:
            guard.tick()
            guard.maybe_save_state()
        except Exception as e:
            print(f"[{logts()}] tick error: {e}")
        time.sleep(CONFIG["poll_interval"])


def main():
    load_config()
    refresh_urls()
    guard.load_state()   # 恢复今日观看时长（升级/重启不丢）
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

"""每日时间额度（次数=时间）/ 重置休息 验证（mock 时钟/电视）。

额度语义：N 次 × 单次X分钟 = 当天可看总时长（毛），只按实际观看时间计，
与开关机次数无关；达额度当天封禁（total_blocked），次日重置，重置休息不解除。
"""
import tv_control as M

MON = 1_000_000.0          # 1970-01-12 周一（weekday）
SAT = MON + 5 * 86400      # 周六（weekend）

fake_now = [MON]
M.time.time = lambda: fake_now[0]

alive_flag = [False]
offs = []
M.tv_is_alive = lambda: alive_flag[0]
M.power_off = lambda: (offs.append(round(fake_now[0])) or True)
M.in_disabled_window = lambda ts: False

g = M.guard


def reset():
    g.__init__()
    offs.clear()


def tick():
    g.tick()


def on():
    alive_flag[0] = True


def off():
    alive_flag[0] = False


def watch(minutes):
    """观看 minutes 分钟（可能中途触发单次X强制关机）。"""
    on(); tick()
    fake_now[0] += int(minutes * 60); tick()


def rest(minutes):
    """确认离线并休息 minutes 分钟（累计抵扣）。"""
    off(); tick(); tick()
    fake_now[0] += int(minutes * 60)
    off(); tick()


# ============ 1) 工作日时间额度：2次×30分 = 60 分钟 ============
M.CONFIG["max_watch_min"] = 30
M.CONFIG["rest_min"] = 40
M.CONFIG["weekday_sessions"] = 2
M.CONFIG["weekend_sessions"] = 4
M.CONFIG["daily_total_min"] = 0
M.CONFIG["off_cooldown_sec"] = 0
fake_now[0] = MON
reset()

assert g._daily_cap(fake_now[0]) == 60, g._daily_cap(fake_now[0])
watch(30)                              # 第1段看满30 -> 单次上限强制关机
assert not g.total_blocked, "30<60 不应封禁"
rest(40)                               # 抵扣回满
watch(29)                              # 累计 59 -> 仍未达额度
assert not g.total_blocked, f"59<60 不应封禁, gross={g.watched_gross_min}"
fake_now[0] += 60; tick()              # 再看 1 分钟 -> 累计 60 达额度
assert g.total_blocked, f"60 达额度应封禁, gross={g.watched_gross_min}"
n = len(offs)
on(); tick()                           # 再开机 -> 被关
assert len(offs) > n, "额度用完开机应被关机"
print("1) 工作日额度=2×30=60分钟，看满60封禁 OK")

# ============ 2) 周末额度不同：4次×30 = 120 分钟 ============
fake_now[0] = SAT
assert g._daily_cap(fake_now[0]) == 120, g._daily_cap(fake_now[0])
fake_now[0] = MON
assert g._daily_cap(fake_now[0]) == 60
print("2) 额度按工作日/周末切换 OK (2×30=60 / 4×30=120)")

# ============ 3) daily_total_min 显式设置时取更严者 ============
M.CONFIG["daily_total_min"] = 45       # min(60, 45) = 45
reset()
watch(20)
rest(10)                               # 抵扣 7.5
watch(26)                              # 毛 46 >= 45 -> 封禁
assert g.total_blocked, f"毛46>=45 应封禁, gross={g.watched_gross_min}"
print("3) daily_total_min=45 比 60 更严，达45封禁 OK")

# ============ 4) 额度用完后「重置休息」不解除（硬上限） ============
M.CONFIG["daily_total_min"] = 0
M.CONFIG["weekday_sessions"] = 1       # 额度 = 1×30 = 30
reset()
watch(30)                              # 单次上限强制关机，毛=30=额度
rest(40)                               # 抵扣回满
on(); tick()                           # 开机 -> 额度检查 -> 封禁
assert g.total_blocked, "额度用完应封禁"
g.reset_rest()
assert g.total_blocked, "重置休息不解除当日额度"
assert g._net() == 0, "重置后净应归零"
n = len(offs)
on(); tick()                           # 再开 -> 仍被封禁关机
assert len(offs) > n, "重置后再开仍应被关机"
print("4) 额度封禁后重置休息不解除（次日自动恢复） OK")

# ============ 5) 跨「重置休息」额度继续累计 ============
# 看30 -> 重置 -> 再看30 -> 额度计 60（不随重置清零）
M.CONFIG["max_watch_min"] = 999
M.CONFIG["rest_min"] = 40
M.CONFIG["weekday_sessions"] = 99      # 额度 99×999 很大
M.CONFIG["daily_total_min"] = 100      # 实际额度取 100
reset()
watch(30)
assert abs(g.watched_gross_min - 30) < 1e-6, g.watched_gross_min
g.reset_rest()
assert abs(g._net() - 0) < 1e-6, "重置后净应归零（立即可看）"
watch(30)
assert abs(g.watched_gross_min - 60) < 1e-6, f"跨重置应计60, {g.watched_gross_min}"
assert g.total_blocked is False, "60<100 不应封禁"
print("5) 看30→重置→再看30，额度累计=60 OK")

# ============ 6) 网络抖动不再误扣额度（时间制核心收益） ============
# 电视一直开着但探测失败（模拟抖动）：恢复后已看时间只少不多，不触发任何封禁
M.CONFIG["max_watch_min"] = 30
M.CONFIG["rest_min"] = 40
M.CONFIG["weekday_sessions"] = 2
M.CONFIG["daily_total_min"] = 0
reset()
on(); tick()
fake_now[0] += 600; tick()             # 看10分钟
off(); tick(); tick()                  # 抖动判离线（10秒）
fake_now[0] += 35
on(); tick()                           # 35秒后恢复，被当新片段（无额度损失，仅统计段数+1）
assert not g.total_blocked, "抖动不应误扣时间额度"
assert g.watched_gross_min < 11, f"观看时间未多计: {g.watched_gross_min}"
print("6) 网络抖动只影响统计，不影响时间额度 OK")

print("\nALL DAILY TESTS PASSED, 关机事件=", len(offs))

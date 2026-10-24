"""每日总时长（工作日/休息日）/ 重置休息 验证（mock 时钟/电视）。

语义：工作日/休息日分别设「每天可看总时长（毛分钟）」，只按实际观看时间计，
与开关机次数无关；达上限当天封禁（total_blocked），次日重置，重置休息不解除。
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


def on():
    alive_flag[0] = True


def off():
    alive_flag[0] = False


def watch(minutes):
    """观看 minutes 分钟（可能中途触发单次X强制关机）。"""
    on(); g.tick()
    fake_now[0] += int(minutes * 60); g.tick()


def rest(minutes):
    """确认离线并休息 minutes 分钟（累计抵扣）。"""
    off(); g.tick(); g.tick()
    fake_now[0] += int(minutes * 60)
    off(); g.tick()


# ============ 1) 工作日总时长：60 分钟 ============
M.CONFIG["max_watch_min"] = 30
M.CONFIG["rest_min"] = 40
M.CONFIG["weekday_total_min"] = 60
M.CONFIG["weekend_total_min"] = 120
M.CONFIG["off_cooldown_sec"] = 0
fake_now[0] = MON
reset()

assert g._daily_cap(fake_now[0]) == 60, g._daily_cap(fake_now[0])
watch(30)                              # 第1段看满30 -> 单次上限强制关机
assert not g.total_blocked, "30<60 不应封禁"
rest(40)                               # 抵扣回满
watch(29)                              # 累计 59 -> 仍未达上限
assert not g.total_blocked, f"59<60 不应封禁, gross={g.watched_gross_min}"
fake_now[0] += 60; g.tick()            # 再看 1 分钟 -> 累计 60 达上限
assert g.total_blocked, f"60 达上限应封禁, gross={g.watched_gross_min}"
n = len(offs)
on(); g.tick()                         # 再开机 -> 被关
assert len(offs) > n, "时长用完开机应被关机"
print("1) 工作日总时长 60 分钟，看满封禁 OK")

# ============ 2) 休息日不同：120 分钟 ============
fake_now[0] = SAT
assert g._daily_cap(fake_now[0]) == 120, g._daily_cap(fake_now[0])
fake_now[0] = MON
assert g._daily_cap(fake_now[0]) == 60
print("2) 工作日/休息日切换 OK (60 / 120)")

# ============ 3) 0 = 不限制 ============
M.CONFIG["weekday_total_min"] = 0
reset()
watch(30)
rest(40)
watch(30)
rest(40)
watch(30)                              # 毛 90 分钟
assert not g.total_blocked, "0=不限不应封禁"
assert g._daily_cap(fake_now[0]) == 0
M.CONFIG["weekday_total_min"] = 60
print("3) 0=不限制 OK")

# ============ 4) 上限用完后「重置休息」不解除（硬上限） ============
M.CONFIG["weekday_total_min"] = 30     # 上限=30
reset()
watch(30)                              # 单次上限强制关机，毛=30=上限
rest(40)                               # 抵扣回满
on(); g.tick()                         # 开机 -> 上限检查 -> 封禁
assert g.total_blocked, "上限用完应封禁"
g.reset_rest()
assert g.total_blocked, "重置休息不解除当日上限"
assert g._net() == 0, "重置后净应归零"
n = len(offs)
on(); g.tick()                         # 再开 -> 仍被封禁关机
assert len(offs) > n, "重置后再开仍应被关机"
print("4) 封禁后重置休息不解除（次日自动恢复） OK")

# ============ 5) 跨「重置休息」总时长继续累计 ============
M.CONFIG["max_watch_min"] = 999
M.CONFIG["weekday_total_min"] = 60
reset()
watch(30)
assert abs(g.watched_gross_min - 30) < 1e-6, g.watched_gross_min
g.reset_rest()
assert abs(g._net() - 0) < 1e-6, "重置后净应归零（立即可看）"
watch(30)
assert abs(g.watched_gross_min - 60) < 1e-6, f"跨重置应计60, {g.watched_gross_min}"
assert g.total_blocked, "60 达上限 60 应封禁"
print("5) 看30→重置→再看30，总时长累计=60并封禁 OK")

# ============ 6) 网络抖动不误扣总时长 ============
M.CONFIG["max_watch_min"] = 30
reset()
on(); g.tick()
fake_now[0] += 600; g.tick()           # 看10分钟
off(); g.tick(); g.tick()              # 抖动判离线（10秒）
fake_now[0] += 35
on(); g.tick()                         # 35秒后恢复，被当新片段
assert not g.total_blocked, "抖动不应误扣总时长"
assert g.watched_gross_min < 11, f"观看时间未多计: {g.watched_gross_min}"
print("6) 网络抖动只影响统计，不误扣总时长 OK")

# ============ 7) snapshot 字段 ============
snap = g.snapshot()
assert "sessions_today" not in snap and "session_limit" not in snap
assert "daily_total_min" not in snap
assert snap["daily_cap_min"] == 60
assert snap["rest_remaining_sec"] is not None
print("7) snapshot 无次数字段、含 daily_cap_min OK")

print("\nALL DAILY TESTS PASSED, 关机事件=", len(offs))

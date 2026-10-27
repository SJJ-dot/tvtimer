"""每日总时长（工作日/休息日）/ 重置休息 验证（mock 时钟/电视）。

语义：工作日/休息日分别设「每天可看总时长（毛分钟）」，只按实际观看时间计，
与开关机次数无关；达上限当天封禁（total_blocked），次日重置，重置休息不解除。
"""
import os
import tempfile

import tv_control as M

# 测试不碰真实状态文件（set_carry/adjust_carry 会 save_state）
M.STATE_PATH = os.path.join(tempfile.gettempdir(), "tvtimer_test_state.json")

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

# ============ 8) 关机后统计四舍五入为整数 ============
# 需求：关机后「今日已看」等统计显示为整数分钟，不出现 29.999999999999844 这类尾数。
# 实现方式：内部累加器保持精度（休息抵扣靠它算净已看，写死会破坏一致性），
#           只在快照/结转出口四舍五入。
reset()
M.CONFIG["max_watch_min"] = 600
M.CONFIG["weekday_total_min"] = 60
M.CONFIG["weekend_total_min"] = 120
g.watched_gross_min = 29.6
off(); g.tick()                      # 关机（离线确认）
snap = g.snapshot()
assert snap["daily_gross_min"] == 30.0, f"关机后展示应为 30, 实际 {snap['daily_gross_min']!r}"
assert snap["watched_gross_min"] == 30.0
assert snap["net_watched_min"] == 30.0
assert g.watched_gross_min == 29.6, "内部累加器不应被改写（否则净已看会偏 0.4）"
g.watched_gross_min = 29.4
assert g.snapshot()["daily_gross_min"] == 29.0, "29.4 应展示为 29"
# 结转用的昨日时长同样先取整（prev_carry=0 隔离）
assert M.TvGuard._carry_from(60, 0, 29.6) == 30.0, "结转按整数分钟计算"
print("8) 关机后四舍五入为整数 OK (29.6->30, 29.4->29, 内部值不变)")

# ============ 9) 余额滚存：结余可无限累计 ============
# 模型：今日结余 = 昨日(基准 + 结余) - 昨日已看，不设上限、可逐日积累。
_c = M.TvGuard._carry_from

# 用户场景：昨日(工作日60) 看了 30，且手里压着 60 的旧结余
# 滚存：今日结余 = (60 + 60) - 30 = 90，今日额度 = 60 + 90 = 150
assert _c(60, 60, 30) == 90.0, f"应滚存结余 90, 实际 {_c(60, 60, 30)}"
assert 60 + _c(60, 60, 30) == 150.0, "今日额度应为 150"

# 完全没看 -> 结余 = 昨日基准 + 昨日结余，可超过一个基准
assert _c(60, 0, 0) == 60.0
assert _c(60, 60, 0) == 120.0, "带 60 结余且没看 -> 120"
assert _c(120, 60, 0) == 180.0, "非工作日带结余且没看 -> 180"

# 看光基准但仍吃结余：昨日(120+60)看120 -> 结余 60，今日额度 120
assert _c(120, 60, 120) == 60.0, f"看光基准仍余 60 结余, 实际 {_c(120, 60, 120)}"
assert 60 + _c(120, 60, 120) == 120.0, "今日额度应为 120"

# 连续多天没看：结余逐日累积（无限累计）
carry = 0.0
for _ in range(10):
    carry = _c(60, carry, 0)
assert carry == 600.0, f"连续10天未看应累积到 600, 实际 {carry}"

# 昨日不限额(基准0)但有带入结余 -> 结余 = 带入 - 已看（不凭空消失）
assert _c(0, 60, 0) == 60.0, "昨日不限额但有结余应保留"
assert _c(0, 60, 70) == 0.0, "昨日不限额且看超结余 -> 0"

# 浮点观看时长先取整，不产生浮点尾数
c = _c(60, 0, 29.6)
assert c == 30.0 and isinstance(c, float), f"应先取整为 30, 实际 {c!r}"
print("9) 余额滚存 OK (60基准+60结余看30 -> 结余90/额度150，连续未看逐日累积)")

# ============ 10) 手动调整结余时长（增加/缩短） ============
reset()
M.CONFIG["weekday_total_min"] = 60
M.CONFIG["weekend_total_min"] = 120
assert g.set_carry(30) == 30.0
assert g.daily_cap_now() == 90.0, f"结余30 -> 额度应为90, 实际 {g.daily_cap_now()}"
assert g.snapshot()["carry_min"] == 30
assert g.adjust_carry(10) == 40.0, "增加 10 分钟"
assert g.daily_cap_now() == 100.0
assert g.adjust_carry(-25) == 15.0, "缩短 25 分钟"
assert g.daily_cap_now() == 75.0
assert g.adjust_carry(-100) == 0.0, "缩短到负数应夹到 0"
assert g.daily_cap_now() == 60.0, "结余 0 -> 额度回到基准"
assert g.set_carry(89.9) == 90.0, "小数应四舍五入到整数"
print("10) 手动调整结余 OK (设定/增加/缩短/夹0/取整)")

print("\nALL DAILY TESTS PASSED, 关机事件=", len(offs))

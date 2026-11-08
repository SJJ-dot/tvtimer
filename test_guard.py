"""TvGuard「单次观看上限 + 按比例休息刷新」验证（mock 时钟/电视/禁用时段）。

核心语义：
  - 净已看 = clamp(今日累计观看 − 休息抵扣, 0, 单次上限)，到顶即关机；
  - 休息抵扣 += 离线分钟 × (max_watch_min / rest_min)，不设任何强制休息闸门；
  - 休息多久就按多久刷新额度，随时可开机，只放行折算出的剩余额度；
  - 今日总时长（毛观看）与结余余额滚存不受休息影响。

离线模拟规则：
  rest(minutes) = off(); tick(); tick() 两次确认离线 → 推进 minutes → off(); tick() 累计抵扣
"""
import tv_control as M

BASE = 1_000_000.0
fake_now = [BASE]
M.time.time = lambda: fake_now[0]

alive_flag = [False]
disabled_flag = [False]
offs = []

M.tv_is_alive = lambda: alive_flag[0]
M.power_off = lambda: (offs.append(round(fake_now[0])) or True)
M.in_disabled_window = lambda ts: disabled_flag[0]

g = M.guard

# 关掉每日总额度的干扰，本文件只验证单次上限与休息刷新
M.CONFIG["weekday_total_min"] = 0
M.CONFIG["weekend_total_min"] = 0


def reset():
    fake_now[0] = BASE
    g.__init__()
    offs.clear()
    alive_flag[0] = False
    disabled_flag[0] = False
    M.CONFIG["max_watch_min"] = 30
    M.CONFIG["rest_min"] = 30
    M.CONFIG["off_cooldown_sec"] = 0
    M.CONFIG["poll_interval"] = 5


def on():
    alive_flag[0] = True


def off():
    alive_flag[0] = False


def rest(minutes):
    """确认离线并从当前时刻休息 minutes 分钟（累计抵扣）。"""
    off(); g.tick(); g.tick()                       # 连续 2 次确认离线
    fake_now[0] += int(minutes * 60)
    off(); g.tick()                                 # 累计该段离线时长


def watch(minutes):
    """在线观看 minutes 分钟。"""
    on(); g.tick()
    fake_now[0] += int(minutes * 60)
    g.tick()


# ============ A) 用户例子 1：30/30 = 1:1 ============
# 看 30 分钟，休息 10 分钟，再次打开观看 10 分钟就必须再休息。
reset()                                            # max=30, rest=30 → ratio 1.0
watch(30)
assert abs(g._net() - 30) < 1e-6, g._net()
assert len(offs) == 1, f"看满 30 应关机 1 次, offs={offs}"
print("A1) 看满 30 分钟自动关机 OK")

rest(10)
assert abs(g.rest_credit_min - 10) < 1e-6, g.rest_credit_min
assert abs(g._net() - 20) < 1e-6, g._net()
fake_now[0] += 1; on(); g.tick()                   # 休 10 分钟即可开机，不等满 30
assert g.last_on_ts is not None, "部分休息后应允许开机"
assert abs(g.snapshot()["cycle_remaining_min"] - 10) < 1e-6
print("A2) 休 10 分刷新 10 分 -> 净已看 20, 还可看 10 分 OK")

fake_now[0] += 10 * 60; g.tick()                   # 再看 10 分钟 -> 净 30
assert abs(g._net() - 30) < 1e-6, g._net()
assert len(offs) == 2, f"再看 10 分钟应再次关机, offs={offs}"
assert not hasattr(g, "rest_until"), "不应存在强制休息闸门"
print("A3) 再看 10 分钟即再次关机（用户例子 30/休10/看10）OK")

# ============ B) 用户例子 2：30/60 = 1:2 ============
reset()
M.CONFIG["rest_min"] = 60                         # ratio = 0.5
assert abs(g._ratio() - 0.5) < 1e-6
watch(30)
assert len(offs) == 1 and abs(g._net() - 30) < 1e-6
rest(2)                                           # 休 2 分钟刷新 1 分钟
assert abs(g.rest_credit_min - 1) < 1e-6, g.rest_credit_min
assert abs(g._net() - 29) < 1e-6, g._net()
fake_now[0] += 1; on(); g.tick()
assert g.last_on_ts is not None, "休 2 分钟也应允许开机"
assert abs(g.snapshot()["cycle_remaining_min"] - 1) < 1e-6
print("B) 30/60 比例：休 2 分钟刷新 1 分钟观看时长 OK")

# ============ C) 净已看顶格时开机立即被关，但休息后随时可开 ============
reset()
M.CONFIG["rest_min"] = 40                         # ratio = 0.75
watch(30)
assert len(offs) == 1
on(); g.tick()                                     # 完全没休息就开机 -> 继续要求关机
assert len(offs) == 2, f"顶格开机应再次关机, offs={offs}"
assert g.last_on_ts is None
assert not hasattr(g, "rest_until"), "不能设置强制休息截止时间"
rest(10)                                          # 只休 10 分钟（不满 40）
assert abs(g.rest_credit_min - 7.5) < 1e-6, g.rest_credit_min
assert abs(g._net() - 22.5) < 1e-6, g._net()
fake_now[0] += 1; on(); g.tick()                   # 立刻可开，放行 7.5 分钟
assert g.last_on_ts is not None
assert abs(g.snapshot()["cycle_remaining_min"] - 7.5) < 1e-6
print("C) 顶格开机仍关机；休 10 分（不满基准）即可开，放行 7.5 分 OK")

# ============ D) 长期离线不囤积负额度 ============
reset()
watch(20)
rest(600)                                         # 离线 10 小时
assert abs(g.rest_credit_min - 20) < 1e-6, g.rest_credit_min
assert abs(g._net()) < 1e-6, g._net()
assert abs(g.snapshot()["cycle_remaining_min"] - 30) < 1e-6
print("D) 长期离线抵扣夹到毛观看量，净已看最低为 0 OK")

# ============ E) 禁用时段：强制关机且不计休息抵扣 ============
reset()
watch(10)
disabled_flag[0] = True
on(); g.tick()
assert len(offs) == 1, f"禁用时段应关机, offs={offs}"
before = g.rest_credit_min
fake_now[0] += 60 * 60; off(); g.tick(); g.tick()
assert g.rest_credit_min == before, "禁用时段不应计抵扣"
disabled_flag[0] = False
print("E) 禁用时段强制关机 + 离线不计抵扣 OK")

# ============ F) 抗抖动：单次离线不动累计 ============
reset()
watch(10)
off(); g.tick()
assert abs(g.watched_gross_min - 10) < 1e-6, "单次离线不应动累计"
off(); g.tick()
assert g.last_on_ts is None
assert abs(g.rest_credit_min) < 1e-6, "刚确认离线不应立刻产生抵扣"
print("F) 抗抖动（连续 2 次离线才确认）OK")

# ============ G) 配置入口硬顶 30 / 比例不除零 ============
assert M.MAX_SESSION_MIN == 30
assert M._clamp_session_cap(999) == 30
assert M._clamp_session_cap(0) == 1
assert M._clamp_session_cap("bad") == 30
assert M._clamp_rest_base(0) == 1
assert M._clamp_rest_base(99999) == 600
print("G) 单次上限硬顶 30 / 休息基准下限 1 OK")

# ============ H) 快照：无强制休息倒计时、模式语义正确 ============
reset()
snap = g.snapshot()
assert "rest_remaining_sec" not in snap, "不应再有强制休息倒计时"
assert "auth" not in str(snap).lower()
watch(30)
assert g.snapshot()["mode"] == "resting", "净已看顶格应为 resting"
rest(10)
assert g.snapshot()["mode"] == "recovering", "休息抵扣中应为 recovering"
print("H) snapshot 无 rest_remaining_sec，resting/recovering 语义正确 OK")

print("\nALL GUARD TESTS PASSED, 关机事件=", len(offs), "发生在:", offs)

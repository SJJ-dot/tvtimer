"""TvGuard 净观看(观看-休息抵扣)模型验证（mock 时钟/电视/禁用时段）。

离线/休息时长模拟规则：
  - 先 off(); tick(); tick()  两次确认离线（last_off_ts 设在当前时刻）
  - 再把时钟推进到休息结束，off(); tick()  一次即可累计该段离线时长×比例
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


def reset():
    g.__init__()
    offs.clear()


def tick():
    g.tick()


def on():
    alive_flag[0] = True


def off():
    alive_flag[0] = False


def rest(minutes):
    """确认离线并从当前时刻休息 minutes 分钟（累计抵扣）。"""
    off(); tick(); tick()                       # 连续2次确认离线
    fake_now[0] += int(minutes * 60)
    off(); tick()                               # 累计该段离线时长


# ============ A) 1:1 情形（max_watch==rest），复现用户示例 20-10=10 ============
M.CONFIG["max_watch_min"] = 30
M.CONFIG["rest_min"] = 30           # RATIO = 1.0
M.CONFIG["off_cooldown_sec"] = 0
reset()

# 看 20 分钟
fake_now[0] = BASE; on(); tick()
fake_now[0] = BASE + 600; tick()
fake_now[0] = BASE + 1200; tick()
assert abs(g.watched_gross_min - 20) < 1e-6, f"应看20, 实际 {g.watched_gross_min}"
assert abs(g._net() - 20) < 1e-6
print("A1) 看 20 分钟, 净已看 =", g._net())

# 休息 10 分钟（抵扣 10*1.0=10） -> 再开净 = 20-10 = 10
rest(10)
assert abs(g.rest_credit_min - 10) < 1e-6, f"抵扣应=10, 实际 {g.rest_credit_min}"
fake_now[0] += 1; on(); tick()
assert abs(g._net() - 10) < 1e-6, f"净应=10, 实际 {g._net()}"
print("A2) 休息 10 分钟抵扣 10 -> 再开净已看 =", g._net(), "(复现 20-10=10)")

# ============ B) 默认 30/40 比例 RATIO=0.75 ============
M.CONFIG["max_watch_min"] = 30
M.CONFIG["rest_min"] = 40            # RATIO = 0.75
reset()

# 看 20 -> 净 20
fake_now[0] = BASE; on(); tick()
fake_now[0] = BASE + 1200; tick()
assert abs(g._net() - 20) < 1e-6
# 休息 10 -> 抵扣 7.5 -> 净 12.5
rest(10)
assert abs(g.rest_credit_min - 7.5) < 1e-6, f"抵扣应=7.5, 实际 {g.rest_credit_min}"
fake_now[0] += 1; on(); tick()
assert abs(g._net() - 12.5) < 1e-6, f"净应=12.5, 实际 {g._net()}"
print("B1) 看20休10(RATIO=0.75) -> 净已看 =", g._net(), "(20-7.5=12.5)")

# 再看 17.5 -> 净 30 -> 强制关机
fake_now[0] += 1050; on(); tick()    # 上次 on 在 BASE+1261，+17.5min
assert len(offs) == 1, f"应强制关机1次, offs={offs}"
assert abs(g._net() - 30) < 1e-6, f"净应=30, 实际 {g._net()}"
print("B2) 净达 30 分钟强制关机 OK")

# 休息满 40 分钟 -> 抵扣 +30 -> 净 0（额度回满，新周期）
rest(40)
assert abs(g.rest_credit_min - 37.5) < 1e-6, f"总抵扣应=37.5, 实际 {g.rest_credit_min}"
fake_now[0] += 1; on(); tick()
assert abs(g._net() - 0) < 1e-6, f"满休息后净应=0, 实际 {g._net()}"
assert len(offs) == 1, f"满休息后不应再关机, offs={offs}"
print("B3) 满休息 40 分钟抵扣回满 -> 净已看 =", g._net(), "新周期开始")

# ============ C) 禁用时段：强制关 + 不计抵扣 ============
disabled_flag[0] = True
fake_now[0] += 100; on(); tick()
assert len(offs) == 2, f"禁用时段应关机, offs={offs}"
before_credit = g.rest_credit_min
fake_now[0] += 60; off(); tick(); tick()  # 禁用时段内离线
assert g.rest_credit_min == before_credit, "禁用时段不应计抵扣"
disabled_flag[0] = False
print("C) 禁用时段强制关机 + 离线不计抵扣 OK")

# ============ D) 抗抖动：单次离线不动累计 ============
M.CONFIG["max_watch_min"] = 30; M.CONFIG["rest_min"] = 40
reset()
fake_now[0] = BASE; on(); tick()
fake_now[0] = BASE + 600; tick()       # 看 10
assert abs(g.watched_gross_min - 10) < 1e-6
off(); tick()                           # 离线1次（未达确认）
assert g.watched_gross_min == 10, "单次离线不应动累计"
off(); tick()                           # 离线2次 -> 暂停
assert g.last_on_ts is None
print("D) 抗抖动（连续2次离线才暂停累计）OK")

print("\nALL TESTS PASSED, 净观看模型正确，关机事件=", len(offs), "发生在:", offs)

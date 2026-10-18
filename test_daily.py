"""每日次数 / 总时长 / 重置休息 验证（mock 时钟/电视）。"""
import tv_control as M

# 周一基准（weekday）
MON = 1_000_000.0
# 周六基准（weekend）：+5 天
SAT = MON + 5 * 86400

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


def end_session():
    """关电视并确认离线，结束当前观看会话。"""
    off(); tick(); tick()


def start_session():
    """开电视并开始一次观看（若未封禁则计入次数）。"""
    on(); tick()


# ============ 1) 工作日次数上限：第 3 次被封禁 ============
M.CONFIG["max_watch_min"] = 999
M.CONFIG["rest_min"] = 1
M.CONFIG["weekday_sessions"] = 2
M.CONFIG["weekend_sessions"] = 4
M.CONFIG["daily_total_min"] = 0
M.CONFIG["off_cooldown_sec"] = 0
fake_now[0] = MON
reset()

start_session(); end_session(); fake_now[0] += 60   # 间隔>最小间隔，算新一次
assert g.sessions_today == 1, f"应=1, {g.sessions_today}"
start_session(); end_session(); fake_now[0] += 60
assert g.sessions_today == 2, f"应=2, {g.sessions_today}"
start_session()                      # 第 3 次：应封禁
assert g.sessions_blocked is True, "次数耗尽应封禁"
assert g.sessions_today == 2, "被封禁的第3次不应计入次数"
assert len(offs) >= 1, "被封禁应发关机"
print("1) 工作日次数=2，第3次被封禁 OK")

# ============ 2) 周末次数上限：第 5 次被封禁 ============
M.CONFIG["weekday_sessions"] = 2
M.CONFIG["weekend_sessions"] = 4
fake_now[0] = SAT                     # 周六 -> 走 weekend_sessions
reset()
for i in range(4):
    start_session(); end_session(); fake_now[0] += 60
assert g.sessions_today == 4, f"周末应=4, {g.sessions_today}"
start_session()                      # 第 5 次
assert g.sessions_blocked is True, "周末次数耗尽应封禁"
print("2) 周末次数=4，第5次被封禁 OK")

# ============ 3) 每日总时长上限 ============
M.CONFIG["max_watch_min"] = 999
M.CONFIG["daily_total_min"] = 15
M.CONFIG["weekday_sessions"] = 99
fake_now[0] = MON
reset()
on(); tick()
fake_now[0] += 1200; tick()           # 看 20 分钟（>15）
assert g.total_blocked is True, "总时长达上限应封禁"
assert g.watched_gross_min >= 15
print("3) 每日总时长=15，看20分钟被封禁 OK")

# ============ 4) 重置休息：清次数封禁，不看总时长封禁 ============
# 4a) 次数耗尽后重置 -> 可再开（突破次数）
M.CONFIG["max_watch_min"] = 999
M.CONFIG["rest_min"] = 40
M.CONFIG["weekday_sessions"] = 1
M.CONFIG["daily_total_min"] = 0
fake_now[0] = MON
reset()
start_session(); end_session(); fake_now[0] += 60   # 第1次
start_session()                       # 第2次 -> 次数耗尽封禁
assert g.sessions_blocked is True
offs_before = len(offs)
g.reset_rest()
assert g.sessions_blocked is False, "重置应解除次数封禁"
assert g.sessions_today == 0, "重置应将次数归零"
start_session()                       # 重置后可再开，不强制关机
assert len(offs) == offs_before, f"重置后不应再关机, offs={offs}"
assert g.sessions_today == 1, f"重置后新会话应=1, {g.sessions_today}"
print("4a) 次数耗尽后重置 -> 突破次数限制 OK")

# 4b) 总时长封禁后重置 -> 仍封禁（总时长是硬上限，重置不解除）
M.CONFIG["max_watch_min"] = 999
M.CONFIG["daily_total_min"] = 15
M.CONFIG["weekday_sessions"] = 99
fake_now[0] = MON
reset()
on(); tick(); fake_now[0] += 1200; tick()   # 看 20 -> 总时长封禁
assert g.total_blocked is True
g.reset_rest()
assert g.total_blocked is True, "重置不应解除总时长封禁"
print("4b) 总时长封禁后重置仍封禁（硬上限）OK")

# ============ 5) 每日总时长跨「重置休息」继续累计 ============
# 看30 -> 重置 -> 再看30 -> 总时长计 60（不随重置清零）
M.CONFIG["max_watch_min"] = 999
M.CONFIG["rest_min"] = 40
M.CONFIG["weekday_sessions"] = 99
M.CONFIG["daily_total_min"] = 100       # 上限100，避免中途封禁
fake_now[0] = MON
reset()
on(); tick(); fake_now[0] += 1800; tick()    # 看 30
assert abs(g.watched_gross_min - 30) < 1e-6, f"应看30, {g.watched_gross_min}"
g.reset_rest()
assert abs(g._net() - 0) < 1e-6, "重置后净应归零（立即可看）"
on(); tick(); fake_now[0] += 1800; tick()    # 再 看 30
assert abs(g.watched_gross_min - 60) < 1e-6, f"跨重置总时长应计60, {g.watched_gross_min}"
assert g.total_blocked is False, "60<100 不应封禁"
print("5) 看30→重置→再看30，总时长累计=60 OK")

print("\nALL DAILY TESTS PASSED, 关机事件=", len(offs))

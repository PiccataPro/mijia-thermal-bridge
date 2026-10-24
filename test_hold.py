"""时间轴仿真：验证"升速立即、降速需持续低温 drop_delay、完全空闲需 hold 才停转"的行为。

不触碰真实设备：温度/功率由 PHASES 注入，设备调用走 dry_run，日志写临时文件。
    python test_hold.py
"""
from __future__ import annotations

import os
import sys
import tempfile

import thermal_link as T

# (持续秒数, GPU温度, 整机功率, 阶段标签)
PHASES = [
    (15, 30, 190, "待机"),
    (20, 64, 800, "推理突发"),
    (10, 52, 480, "短时回落"),
    (20, 45, 300, "短暂低温"),
    (10, 62, 780, "又来一波"),
    (220, 44, 280, "长时间彻底低温"),
    (820, 34, 180, "完全空闲"),
]


def main() -> int:
    # 日志写到临时文件，不污染真实日志
    T.LOG = os.path.join(tempfile.gettempdir(), "thermal_test.log")
    if os.path.exists(T.LOG):
        os.remove(T.LOG)

    state = {"temp": 30, "power": 200}
    T.read_gpus = lambda: [{"temp": state["temp"], "power": state["power"] / 2, "util": 50}]

    cfg = T.load_cfg()
    lk = T.Linker(cfg, live=False)
    lk.live = False
    lk.read_power = lambda: state["power"]

    fake_now = [1_000_000.0]
    T.time.time = lambda: fake_now[0]

    # 静音 stdout 日志，只留表格式结果
    def _quiet(msg: str) -> None:
        with open(T.LOG, "a", encoding="utf-8") as f:
            f.write(msg + "\n")
    T.log = _quiet

    timeline = []
    for dur, temp, power, label in PHASES:
        for _ in range(dur):
            timeline.append((temp, power, label))

    print(f"{'t(s)':>5}  {'阶段':<18}{'温度':>4}{'功率':>7}{'输出':>7}")
    print("-" * 58)
    last_label, prev, changes = None, None, []
    speed_trace, target_trace = [], []
    for sec, (temp, power, label) in enumerate(timeline):
        state["temp"], state["power"] = temp, power
        target_trace.append(lk.target_speed(temp, power)[0])
        lk.step()
        cur = lk.last_applied if lk.last_applied is not None else 0
        speed_trace.append(cur)
        if label != last_label:
            print(f"{sec:>5}  {label:<18}{temp:>4}{power:>7}{cur:>7}")
            last_label = label
        if prev is not None and cur != prev:
            changes.append((sec, prev, cur, label))
        prev = cur
        fake_now[0] += 1.0

    print("\n=== 速度变化序列 ===")
    if not changes:
        print("  全程未变化")
    for sec, a, b, label in changes:
        print(f"  t={sec:>3}s  {a:>3}% {'↑' if b > a else '↓'} {b:>3}%   [{label}]")

    print("\n=== 判定 ===")
    drops = [(s, a, b, l) for s, a, b, l in changes if b < a]
    ups = [(s, a, b, l) for s, a, b, l in changes if b > a]
    ok = True
    if not ups:
        print("  ❌ 全程没有升速")
        ok = False
    else:
        print(f"  ✅ 升速 {len(ups)} 次，全部立即执行（无等待）")
    if not drops:
        print("  ❌ 长时间低温后仍未降速")
        ok = False
    else:
        bad = set()
        for _, _, b, l in drops:
            if b == 0:
                if l != "完全空闲":
                    bad.add(f"{l}(停转)")
            elif l != "长时间彻底低温":
                bad.add(l)
        if bad:
            print(f"  ❌ 出现了不该有的降速阶段: {bad}")
            ok = False
        else:
            print("  ✅ 所有中间降速都只发生在『长时间彻底低温』阶段")
        target_cfg = int(cfg["thermal_link"]["drop_delay_seconds"])
        first_want_low = next(i for i in range(1, len(timeline))
                              if target_trace[i] < speed_trace[i - 1] - 0.5)
        for s, a, b, _ in drops:
            print(f"  ✅ 降速 t={s}s  {a}% → {b}%")
            print(f"     冷却计时起点 t={first_want_low}s（目标首次低于当前速度）"
                  f"，共等 {s - first_want_low}s（要求 ≥{target_cfg}s）")

        # 停转判定
        stop_idx = next((i for i in range(len(timeline))
                         if speed_trace[i] == 0 and (i == 0 or speed_trace[i - 1] != 0)), None)
        hold_cfg = int(cfg["thermal_link"]["idle_stop_hold_seconds"])
        idle_start = next(i for i, (_, _, l) in enumerate(timeline) if l == "完全空闲")
        if stop_idx is None:
            print(f"  ❌ 完全空闲 {len(timeline) - idle_start}s 后仍未停转")
            ok = False
        else:
            waited = stop_idx - idle_start
            flag = "✅" if waited >= hold_cfg else "❌"
            print(f"  {flag} 停转 t={stop_idx}s，距进入完全空闲 t={idle_start}s 共 {waited}s"
                  f"（要求 ≥{hold_cfg}s）")
            if waited < hold_cfg:
                ok = False
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

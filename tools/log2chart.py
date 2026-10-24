#!/usr/bin/env python3
"""把 thermal_link 的运行日志渲染成一张 SVG 图（零依赖，GitHub README 原生渲染 SVG）。

用法:
    python tools/log2chart.py data/thermal_link.log -o docs/demo.svg
    python tools/log2chart.py data/thermal_link.log -o docs/demo.svg --minutes 45

自动挑选"最热闹"的一段连续样本（无重启间隙），画两个共 x 轴的面板：
    上：整机功率(W) + 风扇转速(%)   —— 功率前馈：功率一跳，风扇立刻拉满
    下：GPU 温度(°C) + 风扇转速(%)  —— 温度滞后：温度要慢半拍才跟上来
两张图画的是同一条风扇曲线，对比即见前馈的价值。
"""
from __future__ import annotations

import argparse
import io
import os
import re
import statistics
import sys
from datetime import datetime

# 日志行形如：
# 2026-10-24 15:33:25  [LIVE] 70°C / 761W  温度项 100% / 功率项 61%  GPU功耗 545W  → 风扇 100%  已下发
LINE = re.compile(
    r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)\s+\[LIVE\]\s+(-?\d+)\s*°C\s*/\s*(\d+)\s*W"
    r".*?风扇\s*(\d+)\s*%"
)

W, H1, H2 = 960, 250, 190
ML, MR, MT, MB = 64, 64, 40, 34
GAP = 26

C_TEXT = "#1f2328"
C_GRID = "#d0d7de"
C_POWER = "#57606a"
C_TEMP = "#cf222e"
C_FAN = "#bc4c00"
C_BG = "#ffffff"


def parse(path: str) -> list[tuple[int, int, int, int]]:
    rows = []
    # splitlines() 而非逐行迭代：日志是 \r\n，逐行迭代在部分环境下会留下尾巴
    text = io.open(path, encoding="utf-8", errors="replace").read()
    for line in text.splitlines():
        m = LINE.match(line)
        if not m:
            continue
        temp = int(m.group(2))
        if temp < 0:  # 温度读取故障行（强制全速），温度无意义，丢弃
            continue
        ts = int(datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S").timestamp())
        rows.append((ts, temp, int(m.group(3)), int(m.group(4))))
    return rows


def runs(rows, max_gap: int = 900) -> list[list]:
    """切成连续段。日志只在速度变化时写入，空闲期本就无记录，
    所以 gap 阈值要放宽（默认 15 分钟），只有真正长时间停机/重启才断段。"""
    out, cur = [], [rows[0]]
    for prev, nxt in zip(rows, rows[1:]):
        if nxt[0] - prev[0] <= max_gap:
            cur.append(nxt)
        else:
            out.append(cur)
            cur = [nxt]
    out.append(cur)
    return out


def best_window(win, minutes: int) -> list:
    """固定时长滑窗，选"最热闹"的一段：风扇转速变化幅度最大，
    其次看功率波动。固定长度保证不同窗口可比。"""
    if not minutes or len(win) < 40:
        return win
    limit = minutes * 60
    lo, best, best_score = 0, [], -1.0
    for hi in range(len(win)):
        while win[hi][0] - win[lo][0] > limit:
            lo += 1
        if hi - lo < 30:
            continue
        seg = win[lo:hi + 1]
        span = seg[-1][0] - seg[0][0]
        if span < min(limit * 0.5, 600):
            continue  # 太短的窗口不要，图会显得局促
        score = sum(abs(b[3] - a[3]) for a, b in zip(seg, seg[1:]))
        score += sum(abs(b[2] - a[2]) for a, b in zip(seg, seg[1:])) * 0.02
        score += span / limit * 5  # 同等热闹程度下偏好填满窗口的一段
        if score > best_score:
            best_score, best = score, seg
    return best or win


def downsample(win, n=300):
    if len(win) <= n:
        return win
    out, bucket = [], len(win) / n
    for i in range(n):
        a = int(i * bucket)
        seg = win[a: max(int((i + 1) * bucket), a + 1)]
        out.append((
            seg[len(seg) // 2][0],
            int(statistics.median(s[1] for s in seg)),
            int(statistics.median(s[2] for s in seg)),
            int(statistics.median(s[3] for s in seg)),
        ))
    return out


def nice_max(v: float) -> int:
    for step in (100, 200, 250, 500, 1000):
        if v <= step:
            return step
    return ((int(v) // 1000) + 1) * 1000


def panel(top: int, h: int, title: str, left: tuple, right: tuple,
          series_left: list, series_right: list, t0: int, t1: int) -> str:
    """left/right = (标签, 颜色, 上限)；series_* = [(ts, value)]"""
    iw, ih = W - ML - MR, h - MT - MB
    p = [f'<rect x="{ML}" y="{top + MT}" width="{iw}" height="{ih}" fill="{C_BG}" '
         f'stroke="{C_GRID}"/>',
         f'<text x="{ML}" y="{top + 20}" font-size="14" font-weight="600" '
         f'fill="{C_TEXT}">{title}</text>']

    lmax, rmax = left[2], right[2]
    for k in range(5):
        frac = k / 4
        yy = top + MT + ih * (1 - frac)
        p.append(f'<line x1="{ML}" y1="{yy:.1f}" x2="{ML + iw}" y2="{yy:.1f}" '
                 f'stroke="{C_GRID}" stroke-dasharray="3,3"/>')
        p.append(f'<text x="{ML - 8}" y="{yy + 4:.1f}" font-size="11" text-anchor="end" '
                 f'fill="{left[1]}">{lmax * frac:.0f}</text>')
        p.append(f'<text x="{ML + iw + 8}" y="{yy + 4:.1f}" font-size="11" '
                 f'fill="{right[1]}">{rmax * frac:.0f}</text>')

    def sx(ts: int) -> float:
        return ML + iw * ((ts - t0) / max(t1 - t0, 1))

    for (label, color, vmax), vals in ((left, series_left), (right, series_right)):
        pts = " ".join(
            f"{sx(ts):.1f},{top + MT + ih * (1 - min(v, vmax) / vmax):.1f}" for ts, v in vals
        )
        p.append(f'<polyline points="{pts}" fill="none" stroke="{color}" stroke-width="2" '
                 f'stroke-linejoin="round" stroke-linecap="round"/>')

    # 图例（画在面板内左上角，带白底避免压线）
    for i, (label, color, _) in enumerate((left, right)):
        lx = ML + 12 + i * 190
        ly = top + MT + 12
        p.append(f'<rect x="{lx - 4}" y="{ly - 11}" width="150" height="17" fill="{C_BG}" '
                 f'opacity="0.85"/>')
        p.append(f'<line x1="{lx}" y1="{ly - 3}" x2="{lx + 18}" y2="{ly - 3}" '
                 f'stroke="{color}" stroke-width="3"/>')
        p.append(f'<text x="{lx + 24}" y="{ly}" font-size="11" fill="{C_TEXT}">{label}</text>')

    for k in range(7):
        frac = k / 6
        xx = ML + iw * frac
        ts = t0 + (t1 - t0) * frac
        p.append(f'<line x1="{xx:.1f}" y1="{top + MT + ih}" x2="{xx:.1f}" '
                 f'y2="{top + MT + ih + 4}" stroke="{C_GRID}"/>')
        p.append(f'<text x="{xx:.1f}" y="{top + MT + ih + 18}" font-size="11" '
                 f'text-anchor="middle" fill="{C_TEXT}">{datetime.fromtimestamp(ts):%H:%M}</text>')
    return "\n".join(p)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("log")
    ap.add_argument("-o", "--out", default="docs/demo.svg")
    ap.add_argument("--minutes", type=int, default=0, help="窗口时长（分钟），取该时长内最热闹的一段")
    args = ap.parse_args()

    rows = parse(args.log)
    if len(rows) < 60:
        sys.exit(f"日志里只解析到 {len(rows)} 条 [LIVE] 记录，太少——先跑一段时间再生成图")

    seg = max(runs(rows), key=lambda r: (len(r), sum(abs(b[3] - a[3]) for a, b in zip(r, r[1:]))))
    win = downsample(best_window(seg, args.minutes))
    t0, t1 = win[0][0], win[-1][0]
    print(f"解析 {len(rows)} 条 → 连续段取窗口 {len(win)} 点（降采样后）"
          f"  {datetime.fromtimestamp(t0):%H:%M}~{datetime.fromtimestamp(t1):%H:%M}"
          f"（{(t1 - t0) / 60:.0f} 分钟）")

    pmax = nice_max(max(r[2] for r in win) * 1.15)
    tmax = nice_max(max(r[1] for r in win) * 1.15)
    total_h = H1 + H2 + GAP + 40

    svg = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {total_h}" '
        f'width="{W}" height="{total_h}" role="img" '
        f'aria-label="GPU 负载下整机功率、GPU 温度与风扇转速的实测联动曲线" '
        f'font-family="-apple-system,BlinkMacSystemFont,Segoe UI,Helvetica,Arial,sans-serif">',
        f'<rect width="{W}" height="{total_h}" fill="{C_BG}"/>',
        panel(24, H1, "整机功率 vs 风扇转速 —— 功率一跳，风扇立刻拉满",
              ("整机功率 (W)", C_POWER, pmax), ("风扇转速 (%)", C_FAN, 100),
              [(t, pw) for t, _, pw, _ in win], [(t, f) for t, _, _, f in win], t0, t1),
        panel(24 + H1 + GAP, H2, "GPU 温度 vs 风扇转速 —— 同一条风扇曲线，温度还慢半拍",
              ("GPU 温度 (°C)", C_TEMP, tmax), ("风扇转速 (%)", C_FAN, 100),
              [(t, c) for t, c, _, _ in win], [(t, f) for t, _, _, f in win], t0, t1),
        f'<text x="{ML}" y="{total_h - 6}" font-size="11" fill="{C_TEXT}">'
        f'来自本项目真实运行日志（数据点已降采样）。橙色曲线两图相同：'
        f'上图功率跳变时风扇已响应，下图温度仍在慢慢爬升</text>',
        "</svg>",
    ]

    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    io.open(args.out, "w", encoding="utf-8").write("\n".join(svg) + "\n")
    print(f"已写入 {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

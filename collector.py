"""米家插座功率采集器 —— 本地 miIO 直读，落 SQLite，附显卡温度以便后续联动分析。

用法:
    python collector.py --duration 60      # 采集 60 秒后退出
    python collector.py --loop             # 常驻采集（默认 2s 一次）
    python collector.py --report           # 只出汇总报告
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
import time
from datetime import datetime

from miio import MiotDevice

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(HERE, "data", "power.db")
CFG = os.path.join(HERE, "config.json")

# pythonw 下子进程会弹黑窗，必须抑制
_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

SCHEMA = """
CREATE TABLE IF NOT EXISTS samples (
    ts        INTEGER NOT NULL,
    power_w   REAL,
    on_state  INTEGER,
    gpu0_temp INTEGER,
    gpu1_temp INTEGER
);
CREATE INDEX IF NOT EXISTS idx_samples_ts ON samples(ts);
CREATE TABLE IF NOT EXISTS minutes (
    minute  INTEGER PRIMARY KEY,
    avg_w   REAL,
    max_w   REAL,
    min_w   REAL,
    n       INTEGER,
    kwh     REAL
);
"""


def load_cfg() -> dict:
    with open(CFG, encoding="utf-8") as f:
        return json.load(f)


def gpu_temps() -> tuple[int | None, int | None]:
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=temperature.gpu", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=6,
            creationflags=_NO_WINDOW,
        )
        vals = [int(x.strip()) for x in out.stdout.strip().splitlines() if x.strip().isdigit()]
        if len(vals) >= 2:
            return vals[0], vals[1]
        if len(vals) == 1:
            return vals[0], None
    except Exception:  # noqa: BLE001
        pass
    return None, None


def ensure_db() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(DB), exist_ok=True)
    con = sqlite3.connect(DB, timeout=10)
    con.executescript(SCHEMA)
    con.execute("PRAGMA journal_mode=WAL")
    con.commit()
    return con


def read_plug(md: MiotDevice, props: dict) -> tuple[float | None, int | None]:
    power = on = None
    ps, pp = props["power_w"]
    ss, sp = props["switch"]
    try:
        r = md.send("get_properties", [
            {"did": "p", "siid": ps, "piid": pp},
            {"did": "s", "siid": ss, "piid": sp},
        ])
        for item in r or []:
            if item.get("code") != 0:
                continue
            if item.get("did") == "p":
                v = item.get("value")
                power = float(v) if isinstance(v, (int, float)) else None
            elif item.get("did") == "s":
                on = 1 if item.get("value") else 0
    except Exception:  # noqa: BLE001
        pass
    return power, on


def aggregate_minute(con: sqlite3.Connection, minute: int) -> None:
    row = con.execute(
        "SELECT AVG(power_w), MAX(power_w), MIN(power_w), COUNT(power_w) "
        "FROM samples WHERE ts >= ? AND ts < ?", (minute * 60, minute * 60 + 60),
    ).fetchone()
    avg, mx, mn, n = row
    if n:
        con.execute(
            "INSERT OR REPLACE INTO minutes(minute,avg_w,max_w,min_w,n,kwh) VALUES (?,?,?,?,?,?)",
            (minute, avg, mx, mn, n, (avg or 0) * n * 2 / 3600 / 1000),
        )


def report(con: sqlite3.Connection, last_n: int = 20) -> None:
    rows = con.execute(
        "SELECT ts, power_w, on_state, gpu0_temp, gpu1_temp FROM samples "
        "ORDER BY ts DESC LIMIT ?", (last_n,),
    ).fetchall()
    print(f"\n最近 {len(rows)} 条样本：")
    print(f"{'时间':<20}{'功率(W)':>10}{'通电':>6}{'GPU0':>7}{'GPU1':>7}")
    for ts, p, on, g0, g1 in reversed(rows):
        t = datetime.fromtimestamp(ts).strftime("%H:%M:%S")
        print(f"{t:<20}{(f'{p:.1f}' if p is not None else '-'):>10}"
              f"{('是' if on else '否'):>6}{str(g0 or '-'):>7}{str(g1 or '-'):>7}")

    tot = con.execute("SELECT COUNT(*), MIN(ts), MAX(ts) FROM samples").fetchone()
    if tot[0]:
        print(f"\n累计样本 {tot[0]} 条，跨度 "
              f"{datetime.fromtimestamp(tot[1]):%Y-%m-%d %H:%M:%S} ~ {datetime.fromtimestamp(tot[2]):%H:%M:%S}")
    agg = con.execute(
        "SELECT SUM(kwh), SUM(n), AVG(avg_w), MAX(max_w) FROM minutes").fetchone()
    if agg[0] is not None:
        print(f"累计电量 {agg[0]:.4f} kWh（{agg[1]} 个样本点），平均 {agg[2]:.1f} W，峰值 {agg[3]:.1f} W")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--duration", type=int, default=0, help="采集秒数")
    ap.add_argument("--loop", action="store_true", help="常驻")
    ap.add_argument("--interval", type=float, default=2.0)
    ap.add_argument("--report", action="store_true")
    args = ap.parse_args()

    con = ensure_db()
    if args.report:
        report(con)
        return 0

    cfg = load_cfg()["devices"]["plug"]
    md = MiotDevice(cfg["ip"], cfg["token"], mapping={})
    props = cfg["props"]

    total = args.duration if args.duration else (0 if args.loop else 10)
    end = time.time() + total if total else None
    n = 0
    last_minute = None
    t_start = time.time()
    print(f"开始采集：{cfg['name']} @ {cfg['ip']}，间隔 {args.interval}s"
          f"，{'常驻' if args.loop else f'{total}s'}")
    while end is None or time.time() < end:
        power, on = read_plug(md, props)
        g0, g1 = gpu_temps()
        ts = int(time.time())
        con.execute("INSERT INTO samples(ts,power_w,on_state,gpu0_temp,gpu1_temp) VALUES (?,?,?,?,?)",
                    (ts, power, on, g0, g1))
        con.commit()
        n += 1
        minute = ts // 60
        if last_minute is not None and minute != last_minute:
            aggregate_minute(con, last_minute)
            con.commit()
        last_minute = minute
        if n % 10 == 0:
            print(f"  {datetime.fromtimestamp(ts):%H:%M:%S}  {power if power is None else round(power,1)} W"
                  f"   GPU {g0}/{g1}°C", flush=True)
        time.sleep(args.interval)

    if last_minute:
        aggregate_minute(con, last_minute)
        con.commit()
    print(f"\n采集完成：{n} 条样本，用时 {time.time()-t_start:.0f}s")
    report(con)
    con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""GPU 温度 / 整机功率 → 米家循环扇 变速控制器。

设计要点：
  - 功率前馈：插座功率比 GPU 温度早 5-10s 响应，用于提前拉风量
  - 取 max(温度项, 功率项)，任一信号到顶即全速
  - 非对称滤波：上升立即跟随，下降缓慢，避免抖动
  - 量化 5% + 最小改变间隔，保护风扇驱动电路
  - 降速滞回：必须"彻底低温"持续一段时间才允许下调
  - 空闲停转：低功率 + 低温度持续 N 秒 → 关开关停转，带迟滞唤醒
  - 故障保护：温度连续读取失败 → 强制 100%

安全：永不操作插座开关（该插座给本机供电）。

用法:
    python thermal_link.py --once
    python thermal_link.py --loop
    python thermal_link.py --loop --live
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime

from miio import MiotDevice

HERE = os.path.dirname(os.path.abspath(__file__))
CFG = os.path.join(HERE, "config.json")
LOG = os.path.join(HERE, "data", "thermal_link.log")

# pythonw 无控制台，子进程默认会新开黑窗，必须显式抑制
_NO_WINDOW = 0x08000000 if os.name == "nt" else 0
LOCK = os.path.join(HERE, "data", "thermal_link.lock")


def _pid_alive(pid: int) -> bool:
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
    import ctypes
    h = ctypes.windll.kernel32.OpenProcess(0x1000, 0, pid)  # QUERY_LIMITED
    if not h:
        return False
    try:
        code = ctypes.c_ulong(0)
        ctypes.windll.kernel32.GetExitCodeProcess(h, ctypes.byref(code))
        return code.value == 259  # STILL_ACTIVE
    finally:
        ctypes.windll.kernel32.CloseHandle(h)


def acquire_lock() -> bool:
    """单实例锁。venv 的 pythonw 是重定向器、会派生两个 PID，
    所以用 PID 文件 + 存活检测，不依赖 WMI 命令行匹配。"""
    os.makedirs(os.path.dirname(LOCK), exist_ok=True)
    for _ in range(2):
        try:
            fd = os.open(LOCK, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
            return True
        except FileExistsError:
            try:
                pid = int(open(LOCK, encoding="utf-8").read().strip() or 0)
            except Exception:  # noqa: BLE001
                pid = 0
            if pid and _pid_alive(pid):
                return False
            try:
                os.remove(LOCK)
            except OSError:
                return False
    return False


def release_lock() -> None:
    try:
        os.remove(LOCK)
    except OSError:
        pass


def load_cfg() -> dict:
    with open(CFG, encoding="utf-8") as f:
        return json.load(f)


def log(msg: str) -> None:
    line = f"{datetime.now():%Y-%m-%d %H:%M:%S}  {msg}"
    try:
        print(line, flush=True)
    except Exception:  # noqa: BLE001  pythonw 下 stdout 为 None
        pass
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def clamp(v: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, v))


def read_gpus() -> list[dict]:
    """返回每张卡的 温度/功耗/利用率。"""
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=temperature.gpu,power.draw,utilization.gpu",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5,
            creationflags=_NO_WINDOW,
        )
        rows = []
        for line in out.stdout.strip().splitlines():
            parts = [p.strip() for p in line.split(",")]
            if len(parts) >= 3 and parts[0].isdigit():
                rows.append({
                    "temp": int(parts[0]),
                    "power": float(parts[1]) if parts[1].replace(".", "").isdigit() else None,
                    "util": int(parts[2]) if parts[2].isdigit() else None,
                })
        return rows
    except Exception:  # noqa: BLE001
        return []


class Linker:
    def __init__(self, cfg: dict, live: bool | None):
        self.cfg = cfg
        tl = cfg["thermal_link"]
        self.tl = tl
        # config 是唯一事实来源：enabled + dry_run 决定默认行为；
        # 命令行 --live / --dry-run 可显式覆盖（None 表示不覆盖）
        cfg_live = bool(tl.get("enabled")) and not bool(tl.get("dry_run", True))
        # enabled=false 是硬开关，任何命令行参数都盖不过
        self.live = bool(tl.get("enabled")) and (cfg_live if live is None else bool(live))

        fan = cfg["devices"]["fan"]
        self.fan_ip, self.fan_token = fan["ip"], fan["token"]
        self.fan_props = fan["props"]
        self._fan: MiotDevice | None = None

        plug = cfg["devices"]["plug"]
        self.plug_ip, self.plug_token = plug["ip"], plug["token"]
        self.plug_props = plug["props"]
        self._plug: MiotDevice | None = None

        self.smoothed: float | None = None
        self.last_apply_ts = 0.0
        self.last_applied = None
        self.hold_until = 0.0
        self.drop_since: float | None = None
        self.idle_since: float | None = None
        self.low_since: float | None = None
        self.temp_fail = 0
        self.current_speed: int | None = None
        self.fan_on: bool | None = None  # None = 未知，首次下发时读一次

    @property
    def fan(self) -> MiotDevice:
        # 懒建联：DRY-RUN / 仿真测试全程不碰设备
        if self._fan is None:
            self._fan = MiotDevice(self.fan_ip, self.fan_token, mapping={})
        return self._fan

    @property
    def plug(self) -> MiotDevice:
        if self._plug is None:
            self._plug = MiotDevice(self.plug_ip, self.plug_token, mapping={})
        return self._plug

    def _read_fan_on(self) -> bool:
        siid, piid = self.fan_props["switch"]
        try:
            r = self.fan.send("get_properties", [{"did": "s", "siid": siid, "piid": piid}])
            return bool(r[0].get("value")) if r else False
        except Exception:  # noqa: BLE001
            return False

    def read_power(self) -> float | None:
        siid, piid = self.plug_props["power_w"]
        try:
            r = self.plug.send("get_properties", [{"did": "p", "siid": siid, "piid": piid}])
            if r and r[0].get("code") == 0:
                return float(r[0]["value"])
        except Exception:  # noqa: BLE001
            pass
        return None

    def target_speed(self, temp: int, power: float | None, allow_off: bool = False) -> tuple[float, str]:
        tl = self.tl
        t_ratio = clamp((temp - tl["temp_start_c"]) / (tl["temp_full_c"] - tl["temp_start_c"]))
        p_ratio = 0.0
        why = f"温度项 {t_ratio*100:.0f}%"
        if power is not None:
            p_ratio = clamp((power - tl["power_start_w"]) / (tl["power_full_w"] - tl["power_start_w"]))
            why += f" / 功率项 {p_ratio*100:.0f}%"
        raw = 100.0 * max(t_ratio, p_ratio)
        # 允许停转时下限为 0，否则保持常转下限
        floor = 0.0 if allow_off else float(tl.get("idle_floor_percent", 20))
        raw = max(raw, floor) if max(t_ratio, p_ratio) > 0.02 else floor
        return raw, why

    def smooth(self, target: float, dt: float) -> float:
        if self.smoothed is None:
            self.smoothed = target
            return target
        tau = self.tl["ema_up_seconds"] if target > self.smoothed else self.tl["ema_down_seconds"]
        alpha = 1.0 - pow(2.718281828, -dt / max(tau, 0.1))
        self.smoothed += (target - self.smoothed) * alpha
        return self.smoothed

    def apply(self, speed: int, why: str, temp: int, power: float | None) -> None:
        tl = self.tl
        if not self.live:
            log(f"[DRY] {temp}°C / {power if power is None else round(power)}W  {why}  → 风扇 {speed}%")
            return
        try:
            if self.fan_on is None:
                # 首次下发前读真实开关状态，避免重复开/关或盲设速度
                self.fan_on = self._read_fan_on()
            if speed > 0 and not self.fan_on:
                siid, piid = self.fan_props["switch"]
                self.fan.send("set_properties", [{"did": "on", "siid": siid, "piid": piid, "value": True}])
                self.fan_on = True
                time.sleep(0.3)
            if speed == 0 and self.fan_on:
                siid, piid = self.fan_props["switch"]
                self.fan.send("set_properties", [{"did": "off", "siid": siid, "piid": piid, "value": False}])
                self.fan_on = False
            elif speed > 0:
                siid, piid = self.fan_props["speed"]
                self.fan.send("set_properties", [{"did": "sp", "siid": siid, "piid": piid, "value": int(speed)}])
            self.current_speed = speed
            log(f"[LIVE] {temp}°C / {power if power is None else round(power)}W  {why}  → 风扇 {speed}%  已下发")
        except Exception as exc:  # noqa: BLE001
            log(f"[ERR] 下发失败 {type(exc).__name__}: {exc}")

    def step(self) -> None:
        tl = self.tl
        gpus = read_gpus()
        now = time.time()

        if not gpus:
            self.temp_fail += 1
            log(f"[WARN] 读取显卡温度失败（连续 {self.temp_fail} 次）")
            if self.temp_fail >= 3:
                self.apply(100, "温度读取故障 → 强制全速", -1, None)
                self.last_apply_ts = now
            return
        self.temp_fail = 0

        temp = max(g["temp"] for g in gpus)
        gpu_power = sum(g["power"] or 0 for g in gpus)
        power = self.read_power()

        # 空闲判定：功率低 + 温度低，带迟滞，需持续 idle_stop_hold_seconds 才允许停转
        stop_w = float(tl.get("idle_stop_power_w", 200))
        wake_w = float(tl.get("idle_wake_power_w", 240))
        stop_t = float(tl.get("idle_stop_temp_c", 45))
        wake_t = float(tl.get("idle_wake_temp_c", 48))
        p_known = power is not None
        busy_now = (p_known and power >= wake_w) or temp >= wake_t
        idle_now = (p_known and power < stop_w) and temp < stop_t
        if busy_now:
            self.idle_since = None
        elif idle_now and self.idle_since is None:
            self.idle_since = now
        allow_off = (self.idle_since is not None
                     and (now - self.idle_since) >= float(tl.get("idle_stop_hold_seconds", 600)))

        target, why = self.target_speed(temp, power, allow_off)
        if allow_off:
            why += f" / 闲置{int(now - self.idle_since)}s"
        dt = (now - self.last_apply_ts) if self.last_apply_ts else tl["sample_seconds"]

        # 就高不就低：降速必须"彻底低温"持续 drop_delay_seconds 才允许；
        # 升速不受任何等待，立即执行。
        drop_delay = float(tl.get("drop_delay_seconds", 180))
        wants_lower = self.smoothed is not None and target < self.smoothed - 0.5
        if wants_lower:
            if self.drop_since is None:
                self.drop_since = now
            allow_drop = (now - self.drop_since) >= drop_delay
        else:
            self.drop_since = None
            allow_drop = True
        # 完全空闲已达 hold 时长：直接停转，不再叠加冷却计时
        if allow_off:
            allow_drop = True

        if self.smoothed is None:
            self.smoothed = target
            smoothed = target
        elif allow_off:
            smoothed = 0.0
        elif target >= self.smoothed or allow_drop:
            smoothed = self.smooth(target, max(dt, 0.5))
        else:
            smoothed = self.smoothed  # 冻结在高位，不衰减

        speed = int(round(smoothed / tl["min_step"]) * tl["min_step"])
        speed = max(0, min(int(tl["max_speed"]), speed))

        if self.last_applied is not None:
            if abs(speed - self.last_applied) < tl["min_step"]:
                return
            if now - self.last_apply_ts < tl["min_change_interval"]:
                return
            # 未通过"持续低温"检验，禁止下调
            if speed < self.last_applied and not allow_drop:
                return

        self.apply(speed, f"{why}  GPU功耗 {gpu_power:.0f}W", temp, power)
        if self.last_applied is None or speed > self.last_applied:
            self.drop_since = None  # 刚升过速，低温计时重新开始
        self.last_applied = speed
        self.last_apply_ts = now


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--loop", action="store_true")
    ap.add_argument("--live", action="store_true", default=None, help="强制真实控制（覆盖 config）")
    ap.add_argument("--dry-run", action="store_true", help="强制只记录（覆盖 config）")
    args = ap.parse_args()

    cfg = load_cfg()
    tl = cfg["thermal_link"]
    force = None
    if args.live:
        force = True
    if args.dry_run:
        force = False
    lk = Linker(cfg, live=force)

    mode = "LIVE" if lk.live else "DRY-RUN"
    log(f"启动 [{mode}]  采样{tl['sample_seconds']}s  上升τ{tl['ema_up_seconds']}s/下降τ{tl['ema_down_seconds']}s  "
        f"温度 {tl['temp_start_c']}→{tl['temp_full_c']}°C  功率 {tl['power_start_w']}→{tl['power_full_w']}W  "
        f"最低 {tl.get('idle_floor_percent', 0)}%")
    if args.live and not lk.live:
        log("[BLOCK] --live 被忽略：config 里 enabled=false（硬开关）")

    if args.once:
        lk.step()
        return 0

    if not acquire_lock():
        log("[SKIP] 已有实例在运行（PID 锁占用），本进程退出")
        return 0
    try:
        while True:
            lk.step()
            time.sleep(tl["sample_seconds"])
    except KeyboardInterrupt:
        log("中断退出")
    finally:
        release_lock()
    return 0


if __name__ == "__main__":
    sys.exit(main())

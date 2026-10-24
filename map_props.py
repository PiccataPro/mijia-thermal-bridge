"""逐属性扫描 MIoT 表，找出设备真实可读的 siid/piid，用于填 config.json 的 props。

调用方式必须用 md.send("get_properties", ...)：python-miio 的包装方法在部分固件上
会返回 "undefined command" 错误，绕过包装直接走底层协议才稳。

用法:
    python map_props.py --ip 192.168.1.52 --token <32位hex>
    python map_props.py --ip 192.168.1.52 --token <token> --siid 2 --piid 1-12
"""
from __future__ import annotations

import argparse
import json
import sys
import time

from miio import MiotDevice


def parse_ranges(spec: str, default: range) -> range:
    """'1-12' / '1,3,5' / '' → range"""
    if not spec:
        return default
    if "-" in spec:
        a, b = spec.split("-")
        return range(int(a), int(b) + 1)
    if "," in spec:
        vals = [int(x) for x in spec.split(",")]
        return range(min(vals), max(vals) + 1)
    return range(int(spec), int(spec) + 1)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ip", required=True)
    ap.add_argument("--token", required=True)
    ap.add_argument("--siid", default="", help="如 2 或 2-9，默认 2-9")
    ap.add_argument("--piid", default="", help="如 1 或 1-12，默认 1-8")
    ap.add_argument("--out", default="", help="结果 JSON 输出路径")
    args = ap.parse_args()

    siids = parse_ranges(args.siid, range(2, 10))
    piids = parse_ranges(args.piid, range(1, 9))

    md = MiotDevice(args.ip, args.token, mapping={})
    print(f"===== {args.ip} =====", flush=True)
    found = []
    for siid in siids:
        for piid in piids:
            try:
                res = md.send("get_properties", [{"did": "x", "siid": siid, "piid": piid}])
                if res and res[0].get("code") == 0:
                    v = res[0].get("value")
                    print(f"  siid={siid} piid={piid}  value={v!r}", flush=True)
                    found.append({"siid": siid, "piid": piid, "value": v})
            except Exception as exc:  # noqa: BLE001
                msg = str(exc)
                if "undefined" not in msg and "timeout" not in msg:
                    print(f"  siid={siid} piid={piid}  ERR {type(exc).__name__}: {msg[:60]}", flush=True)
            time.sleep(0.02)

    out = args.out or f"props_{args.ip.replace('.', '_')}.json"
    json.dump(found, open(out, "w", encoding="utf-8"),
              ensure_ascii=False, indent=2, default=str)
    print(f"\n共 {len(found)} 个可读属性，已存 {out}")
    print("下一步：把确认要用的 [siid, piid] 填进 config.json 的 devices.*.props")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

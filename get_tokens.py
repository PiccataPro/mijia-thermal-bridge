"""从米家云端拉取局域网设备的 token（python-miio 本地控制的前置条件）。

token 是 32 位 hex，设备局域网 miIO 协议的 AES 密钥。官方 App 不会显示它，
常见做法是从云端设备列表接口读——本脚本走"浏览器登录 + 脚本换 token"的路线，
不需要把小米账号密码交给脚本，也不做验证码/2FA 自动化。

流程:
    1. 用已登录的浏览器打开 login_url，登录后会跳转到一个 sts 开头的
       `location` URL（页面显示 `&&&START&&&{"location":"https://sts.api.io.mi.com/sts?..."}&&&END&&&`）
    2. 把这段 JSON（或 location URL 本身）保存成文件交给本脚本
    3. 脚本 GET location 换取 serviceToken + ssecurity
    4. 用 ssecurity 对 home/device_list 请求做 RC4+HMAC 签名（复用 micloud 的 miutils）
    5. 输出每台设备的 name / model / did / token / localip

注意:
    - location 有时效，取到后尽快运行脚本
    - 若登录时提示"设备/浏览器需要验证"，在浏览器里完成验证再复制 location
    - 输出的 token 等同设备控制密钥，不要提交到仓库

用法:
    python get_tokens.py <location.json 或含 location 的文本文件>
    python get_tokens.py --print-login-url
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.parse

import requests
from micloud import miutils

DEVICE_LIST_URL = "https://api.io.mi.com/app/home/device_list"

# 登录页：serviceLogin 指向 io.mi 的 sts，回调里就能拿到 location
LOGIN_URL = (
    "https://account.xiaomi.com/long/serviceLogin"
    "?sid=home_proto_biz&callback=https%3A%2F%2Fhome.io.mi.com%2Fhome%2F"
    "&_json=true"
)


def extract_location(text: str) -> str | None:
    """从 `&&&START&&&{...}&&&END&&&` / JSON / 裸 URL 里取出 sts location。"""
    text = text.strip()
    if text.startswith("&&&START&&&"):
        text = text[len("&&&START&&&"):]
    m = re.search(r"https://sts\.api\.io\.mi\.com/sts[^\s\"'}]*", text)
    if m:
        return m.group(0).replace("\\u0026", "&").replace("\\/", "/")
    m = re.search(r'"location"\s*:\s*"([^"]+)"', text)
    if m:
        return m.group(1).replace("\\u0026", "&").replace("\\/", "/")
    if text.startswith("http"):
        return text.split()[0]
    return None


def session() -> requests.Session:
    s = requests.Session()
    s.trust_env = False  # 直连，避免被本机代理干扰
    s.headers.update({
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36")
    })
    return s


def fetch_device_list(s: requests.Session, location: str) -> list[dict]:
    print("1) 换取 serviceToken ...")
    r = s.get(location, timeout=20, allow_redirects=False)
    print(f"   STS: HTTP {r.status_code}")
    token = s.cookies.get("serviceToken") or r.cookies.get("serviceToken")
    if token:
        s.cookies.set("serviceToken", token)
        s.cookies.set("yetAnotherServiceToken", token)
    print("   serviceToken: " + ((token[:12] + "..." + token[-6:]) if token else "<无>"))

    # ssecurity 可能在 STS 响应体里，也可能已在 cookie 中
    ssec = None
    try:
        js = r.json()
        ssec = js.get("ssecurity")
        user_id = js.get("userId") or s.cookies.get("userId")
        cuser_id = js.get("cUserId") or s.cookies.get("cUserId")
    except ValueError:
        ssec = s.cookies.get("ssecurity")
        user_id = s.cookies.get("userId")
        cuser_id = s.cookies.get("cUserId")
    if not ssec:
        ssec = s.cookies.get("ssecurity")
    if not ssec:
        sys.exit("拿不到 ssecurity：location 可能已过期，请重新登录再复制")
    print(f"   userId={user_id}  ssecurity=有")

    print("2) 签名并拉取 device_list ...")
    s.headers.update({
        "Accept-Encoding": "identity",
        "x-xiaomi-protocal-flag-cli": "PROTOCAL-HTTP2",
        "content-type": "application/x-www-form-urlencoded",
        "MIOT-ENCRYPT-ALGORITHM": "ENCRYPT-RC4",
        "User-Agent": "Android-7.1.1-1.0.0-ONEPLUS A3010-136-UNKNOWN APP/xiaomi.smarthome",
    })
    s.cookies.update({
        "userId": str(user_id or ""),
        "cUserId": str(cuser_id or ""),
        "locale": "zh_CN",
        "timezone": "Asia/Shanghai",
        "is_daylight": str(time.daylight),
        "dst_offset": str(time.localtime().tm_isdst * 3600000),
        "channel": "MI_APP_STORE",
    })

    nonce = miutils.gen_nonce()
    sn = miutils.signed_nonce(ssec, nonce)
    params = {"data": '{"getVirtualModel":false,"getHuamiDevices":0}'}
    post = miutils.generate_enc_params(DEVICE_LIST_URL, "POST", sn, nonce, params, ssec)
    resp = s.post(DEVICE_LIST_URL, data=post, timeout=20)
    print(f"   HTTP {resp.status_code}")
    if resp.status_code != 200:
        sys.exit(f"device_list 失败: {resp.text[:200]}")

    plain = miutils.decrypt_rc4(miutils.signed_nonce(ssec, post["_nonce"]), resp.text)
    payload = json.loads(plain)
    if payload.get("code") != 0:
        sys.exit(f"接口返回异常: {plain[:300]}")
    return (payload.get("result") or {}).get("list") or []


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("location_file", nargs="?", help="含 location 的文件（登录回调复制的 JSON）")
    ap.add_argument("--print-login-url", action="store_true")
    ap.add_argument("--out", default="cloud_devices.json")
    args = ap.parse_args()

    if args.print_login_url:
        print("用已登录小米账号的浏览器打开下面地址，登录后把页面里的 JSON 整段存成文件：")
        print()
        print(LOGIN_URL)
        print()
        print("页面内容形如： &&&START&&&{\"location\":\"https://sts.api.io.mi.com/sts?...\",...}&&&END&&&")
        print("然后: python get_tokens.py location.json")
        return 0

    if not args.location_file:
        ap.print_help()
        return 2
    raw = open(args.location_file, encoding="utf-8", errors="ignore").read()
    loc = extract_location(raw)
    if not loc:
        print("未能解析出 location。原始内容前 300 字符：")
        print(raw[:300])
        return 1
    print(f"location = {loc[:100]}...")

    devices = fetch_device_list(session(), loc)
    print(f"\n共 {len(devices)} 台设备\n")
    print(f"{'名称':<26}{'model':<28}{'did':<12}{'token':<34}IP")
    print("-" * 120)
    for d in devices:
        print(f"{str(d.get('name',''))[:25]:<26}{str(d.get('model',''))[:27]:<28}"
              f"{str(d.get('did','')):<12}{str(d.get('token','')):<34}{d.get('localip','')}")

    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(devices, f, ensure_ascii=False, indent=2)
    print(f"\n已存 {args.out}（含 token，等同控制密钥，别提交进 git）")
    print("下一步：把要用的设备 ip/token 填进 config.json，并用 map_props.py 确认 siid/piid")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

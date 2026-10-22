"""用 RDAP（WHOIS 的新标准）查域名的注册时间，识别新注册的冒名站。

冒用"CCTV5""央视"名义的体育站基本都是近一两年才注册的（2026-10-18 实测：
cntv-sports.com 注册于 2025-07，vip-sports-cctv.com 注册于 2025-10，直播吧 zhibo8.com 是 2007 年）。
注册时间只是信号，不是结论。

只用标准库。查询走 rdap.org，它会转到对应注册局的 RDAP 服务。

用法：
    python domain_age.py cntv-sports.com zhibo8.com
    python domain_age.py https://www.cntv-sports.com/34675/nqxD2224/   # 也可以直接给网址
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from datetime import date
from urllib.parse import urlparse


def registrable(host_or_url: str) -> str:
    host = urlparse(host_or_url).hostname if "://" in host_or_url else host_or_url
    host = (host or "").strip().lower().rstrip(".")
    parts = host.split(".")
    # 粗略取主域名：www.cntv-sports.com → cntv-sports.com；.com.cn 这类二级后缀保留三段
    if len(parts) >= 3 and parts[-2] in {"com", "net", "org", "gov", "edu", "co"} and len(parts[-1]) == 2:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def lookup(domain: str) -> dict:
    request = urllib.request.Request(
        f"https://rdap.org/domain/{domain}", headers={"Accept": "application/rdap+json"}
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        data = json.loads(response.read())
    events = {e.get("eventAction"): (e.get("eventDate") or "")[:10] for e in data.get("events", [])}
    return {"registered": events.get("registration"), "expires": events.get("expiration")}


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    today = date.today()
    for raw in sys.argv[1:]:
        domain = registrable(raw)
        try:
            info = lookup(domain)
        except urllib.error.HTTPError as err:
            print(f"{domain:28s} 查不到（HTTP {err.code}；部分国别域名如 .cn 不提供 RDAP，可改用注册局的 WHOIS 页面）")
            continue
        except (urllib.error.URLError, TimeoutError, ValueError) as err:
            print(f"{domain:28s} 查询失败：{err}")
            continue
        registered = info["registered"]
        if not registered:
            print(f"{domain:28s} 没有返回注册时间")
            continue
        years = (today - date.fromisoformat(registered)).days / 365.25
        hint = "  ← 注册不到两年，若冒用知名媒体名义要警惕" if years < 2 else ""
        print(f"{domain:28s} 注册于 {registered}（约 {years:.1f} 年）{hint}")


if __name__ == "__main__":
    main()

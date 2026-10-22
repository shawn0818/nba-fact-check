"""DeepSeek Search / Fetch 命令行封装，供事实核查检索使用。

只用标准库。Key 的读取顺序：环境变量 DEEPSEEK_API_KEY → --env-file 指定的
.env 文件里的 DEEPSEEK_API_KEY 或 AI__DEEPSEEK_API_KEY。

用法：
    python search.py search "詹姆斯 76人 site:bbs.hupu.com after:2026-10-01"
    python search.py search "LeBron James Instagram" --count 5 --content
    python search.py fetch https://news.zhibo8.com/nba/....htm
    python search.py search "..." --json        # 输出原始 JSON（--json 放在子命令前后都可以）

    # 记账：每次调用往 JSONL 文件追加一行（参数、耗时、状态、重试次数、结果数），不记 key
    python search.py --log calls.jsonl --label H03-英文源 search "..."

计费：Search / Fetch 均按成功请求计费，无结果或失败不计费。账号级 5 QPS。
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

API_BASE = "https://api.deepseek.com/v1/tools"
RETRYABLE = {408, 429, 500, 502, 503, 504}


def load_key(env_file: str | None) -> str:
    key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if key:
        return key
    if env_file:
        for line in Path(env_file).read_text(encoding="utf-8").splitlines():
            name, sep, value = line.partition("=")
            if sep and name.strip() in ("DEEPSEEK_API_KEY", "AI__DEEPSEEK_API_KEY"):
                value = value.strip().strip('"').strip("'")
                if value:
                    return value
    sys.exit("缺少 DeepSeek key：设置环境变量 DEEPSEEK_API_KEY，或用 --env-file 指向含它的 .env")


def call(endpoint: str, body: dict, key: str, retries: int = 1, timeout: float = 90) -> tuple[int, dict | str, int]:
    """返回 (HTTP 状态, 数据或错误文本, 实际请求次数)。网络错误记为状态 0。"""
    request = urllib.request.Request(
        f"{API_BASE}/{endpoint}",
        data=json.dumps(body).encode("utf-8"),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
    )
    attempts = 0
    for attempt in range(retries + 1):
        attempts += 1
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.status, json.loads(response.read()), attempts
        except urllib.error.HTTPError as err:
            detail = err.read().decode("utf-8", "replace")
            if err.code in RETRYABLE and attempt < retries:
                # 重试要打到 stderr：调用次数统计需要知道底层其实请求了几次
                print(f"[重试] HTTP {err.code}，3 秒后第 {attempt + 2} 次请求", file=sys.stderr)
                time.sleep(3)
                continue
            return err.code, detail, attempts
        except (urllib.error.URLError, TimeoutError) as err:
            if attempt < retries:
                print(f"[重试] 网络错误 {err}，3 秒后第 {attempt + 2} 次请求", file=sys.stderr)
                time.sleep(3)
                continue
            return 0, f"网络错误：{err}", attempts
    raise AssertionError("unreachable")


def write_log(path: str, entry: dict) -> None:
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")


def print_search(data: dict, show_content: bool) -> None:
    results = data.get("results", [])
    if not results:
        print("（无结果）")
        return
    for i, item in enumerate(results, 1):
        print(f"[{i}] {item.get('title', '')}")
        print(f"    {item.get('url', '')}")
        print(
            f"    date={item.get('date')} site={item.get('site_name', '')} "
            f"authority={item.get('authority')} rerank={item.get('rerank_score', 0):.2f}"
        )
        summary = (item.get("summary") or item.get("snippet") or "").strip()
        if summary:
            print(f"    {summary[:300]}")
        if show_content and item.get("content"):
            print("    --- content ---")
            print(item["content"])
        print()


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    parser.add_argument("--env-file", help="含 DEEPSEEK_API_KEY 的 .env 文件")
    parser.add_argument("--json", action="store_true", help="输出原始 JSON")
    parser.add_argument("--log", help="调用账本（JSONL），每次调用追加一行；不记 key")
    parser.add_argument("--label", default="", help="写进账本的标签，例如样本编号或用途")
    parser.add_argument("--retries", type=int, default=1,
                        help="遇到 5xx 或网络错误时自动重试的次数，默认 1；要严格控制请求数时设 0")
    parser.add_argument("--timeout", type=float, default=90,
                        help="单次请求的超时秒数，默认 90；最长等待约为 超时 ×（重试次数 + 1）")
    # --json 也允许写在子命令后面；SUPPRESS 保证没写时不会覆盖前面的值
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help="输出原始 JSON")
    sub = parser.add_subparsers(dest="mode", required=True)

    p_search = sub.add_parser("search", help="网页搜索", parents=[common])
    p_search.add_argument("query", help="1-128 字符，支持 site: -site: \"精确\" lang: after: before:")
    p_search.add_argument("--count", type=int, default=10, help="1-50，默认 10")
    p_search.add_argument("--content", action="store_true", help="返回索引中的网页正文")

    p_fetch = sub.add_parser("fetch", help="抓取单个网页为 Markdown", parents=[common])
    p_fetch.add_argument("url")
    p_fetch.add_argument("--max-chars", type=int, default=0, help="只打印前 N 个字符，0 为全部")

    args = parser.parse_args()
    key = load_key(args.env_file)

    started = dt.datetime.now(dt.timezone.utc)
    t0 = time.monotonic()
    if args.mode == "search":
        body = {"query": args.query, "count": args.count, "need_content": args.content}
        status, data, attempts = call("search", body, key, retries=max(0, args.retries), timeout=args.timeout)
    else:
        body = {"url": args.url}
        status, data, attempts = call("fetch", body, key, retries=max(0, args.retries), timeout=args.timeout)
    elapsed = round(time.monotonic() - t0, 3)

    if args.log:
        entry = {
            "at": started.isoformat(timespec="seconds"),
            "label": args.label,
            "mode": args.mode,
            **body,
            "status": status,
            "attempts": attempts,
            "seconds": elapsed,
        }
        if status == 200 and isinstance(data, dict):
            if args.mode == "search":
                entry["results"] = len(data.get("results", []))
            else:
                entry["content_chars"] = len(data.get("content", ""))
        write_log(args.log, entry)

    if status != 200:
        print(f"HTTP {status}: {data}", file=sys.stderr)
        sys.exit(1)
    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
    elif args.mode == "search":
        print_search(data, args.content)
    else:
        content = data.get("content", "")
        if len(content) < 800:
            # 实测：抓 NBA.com 比赛页返回成功却只有一千多字的导航；成功不等于拿到了正文
            print(f"[提醒] 只抓到 {len(content)} 字符：可能只是页面外壳，也可能本来就是短讯。"
                  "按字数判断的粗略提醒，看一眼关键内容在不在再用", file=sys.stderr)
        print(f"# {data.get('title', '')}  ({len(content)} 字符)\n")
        print(content[: args.max_chars] if args.max_chars else content)


if __name__ == "__main__":
    main()

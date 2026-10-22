"""在采访视频的英文字幕里定位一句话，打印带时间戳的上下文。

用于核对引语：先找到原话在第几秒、前后几轮怎么说，再决定要不要判断说话人
（见 speakers.py）。只下载字幕，不下载视频。

字幕从哪来（按地址自动判断）：
  - YouTube 等 yt-dlp 支持的网站：只下载字幕（json3），顺带存下视频元数据——频道名、频道 ID、上传时间，
    存成 <视频ID>.meta.json，输出开头也会打印，用来核对是不是官方频道、哪天上传的；
  - NBA.com 球队官网的视频页（例如 https://www.nba.com/cavaliers/videos/...）：页面内嵌的
    `__NEXT_DATA__.props.pageProps.pageObject.videoAssets.cc` 里有 SRT / VTT 字幕地址，脚本直接取，
    同时存下标题和发布时间；
  - 字幕文件直链（.srt / .vtt / .json3）；
  - 本地字幕文件：json3、SRT、VTT 按扩展名识别，也可以用 --json3 / --srt / --vtt 指定。

依赖：YouTube 要 yt-dlp 在 PATH 上（或用 --ytdlp 指定）；其余只用标准库。

用法：
    python captions.py "https://www.youtube.com/watch?v=zNaVP54W_VY" teleprompter
    python captions.py URL "read off" --window 20
    python captions.py URL teleprompter --out-dir D:/tmp/caps    # 保留字幕文件和元数据
    python captions.py --json3 已下载的.json3 teleprompter       # 跳过下载
    python captions.py 已下载的.srt "didn't think"                # 本地 SRT / VTT，按扩展名识别
    python captions.py URL teleprompter --ytdlp-args "--js-runtimes node"   # 额外的 yt-dlp 参数
    python captions.py https://www.nba.com/cavaliers/videos/cavs-media-day-donovan-mitchell-media-availability-09-28-2026 Philly

    # 先找原视频：中文稿常转述某期播客、发布会，用节目名 + 嘉宾 + 日期搜 YouTube
    python captions.py --find "Mind the Game LeBron Nash season preview" --count 5

    # 记账：每次字幕下载、页面请求、视频搜索追加一行 JSONL（格式同 search.py：at、label、mode、status、attempts、seconds）
    python captions.py URL teleprompter --log calls.jsonl --label H03
    # yt-dlp 没有 HTTP 状态码，status 记 ok / error，另记 returncode；直连下载记 HTTP 状态码；读本地文件不联网，不记账

输出说明：每行是一个字幕事件，`>>` 是 YouTube 自动字幕的换人标记。
它只说明"换人了"，不说明换成了谁，而且会漏标、错标，不能拿来数轮次。
命中点离得近时（前后窗口重叠）合并成一段输出，每个命中行都标 `<==`。
"""

from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import re
import shlex
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"
BEIJING = dt.timezone(dt.timedelta(hours=8))
LEDGER: dict = {"path": None, "label": ""}


def log_call(mode: str, started: dt.datetime, **fields) -> None:
    """往调用账本追加一行，字段同 search.py（at、label、mode、status、attempts、seconds）。"""
    if not LEDGER["path"]:
        return
    entry = {"at": started.isoformat(timespec="seconds"), "label": LEDGER["label"], "mode": mode, **fields}
    with open(LEDGER["path"], "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")


def run_ytdlp(command: list[str], mode: str, **fields) -> tuple[subprocess.CompletedProcess, dt.datetime, float]:
    """跑一次 yt-dlp，返回 (结果, 开始时间, 耗时)；找不到 yt-dlp 时记一行 error 再退出。"""
    started, t0 = dt.datetime.now(dt.timezone.utc), time.monotonic()
    try:
        result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace")
    except FileNotFoundError:
        log_call(mode, started, **fields, status="error", returncode=None, attempts=1,
                 seconds=round(time.monotonic() - t0, 3), error=f"找不到 {command[0]}")
        sys.exit(f"找不到 yt-dlp（{command[0]}）：安装后重试，或用 --ytdlp 指定路径")
    return result, started, round(time.monotonic() - t0, 3)


def http_get(url: str, mode: str) -> bytes:
    """直连取一个地址（球队官网页面、字幕直链），记一行账；失败直接退出。"""
    request = urllib.request.Request(url, headers={"User-Agent": UA})
    started, t0 = dt.datetime.now(dt.timezone.utc), time.monotonic()
    try:
        with urllib.request.urlopen(request, timeout=40) as response:
            body = response.read()
            log_call(mode, started, url=url, status=response.status, attempts=1,
                     seconds=round(time.monotonic() - t0, 3), bytes=len(body))
            return body
    except urllib.error.HTTPError as err:
        log_call(mode, started, url=url, status=err.code, attempts=1, seconds=round(time.monotonic() - t0, 3))
        sys.exit(f"HTTP {err.code}：{url}")
    except (urllib.error.URLError, TimeoutError) as err:
        log_call(mode, started, url=url, status=0, attempts=1, seconds=round(time.monotonic() - t0, 3))
        sys.exit(f"网络错误：{err}（{url}）")


# ---------------------------------------------------------------- 视频元数据

def utc_and_beijing(epoch) -> str:
    when = dt.datetime.fromtimestamp(float(epoch), dt.timezone.utc)
    return f"{when:%Y-%m-%d %H:%M} UTC（北京时间 {when.astimezone(BEIJING):%Y-%m-%d %H:%M}）"


def youtube_meta(info: dict) -> dict:
    """从 yt-dlp 的 info.json 里取核对来源要用的几项。"""
    date = str(info.get("upload_date") or "")
    meta = {
        "title": info.get("title"),
        "channel": info.get("channel") or info.get("uploader"),
        "channel_id": info.get("channel_id"),
        "uploader_id": info.get("uploader_id"),
        "channel_url": info.get("channel_url"),
        "upload_date": f"{date[:4]}-{date[4:6]}-{date[6:]}" if len(date) == 8 else None,
        "timestamp": info.get("timestamp"),
        "release_timestamp": info.get("release_timestamp"),
        "duration": info.get("duration"),
        "url": info.get("webpage_url") or info.get("original_url"),
    }
    return {k: v for k, v in meta.items() if v not in (None, "")}


def describe_meta(meta: dict) -> list[str]:
    lines = []
    if meta.get("title"):
        lines.append(f"视频：{meta['title']}")
    if meta.get("channel"):
        channel_id = f"（频道 ID {meta['channel_id']}）" if meta.get("channel_id") else ""
        handle = f" {meta['uploader_id']}" if meta.get("uploader_id") else ""
        lines.append(f"频道：{meta['channel']}{handle}{channel_id}")
    if meta.get("publisher"):
        lines.append(f"发布方：{meta['publisher']}")
    if meta.get("timestamp"):
        lines.append(f"上传：{utc_and_beijing(meta['timestamp'])}")
    elif meta.get("upload_date"):
        lines.append(f"上传：{meta['upload_date']}（只有日期，没有具体时刻）")
    elif meta.get("published"):
        try:
            when = dt.datetime.fromisoformat(str(meta["published"]).replace("Z", "+00:00"))
            lines.append(f"发布：{utc_and_beijing(when.timestamp())}" if when.tzinfo else f"发布：{meta['published']}")
        except ValueError:
            lines.append(f"发布：{meta['published']}")
    if meta.get("release_timestamp") and meta.get("release_timestamp") != meta.get("timestamp"):
        lines.append(f"首播 / 直播开始：{utc_and_beijing(meta['release_timestamp'])}")
    if meta.get("url"):
        lines.append(f"地址：{meta['url']}")
    return lines


def meta_path_for(path: Path) -> Path:
    """字幕文件旁边的元数据文件：vid.en-orig.json3 → vid.meta.json。"""
    return path.with_name(path.name.split(".")[0] + ".meta.json")


def write_meta(path: Path, meta: dict) -> None:
    meta_path_for(path).write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")


def read_meta(path: Path) -> dict:
    target = meta_path_for(path)
    if target.is_file():
        try:
            return json.loads(target.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
    return {}


# ---------------------------------------------------------------- 取字幕

def download_json3(url: str, out_dir: Path, ytdlp: str, extra: list[str]) -> Path:
    command = [
        ytdlp,
        "--skip-download",
        "--write-subs",
        "--write-auto-subs",
        "--sub-langs",
        "en-orig,en",
        "--sub-format",
        "json3",
        # 顺带写出视频信息（频道、上传时间），不多发请求
        "--write-info-json",
        "-o",
        str(out_dir / "%(id)s.%(ext)s"),
        *extra,
        url,
    ]
    result, started, seconds = run_ytdlp(command, "captions_download", url=url)
    files = sorted(out_dir.glob("*.json3"))
    meta: dict = {}
    infos = sorted(out_dir.glob("*.info.json"))
    if infos:
        try:
            meta = youtube_meta(json.loads(infos[0].read_text(encoding="utf-8")))
        except json.JSONDecodeError:
            meta = {}
    log_call("captions_download", started, url=url, status="ok" if files else "error",
             returncode=result.returncode, attempts=1, seconds=seconds, files=[f.name for f in files],
             **{k: meta[k] for k in ("channel", "channel_id", "upload_date") if k in meta})
    if not files:
        sys.exit(f"没拿到字幕：\n{result.stderr[-1500:]}")
    # en-orig 是原始语音识别结果，en 可能是自动翻译或整理过的版本，优先用 en-orig。
    orig = [f for f in files if ".en-orig." in f.name]
    chosen = (orig or files)[0]
    if meta:
        write_meta(chosen, meta)
    return chosen


def caption_links(node) -> list[str]:
    """在页面数据里找字幕地址：先看 pageObject.videoAssets.cc，再全局找 .srt / .vtt 结尾的地址。"""
    links: list[str] = []
    cc = (((node.get("pageObject") or {}).get("videoAssets") or {}).get("cc") or {}) if isinstance(node, dict) else {}
    for key in ("srt", "vtt"):
        if isinstance(cc.get(key), str):
            links.append(cc[key])

    def walk(value) -> None:
        if isinstance(value, dict):
            for item in value.values():
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)
        elif isinstance(value, str) and re.fullmatch(r"https?://\S+\.(srt|vtt)", value):
            links.append(value)

    walk(node)
    unique = list(dict.fromkeys(links))
    # SRT 优先、英文优先
    return sorted(unique, key=lambda u: (not u.endswith(".srt"), "_eng" not in u))


def fetch_nba_page_captions(url: str, out_dir: Path) -> Path:
    """NBA.com 球队官网视频页：从页面内嵌数据里找字幕地址，下载字幕，存下标题和发布时间。"""
    page = http_get(url, "captions_page").decode("utf-8", "replace")
    match = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', page, re.S)
    if not match:
        sys.exit(f"页面里没有 __NEXT_DATA__（页面结构可能变了，或被拦截）：{url}")
    props = json.loads(match.group(1)).get("props", {}).get("pageProps", {})
    links = caption_links(props)
    if not links:
        sys.exit("页面里没有字幕地址（pageObject.videoAssets.cc 为空）：这个视频可能没有字幕，只能听原声")
    target = links[0]
    path = out_dir / Path(urllib.parse.urlparse(target).path).name
    path.write_bytes(http_get(target, "captions_download"))
    obj = props.get("pageObject") or {}
    site = urllib.parse.urlparse(url).path.strip("/").split("/")[0]
    meta = {
        "title": obj.get("title"),
        "publisher": f"NBA.com {site} 官网" if site and site not in ("watch", "news", "video") else "NBA.com",
        "published": obj.get("date"),
        "duration": obj.get("videoDurationSeconds") or obj.get("videoDuration"),
        "url": obj.get("permalink") or url,
        "captions": links,
    }
    write_meta(path, {k: v for k, v in meta.items() if v not in (None, "")})
    return path


def fetch_direct(url: str, out_dir: Path) -> Path:
    path = out_dir / Path(urllib.parse.urlparse(url).path).name
    path.write_bytes(http_get(url, "captions_download"))
    return path


def source_kind(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    if parsed.path.lower().endswith((".srt", ".vtt", ".json3")):
        return "direct"
    if parsed.netloc.lower().endswith("nba.com"):
        return "nba_page"
    return "ytdlp"


def find_videos(args: argparse.Namespace) -> None:
    """搜 YouTube 找原视频。搜索结果按相关度排序，不保证是官方频道、也不保证日期对，要自己核对频道和上传日期。"""
    command = [
        # --flat-playlist 只读搜索结果页，不逐个打开视频页（逐个打开会触发 YouTube 的机器人验证）；
        # 代价是上传日期常常拿不到，确认日期要打开候选视频看
        args.ytdlp, f"ytsearch{args.count}:{args.find}", "--flat-playlist", "--skip-download", "--no-warnings",
        "--print", "%(id)s\t%(upload_date|未知)s\t%(duration_string|?)s\t%(channel|?)s\t%(title)s",
    ]
    if args.proxy:
        command += ["--proxy", args.proxy]
    command += shlex.split(args.ytdlp_args)
    result, started, seconds = run_ytdlp(command, "captions_find", query=args.find, count=args.count)
    lines = [l for l in result.stdout.splitlines() if l.strip()]
    log_call("captions_find", started, query=args.find, count=args.count,
             status="ok" if result.returncode == 0 else "error", returncode=result.returncode,
             attempts=1, seconds=seconds, results=len(lines))
    if not lines:
        sys.exit(f"没搜到视频：\n{result.stderr[-800:]}")
    print("候选视频（核对频道和上传日期，确认是原始来源再用）：")
    unknown = False
    for line in lines:
        vid, date, duration, channel, title = (line.split("\t") + [""] * 5)[:5]
        if len(date) == 8 and date.isdigit():
            date = f"{date[:4]}-{date[4:6]}-{date[6:]}"
        else:
            unknown = True
        print(f"  https://www.youtube.com/watch?v={vid}  {date}  {duration}  [{channel}]  {title}")
    if unknown:
        print("上传日期“未知”的不用另查：下载字幕时会一并给出频道、频道 ID 和上传时间。")


# ---------------------------------------------------------------- 读字幕

def parse_clock(text: str) -> float | None:
    """SRT / VTT 的时间：00:01:02,500、00:01:02.500、01:02.500 都认。"""
    match = re.fullmatch(r"(?:(\d+):)?(\d{1,2}):(\d{2})[,.](\d{1,3})", text.strip())
    if not match:
        return None
    hours, minutes, seconds, fraction = match.groups()
    return int(hours or 0) * 3600 + int(minutes) * 60 + int(seconds) + int(fraction.ljust(3, "0")) / 1000


def parse_cues(text: str) -> list[tuple[float, str]]:
    """SRT 和 VTT 共用：按空行分块，找带 --> 的时间行，后面几行是字幕。去掉标签和行内时间戳。"""
    events = []
    for block in re.split(r"\n\s*\n", text.replace("\r\n", "\n").replace("\r", "\n").lstrip("\ufeff")):
        lines = block.strip().split("\n")
        index = next((i for i, line in enumerate(lines) if "-->" in line), None)
        if index is None:
            continue  # WEBVTT 头、NOTE、STYLE 这类块
        start = parse_clock(lines[index].split("-->")[0])
        if start is None:
            continue
        words = html.unescape(re.sub(r"<[^>]*>", "", " ".join(lines[index + 1 :])))
        words = re.sub(r"\s+", " ", words).strip()
        if words:
            events.append((start, words))
    return events


def caption_kind(path: Path, hint: str | None = None) -> str:
    if hint:
        return hint
    suffix = path.suffix.lower()
    if suffix in (".srt", ".vtt"):
        return suffix[1:]
    if suffix in (".json3", ".json"):
        return "json3"
    head = path.read_text(encoding="utf-8", errors="replace")[:200].lstrip("\ufeff").lstrip()
    if head.startswith("WEBVTT"):
        return "vtt"
    return "json3" if head.startswith("{") else "srt"


def load_events(path: Path, kind: str | None = None) -> list[tuple[float, str]]:
    kind = caption_kind(path, kind)
    text = path.read_text(encoding="utf-8", errors="replace")
    if kind in ("srt", "vtt"):
        return parse_cues(text)
    data = json.loads(text)
    events = []
    for event in data.get("events", []):
        words = "".join(seg.get("utf8", "") for seg in event.get("segs") or [])
        if words.strip():
            events.append((event.get("tStartMs", 0) / 1000, words.replace("\n", " ")))
    return events


def find_hits(events: list[tuple[float, str]], keyword: str) -> list[float]:
    keyword = keyword.lower()
    hits = [t for t, text in events if keyword in text.lower()]
    if not hits:
        # 关键词可能被拆在两个相邻事件里，拼起来再找一次
        for (t1, a), (_, b) in zip(events, events[1:]):
            if keyword in f"{a} {b}".lower():
                hits.append(t1)
    return hits


def windows(hits: list[float], window: float) -> list[tuple[float, float, list[float]]]:
    """把命中点合并成互不重叠的显示窗口 [(起, 止, [命中...])]，起点截到 0。

    以前按"窗口起点 ≤ 上一窗口终点就跳过"去重：第一个命中离开头不到 window 秒时起点是负数，
    会被初始值 -1 直接跳过，什么都不输出；窗口有重叠时后面的命中也会被漏掉。
    """
    merged: list[list] = []
    for hit in sorted(set(hits)):
        start, end = max(0.0, hit - window), hit + window
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
            merged[-1][2].append(hit)
        else:
            merged.append([start, end, [hit]])
    return [(start, end, found) for start, end, found in merged]


def fmt(seconds: float) -> str:
    return f"{int(seconds // 60)}:{seconds % 60:04.1f}"


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")  # 报错和提示也是中文，Windows 默认编码会乱码
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    parser.add_argument("url", nargs="?", help="视频地址（YouTube、NBA.com 球队官网视频页、字幕直链），或本地字幕文件")
    parser.add_argument("keyword", nargs="?", help="要找的词或短语，不区分大小写")
    parser.add_argument("--find", help="不给网址时，用这个查询搜 YouTube，列出候选视频（节目名 + 嘉宾 + 日期效果最好）")
    parser.add_argument("--count", type=int, default=5, help="--find 列出几个候选，默认 5")
    parser.add_argument("--json3", help="直接读已下载的 json3 字幕文件")
    parser.add_argument("--srt", help="直接读本地 SRT 字幕文件")
    parser.add_argument("--vtt", help="直接读本地 VTT 字幕文件")
    parser.add_argument("--window", type=float, default=15, help="命中前后各显示多少秒，默认 15")
    parser.add_argument("--out-dir", help="字幕和元数据的保存目录，默认用临时目录")
    parser.add_argument("--ytdlp", default="yt-dlp", help="yt-dlp 可执行文件")
    parser.add_argument("--proxy", help="传给 yt-dlp 的代理")
    parser.add_argument("--ytdlp-args", default="",
                        help="额外传给 yt-dlp 的参数，整体用引号括起来，例如 \"--js-runtimes node\"")
    parser.add_argument("--log", help="调用账本（JSONL）：每次字幕下载、页面请求、视频搜索追加一行")
    parser.add_argument("--label", default="", help="写进账本的标签，例如样本编号")
    args = parser.parse_args()
    LEDGER["path"], LEDGER["label"] = args.log, args.label

    local, kind = None, None
    for flag in ("json3", "srt", "vtt"):
        if getattr(args, flag):
            local, kind = Path(getattr(args, flag)), flag
    if local is None and args.url and not re.match(r"https?://", args.url) and Path(args.url).is_file():
        local, args.url = Path(args.url), None  # 本地字幕文件当位置参数给，按扩展名识别
        if not args.keyword:
            parser.error("需要要找的关键词")
    # 离线模式只给一个位置参数时，它是关键词而不是网址（captions.py --srt 文件 关键词）
    if local is not None and args.url and not args.keyword:
        args.keyword, args.url = args.url, None
    if args.find:
        find_videos(args)
        return
    if not args.keyword:
        parser.error("需要要找的关键词（或用 --find 先找视频）")

    if local is not None:
        path = local
    else:
        if not args.url:
            parser.error("需要视频地址，或用 --json3 / --srt / --vtt 指定字幕文件")
        out_dir = Path(args.out_dir) if args.out_dir else Path(tempfile.mkdtemp(prefix="caps_"))
        out_dir.mkdir(parents=True, exist_ok=True)
        source = source_kind(args.url)
        if source == "direct":
            path = fetch_direct(args.url, out_dir)
        elif source == "nba_page":
            path = fetch_nba_page_captions(args.url, out_dir)
        else:
            extra = (["--proxy", args.proxy] if args.proxy else []) + shlex.split(args.ytdlp_args)
            path = download_json3(args.url, out_dir, args.ytdlp, extra)
        print(f"字幕文件：{path}")

    meta = read_meta(path)
    for line in describe_meta(meta):
        print(line)
    if local is None or meta:
        print()

    events = load_events(path, kind)
    if not events:
        sys.exit(f"字幕文件里没有读到任何字幕：{path}（格式按 {caption_kind(path, kind)} 解析）")
    hits = find_hits(events, args.keyword)
    if not hits:
        print(f"字幕里没找到 “{args.keyword}”。自动字幕有听写错误，换个更短的词再试。")
        return

    for start, end, found in windows(hits, args.window):
        print(f"===== 命中 {'、'.join(f'{fmt(h)}（{h:.1f}s）' for h in found)} =====")
        for t, text in events:
            if start <= t <= end:
                mark = "  <==" if t in found else ""
                print(f"{fmt(t):>8}  {text}{mark}")
        print()


if __name__ == "__main__":
    main()

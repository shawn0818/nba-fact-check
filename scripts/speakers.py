"""判断对谈 / 播客里"这句话是谁说的"：让能听音频的模型逐轮标注说话人，跑两遍对照。

做法（2026-10-17 实测有效，见 references/primary-sources.md 的"说话人归属"）：
  1. 截出争议段前后的音频，**必须包含一段确定是谁在说的参照**（--ref）。
  2. 第一遍：告诉模型参照段是谁、另一个人是谁。
  3. 第二遍：不给名字和任何声音描述，只说"参照段的人记为 A"。
  4. 按文字对齐两遍的结果，逐轮标出是否一致。只有一致的轮次才可采信。

模型自报的置信度不能当证据。中立那遍有时会在结尾陷入重复输出，脚本会丢掉超出音频长度的轮次。

重点句（--lines）的匹配：缩写会展开再比（didn't = did not，gonna = going to），大小写、标点不计；
一两个词的短句（"Yes"）要原样出现在某一轮里，有整轮就是这句的，只取那几轮。
摘要分几种情况：一致；两遍说话人不一致；文字没匹配上（哪一遍没找到）；匹配到多处；模型标了 unknown。
文字没匹配上不等于没人说过，多半是两遍转写的措辞不同，到"第二遍原文"里找相近的一轮。

片段里可能不止两个人（前半是另一名球员、旁白、解说）：提示词明确告诉模型认不出的标 other，
不要硬塞给两人之一；报告里显示为"其他"。2026-10-20 实测：只告诉模型"两个人"时，
两遍都把前一位球员的尾句归给了后面的受访者，报告却显示"一致"。所以报告开头加了一节"覆盖与参照检查"：
每遍的轮次覆盖了多少时长、哪些时段没分到任何轮次，参照段和参照句（--ref、--ref-text）在这一遍里
是不是落在给定的时间、标的是不是参照人。有问题时提示"一致不代表覆盖完整"，去听列出的时段。

音轨：输入是网址时先用 yt-dlp 列出音频格式，原声（original）优先，没有原声标记的再按英文选，
打印选中的格式、语言、是不是原声。多语言视频的格式 ID 带后缀（140-0 可能是阿拉伯语配音，140-18 才是英文原声），
裸的 140 不存在；要手动指定时用 --audio-format。

依赖：ffmpeg（系统里没有时，会自动改用 pip 包 imageio-ffmpeg 自带的 ffmpeg；两者都没有就先 `pip install imageio-ffmpeg`）；输入是网址时还需要 yt-dlp。Key 读取顺序：环境变量 OPENROUTER_API_KEY →
--env-file 里的 OPENROUTER_API_KEY 或 AI__OPENROUTER_API_KEY。一次约 0.002 美元 × 2。

用法：
    python speakers.py "https://www.youtube.com/watch?v=zNaVP54W_VY" \\
        --from 66 --to 100 --ref "74-77=LeBron James" --ref-text "I went to Philly" --other "Steve Nash" \\
        --lines "read off the teleprompter" "wasn't good" \\
        --ytdlp-args="--js-runtimes node --cookies cookies.txt"
    python speakers.py 本地视频或音频.mp4 --from 66 --to 100 --ref "77-80=LeBron James" --other "Steve Nash"

    # 两遍的完整回复存成 JSON 供复核；复核时读回来重新出报告（可换 --lines），不联网、不花钱
    python speakers.py 地址 --from 66 --to 100 --ref "74-77=甲" --other "乙" --save-raw evidence/speakers.json
    python speakers.py --from-raw evidence/speakers.json --lines "Yes" "did not"

    # 记账：列音频格式、音频下载、两遍模型调用各追加一行 JSONL（格式同 search.py，不记 key）
    python speakers.py 地址 ... --log calls.jsonl --label H03

时间都用原视频里的秒数。账本里模型调用的 status 是 HTTP 状态码（网络错误记 0）；
下载、列格式的 status 是 ok / error（yt-dlp 没有 HTTP 状态码），另记 returncode；
下载那一行还记选中的格式、语言、是不是原声。
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import difflib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from collections import Counter
from pathlib import Path

DEFAULT_MODEL = "google/gemini-2.5-flash"
RETRYABLE = {408, 429, 500, 502, 503, 504}
RETRY_WAIT = 5  # 秒；自检时设为 0

TURN_SCHEMA = """Return ONLY a JSON array, no prose, one object per speaker turn:
[{"start": <seconds from clip start>, "end": <seconds>, "speaker": "<label>", "text": "<words>", "confidence": "high|medium|low"}]
Start a new turn at EVERY change of speaker, even for a one- or two-word interjection ("Yeah, right.", "Clickbait.").
Cover the whole clip: every stretch of speech, from the first second to the last, must belong to some turn.
Mark laughter or overlapping speech inside "text" (e.g. "[laughter]"). Do not repeat turns. Stop at the end of the audio."""


OTHER = "其他"  # 两人以外的声音：第三人、旁白、解说、现场广播
COVERAGE_MIN = 0.8  # 轮次覆盖不到八成时长就提示
GAP_LIST = 3.0  # 列出 3 秒以上没分到轮次的时段
GAP_WARN = 5.0  # 有 5 秒以上的空档就提示
REF_TOLERANCE = 3.0  # 参照句的位置和 --ref 给的时间最多差几秒
WORDS_PER_SECOND = 2.5  # 模型没给 end 时按语速估一轮的长度

LEDGER: dict = {"path": None, "label": ""}


def log_call(mode: str, started: dt.datetime, **fields) -> None:
    """往调用账本追加一行，字段同 search.py（at、label、mode、status、attempts、seconds）；不记 key。"""
    if not LEDGER["path"]:
        return
    entry = {"at": started.isoformat(timespec="seconds"), "label": LEDGER["label"], "mode": mode, **fields}
    with open(LEDGER["path"], "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")


def load_key(env_file: str | None) -> str:
    key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if key:
        return key
    if env_file:
        for line in Path(env_file).read_text(encoding="utf-8").splitlines():
            name, sep, value = line.partition("=")
            if sep and name.strip() in ("OPENROUTER_API_KEY", "AI__OPENROUTER_API_KEY"):
                value = value.strip().strip('"').strip("'")
                if value:
                    return value
    sys.exit("缺少 OpenRouter key：设置 OPENROUTER_API_KEY，或用 --env-file 指向含它的 .env")


DEFAULT_AUDIO_FORMAT = "139/bestaudio[abr<=64]/bestaudio"  # 列不出格式时的退路


def run_ytdlp(command: list[str], mode: str, url: str) -> tuple[subprocess.CompletedProcess, dt.datetime, float]:
    """跑一次 yt-dlp，返回 (结果, 开始时间, 耗时)；找不到 yt-dlp 时记一行 error 再退出。"""
    started, t0 = dt.datetime.now(dt.timezone.utc), time.monotonic()
    try:
        result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace")
    except FileNotFoundError:
        log_call(mode, started, url=url, status="error", returncode=None, attempts=1,
                 seconds=round(time.monotonic() - t0, 3), error=f"找不到 {command[0]}")
        sys.exit(f"找不到 yt-dlp（{command[0]}）：安装后重试，或用 --ytdlp 指定路径")
    return result, started, round(time.monotonic() - t0, 3)


def audio_tracks(info: dict) -> list[dict]:
    """只有音频的格式（多语言视频里每种语言一组，格式 ID 带后缀：140-0、140-18……）。"""
    return [f for f in info.get("formats") or []
            if f.get("vcodec") == "none" and f.get("acodec") not in (None, "none") and f.get("format_id")]


def is_original(track: dict) -> bool:
    # yt-dlp 给原声轨 language_preference=10，format_note 里写 "original"；配音轨写 dubbed 或只有语言名
    return (track.get("language_preference") or 0) >= 10 or "original" in str(track.get("format_note", "")).lower()


def describe_track(track: dict) -> str:
    abr = f"{track['abr']:.0f}k" if isinstance(track.get("abr"), (int, float)) else "码率未知"
    original = "是" if is_original(track) else "否"
    return (f"{track['format_id']}（{track.get('ext', '?')}，{abr}），语言 {track.get('language') or '未标'}，"
            f"原声：{original}（{track.get('format_note') or '无说明'}）")


def choose_audio(info: dict) -> tuple[dict | None, str]:
    """选音轨：原声优先，没有标原声的再找英文，再退到任意一条；同组里挑 40k 以上最小的那条（够听、下得快）。

    返回 (选中的格式, 说明)。只有一种语言时说明为空。
    """
    tracks = audio_tracks(info)
    if not tracks:
        return None, "列不出单独的音频格式"
    languages = {t.get("language") for t in tracks if t.get("language")}
    group = [t for t in tracks if is_original(t)]
    note = ""
    if not group:
        group = [t for t in tracks if str(t.get("language") or "").lower().startswith("en")]
        if len(languages) > 1:
            note = "没有标原声（original）的音轨，按英文选的；核对一下是不是配音"
    if not group:
        group = tracks
        if len(languages) > 1:
            note = "没有原声标记，也没有英文音轨，选的这条可能是配音"
    if not note and len(languages) > 1:
        note = f"这个视频有 {len(languages)} 种语言的音轨，只有标 original 的是原声；配音轨不能拿来判断说话人"

    def key(track: dict) -> tuple:
        abr = track.get("abr")
        return (abr is None, (abr or 0) < 40, track.get("ext") != "m4a", abr or 0)

    return sorted(group, key=key)[0], note


def fetch_audio(source: str, work: Path, ytdlp: str, ytdlp_args: list[str], audio_format: str | None = None) -> Path:
    if Path(source).is_file():
        return Path(source)
    tracks: list[dict] = []
    chosen = None
    if audio_format:
        print(f"音轨：按 --audio-format 指定 {audio_format}", file=sys.stderr)
    else:
        # 先列格式再选：多语言视频的音轨格式 ID 带后缀，默认选法可能落到配音轨上，或者干脆选不到
        listing, started, seconds = run_ytdlp([ytdlp, "-J", "--skip-download", "--no-warnings", *ytdlp_args, source],
                                              "speakers_formats", source)
        info: dict = {}
        try:
            info = json.loads(listing.stdout) if listing.returncode == 0 else {}
        except json.JSONDecodeError:
            info = {}
        tracks = audio_tracks(info)
        chosen, note = choose_audio(info)
        log_call("speakers_formats", started, url=source, status="ok" if info else "error",
                 returncode=listing.returncode, attempts=1, seconds=seconds, audio_tracks=len(tracks))
        if chosen:
            audio_format = chosen["format_id"]
            print(f"选中音轨：{describe_track(chosen)}", file=sys.stderr)
            if note:
                print(f"注意：{note}", file=sys.stderr)
        else:
            audio_format = DEFAULT_AUDIO_FORMAT
            print(f"列不出音频格式（{listing.stderr.strip()[-300:] or '原因不明'}），改用默认选法 {audio_format}，"
                  "下完核对一下是不是原声", file=sys.stderr)

    # 整段下载低码率音频再本地切：--download-sections 走 ffmpeg 远程读取，实测经常卡住。
    command = [ytdlp, "-f", audio_format, "-o", str(work / "audio.%(ext)s"), *ytdlp_args, source]
    result, started, seconds = run_ytdlp(command, "speakers_download", source)
    files = [f for f in work.glob("audio.*") if not f.name.endswith(".part")]
    extra = {"language": chosen.get("language"), "original": is_original(chosen)} if chosen else {}
    log_call("speakers_download", started, url=source, status="ok" if files else "error",
             returncode=result.returncode, attempts=1, seconds=seconds, format=audio_format, **extra,
             bytes=files[0].stat().st_size if files else 0)
    if not files:
        originals = [t for t in tracks if is_original(t)]
        listing_text = "\n".join(f"  {describe_track(t)}" for t in (originals or tracks)[:8])
        sys.exit(
            f"音频下载失败（格式 {audio_format}）：\n{result.stderr[-1200:]}\n"
            "排查：① 多语言视频的格式 ID 带后缀（140-18、139-18……），后缀不同是不同语言的音轨，裸的 140 不存在；"
            "先确认要的是原声（format_note 里有 original），再用 --audio-format 指定；"
            "② 403 多半是 YouTube 临时拒绝，带上 --ytdlp-args \"--js-runtimes node\"，有 cookies 也带上；"
            "③ 不要为了下载成功改用配音轨。"
            + (f"\n可选的原声音轨：\n{listing_text}" if originals else (f"\n可选音轨：\n{listing_text}" if tracks else ""))
        )
    return files[0]


def ffmpeg_exe() -> str:
    """找 ffmpeg：先找系统里的，再找 imageio-ffmpeg 自带的静态版本（盲测环境里没有 ffmpeg 时实际就是这么解决的）。"""
    found = shutil.which("ffmpeg")
    if found:
        return found
    try:
        import imageio_ffmpeg  # type: ignore

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:  # noqa: BLE001 — 没装或下载失败都按"没有 ffmpeg"处理
        sys.exit("找不到 ffmpeg：安装系统 ffmpeg，或 `pip install imageio-ffmpeg`（自带静态 ffmpeg）后重试")


def cut_clip(audio: Path, start: float, end: float, out: Path) -> None:
    command = [
        ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-y",
        "-ss", str(start), "-i", str(audio), "-t", str(end - start),
        "-vn", "-ac", "1", "-ar", "16000", "-b:a", "48k", str(out),
    ]
    subprocess.run(command, check=True)


def parse_turns(content: str) -> list[dict]:
    """从模型回复里取出 JSON 数组；输出被截断时丢掉最后一个不完整的对象再试。"""
    start = content.find("[")
    if start < 0:
        return []
    end = content.rfind("]")
    candidate = content[start:end + 1] if end > start else content[start:]
    try:
        turns = json.loads(candidate)
    except json.JSONDecodeError:
        try:
            turns = json.loads(candidate[: candidate.rfind("}") + 1] + "]")
        except json.JSONDecodeError:
            return []
    if not isinstance(turns, list):
        return []
    return [t for t in turns if isinstance(t, dict) and "start" in t and "speaker" in t]


def ask(key: str, model: str, prompt: str, audio_b64: str, pass_name: str) -> tuple[list[dict], dict]:
    """调一遍模型。返回 (解析出的轮次, 原始记录)；原始记录含提示词、完整回复，供 --save-raw 保存。"""
    body = {
        "model": model,
        "temperature": 0,
        "max_tokens": 3000,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {"type": "input_audio", "input_audio": {"data": audio_b64, "format": "mp3"}},
            ],
        }],
    }
    request = urllib.request.Request(
        "https://openrouter.ai/api/v1/chat/completions",
        data=json.dumps(body).encode("utf-8"),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
    )
    started, t0 = dt.datetime.now(dt.timezone.utc), time.monotonic()
    reply, status, attempts, error = None, 0, 0, ""
    for attempt in range(3):
        attempts += 1
        try:
            with urllib.request.urlopen(request, timeout=300) as response:
                status = response.status
                reply = json.loads(response.read())
            break
        except urllib.error.HTTPError as err:
            status, error = err.code, err.read().decode("utf-8", "replace")[:500]
            if err.code not in RETRYABLE:
                break
        except (urllib.error.URLError, TimeoutError) as err:
            status, error = 0, str(err)
        except ValueError as err:  # 200 但回复不是 JSON
            reply, error = None, f"回复不是 JSON：{err}"
            break
        if attempt < 2:
            print(f"[重试] {error[:120]}，{RETRY_WAIT} 秒后第 {attempt + 2} 次请求", file=sys.stderr)
            time.sleep(RETRY_WAIT)

    content = ""
    if isinstance(reply, dict):
        choices = reply.get("choices") or []
        if choices:
            content = (choices[0].get("message") or {}).get("content") or ""
    turns = parse_turns(content)
    entry = {"pass": pass_name, "model": model, "status": status, "attempts": attempts,
             "seconds": round(time.monotonic() - t0, 3), "turns": len(turns)}
    usage = reply.get("usage") if isinstance(reply, dict) else None
    if isinstance(usage, dict):
        entry["usage"] = {k: usage[k] for k in ("prompt_tokens", "completion_tokens", "cost") if k in usage}
    log_call("speakers_model", started, **entry)

    if reply is None:
        if status == 0:
            sys.exit(f"连不上 OpenRouter（{error}）。网络不稳时用 --proxy 或设置 HTTPS_PROXY 再试。")
        sys.exit(f"OpenRouter 调用失败（HTTP {status}）：{error}")
    if not turns:
        print(f"模型没有返回可用的 JSON：\n{(content or json.dumps(reply, ensure_ascii=False))[:1500]}", file=sys.stderr)
    record = {"pass": pass_name, "model": model, "prompt": prompt, "status": status, "attempts": attempts,
              "response": reply, "content": content, "parsed_turns": turns}
    return turns, record


def clean(turns: list[dict], length: float) -> list[dict]:
    """丢掉超出音频长度的轮次和连续重复的轮次（中立那遍实测会在结尾陷入循环）。"""
    kept: list[dict] = []
    for turn in turns:
        try:
            start = float(turn["start"])
            end = float(turn.get("end", start))
        except (TypeError, ValueError):
            continue
        if start > length + 0.5:
            continue
        text = str(turn.get("text", "")).strip()
        if kept and text == kept[-1]["text"] and turn["speaker"] == kept[-1]["speaker"]:
            continue
        kept.append({**turn, "start": start, "end": max(end, start), "text": text})
    return kept


def build_prompts(length: float, ref_name: str, other: str, anchor: str, focus: str) -> tuple[str, str]:
    """具名、中立两遍的提示词。都明确说片段里可能不止两个人，认不出的标 other，不要硬塞给两人之一。"""
    extra = ("The clip may also contain other voices (a third person, a narrator, a commentator, an announcer), "
             "possibly for long stretches at the start or end.")
    named = (
        f"This {length:.0f}-second clip features {ref_name} and {other}. {extra}\n"
        f"{anchor[0].upper() + anchor[1:]} is {ref_name}; use that as the reference for their voice.\n"
        f"Label every speaker turn {ref_name}, {other}, or \"other\" for anyone else, judging by voice only. "
        f"Do not force a voice onto {ref_name} or {other} just because only two names were given. "
        f"If you truly cannot tell, use \"unknown\".\n{focus}{TURN_SCHEMA}"
    )
    neutral = (
        f"In this {length:.0f}-second clip, call {anchor} \"A\" and the person A is talking with \"B\". {extra}\n"
        f"Label every speaker turn A, B, or \"other\" for anyone else, judging by voice only; "
        f"never put a third voice under A or B. If you truly cannot tell, use \"unknown\".\n{TURN_SCHEMA}"
    )
    return named, neutral


def normalize_speakers(turns: list[dict], known: list[str]) -> list[dict]:
    """标签归一：两个已知标签（大小写不计）和 unknown 原样保留，其余一律记为"其他"，原标签留在 raw_speaker。"""
    canonical = {k.lower(): k for k in known}
    result = []
    for turn in turns:
        label = str(turn.get("speaker", "")).strip()
        if label.lower() in canonical:
            label = canonical[label.lower()]
        elif label.lower() == "unknown":
            label = "unknown"
        elif label != OTHER:
            turn = {**turn, "raw_speaker": label}
            label = OTHER
        result.append({**turn, "speaker": label})
    return result


def spans(turns: list[dict], length: float) -> list[tuple[float, float, dict]]:
    """每一轮占的时间段（片段内秒数）。没给 end 或 end ≤ start 时，按语速估，不超过下一轮的开始。"""
    result = []
    for i, turn in enumerate(turns):
        start = float(turn["start"])
        end = float(turn.get("end", start))
        if end <= start:
            later = [float(u["start"]) for u in turns[i + 1 :] if float(u["start"]) > start]
            guess = start + max(1.0, len(words_of(turn["text"])) / WORDS_PER_SECOND)
            end = min(guess, later[0]) if later else guess
        result.append((max(0.0, start), min(length, end), turn))
    return result


def coverage(turns: list[dict], length: float) -> dict:
    """轮次覆盖了多少时长、哪些时段（≥ GAP_LIST 秒）没分到任何轮次。"""
    merged: list[list[float]] = []
    for start, end, _ in sorted(spans(turns, length), key=lambda x: x[0]):
        if end <= start:
            continue
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    covered = sum(end - start for start, end in merged)
    gaps, cursor = [], 0.0
    for start, end in merged + [[length, length]]:
        if start - cursor >= GAP_LIST:
            gaps.append((cursor, start))
        cursor = max(cursor, end)
    return {"covered": covered, "length": length, "ratio": covered / length if length else 0.0, "gaps": gaps}


def ref_problems(turns: list[dict], run: dict, mapping: dict[str, str]) -> list[str]:
    """参照段、参照句在这一遍里对不对得上：参照段时间里有没有轮次、标的是不是参照人；
    参照句出现的位置离 --ref 给的时间远不远、标的是不是参照人。"""
    name, offset = run["ref_name"], run["start"]
    rel_start, rel_end = run["ref_start"] - offset, run["ref_end"] - offset
    window = f"{run['ref_start']:.0f}-{run['ref_end']:.0f}s"
    problems = []
    inside = [mapping.get(str(t["speaker"]), str(t["speaker"]))
              for start, end, t in spans(turns, run["end"] - offset) if start < rel_end and end > rel_start]
    if not inside:
        problems.append(f"参照段 {window} 没有任何轮次")
    elif name not in inside:
        problems.append(f"参照段 {window} 被标成了 {'、'.join(dict.fromkeys(inside))}，不是 {name}")
    if run.get("ref_text"):
        found = find_line(run["ref_text"], turns, mapping)
        if not found:
            problems.append(f"参照句“{run['ref_text']}”没在这一遍的文字里找到")
        else:
            def distance(match: dict) -> float:
                at = float(match.get("start") or 0)
                return max(0.0, rel_start - at, at - rel_end)

            best = min(found, key=distance)
            if distance(best) > REF_TOLERANCE:
                problems.append(f"参照句出现在 ≈{offset + float(best['start']):.0f}s，--ref 给的是 {window}，"
                                f"相差 {distance(best):.0f} 秒：模型的时间轴或参照位置可能对错了")
            if best["speaker"] != name:
                problems.append(f"参照句被标成了 {best['speaker']}，不是 {name}：这一遍的身份锚点就错了")
    return problems


# 缩写和口语写法：两遍转写、字幕、中文稿回译常常一边写缩写一边写全，比对前统一展开
SPOKEN_FORMS = {
    "can't": ["can", "not"], "cannot": ["can", "not"], "won't": ["will", "not"], "shan't": ["shall", "not"],
    "gonna": ["going", "to"], "wanna": ["want", "to"], "gotta": ["got", "to"],
    "'cause": ["because"], "'em": ["them"],
}
SUFFIXES = (("n't", ["not"]), ("'re", ["are"]), ("'m", ["am"]), ("'ll", ["will"]), ("'ve", ["have"]),
            ("'d", ["'d"]), ("'s", ["'s"]))
# 'd 可以是 would 也可以是 had，'s 可以是 is、has 或所有格：比对时把它们当成同一个词
SAME_WORD = {"would": "'d", "had": "'d", "is": "'s", "has": "'s"}


def words_of(text: str) -> list[str]:
    """比对用的词序列：小写、去标点、展开缩写（didn't → did not）。"""
    words: list[str] = []
    lowered = str(text).lower().replace("’", "'").replace("‘", "'")
    for token in re.findall(r"[a-z0-9']+", lowered):
        if token in SPOKEN_FORMS:
            words += SPOKEN_FORMS[token]
            continue
        token = token.strip("'")
        if not token:
            continue
        if token in SPOKEN_FORMS:
            words += SPOKEN_FORMS[token]
            continue
        for suffix, rest in SUFFIXES:
            if token.endswith(suffix) and len(token) > len(suffix):
                words.append(token[: -len(suffix)])
                words += rest
                break
        else:
            words.append(token)
    return [SAME_WORD.get(w, w) for w in words]


def word_track(turns: list[dict]) -> tuple[list[str], list[str]]:
    """把逐轮结果展开成 (词序列, 每个词的说话人)。"""
    words: list[str] = []
    speakers: list[str] = []
    for turn in turns:
        for word in words_of(turn["text"]):
            words.append(word)
            speakers.append(str(turn["speaker"]))
    return words, speakers


def align_speakers(first: list[dict], second: list[dict], mapping: dict[str, str]) -> list[str]:
    """按**文字**对齐两遍结果，给第一遍的每一轮找第二遍里对应词的多数说话人。

    不按时间对齐：模型给的时间戳实测偏差可达 7 秒以上（参照不准、越往后越漂），
    按时间重叠对齐会制造大量假的"不一致"。两遍转写的文字则高度一致。
    """
    w1, _ = word_track(first)
    w2, s2 = word_track(second)
    owner = [None] * len(w1)
    matcher = difflib.SequenceMatcher(a=w1, b=w2, autojunk=False)
    for block in matcher.get_matching_blocks():
        for k in range(block.size):
            owner[block.a + k] = mapping.get(s2[block.b + k], s2[block.b + k])
    result: list[str] = []
    pos = 0
    for turn in first:
        n = len(words_of(turn["text"]))
        votes = [o for o in owner[pos : pos + n] if o]
        pos += n
        result.append(Counter(votes).most_common(1)[0][0] if votes else "—")
    return result


def find_line(line: str, turns: list[dict], mapping: dict[str, str]) -> list[dict]:
    """在一遍结果里找含这句话的轮次，返回 [{"speaker", "text", "start"}]。

    三个词以上：最长连续相同片段至少占六成（且至少两个词）。
    一两个词的短句：要原样连续出现在一轮里；如果有的轮次整轮就是这句（"Yes."），只取这些轮次。
    """
    target = words_of(line)
    if not target:
        return []
    n = len(target)
    found, exact = [], []
    for turn in turns:
        tw = words_of(turn["text"])
        if not tw:
            continue
        if n <= 2:
            hit = any(tw[i : i + n] == target for i in range(len(tw) - n + 1))
        else:
            size = difflib.SequenceMatcher(a=target, b=tw, autojunk=False).find_longest_match(
                0, n, 0, len(tw)
            ).size
            hit = size >= max(2, int(n * 0.6))
        if hit:
            match = {"speaker": mapping.get(str(turn["speaker"]), str(turn["speaker"])),
                     "text": turn["text"], "start": turn.get("start")}
            found.append(match)
            if tw == target:
                exact.append(match)
    return exact if n <= 2 and exact else found


def who_said(line: str, turns: list[dict], mapping: dict[str, str]) -> str:
    """在一遍结果里找最像这句话的那一轮，返回它的说话人；可能有多轮都含这句。"""
    found = find_line(line, turns, mapping)
    return " / ".join(dict.fromkeys(m["speaker"] for m in found)) if found else "文字没匹配上"


def judge_line(line: str, named: list[dict], neutral: list[dict], mapping: dict[str, str]) -> dict:
    """一句重点句在两遍里各是谁说的，以及属于哪种情况。

    verdict 取值：一致 / 两遍说话人不一致 / 文字没匹配上 / 匹配到多处 / 模型标了 unknown。
    只有"一致"可采信；其余都写归属存疑。
    """
    first = find_line(line, named, {})
    second = find_line(line, neutral, mapping)
    s1 = list(dict.fromkeys(m["speaker"] for m in first))
    s2 = list(dict.fromkeys(m["speaker"] for m in second))
    if not words_of(line):
        verdict, detail = "文字没匹配上", "重点句里没有英文单词，要用英文原话"
    elif not first or not second:
        which = "两遍都" if not first and not second else ("第一遍" if not first else "第二遍")
        verdict, detail = "文字没匹配上", f"{which}没找到这句；多半是转写措辞不同，到下面的逐轮对照和第二遍原文里找相近的一轮"
    elif len(s1) > 1 or len(s2) > 1:
        verdict, detail = "匹配到多处", "这句出现在不同人的轮次里，换一句更长、更独特的原话"
    elif "unknown" in (s1[0].lower(), s2[0].lower()):
        verdict, detail = "模型标了 unknown", "模型没听出是谁"
    elif s1 != s2:
        verdict, detail = "两遍说话人不一致", "两遍听出的人不同"
    elif s1 == [OTHER]:
        verdict, detail = "一致", "两遍都标成两人以外的声音，不是这两个人说的"
    else:
        verdict, detail = "一致", ""
    return {"line": line, "first": first, "second": second, "verdict": verdict, "detail": detail}


def render(run: dict, named: list[dict], neutral: list[dict], lines: list[str]) -> str:
    """出报告：覆盖与参照检查、重点句摘要、逐轮对照（按文字对齐）、第二遍原文。"""
    mapping = {"A": run["ref_name"], "B": run["other"]}
    named = normalize_speakers(named, [run["ref_name"], run["other"]])
    neutral = normalize_speakers(neutral, ["A", "B"])
    length = run["end"] - run["start"]
    out = [f"截取 {run['start']:.0f}-{run['end']:.0f}s，参照 {run['ref_start']:.0f}-{run['ref_end']:.0f}s = {run['ref_name']}",
           "时间是模型估的，可能偏差数秒，只作定位参考；对照原视频时以 captions.py 的时间为准。", ""]

    def when(turn: dict) -> str:
        try:
            return f"≈{run['start'] + float(turn['start']):.0f}s"
        except (TypeError, ValueError, KeyError):
            return "≈?"

    # 两遍一致只说明两遍听法相同，不说明每段话都分对了人：先看轮次覆盖了多少时长、参照对不对得上
    out.append("== 覆盖与参照检查 ==")
    caution = False
    for name, turns, labels in (("第一遍", named, {}), ("第二遍", neutral, mapping)):
        cov = coverage(turns, length)
        problems = ref_problems(turns, run, labels)
        gaps = "、".join(f"≈{run['start'] + a:.0f}-{run['start'] + b:.0f}s" for a, b in cov["gaps"])
        line = f"  {name}：轮次覆盖 {cov['covered']:.0f}/{length:.0f} 秒（{cov['ratio']:.0%}）"
        out.append(line + (f"；没分到任何轮次的时段：{gaps}" if gaps else ""))
        out.extend(f"        ⚠ {p}" for p in problems)
        longest = max((b - a for a, b in cov["gaps"]), default=0.0)
        if problems or cov["ratio"] < COVERAGE_MIN or longest >= GAP_WARN:
            caution = True
    others = sum(1 for t in named + neutral if t["speaker"] == OTHER)
    if others:
        out.append(f"  有 {others} 轮标成了“其他”（两人以外的声音：第三人、旁白、解说），这些话不是这两个人说的。")
    if caution:
        out.append("  ⚠ 一致不代表覆盖完整：没分到轮次的时段里可能有第三个人在说话、或者模型漏听了；参照对不上时，"
                   "这一遍的身份锚点可能就错了，下面的\"一致\"都要打折扣。去听上面列出的时段和参照段。")
    else:
        out.append(f"  两遍的轮次都覆盖了 {COVERAGE_MIN:.0%} 以上的时长，没有 {GAP_WARN:.0f} 秒以上的空档，参照段和参照句对得上。")
    out.append("")

    if lines:
        out.append("== 重点句 ==")
        tally: Counter = Counter()
        for line in lines:
            judged = judge_line(line, named, neutral, mapping)
            tally[judged["verdict"]] += 1
            out.append(f'  "{line}"')
            for name, matches in (("第一遍", judged["first"]), ("第二遍", judged["second"])):
                if not matches:
                    out.append(f"    {name}：文字没匹配上")
                for m in matches[:3]:
                    out.append(f"    {name}：{m['speaker']} ← “{m['text']}”（{when(m)}）")
                if len(matches) > 3:
                    out.append(f"    {name}：……另有 {len(matches) - 3} 处")
            if judged["verdict"] == "一致":
                tail = "一致" + (f"（{judged['detail']}）" if judged["detail"] else "")
                if caution:
                    tail += "；注意：覆盖或参照检查有问题，一致不代表覆盖完整，见上方"
            else:
                tail = f"存疑：{judged['verdict']}（{judged['detail']}）"
            out.append(f"    → {tail}")
        out.append("  小计：" + "，".join(f"{k} {v}" for k, v in tally.items()))
        out.append("")

    out.append("== 逐轮对照（按文字对齐）==")
    out.append(f"{'≈时间':>8}  {'第一遍':<14} {'第二遍(中立)':<14} {'':<2} 第一遍原话")
    second_labels = align_speakers(named, neutral, mapping)
    agree = 0
    for turn, second in zip(named, second_labels):
        same = second == turn["speaker"]
        agree += same
        out.append(f"{when(turn)[1:]:>8}  {str(turn['speaker']):<14} {second:<14} {'✓' if same else '✗':<2} {turn['text']}")
    unaligned = second_labels.count("—")
    out.append(f"\n{len(named)} 轮里一致 {agree} 轮" + (f"；第二遍对不上文字的 {unaligned} 轮（标“—”，看下面的第二遍原文）" if unaligned else "") + "。"
               + ("一致不代表覆盖完整，见开头的覆盖与参照检查。" if caution else ""))
    out.append("")

    out.append("== 第二遍（中立）原文 ==")
    for turn in neutral:
        label = str(turn["speaker"])
        shown = f"{label}={mapping[label]}" if label in mapping else label
        out.append(f"{when(turn)[1:]:>8}  {shown:<20} {turn['text']}")
    out.append("")
    out.append("只采信两遍一致的结论；不一致的写“归属存疑”，附上时间点请人听。标成“其他”的是两人以外的声音。")
    out.append("同一轮里如果其实混着两个人的话（模型漏切了换人），两遍都会错得一样，所以一致≠一定对，重点句仍要看上下文是否通顺。")
    return "\n".join(out)


def save_raw(path: str, run: dict, passes: dict) -> None:
    """两遍的完整记录（提示词、模型回复原文、解析出的轮次、清理后的轮次）存成一个 JSON。不含 key。"""
    data = {
        "saved_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "note": "speakers.py 两遍的完整记录；用 --from-raw 可离线重新出报告",
        "run": run,
        "passes": passes,
    }
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")  # 报错和提示也是中文，Windows 默认编码会乱码
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    parser.add_argument("source", nargs="?", help="YouTube 地址，或本地音视频文件")
    parser.add_argument("--from", dest="start", type=float, help="截取起点（原视频秒数）")
    parser.add_argument("--to", dest="end", type=float, help="截取终点（原视频秒数）")
    parser.add_argument("--ref", help='参照段，格式 "起-止=姓名"，例如 "77-80=LeBron James"')
    parser.add_argument(
        "--ref-text",
        help="参照段里此人说的一句原话（强烈建议给，从 captions.py 的输出里抄），比时间锚可靠",
    )
    parser.add_argument("--other", help="另一位说话人的姓名")
    parser.add_argument("--lines", nargs="*", default=[], help="特别关心的几句原话，会提示模型重点标注")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--env-file", help="含 OpenRouter key 的 .env")
    parser.add_argument("--ytdlp", default="yt-dlp")
    parser.add_argument("--ytdlp-args", default="", help='额外传给 yt-dlp 的参数，例如 "--js-runtimes node"')
    parser.add_argument("--audio-format", help="指定音轨的格式 ID（例如 139-18）；默认先列出格式，原声优先自动选")
    parser.add_argument("--keep", help="把截出的音频另存到这个路径")
    parser.add_argument("--proxy", help="访问 OpenRouter 用的代理，例如 http://127.0.0.1:端口")
    parser.add_argument("--save-raw", metavar="路径",
                        help="把两遍的完整记录（提示词、模型回复原文、解析出的轮次）存成一个 JSON，供复核")
    parser.add_argument("--from-raw", metavar="路径",
                        help="读 --save-raw 存下的文件重新出报告，不联网、不花钱；可以换 --lines")
    parser.add_argument("--log", help="调用账本（JSONL）：音频下载、每遍模型调用各追加一行；不记 key")
    parser.add_argument("--label", default="", help="写进账本的标签，例如样本编号")
    args = parser.parse_args()
    LEDGER["path"], LEDGER["label"] = args.log, args.label

    if args.from_raw:
        if args.save_raw:
            parser.error("--save-raw 和 --from-raw 不能同时用")
        saved = json.loads(Path(args.from_raw).read_text(encoding="utf-8"))
        run = saved["run"]
        named, neutral = saved["passes"]["named"]["turns"], saved["passes"]["neutral"]["turns"]
        print(render(run, named, neutral, args.lines or run.get("lines") or []))
        return

    missing = [flag for flag, value in (("视频地址或文件", args.source), ("--from", args.start), ("--to", args.end),
                                        ("--ref", args.ref), ("--other", args.other)) if value is None]
    if missing:
        parser.error(f"缺少 {'、'.join(missing)}（或用 --from-raw 读已保存的结果）")
    if args.proxy:
        urllib.request.install_opener(
            urllib.request.build_opener(urllib.request.ProxyHandler({"https": args.proxy, "http": args.proxy}))
        )

    ref_match = re.fullmatch(r"\s*([\d.]+)\s*-\s*([\d.]+)\s*=\s*(.+)", args.ref)
    if not ref_match:
        parser.error('--ref 格式应为 "起-止=姓名"')
    ref_start, ref_end, ref_name = float(ref_match[1]), float(ref_match[2]), ref_match[3].strip()
    if not (args.start <= ref_start < ref_end <= args.end):
        parser.error("参照段必须落在截取范围之内")

    key = load_key(args.env_file)
    work = Path(tempfile.mkdtemp(prefix="speakers_"))
    audio = fetch_audio(args.source, work, args.ytdlp, shlex.split(args.ytdlp_args), args.audio_format)
    clip = work / "clip.mp3"
    cut_clip(audio, args.start, args.end, clip)
    if args.keep:
        Path(args.keep).write_bytes(clip.read_bytes())
    audio_b64 = base64.b64encode(clip.read_bytes()).decode()

    length = args.end - args.start
    rel_start, rel_end = ref_start - args.start, ref_end - args.start
    focus = ""
    if args.lines:
        focus = "Pay special attention to who says each of these lines:\n" + "\n".join(
            f'- "{line}"' for line in args.lines
        ) + "\n"

    # 锚点优先用参照人说过的原话：模型对时间的感知会漂，"0:11-0:14 说话的人"它可能找不到，
    # 一句原话则不受时间戳影响（实测只给时间锚时，中立那遍几乎全标成 unknown）。
    if args.ref_text:
        anchor = f'the person who says "{args.ref_text}" (around {rel_start:.0f}-{rel_end:.0f}s)'
    else:
        anchor = f"the person who does most of the talking from {rel_start:.0f}s to {rel_end:.0f}s"
    named_prompt, neutral_prompt = build_prompts(length, ref_name, args.other, anchor, focus)

    run = {"source": args.source, "start": args.start, "end": args.end, "ref_start": ref_start, "ref_end": ref_end,
           "ref_name": ref_name, "ref_text": args.ref_text, "other": args.other, "lines": args.lines,
           "model": args.model}
    passes: dict = {}
    results = {}
    for pass_name, prompt in (("named", named_prompt), ("neutral", neutral_prompt)):
        parsed, record = ask(key, args.model, prompt, audio_b64, pass_name)
        results[pass_name] = clean(parsed, length)
        passes[pass_name] = {**record, "turns": results[pass_name]}
        if args.save_raw:  # 每遍跑完就存，第二遍失败时第一遍的记录也不丢
            save_raw(args.save_raw, run, passes)

    print(render(run, results["named"], results["neutral"], args.lines))
    if args.save_raw:
        print(f"\n两遍的完整记录已保存：{args.save_raw}（复核时用 --from-raw 读回来）")


if __name__ == "__main__":
    main()

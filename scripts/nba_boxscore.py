"""从 NBA.com 的比赛页、球员页和 stats.nba.com 取官方数据，用于核对数字、排名、"全队最高"和生涯累计。

为什么要单独写：DeepSeek Fetch 把 NBA.com 页面转成 Markdown 时，表格的球员姓名整列会丢，
有的页面只剩导航栏（2026-10-18 盲测实测）。这里直接取同一公开页面的 HTML，解析页面里内嵌的
`__NEXT_DATA__` JSON，拿到带姓名、personId 的逐人数据。只用标准库；会读取环境变量里的代理。

用法：
    # 比赛页：列出一队所有球员的某项数据，并按包含并列的规则标出最高者
    python nba_boxscore.py game https://www.nba.com/game/bos-vs-phi-0012600018/box-score --team PHI --stat assists
    python nba_boxscore.py game 0012600018 --team PHI --stat points --json

    # 一次列出全部常用数据（得分、篮板、助攻、抢断、盖帽、失误、投篮、三分、罚球、时长）和各项最高
    python nba_boxscore.py game 0012600018 --team PHI --all-stats

    # 半场、单节：用比赛页里的逐回合记录（playByPlay.actions）按节累计。
    # 会先拿全场复算和官方全场表逐人逐项对一遍，对上了才说节次数据可用；对不上时照样输出，但退出码是 3
    python nba_boxscore.py game 0012600034 --periods 1-2 --all-stats
    python nba_boxscore.py game 0012600034 --periods 3 --team PHI --stat steals
    # 正负值（"半场正负值+10最高"）：按在场推断累计，单独核对全场官方正负值，过了才能用
    python nba_boxscore.py game 0012600034 --periods 1-2 --team PHI --stat plusMinusPoints

    # 保存原始 JSON 作证据；复核时用 --from-raw 读回来，不联网
    python nba_boxscore.py game 0012600034 --periods 1-2 --save-raw evidence/0012600034.json
    python nba_boxscore.py game 0012600034 --periods 1-2 --all-stats --from-raw evidence/0012600034.json

    # 球员页：生日、最近若干场的逐场数据（注意这是"最近几场"，不保证是完整赛季）
    python nba_boxscore.py player https://www.nba.com/player/2544/lebron-james

    # 球员生涯逐季与总计（stats.nba.com playercareerstats，PerMode=Totals，含常规赛和季后赛）：
    # 生涯投丢数、得分总计、季后赛场次这类；会先核对逐季合计（转会赛季只算 TOT 行）和官方生涯总计
    python nba_boxscore.py career 2544
    python nba_boxscore.py career https://www.nba.com/player/2544/lebron-james --season-type playoffs --save-raw evidence/2544-career.json

    # 记账：每次调用往 JSONL 追加一行（和 search.py 的 --log 同一种格式）
    python nba_boxscore.py --log calls.jsonl --label H03 game 0012600018 --team PHI --stat assists

要点：
  - "全队最高"按包含并列判断；比较对象是**所有实际出场的球员**，替补也算。
  - 页面上的比赛时间是计划开赛时间，不是任何报道的发布时间。
  - 比赛页、球员页、官方数据接口是同一套统计来源，不能算成几个独立确认。
  - stats.nba.com 的请求头给不全时不报错、直接挂到超时，career 已带全套浏览器请求头；返回 200 却是一张网页时，
    就是被拦截了（2026-10-20 实测：代理出口和国内家庭宽带直连都如此，换出口不一定有用），用 --from-raw 读别处存下的数据。
    "30 分场次"这类要逐场数据，career 给不了。
  - 节次数据是按**现在**的官方逐回合记录复算的；赛后数据更正会让它和发稿当时的数字不同，
    不能据此断定半场快讯在发稿当时就写错了。
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from pathlib import Path

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"
BEIJING = dt.timezone(dt.timedelta(hours=8))

# --all-stats 输出的数据项（官方全场表里的字段名 → 中文），也是节次复算支持的全部数据项
ALL_STATS = [
    ("points", "得分"), ("reboundsTotal", "篮板"), ("assists", "助攻"), ("steals", "抢断"),
    ("blocks", "盖帽"), ("turnovers", "失误"),
    ("fieldGoalsMade", "投篮命中"), ("fieldGoalsAttempted", "投篮出手"),
    ("threePointersMade", "三分命中"), ("threePointersAttempted", "三分出手"),
    ("freeThrowsMade", "罚球命中"), ("freeThrowsAttempted", "罚球出手"),
]
STAT_KEYS = [key for key, _ in ALL_STATS]
# 正负值不是计数，靠在场推断算出来，单独核对，不算进 STAT_KEYS
PLUS_MINUS = "plusMinusPoints"
STAT_NAMES = {**dict(ALL_STATS), PLUS_MINUS: "正负值"}
LEADER_STATS = STAT_KEYS[:6]  # "各项最高"只比计数类，命中、出手不比；有正负值时另外加上
SHOOTING = [("投篮", "fieldGoalsMade", "fieldGoalsAttempted"), ("三分", "threePointersMade", "threePointersAttempted"),
            ("罚球", "freeThrowsMade", "freeThrowsAttempted")]
MINUTES_TOLERANCE = 1.0  # 全场时长核对允许的误差（秒）：官方表只精确到秒
INTEGRITY_EXIT = 3  # 节次复算没通过全场核对时的退出码


LEDGER: dict = {"path": None, "label": ""}


def log_call(url: str, status, seconds: float, **extra) -> None:
    if not LEDGER["path"]:
        return
    entry = {"at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), "label": LEDGER["label"],
             "mode": "nba_boxscore", "url": url, "status": status, "attempts": 1, "seconds": round(seconds, 3),
             **extra}
    with open(LEDGER["path"], "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")


def fetch_next_data(url: str) -> dict:
    request = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "text/html"})
    t0 = time.monotonic()
    try:
        with urllib.request.urlopen(request, timeout=40) as response:
            html = response.read().decode("utf-8", "replace")
            log_call(url, response.status, time.monotonic() - t0)
    except urllib.error.HTTPError as err:
        log_call(url, err.code, time.monotonic() - t0)
        sys.exit(f"HTTP {err.code}：{url}")
    except (urllib.error.URLError, TimeoutError) as err:
        log_call(url, 0, time.monotonic() - t0)
        sys.exit(f"网络错误：{err}（{url}）")
    match = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', html, re.S)
    if not match:
        sys.exit(f"页面里没有 __NEXT_DATA__（页面结构可能变了，或被拦截）：{url}")
    return json.loads(match.group(1))["props"]["pageProps"]


def save_raw(path: str, url: str, props: dict, key: str = "pageProps") -> None:
    """把页面内嵌的 pageProps（或接口返回的 JSON）原样存下来，外面只包一层来源和取得时间。"""
    what = "NBA.com 页面内嵌 __NEXT_DATA__ 的 props.pageProps" if key == "pageProps" else "stats.nba.com 接口返回的 JSON"
    record = {
        "source_url": url,
        "fetched_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "note": f"{what}，原样保存；用 --from-raw 可离线重跑",
        key: props,
    }
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(record, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"原始数据已保存：{target}", file=sys.stderr)


def load_page(args: argparse.Namespace, url: str) -> dict:
    """取页面 pageProps：给了 --from-raw 就读文件、不联网；否则联网取，给了 --save-raw 就存一份。"""
    if getattr(args, "from_raw", None):
        saved = json.loads(Path(args.from_raw).read_text(encoding="utf-8"))
        # 也接受直接存下来的 pageProps（没有外面那一层）
        return saved.get("pageProps", saved)
    props = fetch_next_data(url)
    if getattr(args, "save_raw", None):
        save_raw(args.save_raw, url, props)
    return props


def game_url(arg: str, page: str = "box-score") -> str:
    if arg.startswith("http"):
        if page == "box-score":
            return arg
        return re.sub(r"/(box-score|play-by-play|game-charts|summary)\b.*$", "", arg).rstrip("/") + f"/{page}"
    # 只给比赛 ID 时，NBA.com 也接受 /game/<id> 的短地址
    return f"https://www.nba.com/game/{arg}/{page}"


def minutes_played(raw) -> float:
    """统一两种时长写法：ISO（PT24M42.00S）和 "24:42"。"""
    if raw in (None, ""):
        return 0.0
    text = str(raw)
    iso = re.fullmatch(r"PT(\d+)M([\d.]+)S", text)
    if iso:
        return int(iso[1]) + float(iso[2]) / 60
    clock = re.fullmatch(r"(\d+):(\d+)", text)
    if clock:
        return int(clock[1]) + int(clock[2]) / 60
    try:
        return float(text)
    except ValueError:
        return 0.0


def player_name(player: dict) -> str:
    return player.get("name") or " ".join(p for p in (player.get("firstName"), player.get("familyName")) if p)


def is_starter(player: dict) -> bool:
    # 官方数据里没有 starter 字段：首发球员带位置（F/C/G），替补的 position 为空
    return bool(player.get("position")) or str(player.get("starter", "")).lower() in ("1", "true")


def summarize(tricode, rows: list[dict], stat: str) -> dict:
    """一队的最高者：只在实际出场的球员里比，首发和替补一起比；最高值有几人就列几人（并列）。"""

    def leaders_of(key: str) -> tuple:
        played = [r for r in rows if r["played"]]
        values = [r[key] for r in played if isinstance(r.get(key), (int, float))]
        top = max(values) if values else None
        return top, ([r["name"] for r in played if r.get(key) == top] if top is not None else [])

    top, leaders = leaders_of(stat)
    result = {
        "team": tricode,
        "stat": stat,
        "max_among_players_who_played": top,
        "leaders": leaders,
        "tie": len(leaders) > 1,
        "players": sorted(rows, key=lambda r: (-(r.get(stat) or 0), -r["minutes"])),
    }
    if rows and all(key in rows[0] for key in LEADER_STATS):
        result["leaders_by_stat"] = {}
        for key in LEADER_STATS + ([PLUS_MINUS] if PLUS_MINUS in rows[0] else []):
            best, names = leaders_of(key)
            result["leaders_by_stat"][key] = {"max": best, "leaders": names}
    return result


def team_table(team: dict, stat: str, fields: list[str] | None = None) -> dict:
    """一队的逐人数据（官方全场表）和某项数据的全队最高。

    fields 是要一并列出的数据项（--all-stats 时是全部常用项）。
    数据项不存在时抛 ValueError，并列出可用的数据项。
    """
    fields = list(dict.fromkeys([stat, *(fields or [])]))
    rows = []
    for p in team.get("players", []):
        stats = p.get("statistics", {}) or {}
        minutes = round(minutes_played(stats.get("minutes")), 2)
        row = {"person_id": p.get("personId"), "name": player_name(p), "starter": is_starter(p),
               "minutes": minutes, "played": minutes > 0}
        for key in fields:
            row[key] = stats.get(key)
        row["comment"] = p.get("comment") or ""
        rows.append(row)
    if rows and all(r[stat] is None for r in rows):
        sample = next((p.get("statistics") or {} for p in team.get("players", []) if p.get("statistics")), {})
        raise ValueError(f"数据项 {stat!r} 不存在。可用的有：{', '.join(sorted(sample)) or '（页面没有逐人数据）'}")
    return summarize(team.get("teamTricode"), rows, stat)


# ---------------------------------------------------------------- 逐回合复算

SUB_RE = re.compile(r"SUB:\s*(.+?)\s+FOR\s+(.+?)\s*$")
ASSIST_RE = re.compile(r"\(([^()]+?) \d+ AST\)")
STEAL_RE = re.compile(r"\bSTEAL \(\d+ STL\)")
BLOCK_RE = re.compile(r"\bBLOCK \(\d+ BLK\)")
SUFFIX_RE = re.compile(r"\s+(jr|sr|ii|iii|iv)\.?$", re.I)


def fold(name: str) -> str:
    """名字比对用：去掉重音符号（Dëmin → demin），只留字母数字，转小写。"""
    decomposed = unicodedata.normalize("NFKD", name or "")
    return re.sub(r"[^a-z0-9]", "", "".join(c for c in decomposed if not unicodedata.combining(c)).lower())


def name_keys(name: str) -> set[str]:
    """一个名字的几种比对写法：原样，以及去掉 Jr./III 这类后缀。"""
    return {fold(name), fold(SUFFIX_RE.sub("", (name or "").strip()))} - {""}


class Roster:
    """比赛页里两队的球员名单，用来把描述里的姓（助攻、换人）对回 personId。只在本队里找。"""

    def __init__(self, game: dict) -> None:
        self.team_of: dict = {}
        self.players: dict = {}
        self.keys: dict = {}
        self.tricodes = []
        for side in ("homeTeam", "awayTeam"):
            team = game.get(side) or {}
            self.tricodes.append(team.get("teamTricode"))
            for p in team.get("players", []):
                pid = p.get("personId")
                self.players[pid] = p
                self.team_of[pid] = team.get("teamTricode")
                self.keys[pid] = (name_keys(p.get("familyName", "")) | name_keys(p.get("nameI", ""))
                                  | name_keys(player_name(p)))

    def resolve(self, tricode, name: str):
        wanted = name_keys(name)
        candidates = [pid for pid, keys in self.keys.items() if self.team_of[pid] == tricode and wanted & keys]
        if len(candidates) > 1:
            # 同队同姓：只留真正上过场的
            played = [pid for pid in candidates
                      if minutes_played((self.players[pid].get("statistics") or {}).get("minutes")) > 0]
            candidates = played if len(played) == 1 else candidates
        return candidates[0] if len(candidates) == 1 else None


def period_length(period: int) -> float:
    return 720.0 if period <= 4 else 300.0


def clock_remaining(clock) -> float | None:
    text = str(clock or "")
    iso = re.fullmatch(r"PT(\d+)M([\d.]+)S", text)
    if iso:
        return int(iso[1]) * 60 + float(iso[2])
    plain = re.fullmatch(r"(\d+):([\d.]+)", text)
    if plain:
        return int(plain[1]) * 60 + float(plain[2])
    return None


def chronological(actions: list[dict]) -> list[dict]:
    """一节里的事件按发生先后排：先按比赛时钟（剩余时间从多到少）；同一时刻先排得分等其他事件、
    后排换人；再按 actionNumber。

    同一停表时刻的罚球，官方正负值算给停表那一刻在场的阵容，罚球中间换上来的人不算。
    0012600034 实测对比了几种排法：照页面列表顺序 20 人对不上，按时钟加 actionNumber 10 人对不上，
    同一时刻先得分后换人则 30 人全部与官方一致。不能只按 actionNumber 排：赛后更正的事件会拿到
    很大的编号插回去。读不出时钟的事件沿用前一条的时间。时长和计数不受这个先后影响。
    """
    keyed, last = [], float("inf")
    for index, action in enumerate(actions):
        remaining = clock_remaining(action.get("clock"))
        last = remaining if remaining is not None else last
        number = action.get("actionNumber")
        is_sub = (action.get("actionType") or "") == "Substitution"
        keyed.append(((-last, is_sub, number if isinstance(number, (int, float)) else float("inf"), index), action))
    return [action for _, action in sorted(keyed, key=lambda pair: pair[0])]


def recount(game: dict, actions: list[dict]) -> dict:
    """用逐回合记录按节复算每名球员的数据和上场时间。

    计数口径：命中/不中的投篮（shotValue 为 3 的算三分）、罚球（描述以 MISS 开头的是罚丢）、
    篮板、失误按事件的 personId 记；抢断、盖帽是单独的事件（"X STEAL (1 STL)"）；助攻只从
    投篮命中描述里的 "(某人 3 AST)" 取人名，每次命中记一次，不采信括号里的累计数。
    personId 不在名单里的（全队篮板、全队失误、暂停）不记给任何球员。

    时长：每节开节在场的人 = 本节第一次出现时不是"被换上"的人，再按换人事件累计。
    一整节没有任何事件记录的人会被漏掉，所以时长必须先和全场官方时长对上才能用。

    正负值：得分事件（命中 shotValue 分、罚中 1 分）和换人事件按 chronological() 排成一条时间线，
    得分那一刻在场的本队球员各加、对方在场球员各减；同一停表时刻的罚球算给换人之前的阵容。
    它依赖在场推断，同样要先和全场官方正负值对上才能用。记在 stats 里的 plusMinusPoints。

    返回 {"stats": {节: {personId: Counter}}, "seconds": {节: {personId: 秒}},
          "periods": [出现过的节], "unresolved": [对不回人的名字], "warnings": [...]}
    """
    roster = Roster(game)
    stats: dict = defaultdict(lambda: defaultdict(Counter))
    seconds: dict = defaultdict(lambda: defaultdict(float))
    unresolved: list[str] = []
    warnings: list[str] = []
    by_period: dict = defaultdict(list)
    for action in actions:
        if isinstance(action.get("period"), int) and action["period"] >= 1:
            by_period[action["period"]].append(action)

    def resolve(tricode, name: str, period: int, desc: str):
        pid = roster.resolve(tricode, name)
        if pid is None:
            unresolved.append(f"第{period}节 {tricode or '?'} “{name}”（{desc}）")
        return pid

    for period in sorted(by_period):
        length = period_length(period)
        first_role: dict = {}  # 本节第一次出现时的身份：in = 被换上，其他 = 开节就在场
        timeline: list[tuple] = []  # ("sub", 已过秒数, 换上, 换下, 描述) 或 ("score", 球队, 分数)
        for action in chronological(by_period[period]):
            pid = action.get("personId") if action.get("personId") in roster.players else None
            tricode = action.get("teamTricode") or roster.team_of.get(pid)
            kind = action.get("actionType") or ""
            desc = action.get("description") or ""
            if kind == "Substitution":
                match = SUB_RE.search(desc)
                if match:
                    pid_in = resolve(tricode, match[1], period, desc)
                    pid_out = pid or resolve(tricode, match[2], period, desc)
                else:  # 另一种写法：换上、换下各一条，subType 是 in / out
                    sub_type = str(action.get("subType", "")).lower()
                    pid_in, pid_out = (pid, None) if sub_type == "in" else (None, pid)
                for who, role in ((pid_out, "out"), (pid_in, "in")):
                    if who is not None:
                        first_role.setdefault(who, role)
                remaining = clock_remaining(action.get("clock"))
                if remaining is None:
                    warnings.append(f"第{period}节换人事件没有可读的时间：{desc}")
                    continue
                timeline.append(("sub", length - remaining, pid_in, pid_out, desc))
                continue

            scored = 0
            if kind == "Made Shot":
                scored = int(action.get("shotValue") or (3 if "3PT" in desc else 2))
            elif kind == "Free Throw" and not desc.upper().startswith("MISS"):
                scored = 1
            if scored:
                if tricode in roster.tricodes:
                    timeline.append(("score", tricode, scored))
                else:
                    warnings.append(f"第{period}节得分事件认不出是哪队的：{desc}")

            if pid is not None:
                first_role.setdefault(pid, "event")
                row = stats[period][pid]
                if kind == "Made Shot":
                    value = int(action.get("shotValue") or (3 if "3PT" in desc else 2))
                    row["points"] += value
                    row["fieldGoalsMade"] += 1
                    row["fieldGoalsAttempted"] += 1
                    if value == 3:
                        row["threePointersMade"] += 1
                        row["threePointersAttempted"] += 1
                elif kind == "Missed Shot":
                    row["fieldGoalsAttempted"] += 1
                    if int(action.get("shotValue") or (3 if "3PT" in desc else 2)) == 3:
                        row["threePointersAttempted"] += 1
                elif kind == "Free Throw":
                    row["freeThrowsAttempted"] += 1
                    if not desc.upper().startswith("MISS"):
                        row["freeThrowsMade"] += 1
                        row["points"] += 1
                elif kind == "Rebound":
                    row["reboundsTotal"] += 1
                elif kind == "Turnover":
                    row["turnovers"] += 1
                elif STEAL_RE.search(desc):
                    row["steals"] += 1
                elif BLOCK_RE.search(desc):
                    row["blocks"] += 1
            if kind == "Made Shot":
                for match in ASSIST_RE.finditer(desc):
                    helper = resolve(tricode, match[1], period, desc)
                    if helper is not None:
                        stats[period][helper]["assists"] += 1
                        first_role.setdefault(helper, "event")

        on_court = {pid: 0.0 for pid, role in first_role.items() if role != "in"}
        for tricode in roster.tricodes:
            count = sum(1 for pid in on_court if roster.team_of[pid] == tricode)
            if count != 5:
                warnings.append(f"第{period}节 {tricode} 推断出 {count} 名开节在场球员（应为 5），这一节的时长不可靠")
        for item in timeline:
            if item[0] == "score":
                _, scorer, points = item
                for pid in on_court:
                    stats[period][pid][PLUS_MINUS] += points if roster.team_of[pid] == scorer else -points
                continue
            _, elapsed, pid_in, pid_out, desc = item
            if pid_out is not None:
                if pid_out in on_court:
                    seconds[period][pid_out] += elapsed - on_court.pop(pid_out)
                else:
                    warnings.append(f"第{period}节被换下的人此前不在场：{desc}")
            if pid_in is not None:
                if pid_in in on_court:
                    warnings.append(f"第{period}节被换上的人已经在场：{desc}")
                else:
                    on_court[pid_in] = elapsed
        for pid, since in on_court.items():
            seconds[period][pid] += length - since

    return {"stats": stats, "seconds": seconds, "periods": sorted(by_period),
            "unresolved": unresolved, "warnings": warnings, "roster": roster}


def total(counted: dict, periods) -> tuple[dict, dict]:
    """把若干节加起来：返回 ({personId: Counter}, {personId: 秒})。"""
    sums: dict = defaultdict(Counter)
    secs: dict = defaultdict(float)
    for period in periods:
        for pid, row in counted["stats"].get(period, {}).items():
            sums[pid].update(row)
        for pid, value in counted["seconds"].get(period, {}).items():
            secs[pid] += value
    return sums, secs


def check_full_game(game: dict, counted: dict, periods: list[int]) -> dict:
    """全场复算和官方全场表逐人逐项对照；再把所选节次的复算比分和官方分节比分对照。

    计数类全部对上、名字全部对得回人、分节比分对上，节次的计数数据才可信（counting_ok）；
    时长另算（minutes_ok），因为整节没有事件的人会被漏掉；正负值也靠在场推断，
    要时长先过、再逐人对上官方正负值（plus_minus_ok）。
    """
    full, full_secs = total(counted, counted["periods"])
    differences, minute_differences, pm_differences = [], [], []
    pm_checked = False
    players = 0
    for side in ("homeTeam", "awayTeam"):
        for p in game[side].get("players", []):
            official = p.get("statistics") or {}
            pid = p.get("personId")
            official_secs = minutes_played(official.get("minutes")) * 60
            if official_secs > 0 or full.get(pid):
                players += 1
            for key in STAT_KEYS:
                if official.get(key) is None:
                    continue
                if int(official[key]) != full.get(pid, Counter()).get(key, 0):
                    differences.append({"player": player_name(p), "stat": key,
                                        "recount": full.get(pid, Counter()).get(key, 0), "official": official[key]})
            if abs(official_secs - full_secs.get(pid, 0.0)) > MINUTES_TOLERANCE:
                minute_differences.append({"player": player_name(p), "recount_seconds": round(full_secs.get(pid, 0.0), 1),
                                           "official": official.get("minutes")})
            if official.get(PLUS_MINUS) is not None and (official_secs > 0 or full_secs.get(pid, 0.0) > 0):
                pm_checked = True
                recounted = full.get(pid, Counter()).get(PLUS_MINUS, 0)
                if int(round(float(official[PLUS_MINUS]))) != recounted:
                    pm_differences.append({"player": player_name(p), "recount": recounted,
                                           "official": official[PLUS_MINUS]})

    sums, _ = total(counted, periods)
    roster = counted["roster"]
    period_score = {}
    for side in ("homeTeam", "awayTeam"):
        team = game[side]
        tricode = team.get("teamTricode")
        official_periods = {q.get("period"): q.get("score") for q in team.get("periods") or []}
        official = (sum(int(official_periods[q]) for q in periods)
                    if all(official_periods.get(q) is not None for q in periods) else None)
        recounted = sum(row.get("points", 0) for pid, row in sums.items() if roster.team_of.get(pid) == tricode)
        period_score[tricode] = {"recount": recounted, "official_linescore": official}
    score_ok = all(v["official_linescore"] in (None, v["recount"]) for v in period_score.values())

    counting_ok = not differences and not counted["unresolved"] and score_ok
    minutes_ok = counting_ok and not minute_differences and not counted["warnings"]
    return {
        "counting_ok": counting_ok,
        "minutes_ok": minutes_ok,
        "plus_minus_ok": minutes_ok and pm_checked and not pm_differences,
        "plus_minus_checked": pm_checked,
        "plus_minus_differences": pm_differences,
        "players_checked": players,
        "stats_checked": len(STAT_KEYS),
        "differences": differences,
        "minutes_differences": minute_differences,
        "unresolved_names": counted["unresolved"],
        "lineup_warnings": counted["warnings"],
        "period_score": period_score,
    }


def parse_periods(text: str) -> list[int]:
    """"1-2" "3" "1,3" "1-2,5"（5 是第一个加时）。"""
    periods: set[int] = set()
    for part in text.replace("，", ",").split(","):
        part = part.strip()
        match = re.fullmatch(r"(\d+)\s*-\s*(\d+)", part)
        if match and 1 <= int(match[1]) <= int(match[2]):
            periods.update(range(int(match[1]), int(match[2]) + 1))
        elif part.isdigit() and int(part) >= 1:
            periods.add(int(part))
        else:
            raise ValueError(f"节次写法不对：{text!r}（例如 1-2、3、1,3；5 是第一个加时）")
    return sorted(periods)


def period_label(periods: list[int]) -> str:
    def one(q: int) -> str:
        return f"第{q}节" if q <= 4 else f"加时{q - 4}"

    if periods == list(range(periods[0], periods[-1] + 1)) and len(periods) > 1:
        return f"{one(periods[0])}到{one(periods[-1])}"
    return "、".join(one(q) for q in periods)


def recount_table(team: dict, counted: dict, periods: list[int], stat: str) -> dict:
    """所选节次的逐人数据（复算）和全队最高。首发/替补指的是全场首发。"""
    sums, secs = total(counted, periods)
    rows = []
    for p in team.get("players", []):
        pid = p.get("personId")
        row = {"person_id": pid, "name": player_name(p), "starter": is_starter(p),
               "minutes": round(secs.get(pid, 0.0) / 60, 2)}
        counts = sums.get(pid, Counter())
        row["played"] = secs.get(pid, 0.0) > 0 or any(counts.values())
        for key in STAT_KEYS + [PLUS_MINUS]:
            row[key] = counts.get(key, 0)
        row["comment"] = p.get("comment") or ""
        rows.append(row)
    return summarize(team.get("teamTricode"), rows, stat)


# ---------------------------------------------------------------- 输出

def shooting(row: dict, made: str, attempted: str) -> str:
    return f"{row.get(made)}/{row.get(attempted)}"


def show(key: str, value) -> str:
    """正负值带正负号（+10、-3、0），其余原样；没有的写 -。"""
    if value is None:
        return "-"
    if key == PLUS_MINUS and isinstance(value, (int, float)):
        value = int(round(value))
        return f"{value:+d}" if value else "0"
    return str(value)


def shown_width(text: str) -> int:
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in text)


def rjust(text: str, width: int) -> str:
    """按显示宽度右对齐（一个汉字占两格）。"""
    return " " * max(0, width - shown_width(text)) + text


def ljust(text: str, width: int) -> str:
    return text + " " * max(0, width - shown_width(text))


def print_team(team: dict, stat: str, all_stats: bool, minutes_note: str = "", pm_note: str = "") -> None:
    """minutes_note / pm_note 为 "?" 时表示节次时长 / 正负值没通过全场核对，不能用。"""
    label = STAT_NAMES.get(stat, stat)
    tie = "（并列）" if team["tie"] else ""
    caveat = "（没通过全场核对，不能用）" if stat == PLUS_MINUS and pm_note else ""
    best_value = show(stat, team["max_among_players_who_played"])
    print(f"[{team['team']}] {label} 最高 {best_value}：{'、'.join(team['leaders'])}{tie}{caveat}")
    if all_stats:
        best = team.get("leaders_by_stat", {})
        parts = [f"{STAT_NAMES[k]} {show(k, v['max'])} {'、'.join(v['leaders'])}"
                 + ("（未通过核对）" if k == PLUS_MINUS and pm_note else "")
                 for k, v in best.items() if v["leaders"]]
        print(f"   各项最高（含并列，只比实际出场的球员）：{'｜'.join(parts)}")
        with_pm = bool(team["players"]) and PLUS_MINUS in team["players"][0]
        header = "".join(rjust(STAT_NAMES[k], 6) for k in LEADER_STATS)
        header += rjust("正负值" + pm_note, 8) if with_pm else ""
        shots = " ".join(rjust(label, 8) for label, _, _ in SHOOTING)
        print(f"   {'姓名':<24} {'':<4} {rjust('时长' + minutes_note, 6)}{header}  {shots}")
        for r in team["players"]:
            flag = "" if r["played"] else "  未出场"
            role = "首发" if r["starter"] else "替补"
            counts = "".join(f"{r.get(k)!s:>6}" for k in LEADER_STATS)
            counts += f"{show(PLUS_MINUS, r.get(PLUS_MINUS)):>8}" if with_pm else ""
            shots = " ".join(f"{shooting(r, made, attempted):>8}" for _, made, attempted in SHOOTING)
            print(f"   {r['name']:<26} {role} {r['minutes']:>6.1f}{counts}  {shots}{flag} {r['comment']}")
    else:
        note = pm_note if stat == PLUS_MINUS else ""
        for r in team["players"]:
            flag = "" if r["played"] else "  未出场"
            role = "首发" if r["starter"] else "替补"
            value = show(stat, r[stat]) + note
            print(f"   {r['name']:<26} {role}  {stat}={value:<4} 时长={r['minutes']:.1f}{minutes_note}{flag} {r['comment']}")
    print()


def print_integrity(check: dict, periods: list[int]) -> None:
    if check["counting_ok"]:
        print(f"全场复算核对：{check['players_checked']} 名出场球员 × {check['stats_checked']} 项与官方全场表一致，"
              f"所选节次比分与官方分节比分一致 → 节次的计数数据可用")
    else:
        print(f"⚠ 全场复算核对没通过，节次数据不能直接采信（退出码 {INTEGRITY_EXIT}）：")
        for d in check["differences"][:20]:
            print(f"   {d['player']} {d['stat']}：复算 {d['recount']} / 官方 {d['official']}")
        for name in check["unresolved_names"][:10]:
            print(f"   名字对不回人：{name}")
        for tricode, v in check["period_score"].items():
            if v["official_linescore"] not in (None, v["recount"]):
                print(f"   {tricode} {period_label(periods)}得分：复算 {v['recount']} / 官方分节比分 {v['official_linescore']}")
    if check["minutes_ok"]:
        print(f"时长核对：全场时长逐人与官方一致（误差 ≤{MINUTES_TOLERANCE:.0f} 秒）→ 节次时长可用")
    elif not check["counting_ok"]:
        print("⚠ 计数核对没通过，节次时长也不能用")
    else:
        print("⚠ 时长核对没通过，下面的节次时长不能用：")
        for d in check["minutes_differences"][:10]:
            print(f"   {d['player']}：复算 {d['recount_seconds']} 秒 / 官方 {d['official']}")
        for w in check["lineup_warnings"][:10]:
            print(f"   {w}")
    if check["plus_minus_ok"]:
        print("正负值核对：全场正负值逐人与官方一致 → 节次正负值可用")
    elif not check["minutes_ok"]:
        print("⚠ 时长（在场推断）没通过，节次正负值也不能用")
    elif not check["plus_minus_checked"]:
        print("⚠ 官方全场表里没有正负值，节次正负值无法核对，不能用")
    else:
        print("⚠ 正负值核对没通过，下面的节次正负值不能用：")
        for d in check["plus_minus_differences"][:10]:
            print(f"   {d['player']}：复算 {show(PLUS_MINUS, d['recount'])} / 官方 {show(PLUS_MINUS, d['official'])}")
    score = "  ".join(f"{t} {v['recount']}" for t, v in check["period_score"].items())
    print(f"{period_label(periods)}比分（复算）：{score}")
    print("节次数据是按现在的官方逐回合记录复算的；赛后的数据更正会让它和发稿当时的数字不同。\n")


def cmd_game(args: argparse.Namespace) -> None:
    periods = None
    if args.periods:
        try:
            periods = parse_periods(args.periods)
        except ValueError as err:
            sys.exit(str(err))
        if args.stat not in STAT_KEYS + [PLUS_MINUS]:
            sys.exit(f"节次复算只支持这些数据项：{', '.join(STAT_KEYS + [PLUS_MINUS])}")

    props = load_page(args, game_url(args.game))
    actions = ((props.get("playByPlay") or {}).get("actions")) or []
    if periods and not actions:
        if args.from_raw:
            sys.exit("保存的文件里没有逐回合记录（playByPlay.actions），做不了节次复算")
        # 比赛页通常自带逐回合记录；没有时取逐回合页（它也带全场表，两者是同一时刻的数据）
        props = load_page(args, game_url(args.game, "play-by-play"))
        actions = ((props.get("playByPlay") or {}).get("actions")) or []
        if not actions:
            sys.exit("比赛页和逐回合页里都没有逐回合记录，做不了节次复算")

    game = props["game"]
    tipoff = dt.datetime.fromisoformat(game["gameTimeUTC"].replace("Z", "+00:00"))
    teams = [game["homeTeam"], game["awayTeam"]]
    if args.team:
        teams = [t for t in teams if t["teamTricode"].upper() == args.team.upper()]
        if not teams:
            sys.exit(f"这场比赛里没有 {args.team}：{game['homeTeam']['teamTricode']} vs {game['awayTeam']['teamTricode']}")

    result = {
        "game_id": game["gameId"],
        "status": game.get("gameStatusText"),
        "periods": game.get("period"),
        "score": {t["teamTricode"]: t.get("score") for t in (game["homeTeam"], game["awayTeam"])},
        "tipoff_scheduled_beijing": tipoff.astimezone(BEIJING).isoformat(),
        "teams": [],
    }
    check = None
    if periods:
        counted = recount(game, actions)
        missing = [q for q in periods if q not in counted["periods"]]
        if missing:
            sys.exit(f"逐回合记录里没有{period_label(missing)}（记录里有：{period_label(counted['periods'])}）")
        check = check_full_game(game, counted, periods)
        result["scope"] = {"periods": periods, "label": period_label(periods),
                           "source": "比赛页内嵌的逐回合记录 playByPlay.actions，按节复算"}
        result["integrity"] = check
        for team in teams:
            result["teams"].append(recount_table(team, counted, periods, args.stat))
    else:
        fields = STAT_KEYS + [PLUS_MINUS] if args.all_stats else None
        for team in teams:
            try:
                result["teams"].append(team_table(team, args.stat, fields))
            except ValueError as err:
                sys.exit(str(err))

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"{result['game_id']}  {result['status']}  节数 {result['periods']}  比分 {result['score']}")
        print(f"计划开赛（北京时间）：{result['tipoff_scheduled_beijing']}——这不是任何报道的发布时间\n")
        minutes_note = pm_note = ""
        if periods:
            print(f"范围：{period_label(periods)}（{result['scope']['source']}）")
            print_integrity(check, periods)
            minutes_note = "" if check["minutes_ok"] else "?"
            pm_note = "" if check["plus_minus_ok"] else "?"
        for team in result["teams"]:
            print_team(team, args.stat, args.all_stats, minutes_note, pm_note)
    if check is not None and not check["counting_ok"]:
        sys.exit(INTEGRITY_EXIT)


# ---------------------------------------------------------------- 生涯数据（stats.nba.com）

STATS_API = "https://stats.nba.com/stats"
# stats.nba.com 的请求头给不全时不报错，而是一直挂到超时，很像被墙；照浏览器给全
STATS_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Encoding": "gzip, deflate",
    "Accept-Language": "en-US,en;q=0.9",
    "Connection": "keep-alive",
    "Origin": "https://www.nba.com",
    "Referer": "https://www.nba.com/",
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-site",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/142.0.0.0 Safari/537.36",
    "sec-ch-ua": '"Chromium";v="142", "Not_A Brand";v="99"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "x-nba-stats-origin": "stats",
    "x-nba-stats-token": "true",
}
STATS_TIMEOUT = 30
BLOCKED_HINT = ("被 Akamai 拦截了（2026-10-20 实测：经代理出口、从国内家庭宽带直连都只回这张错误页，请求头给全也一样，"
                "换出口不一定有用）。生涯和历史数据改用 Basketball-Reference 的搜索快照，"
                "或在能访问的环境里加 --save-raw 存下来，再用 --from-raw 读")

CAREER_SETS = {
    "regular": ("SeasonTotalsRegularSeason", "CareerTotalsRegularSeason", "常规赛"),
    "playoffs": ("SeasonTotalsPostSeason", "CareerTotalsPostSeason", "季后赛"),
}
CAREER_COUNTS = [("GP", "场次"), ("GS", "首发"), ("MIN", "时长"), ("PTS", "得分"), ("REB", "篮板"), ("AST", "助攻"),
                 ("STL", "抢断"), ("BLK", "盖帽"), ("TOV", "失误")]
CAREER_SHOTS = [("投篮", "FGM", "FGA"), ("三分", "FG3M", "FG3A"), ("罚球", "FTM", "FTA")]
CAREER_SUM_FIELDS = ["GP", "GS", "MIN", "FGM", "FGA", "FG3M", "FG3A", "FTM", "FTA", "OREB", "DREB", "REB",
                     "AST", "STL", "BLK", "TOV", "PF", "PTS"]


def decode_stats_body(body: bytes, content_type: str, encoding: str | None) -> dict:
    """解压（gzip / deflate）并解析 JSON。拿到的是网页而不是数据时抛 ValueError，说明多半被拦截了。"""
    import gzip
    import zlib

    if encoding == "gzip":
        body = gzip.decompress(body)
    elif encoding == "deflate":
        try:
            body = zlib.decompress(body)
        except zlib.error:
            body = zlib.decompress(body, -zlib.MAX_WBITS)
    if "html" in (content_type or "").lower() or body.lstrip()[:1] == b"<":
        raise ValueError(f"stats.nba.com 返回的是网页（{len(body)} 字节），不是数据：{BLOCKED_HINT}")
    try:
        return json.loads(body)
    except json.JSONDecodeError as err:
        raise ValueError(f"stats.nba.com 返回的不是 JSON（{err}）：{body[:200]!r}") from err


def fetch_stats(endpoint: str, params: dict) -> tuple[str, dict]:
    url = f"{STATS_API}/{endpoint}?{urllib.parse.urlencode(params)}"
    request = urllib.request.Request(url, headers=STATS_HEADERS)
    t0 = time.monotonic()
    try:
        with urllib.request.urlopen(request, timeout=STATS_TIMEOUT) as response:
            body = response.read()
            status = response.status
            content_type = response.headers.get("Content-Type", "")
            encoding = response.headers.get("Content-Encoding")
    except urllib.error.HTTPError as err:
        log_call(url, err.code, time.monotonic() - t0)
        sys.exit(f"HTTP {err.code}：{url}")
    except (urllib.error.URLError, TimeoutError) as err:
        log_call(url, 0, time.monotonic() - t0)
        sys.exit(f"请求 stats.nba.com 失败或超时（{err}）：它在请求头给不全时不报错、直接挂住，这里已带全套浏览器"
                 f"请求头；仍然超时多半是网络出口的问题。{url}")
    try:
        payload = decode_stats_body(body, content_type, encoding)
    except ValueError as err:
        log_call(url, status, time.monotonic() - t0, blocked=True)
        sys.exit(str(err))
    log_call(url, status, time.monotonic() - t0)
    return url, payload


def result_sets(payload: dict) -> dict[str, list[dict]]:
    """stats.nba.com 的 resultSets（表头 + 行）转成 {表名: [逐行字典]}。"""
    sets = payload.get("resultSets") or []
    if isinstance(sets, dict):
        sets = [sets]
    tables = {}
    for table in sets:
        headers, rows = table.get("headers") or [], table.get("rowSet") or []
        if table.get("name"):
            tables[table["name"]] = [dict(zip(headers, row)) for row in rows]
    return tables


def counted_seasons(rows: list[dict]) -> list[dict]:
    """逐季合计用哪些行：赛季中被交易时每队一行、另有一行 TOT（全季合计），合计只用 TOT 那行。"""
    by_season: dict = defaultdict(list)
    for row in rows:
        by_season[row.get("SEASON_ID")].append(row)
    chosen = []
    for items in by_season.values():
        tot = [r for r in items if r.get("TEAM_ABBREVIATION") == "TOT"]
        chosen.extend(tot or items)
    return chosen


def career_section(tables: dict, kind: str) -> dict | None:
    """一种赛季类型（常规赛 / 季后赛）：逐季、官方生涯总计、投丢数，以及逐季合计和官方总计的核对。"""
    season_name, career_name, label = CAREER_SETS[kind]
    seasons = tables.get(season_name) or []
    career = (tables.get(career_name) or [None])[0]
    if not seasons and not career:
        return None
    counted = counted_seasons(seasons)
    differences = []
    sums = {}
    for field in CAREER_SUM_FIELDS:
        values = [r.get(field) for r in counted]
        if not values or any(v is None for v in values):
            continue  # 早年有的数据项没统计（例如 1970 年代前没有抢断、盖帽），不拿来核对
        sums[field] = round(sum(values), 1)
        official = (career or {}).get(field)
        if official is None:
            continue
        # 时长每季可能四舍五入过，允许每季 0.5 的误差；其余逐项必须相等
        tolerance = 0.5 * len(counted) + 0.01 if field == "MIN" else 0.01
        if abs(sums[field] - official) > tolerance:
            differences.append({"stat": field, "season_sum": sums[field], "career_total": official})
    base = career or sums
    missed = {name: base.get(attempted) - base.get(made) for name, made, attempted in CAREER_SHOTS
              if isinstance(base.get(attempted), (int, float)) and isinstance(base.get(made), (int, float))}
    return {
        "label": label,
        "seasons": seasons,
        "career_total": career,
        "missed": missed,
        "check": {"ok": career is not None and not differences, "seasons_counted": len(counted),
                  "differences": differences, "season_sum": sums},
    }


def player_id_of(text: str) -> str:
    match = re.search(r"/player/(\d+)", text) or re.fullmatch(r"\s*(\d+)\s*", text)
    if not match:
        sys.exit(f"认不出球员 ID：{text!r}（给数字 ID，例如 2544，或球员页网址）")
    return match[1]


def fmt_number(value) -> str:
    if value is None:
        return "-"
    return f"{value:.0f}" if isinstance(value, float) and value.is_integer() else str(value)


def print_career_section(section: dict) -> None:
    print(f"== {section['label']} ==")
    header = "".join(rjust(name, 7) for _, name in CAREER_COUNTS)
    shots = " ".join(rjust(name, 11) for name, _, _ in CAREER_SHOTS)
    print(f"   {ljust('赛季', 8)} {ljust('球队', 5)}{header}  {shots}")

    def line(season: str, team: str, row: dict, note: str = "") -> str:
        counts = "".join(f"{fmt_number(row.get(key)):>7}" for key, _ in CAREER_COUNTS)
        made = " ".join(f"{fmt_number(row.get(m)) + '/' + fmt_number(row.get(a)):>11}" for _, m, a in CAREER_SHOTS)
        return f"   {ljust(season, 8)} {ljust(team, 5)}{counts}  {made}{note}"

    traded = {r.get("SEASON_ID") for r in section["seasons"] if r.get("TEAM_ABBREVIATION") == "TOT"}
    for row in section["seasons"]:
        split = row.get("SEASON_ID") in traded and row.get("TEAM_ABBREVIATION") != "TOT"
        print(line(str(row.get("SEASON_ID")), str(row.get("TEAM_ABBREVIATION")), row, "  （分队，不计入合计）" if split else ""))
    if section["career_total"]:
        print(line("生涯总计", "", section["career_total"]))
    if section["missed"]:
        missed = "，".join(f"{name} {fmt_number(value)} 次" for name, value in section["missed"].items())
        print(f"   投丢（出手 − 命中）：{missed}")
    check = section["check"]
    if check["ok"]:
        print(f"   核对：{check['seasons_counted']} 个赛季逐季合计（转会赛季只算 TOT 行）与官方生涯总计一致")
    elif section["career_total"] is None:
        print("   ⚠ 没有官方生涯总计，只有逐季数据")
    else:
        print("   ⚠ 逐季合计与官方生涯总计对不上：" + "；".join(
            f"{d['stat']} 逐季合计 {d['season_sum']} / 官方 {d['career_total']}" for d in check["differences"]))
    print()


def cmd_career(args: argparse.Namespace) -> None:
    person_id = player_id_of(args.player)
    params = {"PlayerID": person_id, "PerMode": "Totals", "LeagueID": "00"}
    if getattr(args, "from_raw", None):
        saved = json.loads(Path(args.from_raw).read_text(encoding="utf-8"))
        url, payload = saved.get("source_url", "（离线文件）"), saved.get("payload", saved)
    else:
        url, payload = fetch_stats("playercareerstats", params)
        if getattr(args, "save_raw", None):
            save_raw(args.save_raw, url, payload, key="payload")
    tables = result_sets(payload)
    kinds = ["regular", "playoffs"] if args.season_type == "all" else [args.season_type]
    result = {"person_id": int(person_id), "source_url": url, "per_mode": "Totals"}
    for kind in kinds:
        result[kind] = career_section(tables, kind)
    if not any(result[k] for k in kinds):
        sys.exit(f"返回的数据里没有生涯表（有的表：{', '.join(tables) or '无'}）")

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    print(f"personId {person_id} 生涯数据（stats.nba.com playercareerstats，PerMode=Totals，含常规赛和季后赛）\n")
    for kind in kinds:
        if result[kind]:
            print_career_section(result[kind])
        else:
            print(f"== {CAREER_SETS[kind][2]} ==\n   没有数据\n")
    print("说明：这里只有逐季和生涯的合计数。\"30 分场次\"\"连续 N 场\"这类要逐场数据，这个接口给不了；"
          "投丢数按出手减命中算。")


def iter_games(node):
    """在赛程页数据里递归找比赛对象（带 gameId 和主客队的字典）。"""
    if isinstance(node, dict):
        if "gameId" in node and "homeTeam" in node and "awayTeam" in node:
            yield node
        for value in node.values():
            yield from iter_games(value)
    elif isinstance(node, list):
        for value in node:
            yield from iter_games(value)


def cmd_schedule(args: argparse.Namespace) -> None:
    """只知道对阵和日期时，用球队赛程页找比赛 ID。"""
    data = load_page(args, f"https://www.nba.com/{args.team_slug}/schedule")
    seen, rows = set(), []
    for g in iter_games(data):
        if g["gameId"] in seen:
            continue
        seen.add(g["gameId"])
        utc = g.get("gameTimeUTC") or g.get("gameDateUTC")
        when = dt.datetime.fromisoformat(utc.replace("Z", "+00:00")) if utc else None
        et_date = (g.get("gameDateEst") or "")[:10]
        bj = when.astimezone(BEIJING) if when else None
        if args.date and args.date not in (et_date, bj.date().isoformat() if bj else ""):
            continue
        rows.append({
            "game_id": g["gameId"],
            "date_et": et_date,
            "tipoff_beijing": bj.isoformat() if bj else None,
            "away": g["awayTeam"].get("teamTricode"),
            "home": g["homeTeam"].get("teamTricode"),
            "score": f"{g['awayTeam'].get('score')}-{g['homeTeam'].get('score')}",
            "status": g.get("gameStatusText"),
            "label": g.get("gameLabel") or "",
        })
    rows.sort(key=lambda r: r["tipoff_beijing"] or "")
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return
    if not rows:
        print("没有找到符合条件的比赛（日期可以用美东日期，也可以用北京日期）")
    for r in rows:
        print(f"{r['game_id']}  美东 {r['date_et']}  北京开赛 {r['tipoff_beijing']}  "
              f"{r['away']} @ {r['home']}  {r['score']}  {r['status']}  {r['label']}")


def cmd_player(args: argparse.Namespace) -> None:
    player = load_page(args, args.url)["player"]
    info = player.get("info", {}) or {}
    logs = player.get("gameLogs", []) or []
    result = {
        "name": " ".join(filter(None, [info.get("FIRST_NAME"), info.get("LAST_NAME")])) or info.get("DISPLAY_FIRST_LAST"),
        "person_id": info.get("PERSON_ID"),
        "birthdate": info.get("BIRTHDATE"),
        "team": info.get("TEAM_ABBREVIATION"),
        "recent_game_logs": logs,
        "note": "gameLogs 是最近若干场，不保证是完整赛季；SEASON_ID 以 1 开头是季前赛、2 开头是常规赛、4 开头是季后赛",
    }
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    print(f"{result['name']}  personId={result['person_id']}  生日={result['birthdate']}  球队={result['team']}")
    print(result["note"] + "\n")
    for g in logs:
        print(
            f"   {g.get('GAME_DATE', '')}  赛季={g.get('SEASON_ID')}  {g.get('MATCHUP', '')}  "
            f"分钟={g.get('MIN')}  得分={g.get('PTS')}  篮板={g.get('REB')}  助攻={g.get('AST')}"
        )


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")  # 报错和提示也是中文，Windows 默认编码会乱码
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    parser.add_argument("--log", help="调用账本（JSONL），每次请求追加一行")
    parser.add_argument("--label", default="", help="写进账本的标签")
    # 原始数据的存取三个子命令都能用，写在子命令前后都可以；SUPPRESS 保证没写时不会覆盖前面的值
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", help="输出 JSON")
    common.add_argument("--save-raw", default=argparse.SUPPRESS, metavar="路径",
                        help="把页面内嵌的原始 JSON（career 是接口返回的 JSON）存到这个文件（作证据、供复核）")
    common.add_argument("--from-raw", default=argparse.SUPPRESS, metavar="路径",
                        help="读 --save-raw 存下的文件，不联网")
    sub = parser.add_subparsers(dest="mode", required=True)
    g = sub.add_parser("game", help="比赛页逐人数据", parents=[common])
    g.add_argument("game", help="比赛页网址或比赛 ID（例如 0012600018）")
    g.add_argument("--team", help="只看一队，例如 PHI")
    g.add_argument("--stat", default="points", help="要比较的数据项：points / reboundsTotal / assists / steals / blocks / turnovers …")
    g.add_argument("--all-stats", action="store_true",
                   help="一次列出得分、篮板、助攻、抢断、盖帽、失误、投篮、三分、罚球、时长，并给出各项最高")
    g.add_argument("--periods", help="只算这几节，例如 1-2（上半场）、3、1,3；5 是第一个加时。"
                                     "按逐回合记录复算，先和全场官方表对一遍")
    p = sub.add_parser("player", help="球员页：生日与最近逐场数据", parents=[common])
    p.add_argument("url", help="球员页网址，例如 https://www.nba.com/player/2544/lebron-james")
    s = sub.add_parser("schedule", help="用球队赛程页找比赛 ID", parents=[common])
    s.add_argument("team_slug", help="球队在 NBA.com 网址里的名字，例如 sixers、lakers、celtics")
    s.add_argument("--date", help="只看这一天，YYYY-MM-DD，美东或北京日期都可以")
    c = sub.add_parser("career", help="球员生涯逐季与总计（stats.nba.com，常规赛和季后赛）", parents=[common])
    c.add_argument("player", help="球员 ID（例如 2544）或球员页网址")
    c.add_argument("--season-type", choices=["all", "regular", "playoffs"], default="all",
                   help="all（默认，常规赛和季后赛都列）/ regular / playoffs")
    parser.add_argument("--save-raw", dest="save_raw", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--from-raw", dest="from_raw", default=None, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.save_raw and args.from_raw:
        parser.error("--save-raw 和 --from-raw 不能同时用")
    LEDGER["path"], LEDGER["label"] = args.log, args.label
    {"game": cmd_game, "player": cmd_player, "schedule": cmd_schedule, "career": cmd_career}[args.mode](args)


if __name__ == "__main__":
    main()

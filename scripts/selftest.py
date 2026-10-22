"""nba-fact-check 的离线自检：改了规则或脚本之后先跑一遍，确认没有把别的东西改坏。

不联网、不花钱。需要 PyYAML。

检查的内容：
  1. 所有脚本能编译
  2. assets/examples/ 里的每个示例都能通过检查器（0 个错误）
  3. tests/checker_cases.yaml 里的变异用例：每条规则都能被触发、报出预期的错误或提醒
  4. 能离线测的脚本逻辑：中文锚点抽取、全队最高（含并列、含替补）、全部数据项与节次复算
     （用构造的一场两节小比赛，含全场核对没通过的情形）、原始数据存取、说话人两遍对齐与重点句
     匹配（缩写、短句、几种存疑情况）、captions.py / speakers.py 的调用账本（用假的 yt-dlp 和
     OpenRouter 回复）、盲审包不泄露作者判定、复核比对的通过/不通过

用法：
    python selftest.py            # 全部跑，只打印失败项和汇总
    python selftest.py -v         # 打印每一项
    python selftest.py -k L3      # 只跑名字里含 "L3" 的检查器用例
"""

from __future__ import annotations

import argparse
import contextlib
import copy
import importlib.util
import io
import json
import py_compile
import re
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
ROOT = SCRIPTS.parent
EXAMPLES = ROOT / "assets" / "examples"
CASES = ROOT / "tests" / "checker_cases.yaml"

try:
    import yaml
except ImportError:
    sys.exit("自检需要 PyYAML：pip install pyyaml")


def load_module(name: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Results:
    def __init__(self, verbose: bool) -> None:
        self.verbose = verbose
        self.passed = 0
        self.failed: list[str] = []

    def check(self, group: str, name: str, ok: bool, detail: str = "") -> None:
        if ok:
            self.passed += 1
            if self.verbose:
                print(f"  ✓ [{group}] {name}")
        else:
            self.failed.append(f"[{group}] {name}：{detail}")
            print(f"  ✗ [{group}] {name}：{detail}")


# ---------------------------------------------------------------- 路径操作

def parse_path(path: str) -> list:
    tokens = []
    for part in path.split("."):
        name, *indexes = re.split(r"\[(\d+)\]", part)
        if name:
            tokens.append(name)
        tokens += [int(i) for i in indexes if i != ""]
    return tokens


def apply_op(data: dict, op: dict) -> None:
    if "set" in op:
        tokens = parse_path(op["set"])
        target = data
        for token in tokens[:-1]:
            target = target[token]
        target[tokens[-1]] = op.get("value")
    elif "delete" in op:
        tokens = parse_path(op["delete"])
        target = data
        for token in tokens[:-1]:
            target = target[token]
        del target[tokens[-1]]
    else:
        raise ValueError(f"不认识的改动：{op}")


# ---------------------------------------------------------------- 1. 编译

def test_compile(results: Results) -> None:
    for path in sorted(SCRIPTS.glob("*.py")):
        try:
            py_compile.compile(str(path), doraise=True, cfile=str(Path(tempfile.gettempdir()) / f"{path.stem}.selftest.pyc"))
            results.check("编译", path.name, True)
        except py_compile.PyCompileError as err:
            results.check("编译", path.name, False, str(err))


# ---------------------------------------------------------------- 2/3. 检查器

def run_checker(checker, data: dict) -> tuple[list[str], list[str]]:
    report = checker.Report()
    checker.check_core(data, report)
    checker.check_display(data, report)
    return report.errors, report.warnings


def test_examples(results: Results, checker) -> dict:
    bases = {}
    for path in sorted(EXAMPLES.glob("*.yaml")):
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        bases[path.name] = data
        errors, _ = run_checker(checker, data)
        results.check("示例", path.name, not errors, "；".join(errors[:3]))
    return bases


def test_cases(results: Results, checker, bases: dict, keyword: str | None) -> None:
    spec = yaml.safe_load(CASES.read_text(encoding="utf-8"))
    for case in spec.get("cases", []):
        name = case["name"]
        if keyword and keyword not in name:
            continue
        base = bases.get(case["base"])
        if base is None:
            results.check("用例", name, False, f"底板 {case['base']} 不存在")
            continue
        data = copy.deepcopy(base)
        try:
            for op in case.get("ops") or []:
                apply_op(data, op)
        except (KeyError, IndexError, TypeError) as err:
            results.check("用例", name, False, f"改动路径无效（示例结构可能变了）：{err!r}")
            continue
        errors, warnings = run_checker(checker, data)
        problems = []
        if case.get("pass") and errors:
            problems.append("期望 0 个错误，实际：" + "；".join(errors[:2]))
        for want in case.get("errors") or []:
            if not any(want in e for e in errors):
                problems.append(f"没报出期望的错误「{want}」，实际错误：{errors[:2] or '无'}")
        for want in case.get("warnings") or []:
            if not any(want in w for w in warnings):
                problems.append(f"没报出期望的提醒「{want}」，实际提醒：{warnings[:2] or '无'}")
        results.check("用例", name, not problems, "；".join(problems))


# ---------------------------------------------------------------- 4. 其他脚本

def test_zh_anchors(results: Results) -> None:
    z = load_module("zh_anchors")
    a = z.analyse("美媒Basketball Ground分享数据：詹姆斯季前赛出战两场，都在助攻上领先全队。")
    results.check("中文锚点", "模糊称呼后的具名媒体算信源，且不拆成碎片",
                  a["sources"] == ["Basketball Ground"], f"实际 {a['sources']}")
    a = z.analyse("https://bbs.hupu.com/643032927.html")
    results.check("中文锚点", "网址单列，不当信源、不当数字",
                  a["links"] and not a["sources"] and not a["numbers"], f"实际 {a}")
    sentences = z.split_sentences("第一场送出5次助攻全队最高。\n来源：X")
    results.check("中文锚点", "“来源：X”这样的短出处行不被并进上一句",
                  len(sentences) == 2 and z.analyse(sentences[1])["sources"] == ["X"], f"实际 {sentences}")
    a = z.analyse("凯尔特人125-120战胜76人。")
    results.check("中文锚点", "队名里的 76 不当数字", "76" not in a["numbers"], f"实际 {a['numbers']}")
    a = z.analyse("热火随后澄清为社媒团队操作失误，涉事员工遭解雇。")
    results.check("中文锚点", "有信源的句子里仍标出事件断言",
                  "遭解雇" in a["events"] and a["sources"] == ["热火"], f"实际 events={a['events']} sources={a['sources']}")
    a = z.analyse("针对这一消息，詹姆斯商业合作伙伴马弗里克·卡特率先发文澄清。")
    results.check("中文锚点", "去掉头衔取出说话人", a["sources"] == ["马弗里克·卡特"], f"实际 {a['sources']}")
    sentences = z.split_sentences("勒布朗?詹姆斯正式对外辟谣。")
    results.check("中文锚点", "编码损坏的“?”不当成句末", len(sentences) == 1, f"实际 {sentences}")
    sentences = z.split_sentences("而詹姆斯当众指出：“你迟到了！这不行。”随后詹姆斯否认。")
    results.check("中文锚点", "引号里的“！”“。”不断句，闭引号后的新陈述另起一句",
                  sentences == ["而詹姆斯当众指出：“你迟到了！这不行。”", "随后詹姆斯否认。"], f"实际 {sentences}")
    sentences = z.split_sentences("“这是我打过球最棒的球馆之一。”詹姆斯说道。")
    results.check("中文锚点", "闭引号后的归因不断开", len(sentences) == 1, f"实际 {sentences}")
    a = z.analyse("詹姆斯半场出战16分钟，得到6分5篮板3助攻。")
    results.check("中文锚点", "“分钟”“篮板”不被截成“分”“板”",
                  {"16分钟", "5篮板", "3助攻"} <= set(a["numbers"]), f"实际 {a['numbers']}")
    a = z.analyse("卢卡-东契奇接受Sports Illustrated专访，谈到了詹姆斯。")
    results.check("中文锚点", "“接受某媒体专访”认出做采访的媒体", "Sports Illustrated" in a["sources"], f"实际 {a['sources']}")
    a = z.analyse("作者：迈克尔·杜阿尔特")
    results.check("中文锚点", "署名行抽作者名而不是“作者”", a["sources"] == ["迈克尔·杜阿尔特"], f"实际 {a['sources']}")
    a = z.analyse("米切尔：我绝对没想到他会去费城。")
    results.check("中文锚点", "“绝对”标为强断言", "绝对" in a["strong"], f"实际 {a['strong']}")
    # 以下来自 2026-10-20 第三批批量核查
    a = z.analyse("直播吧10月8日讯 76人将于明日7:30客场挑战篮网。")
    results.check("中文锚点", "时刻和日期整体当一个锚点", {"10月8日", "7:30"} <= set(a["numbers"]), f"实际 {a['numbers']}")
    a = z.analyse("赔率从+950, +900一路降到+850.")
    results.check("中文锚点", "数字后面紧跟逗号、句号也照样抽出",
                  {"950", "900", "850"} <= set(a["numbers"]), f"实际 {a['numbers']}")
    a = z.analyse("今日，ESPN发布了对76人后卫VJ·埃奇库姆的采访。")
    results.check("中文锚点", "“某媒体发布了对某人的采访”认出媒体", "ESPN" in a["sources"], f"实际 {a['sources']}")
    a = z.analyse("詹姆斯配文道：“这一刻意味着一切。”")
    results.check("中文锚点", "“配文道”不并进人名", a["sources"] == ["詹姆斯"], f"实际 {a['sources']}")
    a = z.analyse("[流言板]76人内线再遭重创！TA：胡克波尔蒂跟腱撕裂赛季报销")
    results.check("中文锚点", "“TA：”这类缩写信源认得出，伤病断言标为事件",
                  a["sources"] == ["TA"] and "撕裂" in a["events"], f"实际 sources={a['sources']} events={a['events']}")
    a = z.analyse("最关键的一点是：他回来了。")
    results.check("中文锚点", "句中“……的一点是：”不当信源", a["sources"] == [], f"实际 {a['sources']}")
    sentences = z.split_sentences("詹姆斯说：“我只关注每一天。\n“更多的是活在当下。”詹姆斯补充道。\n随后他离开了球馆。")
    results.check("中文锚点", "续段自带开引号时不重复补、引语结束后不再算引语",
                  sentences[1].startswith("“更多") and not sentences[1].startswith("““")
                  and not z.analyse(sentences[2])["quotes"], f"实际 {sentences}")
    a = z.analyse("而詹姆斯当众指出：“你迟到了！”")
    results.check("中文锚点", "说话人前的连词、后的副词不并进名字", a["sources"] == ["詹姆斯"], f"实际 {a['sources']}")
    # 以下来自 2026-10-19 新闻源批量核查暴露的问题
    a = z.analyse("巴登豪森透露，签下詹姆斯后72小时内有15000人支付了季票定金，还以两年老将底薪签下他。")
    results.check("中文锚点", "人数照常是锚点，中文数量词也算", "15000人" in a["numbers"] and "两年" in a["numbers"],
                  f"实际 {a['numbers']}")
    a = z.analyse("据Sportico的库尔特-巴登豪森统计，76人的估值在过去一年上涨27%。")
    results.check("中文锚点", "“某机构的某记者”拆成两个信源",
                  set(a["sources"]) == {"Sportico", "库尔特-巴登豪森"}, f"实际 {a['sources']}")
    a = z.analyse("篮网：杰明11分1助、兰德尔10分2板1助3断")
    results.check("中文锚点", "“球队：球员数据”不当信源", a["sources"] == [], f"实际 {a['sources']}")
    a = z.analyse("76人球员勒布朗-詹姆斯在《球场大脑》节目中表示：他至今仍会想起这件事。")
    results.check("中文锚点", "带连字符的名字、中间插场合也认得出说话人",
                  a["sources"] == ["勒布朗-詹姆斯"], f"实际 {a['sources']}")
    sentences = z.split_sentences("76人半场58-56篮网\n双方数据\n76人：马克西14分")
    results.check("中文锚点", "独占一行的栏目标签不并进标题", sentences[0] == "76人半场58-56篮网", f"实际 {sentences}")
    sentences = z.split_sentences("他表示：\n“第一段原话。\n第二段原话没有引号。\n最后一段原话。”\n来源：X")
    quoted = [s for s in sentences if z.analyse(s)["quotes"]]
    results.check("中文锚点", "跨段引语的中间几段也认作原话", len(quoted) == 3, f"实际 {sentences}")


def test_boxscore(results: Results) -> None:
    b = load_module("nba_boxscore")
    results.check("官方数据", "ISO 时长", abs(b.minutes_played("PT24M42.00S") - 24.7) < 0.01, "")
    results.check("官方数据", "时钟时长", abs(b.minutes_played("18:04") - 18.067) < 0.01, "")
    team = {"teamTricode": "PHI", "players": [
        {"firstName": "A", "familyName": "Starter", "position": "F", "statistics": {"assists": 6, "minutes": "PT30M00.00S"}},
        {"firstName": "B", "familyName": "Bench", "position": "", "statistics": {"assists": 6, "minutes": "12:00"}},
        {"firstName": "C", "familyName": "DNP", "position": "", "statistics": {"assists": 9, "minutes": ""}},
    ]}
    t = b.team_table(team, "assists")
    results.check("官方数据", "全队最高含并列、含替补，未出场的不算",
                  t["leaders"] == ["A Starter", "B Bench"] and t["tie"] and t["max_among_players_who_played"] == 6,
                  f"实际 {t['leaders']} max={t['max_among_players_who_played']}")
    roles = {r["name"]: r["starter"] for r in t["players"]}
    results.check("官方数据", "首发看 position、替补的 position 为空",
                  roles["A Starter"] and not roles["B Bench"], f"实际 {roles}")
    try:
        b.team_table(team, "notAStat")
        results.check("官方数据", "数据项写错要报错", False, "没有报错")
    except ValueError:
        results.check("官方数据", "数据项写错要报错", True)


def test_speakers(results: Results) -> None:
    s = load_module("speakers")
    first = [
        {"start": 0, "end": 3, "speaker": "LeBron James", "text": "I went to Philly"},
        {"start": 3, "end": 4, "speaker": "Steve Nash", "text": "Clickbait."},
        {"start": 4, "end": 6, "speaker": "LeBron James", "text": "he just read off the teleprompter"},
    ]
    # 第二遍的时间戳故意偏得很厉害：对齐必须按文字，不能按时间
    second = [
        {"start": 10, "end": 12, "speaker": "A", "text": "I went to Philly"},
        {"start": 20, "end": 21, "speaker": "B", "text": "Clickbait"},
        {"start": 30, "end": 33, "speaker": "A", "text": "he just read off the teleprompter"},
    ]
    mapping = {"A": "LeBron James", "B": "Steve Nash"}
    labels = s.align_speakers(first, second, mapping)
    results.check("说话人", "两遍按文字对齐，不受时间戳偏差影响",
                  labels == ["LeBron James", "Steve Nash", "LeBron James"], f"实际 {labels}")
    results.check("说话人", "重点句找到说话人",
                  s.who_said("read off the teleprompter", second, mapping) == "LeBron James", "")
    cleaned = s.clean([{"start": 1, "end": 2, "speaker": "A", "text": "x"},
                       {"start": 1, "end": 2, "speaker": "A", "text": "x"},
                       {"start": 99, "end": 100, "speaker": "A", "text": "y"}], length=30)
    results.check("说话人", "去掉重复轮次和超出音频长度的轮次", len(cleaned) == 1, f"实际 {cleaned}")


# ---------------------------------------------------------------- 4b. 节次复算（构造的小比赛）

def tiny_game(b) -> dict:
    """构造一场两节的小比赛（pageProps 的形状）：每队 5 名首发 + 替补，官方全场表按手算填好。

    要点：Dëmin 在描述里写成 Demin（去重音）；BBB 的 Nelson Jr. 在换人描述里写成 Nelson（去后缀）；
    第一节两次罚球中间换人，页面里换人排在罚中的第二罚前面——官方正负值把同一停表时刻的罚球
    算给换人之前的阵容（Fiver 吃这 1 分，换上来的 Sixer 不吃）；全队篮板、全队失误的 personId
    是球队 ID，不记给任何球员。正负值手算：AAA 第一节 +3−1、第二节 +2+1−2。
    """
    def player(pid, first, family, position, minutes, comment="", **stats):
        full = {key: 0 for key in b.STAT_KEYS}
        full.update(stats)
        full["minutes"] = minutes
        return {"personId": pid, "firstName": first, "familyName": family, "nameI": f"{first[0]}. {family}",
                "position": position, "comment": comment, "statistics": full}

    home = {"teamId": 1, "teamTricode": "AAA", "score": 6,
            "periods": [{"period": 1, "score": 3}, {"period": 2, "score": 3}],
            "players": [
                player(11, "Egor", "Dëmin", "G", "24:00", points=3, fieldGoalsMade=1, fieldGoalsAttempted=1,
                       threePointersMade=1, threePointersAttempted=1, assists=1, plusMinusPoints=3),
                player(12, "Al", "Twoey", "G", "24:00", assists=1, fieldGoalsAttempted=1, threePointersAttempted=1,
                       plusMinusPoints=3),
                player(13, "Al", "Threet", "C", "24:00", blocks=1, points=1, freeThrowsMade=1, freeThrowsAttempted=1,
                       plusMinusPoints=3),
                player(14, "Al", "Fourman", "F", "24:00", reboundsTotal=1, plusMinusPoints=3),
                player(15, "Al", "Fiver", "F", "6:00", turnovers=1, plusMinusPoints=2),
                player(16, "Al", "Sixer", "", "18:00", points=2, fieldGoalsMade=1, fieldGoalsAttempted=1,
                       plusMinusPoints=1),
                player(17, "Al", "Benchy", "", "", comment="DNP - Coach's Decision"),
            ]}
    away = {"teamId": 2, "teamTricode": "BBB", "score": 3,
            "periods": [{"period": 1, "score": 1}, {"period": 2, "score": 2}],
            "players": [
                player(21, "Bo", "Bone", "G", "21:00", fieldGoalsAttempted=1, reboundsTotal=1, plusMinusPoints=-3),
                player(22, "Bo", "Btwo", "G", "24:00", steals=1, plusMinusPoints=-3),
                player(23, "Bo", "Bthree", "C", "24:00", points=3, freeThrowsMade=1, freeThrowsAttempted=2,
                       fieldGoalsMade=1, fieldGoalsAttempted=1, plusMinusPoints=-3),
                player(24, "Bo", "Bfour", "F", "24:00", turnovers=1, plusMinusPoints=-3),
                player(25, "Bo", "Bfive", "F", "24:00", reboundsTotal=2, plusMinusPoints=-3),
                player(26, "Grant", "Nelson Jr.", "", "3:00", steals=1, plusMinusPoints=0),
            ]}

    def act(period, clock, kind, desc, pid=0, team="", value=0):
        return {"period": period, "clock": f"PT{clock}S", "actionType": kind, "subType": "", "description": desc,
                "personId": pid, "teamTricode": team, "shotValue": value}

    actions = [
        act(1, "12M00.00", "period", "Start of 1st Period"),
        act(1, "11M30.00", "Made Shot", "Demin 25' 3PT Jump Shot (3 PTS) (Twoey 1 AST)", 11, "AAA", 3),
        act(1, "11M00.00", "Missed Shot", "MISS Bone 10' Jump Shot", 21, "BBB", 2),
        act(1, "11M00.00", "", "Threet BLOCK (1 BLK)", 13, "AAA"),
        act(1, "10M58.00", "Rebound", "Fourman REBOUND (Off:0 Def:1)", 14, "AAA"),
        act(1, "10M00.00", "Turnover", "Fiver Bad Pass Turnover (P1.T1)", 15, "AAA"),
        act(1, "10M00.00", "", "Btwo STEAL (1 STL)", 22, "BBB"),
        act(1, "09M00.00", "Foul", "Bfour P.FOUL (P1.T1)", 24, "BBB"),
        act(1, "06M00.00", "Free Throw", "MISS Bthree Free Throw 1 of 2", 23, "BBB"),
        act(1, "06M00.00", "Substitution", "SUB: Sixer FOR Fiver", 15, "AAA"),
        act(1, "06M00.00", "Free Throw", "Bthree Free Throw 2 of 2 (1 PTS)", 23, "BBB"),
        act(1, "05M59.00", "Rebound", "Bfive REBOUND (Off:0 Def:1)", 25, "BBB"),
        act(1, "03M00.00", "Rebound", "AAA Rebound", 1, "AAA"),
        act(1, "02M00.00", "Turnover", "AAA Turnover: Shot Clock (T#1)", 1, "AAA"),
        act(1, "00M00.00", "period", "End of 1st Period"),
        act(2, "12M00.00", "period", "Start of 2nd Period"),
        act(2, "11M00.00", "Made Shot", "Sixer 2' Layup (2 PTS) (Demin 1 AST)", 16, "AAA", 2),
        act(2, "10M00.00", "Missed Shot", "MISS Twoey 26' 3PT Jump Shot", 12, "AAA", 3),
        act(2, "09M50.00", "Rebound", "Bone REBOUND (Off:0 Def:1)", 21, "BBB"),
        act(2, "09M00.00", "Foul", "Fourman P.FOUL (P1.T1)", 14, "AAA"),
        act(2, "08M00.00", "Free Throw", "Threet Free Throw 1 of 1 (1 PTS)", 13, "AAA"),
        act(2, "07M00.00", "Foul", "Btwo P.FOUL (P1.T1)", 22, "BBB"),
        act(2, "06M00.00", "Made Shot", "Bthree 5' Jump Shot (3 PTS)", 23, "BBB", 2),
        act(2, "05M00.00", "Turnover", "Bfour Lost Ball Turnover (P1.T1)", 24, "BBB"),
        act(2, "04M00.00", "Rebound", "Bfive REBOUND (Off:0 Def:2)", 25, "BBB"),
        act(2, "03M00.00", "Substitution", "SUB: Nelson FOR Bone", 21, "BBB"),
        act(2, "02M00.00", "", "Nelson Jr. STEAL (1 STL)", 26, "BBB"),
        act(2, "00M00.00", "period", "End of 2nd Period"),
    ]
    game = {"gameId": "0019900001", "gameTimeUTC": "2026-10-09T00:00:00Z", "gameStatusText": "Final", "period": 2,
            "homeTeam": home, "awayTeam": away}
    return {"game": game, "playByPlay": {"actions": actions}}


def row_of(table: dict, name: str) -> dict:
    return next(r for r in table["players"] if r["name"] == name)


def test_boxscore_periods(results: Results) -> None:
    b = load_module("nba_boxscore")
    props = tiny_game(b)
    game, actions = props["game"], props["playByPlay"]["actions"]

    counted = b.recount(game, actions)
    check = b.check_full_game(game, counted, [1, 2])
    results.check("节次复算", "全场复算与官方全场表逐人逐项一致（含去重音、去后缀的名字，全队篮板/失误不记给人）",
                  check["counting_ok"] and not check["differences"] and not check["unresolved_names"],
                  f"差异 {check['differences']} 对不回 {check['unresolved_names']}")
    results.check("节次复算", "按换人事件复算的全场时长与官方一致（罚球中间换人也对）",
                  check["minutes_ok"], f"{check['minutes_differences']} {check['lineup_warnings']}")

    home, away = game["homeTeam"], game["awayTeam"]
    q1 = b.recount_table(home, counted, [1], "points")
    demin, sixer, fiver = row_of(q1, "Egor Dëmin"), row_of(q1, "Al Sixer"), row_of(q1, "Al Fiver")
    results.check("节次复算", "单节：得分、三分、助攻、盖帽只算这一节",
                  demin["points"] == 3 and demin["threePointersMade"] == 1 and demin["assists"] == 0
                  and row_of(q1, "Al Twoey")["assists"] == 1 and row_of(q1, "Al Threet")["blocks"] == 1,
                  f"实际 Dëmin={demin}")
    results.check("节次复算", "单节时长：换下的 6 分钟、换上的 6 分钟",
                  fiver["minutes"] == 6.0 and sixer["minutes"] == 6.0, f"实际 Fiver={fiver['minutes']} Sixer={sixer['minutes']}")
    q2 = b.recount_table(away, counted, [2], "steals")
    nelson = row_of(q2, "Grant Nelson Jr.")
    results.check("节次复算", "换人描述里没写 Jr. 也能对回人，替补的抢断和时长算对",
                  nelson["steals"] == 1 and nelson["minutes"] == 3.0
                  and q2["leaders"] == ["Grant Nelson Jr."] and not q2["tie"],
                  f"实际 {nelson} leaders={q2['leaders']}")
    q1_away = b.recount_table(away, counted, [1], "points")
    results.check("节次复算", "罚球按 MISS 区分，得分 1、罚球 1/2",
                  row_of(q1_away, "Bo Bthree")["points"] == 1 and row_of(q1_away, "Bo Bthree")["freeThrowsAttempted"] == 2,
                  f"实际 {row_of(q1_away, 'Bo Bthree')}")
    results.check("节次复算", "节次比分与官方分节比分对照", check["period_score"]["AAA"] == {"recount": 6, "official_linescore": 6},
                  f"实际 {check['period_score']}")

    bad = copy.deepcopy(game)
    next(p for p in bad["awayTeam"]["players"] if p["personId"] == 22)["statistics"]["steals"] = 2
    check = b.check_full_game(bad, b.recount(bad, actions), [1])
    results.check("节次复算", "全场复算与官方表对不上时不可信，并指出是谁的哪一项",
                  not check["counting_ok"] and not check["minutes_ok"]
                  and check["differences"] == [{"player": "Bo Btwo", "stat": "steals", "recount": 1, "official": 2}],
                  f"实际 {check['differences']}")
    bad = copy.deepcopy(game)
    bad["homeTeam"]["periods"][0]["score"] = 5
    check = b.check_full_game(bad, b.recount(bad, actions), [1])
    results.check("节次复算", "所选节次的复算比分和官方分节比分对不上时不可信",
                  not check["counting_ok"] and check["period_score"]["AAA"] == {"recount": 3, "official_linescore": 5},
                  f"实际 {check['period_score']}")
    dropped = [a for a in actions if a["description"] != "SUB: Sixer FOR Fiver"]
    check = b.check_full_game(game, b.recount(game, dropped), [1, 2])
    results.check("节次复算", "漏了换人事件时时长核对不通过（计数仍可用）",
                  check["counting_ok"] and not check["minutes_ok"], f"实际 {check['minutes_differences']}")
    results.check("正负值", "漏了换人事件时正负值也不能用", not check["plus_minus_ok"], f"实际 {check}")

    # 正负值：在场推断 + 得分时间线，先和全场官方正负值逐人对上
    check = b.check_full_game(game, counted, [1, 2])
    results.check("正负值", "全场复算正负值与官方逐人一致", check["plus_minus_ok"] and not check["plus_minus_differences"],
                  f"实际 {check['plus_minus_differences']}")
    q1 = b.recount_table(home, counted, [1], b.PLUS_MINUS)
    results.check("正负值", "同一停表时刻的罚球算给换人之前的阵容（页面里换人排在前面也一样）",
                  row_of(q1, "Al Fiver")[b.PLUS_MINUS] == 2 and row_of(q1, "Al Sixer")[b.PLUS_MINUS] == 0,
                  f"实际 Fiver={row_of(q1, 'Al Fiver')[b.PLUS_MINUS]} Sixer={row_of(q1, 'Al Sixer')[b.PLUS_MINUS]}")
    q2_away = b.recount_table(away, counted, [2], b.PLUS_MINUS)
    results.check("正负值", "单节正负值：被换下的只算在场那段，换上的从换上起算",
                  row_of(q2_away, "Bo Bone")[b.PLUS_MINUS] == -1 and row_of(q2_away, "Grant Nelson Jr.")[b.PLUS_MINUS] == 0
                  and q2_away["leaders"] == ["Grant Nelson Jr."], f"实际 {q2_away['leaders']}")
    order = [a["description"] for a in b.chronological([a for a in actions if a["period"] == 1])
             if a["clock"] == "PT06M00.00S"]
    results.check("正负值", "同一时刻先排得分等事件、后排换人",
                  order[-1] == "SUB: Sixer FOR Fiver", f"实际 {order}")
    bad = copy.deepcopy(game)
    next(p for p in bad["awayTeam"]["players"] if p["personId"] == 22)["statistics"][b.PLUS_MINUS] = -2
    check = b.check_full_game(bad, b.recount(bad, actions), [1])
    results.check("正负值", "正负值对不上时只判正负值不能用，计数和时长照常可用",
                  check["counting_ok"] and check["minutes_ok"] and not check["plus_minus_ok"]
                  and check["plus_minus_differences"] == [{"player": "Bo Btwo", "recount": -3, "official": -2}],
                  f"实际 {check['plus_minus_differences']}")
    bare = copy.deepcopy(game)
    for side in ("homeTeam", "awayTeam"):
        for p in bare[side]["players"]:
            p["statistics"].pop(b.PLUS_MINUS, None)
    check = b.check_full_game(bare, b.recount(bare, actions), [1])
    results.check("正负值", "官方表没有正负值时无法核对，不能用",
                  not check["plus_minus_checked"] and not check["plus_minus_ok"], f"实际 {check}")
    t = b.team_table(home, "points", b.STAT_KEYS + [b.PLUS_MINUS])
    results.check("正负值", "全场 --all-stats 的各项最高带上正负值（含并列）",
                  t["leaders_by_stat"][b.PLUS_MINUS] == {"max": 3, "leaders": ["Egor Dëmin", "Al Twoey", "Al Threet",
                                                                                "Al Fourman"]},
                  f"实际 {t['leaders_by_stat'].get(b.PLUS_MINUS)}")
    results.check("正负值", "正负值显示带正负号", b.show(b.PLUS_MINUS, 10) == "+10" and b.show(b.PLUS_MINUS, -3) == "-3"
                  and b.show(b.PLUS_MINUS, 0) == "0" and b.show(b.PLUS_MINUS, None) == "-", "")

    results.check("节次复算", "节次写法", b.parse_periods("1-2") == [1, 2] and b.parse_periods("1,3") == [1, 3]
                  and b.parse_periods("2") == [2], "")
    for wrong in ("2-1", "OT", "0"):
        try:
            b.parse_periods(wrong)
            results.check("节次复算", f"节次写错要报错（{wrong}）", False, "没有报错")
        except ValueError:
            results.check("节次复算", f"节次写错要报错（{wrong}）", True)

    t = b.team_table(away, "points", b.STAT_KEYS)
    best = t["leaders_by_stat"]
    results.check("官方数据", "--all-stats：各项最高（篮板、抢断含并列）",
                  best["reboundsTotal"] == {"max": 2, "leaders": ["Bo Bfive"]}
                  and set(best["steals"]["leaders"]) == {"Bo Btwo", "Grant Nelson Jr."}
                  and all(k in t["players"][0] for k in b.STAT_KEYS), f"实际 {best}")

    with tempfile.TemporaryDirectory() as tmp:
        raw = Path(tmp) / "raw" / "game.json"
        with contextlib.redirect_stderr(io.StringIO()):
            b.save_raw(str(raw), "https://www.nba.com/game/0019900001/box-score", props)
        loaded = b.load_page(argparse.Namespace(from_raw=str(raw), save_raw=None), "不会联网")
        results.check("官方数据", "--save-raw 存下的原始数据能原样读回", loaded == props, "读回的内容不一致")

        done = run_script("nba_boxscore", "game", "0019900001", "--periods", "1", "--team", "AAA", "--json",
                          "--from-raw", str(raw))
        try:
            data = json.loads(done.stdout)
        except json.JSONDecodeError:
            data = {}
        results.check("官方数据", "命令行 --periods --from-raw 离线跑通（退出码 0）",
                      done.returncode == 0 and data.get("scope", {}).get("periods") == [1]
                      and data["teams"][0]["leaders"] == ["Egor Dëmin"],
                      f"退出码 {done.returncode} {done.stderr[-300:]}")
        done = run_script("nba_boxscore", "game", "0019900001", "--all-stats", "--from-raw", str(raw))
        results.check("官方数据", "命令行 --all-stats 列出各项最高和投篮、三分、罚球",
                      done.returncode == 0 and "各项最高" in done.stdout and "1/2" in done.stdout,
                      f"退出码 {done.returncode} {done.stderr[-300:]}")
        done = run_script("nba_boxscore", "game", "0019900001", "--periods", "1-2", "--team", "BBB",
                          "--stat", "plusMinusPoints", "--from-raw", str(raw))
        results.check("正负值", "命令行 --periods --stat plusMinusPoints：先报正负值核对，再列带符号的正负值",
                      done.returncode == 0 and "正负值核对：全场正负值逐人与官方一致" in done.stdout
                      and "正负值 最高 0：Grant Nelson Jr." in done.stdout and "plusMinusPoints=-3" in done.stdout,
                      f"退出码 {done.returncode} {done.stdout[-500:]} {done.stderr[-200:]}")

        bad_props = copy.deepcopy(props)
        next(p for p in bad_props["game"]["awayTeam"]["players"] if p["personId"] == 22)["statistics"]["steals"] = 2
        bad_raw = Path(tmp) / "bad.json"
        bad_raw.write_text(json.dumps(bad_props, ensure_ascii=False), encoding="utf-8")  # 不带外层，也要认得
        done = run_script("nba_boxscore", "game", "0019900001", "--periods", "1-2", "--from-raw", str(bad_raw))
        results.check("官方数据", "全场核对没通过时照样输出，但退出码是 3",
                      done.returncode == 3 and "核对没通过" in done.stdout and "Bo Btwo steals" in done.stdout,
                      f"退出码 {done.returncode} {done.stdout[-300:]}")
        done = run_script("nba_boxscore", "game", "x", "--save-raw", str(raw), "--from-raw", str(raw))
        results.check("官方数据", "--save-raw 和 --from-raw 不能同时用", done.returncode == 2, f"退出码 {done.returncode}")


# ---------------------------------------------------------------- 4c. 说话人重点句、账本

@contextlib.contextmanager
def patched(owner, name: str, value):
    old = getattr(owner, name)
    setattr(owner, name, value)
    try:
        yield
    finally:
        setattr(owner, name, old)


def read_ledger(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def ledger_ok(entries: list[dict], mode: str, label: str, secret: str = "") -> bool:
    """账本行要有 search.py 的那几个字段，标签对，不含 key。"""
    needed = {"at", "label", "mode", "status", "attempts", "seconds"}
    return bool(entries) and all(
        needed <= set(e) and e["mode"] == mode and e["label"] == label
        and (not secret or secret not in json.dumps(e, ensure_ascii=False)) for e in entries
    )


class FakeResponse:
    def __init__(self, payload: dict, status: int = 200) -> None:
        self.status = status
        self._body = json.dumps(payload).encode("utf-8")

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> bool:
        return False


def test_speakers_lines(results: Results) -> None:
    s = load_module("speakers")
    results.check("说话人", "缩写展开后相同（didn't = did not，he's = he is，gonna = going to）",
                  s.words_of("I didn't say that") == s.words_of("I did not say that")
                  and s.words_of("He's gonna win") == s.words_of("he is going to win")
                  and s.words_of("I can’t") == s.words_of("I cannot"), f"实际 {s.words_of('I didn’t')}")
    turns = [{"start": 1, "speaker": "A", "text": "Yes, and I think so"}, {"start": 3, "speaker": "B", "text": "Yes."}]
    results.check("说话人", "单个词能匹配，整轮就是这句的优先",
                  s.who_said("Yes", turns, {}) == "B", f"实际 {s.who_said('Yes', turns, {})}")
    results.check("说话人", "短句写全也能匹配字幕里的缩写",
                  s.who_said("did not", [{"start": 0, "speaker": "A", "text": "I didn't do it"}], {}) == "A", "")
    results.check("说话人", "单个词不会误配到别的词里",
                  s.who_said("no", [{"start": 0, "speaker": "A", "text": "I know nothing"}], {}) == "文字没匹配上", "")
    results.check("说话人", "模型回复被截断时丢掉最后一个不完整的对象",
                  len(s.parse_turns('[{"start": 0, "speaker": "A", "text": "x"}, {"start": 2, "speaker": "B", "te')) == 1, "")

    named = [
        {"start": 0, "end": 3, "speaker": "LeBron James", "text": "I went to Philly"},
        {"start": 3, "end": 4, "speaker": "Steve Nash", "text": "Yes."},
        {"start": 4, "end": 8, "speaker": "LeBron James", "text": "I didn't read off the teleprompter"},
        {"start": 8, "end": 11, "speaker": "Steve Nash", "text": "That wasn't good at all"},
        {"start": 11, "end": 12, "speaker": "LeBron James", "text": "Hmm okay then"},
    ]
    neutral = [
        {"start": 0, "end": 3, "speaker": "A", "text": "I went to Philly"},
        {"start": 3, "end": 4, "speaker": "B", "text": "Yes."},
        {"start": 4, "end": 8, "speaker": "A", "text": "I did not read off the teleprompter"},
        {"start": 8, "end": 11, "speaker": "A", "text": "That was not good at all"},
        {"start": 11, "end": 12, "speaker": "B", "text": "Something completely different"},
    ]
    mapping = {"A": "LeBron James", "B": "Steve Nash"}
    verdicts = {line: s.judge_line(line, named, neutral, mapping)["verdict"]
                for line in ("Yes", "did not read off", "wasn't good", "Hmm okay then", "nobody said this")}
    results.check("说话人", "重点句：单个词、缩写两遍一致", verdicts["Yes"] == "一致" and verdicts["did not read off"] == "一致",
                  f"实际 {verdicts}")
    results.check("说话人", "重点句：文字对上了但两遍说话人不同，单列一种",
                  verdicts["wasn't good"] == "两遍说话人不一致", f"实际 {verdicts}")
    results.check("说话人", "重点句：文字没匹配上单列一种，并指出是哪一遍",
                  verdicts["Hmm okay then"] == verdicts["nobody said this"] == "文字没匹配上"
                  and "第二遍" in s.judge_line("Hmm okay then", named, neutral, mapping)["detail"]
                  and "两遍都" in s.judge_line("nobody said this", named, neutral, mapping)["detail"], f"实际 {verdicts}")

    run = {"source": "clip.mp3", "start": 60, "end": 72, "ref_start": 60, "ref_end": 63, "ref_name": "LeBron James",
           "ref_text": "I went to Philly", "other": "Steve Nash", "lines": ["Yes"], "model": "m"}
    report = s.render(run, named, neutral, ["Yes", "wasn't good"])
    results.check("说话人", "报告输出中立那一遍的原文和 A/B 对应的人",
                  "第二遍（中立）原文" in report and "Something completely different" in report and "B=Steve Nash" in report,
                  report[-400:])
    yes_row = next((l for l in report.splitlines() if l.rstrip().endswith("Yes.") and "≈" not in l), "")
    results.check("说话人", "摘要和逐轮对照不再互相矛盾（单个词两遍一致，逐轮也打 ✓）",
                  "→ 一致" in report and "✓" in yes_row, f"逐轮行 {yes_row!r}")

    with tempfile.TemporaryDirectory() as tmp:
        raw = Path(tmp) / "speakers.json"
        passes = {"named": {"prompt": "p1", "content": "[]", "turns": named},
                  "neutral": {"prompt": "p2", "content": "[]", "turns": neutral}}
        s.save_raw(str(raw), run, passes)
        done = run_script("speakers", "--from-raw", str(raw), "--lines", "Yes", "wasn't good")
        results.check("说话人", "--save-raw 存两遍，--from-raw 离线重出报告（可换重点句）",
                      done.returncode == 0 and "两遍说话人不一致" in done.stdout and "Something completely different" in done.stdout,
                      f"退出码 {done.returncode} {done.stderr[-300:]}")
        done = run_script("speakers", "--from", "1")
        results.check("说话人", "不用 --from-raw 时缺参数要报错", done.returncode == 2, f"退出码 {done.returncode}")


def test_ledgers(results: Results) -> None:
    s = load_module("speakers")
    c = load_module("captions")
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)

        # captions.py：下载字幕
        log = tmp / "captions.jsonl"
        c.LEDGER.update(path=str(log), label="C01")

        def fake_download(command, **kwargs):
            out = Path(command[command.index("-o") + 1]).parent
            (out / "vid.en-orig.json3").write_text('{"events": []}', encoding="utf-8")
            return subprocess.CompletedProcess(command, 0, "", "")

        with patched(subprocess, "run", fake_download):
            got = c.download_json3("https://youtu.be/x", tmp, "yt-dlp", [])
        entries = read_ledger(log)
        results.check("账本", "captions.py 下载字幕记一行（ok、attempts、seconds、label）",
                      got.name == "vid.en-orig.json3" and ledger_ok(entries, "captions_download", "C01")
                      and entries[0]["status"] == "ok" and entries[0]["attempts"] == 1, f"实际 {entries}")

        empty = tmp / "empty"
        empty.mkdir()
        with patched(subprocess, "run", lambda command, **kw: subprocess.CompletedProcess(command, 1, "", "HTTP 429")):
            try:
                c.download_json3("https://youtu.be/y", empty, "yt-dlp", [])
                exited = False
            except SystemExit:
                exited = True
        entries = read_ledger(log)
        results.check("账本", "captions.py 下载失败也记账（status=error，带 returncode）",
                      exited and entries[-1]["status"] == "error" and entries[-1]["returncode"] == 1, f"实际 {entries[-1:]}")

        found = "abc123\t20261001\t10:00\tMind the Game\tEpisode 1\n"
        args = argparse.Namespace(ytdlp="yt-dlp", count=3, find="Mind the Game", proxy=None, ytdlp_args="")
        with patched(subprocess, "run", lambda command, **kw: subprocess.CompletedProcess(command, 0, found, "")), \
                contextlib.redirect_stdout(io.StringIO()):
            c.find_videos(args)
        entries = read_ledger(log)
        results.check("账本", "captions.py 搜视频记一行（查询、结果数）",
                      ledger_ok(entries[-1:], "captions_find", "C01") and entries[-1]["results"] == 1
                      and entries[-1]["query"] == "Mind the Game", f"实际 {entries[-1:]}")

        args.ytdlp = str(tmp / "no-such-yt-dlp")
        try:
            c.find_videos(args)
            exited = False
        except SystemExit:
            exited = True
        entries = read_ledger(log)
        results.check("账本", "找不到 yt-dlp 时也记一行 error", exited and entries[-1]["status"] == "error", f"实际 {entries[-1:]}")

        # speakers.py：下载音频、两遍模型调用
        log = tmp / "speakers.jsonl"
        s.LEDGER.update(path=str(log), label="S01")
        s.RETRY_WAIT = 0

        single = {"formats": [{"format_id": "139", "ext": "m4a", "abr": 48.8, "vcodec": "none", "acodec": "mp4a.40.5",
                               "language": "en", "format_note": "low"}]}

        def fake_audio(command, **kwargs):
            if "-J" in command:  # 先列格式
                return subprocess.CompletedProcess(command, 0, json.dumps(single), "")
            Path(command[command.index("-o") + 1].replace("%(ext)s", "m4a")).write_bytes(b"audio")
            return subprocess.CompletedProcess(command, 0, "", "")

        work = tmp / "work"
        work.mkdir()
        with patched(subprocess, "run", fake_audio), contextlib.redirect_stderr(io.StringIO()):
            audio = s.fetch_audio("https://youtu.be/z", work, "yt-dlp", [])
        entries = read_ledger(log)
        results.check("账本", "speakers.py 列格式、下载音频各记一行", audio.name == "audio.m4a"
                      and ledger_ok(entries[:1], "speakers_formats", "S01")
                      and ledger_ok(entries[1:2], "speakers_download", "S01") and entries[1]["bytes"] == 5
                      and entries[1]["format"] == "139", f"实际 {entries}")
        log.unlink()

        secret = "sk-or-selftest-secret"
        reply = {"choices": [{"message": {"content": '[{"start": 0, "end": 1, "speaker": "A", "text": "Yes."}]'}}],
                 "usage": {"prompt_tokens": 10, "completion_tokens": 5}}
        calls = []

        def flaky(request, timeout=None):
            calls.append(request)
            if len(calls) == 1:
                raise urllib.error.URLError("连接被重置")
            return FakeResponse(reply)

        with patched(urllib.request, "urlopen", flaky), contextlib.redirect_stderr(io.StringIO()):
            turns, record = s.ask(secret, "m", "prompt", "QUFB", "neutral")
        entries = read_ledger(log)
        last = entries[-1]
        results.check("账本", "speakers.py 模型调用记一行（重试计入 attempts，带 pass、用量，不含 key）",
                      ledger_ok(entries[-1:], "speakers_model", "S01", secret) and last["attempts"] == 2
                      and last["status"] == 200 and last["pass"] == "neutral" and last["turns"] == 1
                      and last["usage"]["prompt_tokens"] == 10, f"实际 {last}")
        results.check("说话人", "原始记录保留提示词和完整回复，不含 key",
                      record["response"] == reply and record["prompt"] == "prompt" and turns[0]["text"] == "Yes."
                      and secret not in json.dumps(record, ensure_ascii=False), f"实际 {record}")

        def unauthorized(request, timeout=None):
            raise urllib.error.HTTPError(request.full_url, 401, "Unauthorized", None, io.BytesIO(b'{"error": "bad key"}'))

        with patched(urllib.request, "urlopen", unauthorized):
            try:
                s.ask(secret, "m", "prompt", "QUFB", "named")
                exited = False
            except SystemExit:
                exited = True
        last = read_ledger(log)[-1]
        results.check("账本", "401 不重试，失败也记账", exited and last["status"] == 401 and last["attempts"] == 1,
                      f"实际 {last}")
        s.LEDGER.update(path=None, label="")
        c.LEDGER.update(path=None, label="")


# ---------------------------------------------------------------- 4d. 第二批反馈：字幕来源、音轨、生涯数据

SRT_SAMPLE = """1
00:00:00.770 --> 00:00:01.790
How are we doing?

2
00:02:57,000 --> 00:02:58,000
I mean, the future

3
00:03:02.200 --> 00:03:03.100
I didn't think Philly.
"""

VTT_SAMPLE = """﻿WEBVTT
Kind: captions
Language: en

NOTE 这一块不是字幕

intro
00:00.500 --> 00:02.000 align:start position:0%
Hello <c>there</c> &amp; welcome

00:01:05.000 --> 00:01:07.500
<00:01:05.200><c>the</c><00:01:05.600><c> future</c> is bright
"""


def test_captions_sources(results: Results) -> None:
    c = load_module("captions")
    w = c.windows([177.0], 200)
    results.check("字幕", "开头附近的命中不再被跳过（窗口起点截到 0）", w == [(0.0, 377.0, [177.0])], f"实际 {w}")
    w = c.windows([30.0, 10.0, 100.0], 15)
    results.check("字幕", "窗口重叠的命中合并成一段，后面的命中不漏", w == [(0.0, 45.0, [10.0, 30.0]), (85.0, 115.0, [100.0])],
                  f"实际 {w}")

    srt = c.parse_cues(SRT_SAMPLE)
    results.check("字幕", "SRT：毫秒用逗号、用点号都认", [round(t, 2) for t, _ in srt] == [0.77, 177.0, 182.2]
                  and srt[2][1] == "I didn't think Philly.", f"实际 {srt}")
    vtt = c.parse_cues(VTT_SAMPLE)
    results.check("字幕", "VTT：跳过头和 NOTE、去掉标签和行内时间戳、还原 &amp;",
                  vtt == [(0.5, "Hello there & welcome"), (65.0, "the future is bright")], f"实际 {vtt}")

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        (tmp / "cc.srt").write_text(SRT_SAMPLE, encoding="utf-8")
        (tmp / "cc.txt").write_text(VTT_SAMPLE, encoding="utf-8")
        results.check("字幕", "按扩展名识别格式，扩展名不认识时看文件头",
                      c.caption_kind(tmp / "cc.srt") == "srt" and c.caption_kind(tmp / "cc.txt") == "vtt", "")
        done = run_script("captions", str(tmp / "cc.srt"), "future", "--window", "200")
        results.check("字幕", "命令行：本地 SRT、命中在开头附近、窗口很大时照样输出",
                      done.returncode == 0 and "2:57.0  I mean, the future  <==" in done.stdout,
                      f"退出码 {done.returncode} 输出 {done.stdout[-300:]!r} {done.stderr[-200:]}")
        done = run_script("captions", "--vtt", str(tmp / "cc.txt"), "future")
        results.check("字幕", "命令行：--vtt 指定格式", done.returncode == 0 and "the future is bright  <==" in done.stdout,
                      f"退出码 {done.returncode} {done.stderr[-200:]}")

        # NBA.com 球队官网视频页：页面数据 → 字幕地址 → 下载字幕，存下标题和发布时间
        props = {"pageObject": {"title": "Media Day | Donovan Mitchell", "date": "2026-09-28T17:17:03Z",
                                "permalink": "https://www.nba.com/cavaliers/videos/media-day",
                                "videoAssets": {"cc": {"vtt": "https://cdn.nba.com/x/abc_eng.vtt",
                                                       "srt": "https://cdn.nba.com/x/abc_eng.srt"}}}}
        results.check("字幕", "球队官网页面数据里找字幕地址，SRT 优先",
                      c.caption_links(props)[0] == "https://cdn.nba.com/x/abc_eng.srt", f"实际 {c.caption_links(props)}")
        page = ('<html><script id="__NEXT_DATA__" type="application/json">'
                + json.dumps({"props": {"pageProps": props}}) + "</script></html>")
        served = {"https://www.nba.com/cavaliers/videos/media-day": page.encode("utf-8"),
                  "https://cdn.nba.com/x/abc_eng.srt": SRT_SAMPLE.encode("utf-8")}

        def fake_urlopen(request, timeout=None):
            return FakeBytes(served[request.full_url])

        log = tmp / "captions.jsonl"
        c.LEDGER.update(path=str(log), label="C02")
        out = tmp / "team"
        out.mkdir()
        with patched(urllib.request, "urlopen", fake_urlopen):
            path = c.fetch_nba_page_captions("https://www.nba.com/cavaliers/videos/media-day", out)
        meta = c.read_meta(path)
        entries = read_ledger(log)
        results.check("字幕", "球队官网视频页：下载字幕、存下标题和发布方，页面和字幕各记一行账",
                      path.name == "abc_eng.srt" and meta.get("publisher") == "NBA.com cavaliers 官网"
                      and [e["mode"] for e in entries] == ["captions_page", "captions_download"]
                      and all(e["status"] == 200 for e in entries), f"实际 meta={meta} 账本={entries}")
        shown = c.describe_meta(meta)
        results.check("字幕", "发布时间换算成北京时间", any("北京时间 2026-09-29 01:17" in line for line in shown), f"实际 {shown}")

        # YouTube：下载字幕时顺带写出的 info.json → 频道名、频道 ID、上传时间
        info = {"id": "vid", "title": "Season Preview", "channel": "Mind the Game", "channel_id": "UC6L",
                "uploader_id": "@MindTheGamePodcast", "upload_date": "20261013", "timestamp": 1792242000,
                "webpage_url": "https://www.youtube.com/watch?v=vid"}

        def fake_download(command, **kwargs):
            folder = Path(command[command.index("-o") + 1]).parent
            (folder / "vid.en-orig.json3").write_text('{"events": [{"tStartMs": 1000, "segs": [{"utf8": "hi"}]}]}',
                                                      encoding="utf-8")
            (folder / "vid.info.json").write_text(json.dumps(info), encoding="utf-8")
            return subprocess.CompletedProcess(command, 0, "", "")

        yt = tmp / "yt"
        yt.mkdir()
        with patched(subprocess, "run", fake_download):
            path = c.download_json3("https://www.youtube.com/watch?v=vid", yt, "yt-dlp", [])
        meta = c.read_meta(path)
        last = read_ledger(log)[-1]
        results.check("字幕", "YouTube 字幕下载顺带存下频道名、频道 ID、上传时间，账本里也有",
                      meta.get("channel") == "Mind the Game" and meta.get("channel_id") == "UC6L"
                      and meta.get("upload_date") == "2026-10-13" and last.get("channel_id") == "UC6L"
                      and last.get("upload_date") == "2026-10-13",
                      f"实际 meta={meta} 账本={last}")
        shown = "\n".join(c.describe_meta(meta))
        results.check("字幕", "元数据输出写明频道 ID 和上传时刻（UTC 与北京时间）",
                      "频道 ID UC6L" in shown and "UTC（北京时间" in shown, shown)
        done = run_script("captions", "--json3", str(path), "hi")
        results.check("字幕", "之后离线读同一份字幕，也会带出旁边存的元数据",
                      done.returncode == 0 and "频道：Mind the Game" in done.stdout, f"{done.stdout[:300]!r}")
        c.LEDGER.update(path=None, label="")


class FakeBytes:
    def __init__(self, body: bytes, status: int = 200, headers: dict | None = None) -> None:
        self.status, self._body = status, body
        self.headers = headers or {}

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> bool:
        return False


def test_speakers_audio(results: Results) -> None:
    s = load_module("speakers")

    def track(fid, ext, abr, lang, note, pref=-1):
        return {"format_id": fid, "ext": ext, "abr": abr, "language": lang, "format_note": note,
                "language_preference": pref, "vcodec": "none", "acodec": "mp4a.40.2"}

    multi = {"formats": [
        track("140-0", "m4a", 129.5, "ar", "Arabic, medium"),
        track("139-0", "m4a", 48.8, "ar", "Arabic, low"),
        track("251-18", "webm", 133.3, "en-US", "English (US) original (default), medium", 10),
        track("140-18", "m4a", 129.5, "en-US", "English (US) original (default), medium", 10),
        track("139-18", "m4a", 48.8, "en-US", "English (US) original (default), low", 10),
        {"format_id": "18", "ext": "mp4", "vcodec": "avc1", "acodec": "mp4a.40.2"},
    ]}
    chosen, note = s.choose_audio(multi)
    results.check("说话人", "多语言视频：选原声轨里码率够用的最小那条（139-18），并提示有几种语言",
                  chosen["format_id"] == "139-18" and "2 种语言" in note, f"实际 {chosen and chosen['format_id']} {note}")
    results.check("说话人", "音轨说明写出格式、语言、是不是原声",
                  "139-18" in s.describe_track(chosen) and "en-US" in s.describe_track(chosen)
                  and "原声：是" in s.describe_track(chosen), s.describe_track(chosen))
    dubbed = {"formats": [track("140-0", "m4a", 129.5, "ar", "Arabic"), track("140-1", "m4a", 129.5, "en", "English")]}
    chosen, note = s.choose_audio(dubbed)
    results.check("说话人", "没有原声标记时按英文选，并提醒核对是不是配音",
                  chosen["format_id"] == "140-1" and "配音" in note, f"实际 {chosen['format_id']} {note}")

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        log = tmp / "speakers.jsonl"
        s.LEDGER.update(path=str(log), label="S02")

        def fake(command, **kwargs):
            if "-J" in command:
                return subprocess.CompletedProcess(command, 0, json.dumps(multi), "")
            return subprocess.CompletedProcess(command, 1, "", "ERROR: HTTP Error 403: Forbidden")

        err = io.StringIO()
        with patched(subprocess, "run", fake), contextlib.redirect_stderr(err):
            try:
                s.fetch_audio("https://youtu.be/multi", tmp, "yt-dlp", [])
                message = ""
            except SystemExit as stop:
                message = str(stop.code)
        entries = read_ledger(log)
        results.check("说话人", "下载前打印选中的音轨（格式、语言、是否原声）",
                      "选中音轨：139-18" in err.getvalue() and "原声：是" in err.getvalue(), err.getvalue()[:300])
        results.check("说话人", "下载失败时提示检查语言和带后缀的格式 ID，并列出原声轨",
                      "带后缀" in message and "140-18" in message and "--audio-format" in message, message[:400])
        results.check("账本", "失败的下载也记下选中的格式、语言、是不是原声",
                      entries[-1]["mode"] == "speakers_download" and entries[-1]["status"] == "error"
                      and entries[-1]["format"] == "139-18" and entries[-1]["original"] is True, f"实际 {entries}")

        calls = []

        def forced(command, **kwargs):
            calls.append(command)
            Path(command[command.index("-o") + 1].replace("%(ext)s", "m4a")).write_bytes(b"x")
            return subprocess.CompletedProcess(command, 0, "", "")

        with patched(subprocess, "run", forced), contextlib.redirect_stderr(io.StringIO()):
            s.fetch_audio("https://youtu.be/multi", tmp, "yt-dlp", [], "140-18")
        results.check("说话人", "--audio-format 指定时不再列格式，直接按指定的下",
                      len(calls) == 1 and calls[0][calls[0].index("-f") + 1] == "140-18", f"实际 {calls}")
        s.LEDGER.update(path=None, label="")


def test_speakers_others(results: Results) -> None:
    """第三批反馈：片段里不止两个人（前半是另一名球员，后半才是主持人和受访者）。"""
    s = load_module("speakers")
    named_prompt, neutral_prompt = s.build_prompts(34, "VJ Edgecombe", "Host",
                                                   'the person who says "It was great" (around 28-31s)', "")
    results.check("说话人", "两遍提示词都说可能不止两个人，认不出的标 other、不要硬塞给两人之一",
                  all('"other"' in p and "other voices" in p for p in (named_prompt, neutral_prompt))
                  and "Do not force" in named_prompt and "never put a third voice" in neutral_prompt
                  and "two speakers:" not in named_prompt and "Two people talk" not in neutral_prompt
                  and "Cover the whole clip" in named_prompt, neutral_prompt[:300])
    labels = [t["speaker"] for t in s.normalize_speakers(
        [{"speaker": "Other", "text": "a"}, {"speaker": "Jaylen Brown", "text": "b"},
         {"speaker": "UNKNOWN", "text": "c"}, {"speaker": "vj edgecombe", "text": "d"}], ["VJ Edgecombe", "Host"])]
    results.check("说话人", "两人以外的标签一律归成“其他”，大小写不计",
                  labels == ["其他", "其他", "unknown", "VJ Edgecombe"], f"实际 {labels}")
    guessed = s.spans([{"start": 0, "end": 0, "speaker": "A", "text": "one two three four five"},
                       {"start": 10, "end": 12, "speaker": "B", "text": "x"}], 20)
    results.check("说话人", "模型没给 end 时按语速估一轮的长度", guessed[0][1] == 2.0, f"实际 {guessed}")

    run = {"source": "clip.mp3", "start": 100, "end": 134, "ref_start": 128, "ref_end": 131, "ref_name": "VJ Edgecombe",
           "ref_text": "It was a great experience", "other": "Host", "lines": [], "model": "m"}
    # 旧提示词下的结果：布朗的话只剩 20-23 秒的尾句，两遍都归给了埃奇库姆，0-20 秒没有任何轮次
    named = [
        {"start": 20, "end": 23, "speaker": "VJ Edgecombe", "text": "and that is why we kept going"},
        {"start": 23, "end": 27, "speaker": "Host", "text": "VJ what did you think of the trip"},
        {"start": 28, "end": 31, "speaker": "VJ Edgecombe", "text": "It was a great experience"},
        {"start": 31, "end": 34, "speaker": "Host", "text": "Thanks everybody"},
    ]
    neutral = [{**t, "speaker": "A" if t["speaker"] == "VJ Edgecombe" else "B"} for t in named]
    cov = s.coverage(named, 34)
    results.check("说话人", "覆盖率：算出轮次覆盖的时长和没分到轮次的时段",
                  cov["covered"] == 13.0 and cov["gaps"] == [(0.0, 20.0)], f"实际 {cov}")  # 27-28 秒的 1 秒空档不列
    report = s.render(run, named, neutral, ["and that is why we kept going"])
    results.check("说话人", "两遍一致但大段时间没分到轮次时，提示“一致不代表覆盖完整”并列出时段",
                  "一致不代表覆盖完整" in report and "≈100-120s" in report
                  and "→ 一致；注意" in report, report[:700])

    # 新提示词下应有的结果：前半标成“其他”，整段都有轮次
    fixed_named = [{"start": 0, "end": 23, "speaker": "other",
                    "text": "We had a tough stretch and that is why we kept going"}] + named[1:]
    fixed_neutral = [{**fixed_named[0]}] + neutral[1:]
    report = s.render(run, fixed_named, fixed_neutral, ["and that is why we kept going"])
    results.check("说话人", "覆盖完整、参照对得上时不提示；两遍都标“其他”的句子写明不是这两个人说的",
                  "一致不代表覆盖完整" not in report and "参照段和参照句对得上" in report
                  and "两遍都标成两人以外的声音" in report and "其他" in report, report[:700])

    # 参照对不上：参照句出现在 8 秒（--ref 给的是 28-31 秒），参照段里说话的是主持人
    shifted = [
        {"start": 0, "end": 8, "speaker": "other", "text": "We had a tough stretch"},
        {"start": 8, "end": 11, "speaker": "VJ Edgecombe", "text": "It was a great experience"},
        {"start": 11, "end": 34, "speaker": "Host", "text": "VJ what did you think of the trip and the whole season so far"},
    ]
    problems = s.ref_problems(shifted, run, {})
    results.check("说话人", "参照句的位置离 --ref 给的时间太远、参照段被标成别人时都报出来",
                  any("相差 20 秒" in p for p in problems) and any("被标成了 Host" in p for p in problems), f"实际 {problems}")
    wrong_anchor = [{**t, "speaker": "Host" if t["speaker"] == "VJ Edgecombe" else t["speaker"]} for t in named]
    problems = s.ref_problems(wrong_anchor, run, {})
    results.check("说话人", "参照句本身被标成别人时指出身份锚点错了",
                  any("身份锚点" in p for p in problems), f"实际 {problems}")
    report = s.render(run, shifted, [{**t, "speaker": {"VJ Edgecombe": "A", "Host": "B"}.get(t["speaker"], t["speaker"])}
                                     for t in shifted], [])
    results.check("说话人", "参照对不上时，即使覆盖完整也提示一致不代表覆盖完整",
                  "一致不代表覆盖完整" in report and "相差 20 秒" in report, report[:600])


CAREER_SEASON_HEADERS = ["PLAYER_ID", "SEASON_ID", "LEAGUE_ID", "TEAM_ID", "TEAM_ABBREVIATION", "PLAYER_AGE", "GP", "GS",
                         "MIN", "FGM", "FGA", "FG_PCT", "FG3M", "FG3A", "FG3_PCT", "FTM", "FTA", "FT_PCT", "OREB",
                         "DREB", "REB", "AST", "STL", "BLK", "TOV", "PF", "PTS"]
CAREER_TOTAL_HEADERS = ["PLAYER_ID", "LEAGUE_ID", "Team_ID", "GP", "GS", "MIN", "FGM", "FGA", "FG_PCT", "FG3M", "FG3A",
                        "FG3_PCT", "FTM", "FTA", "FT_PCT", "OREB", "DREB", "REB", "AST", "STL", "BLK", "TOV", "PF", "PTS"]


def career_payload() -> dict:
    """构造的生涯数据：第二季中途被交易（两队各一行 + TOT 一行），表头照 playercareerstats 的真实表头。"""
    def season(sid, team, gp, gs, mins, fgm, fga, fg3m, fg3a, ftm, fta, reb, ast, stl, blk, tov, pts):
        values = {"PLAYER_ID": 1, "SEASON_ID": sid, "LEAGUE_ID": "00", "TEAM_ID": 0, "TEAM_ABBREVIATION": team,
                  "PLAYER_AGE": 20.0, "GP": gp, "GS": gs, "MIN": mins, "FGM": fgm, "FGA": fga, "FG_PCT": 0.5,
                  "FG3M": fg3m, "FG3A": fg3a, "FG3_PCT": 0.3, "FTM": ftm, "FTA": fta, "FT_PCT": 0.8, "OREB": 1,
                  "DREB": reb - 1, "REB": reb, "AST": ast, "STL": stl, "BLK": blk, "TOV": tov, "PF": 10, "PTS": pts}
        return [values[h] for h in CAREER_SEASON_HEADERS]

    def total(**v):
        base = {"PLAYER_ID": 1, "LEAGUE_ID": "00", "Team_ID": 0, "FG_PCT": 0.5, "FG3_PCT": 0.3, "FT_PCT": 0.8}
        base.update(v)
        return [base[h] for h in CAREER_TOTAL_HEADERS]

    regular = [
        season("2003-04", "CLE", 79, 79, 3122, 622, 1492, 63, 217, 347, 460, 432, 465, 130, 58, 273, 1654),
        season("2004-05", "CLE", 40, 40, 1600, 400, 800, 50, 150, 200, 250, 300, 200, 50, 20, 100, 1050),
        season("2004-05", "MIA", 30, 10, 900, 200, 450, 20, 60, 100, 120, 150, 100, 20, 10, 60, 520),
        season("2004-05", "TOT", 70, 50, 2500, 600, 1250, 70, 210, 300, 370, 450, 300, 70, 30, 160, 1570),
    ]
    playoffs = [season("2005-06", "CLE", 13, 13, 604, 146, 309, 15, 46, 116, 158, 106, 74, 18, 7, 42, 423)]
    return {"resource": "playercareerstats", "parameters": {"PlayerID": 1, "PerMode": "Totals"}, "resultSets": [
        {"name": "SeasonTotalsRegularSeason", "headers": CAREER_SEASON_HEADERS, "rowSet": regular},
        {"name": "CareerTotalsRegularSeason", "headers": CAREER_TOTAL_HEADERS, "rowSet": [total(
            GP=149, GS=129, MIN=5622, FGM=1222, FGA=2742, FG3M=133, FG3A=427, FTM=647, FTA=830, OREB=2,
            DREB=880, REB=882, AST=765, STL=200, BLK=88, TOV=433, PF=20, PTS=3224)]},
        {"name": "SeasonTotalsPostSeason", "headers": CAREER_SEASON_HEADERS, "rowSet": playoffs},
        {"name": "CareerTotalsPostSeason", "headers": CAREER_TOTAL_HEADERS, "rowSet": [total(
            GP=13, GS=13, MIN=604, FGM=146, FGA=309, FG3M=15, FG3A=46, FTM=116, FTA=158, OREB=1, DREB=105,
            REB=106, AST=74, STL=18, BLK=7, TOV=42, PF=10, PTS=423)]},
    ]}


def test_career(results: Results) -> None:
    b = load_module("nba_boxscore")
    payload = career_payload()
    tables = b.result_sets(payload)
    regular = b.career_section(tables, "regular")
    results.check("生涯数据", "逐季合计只算转会赛季的 TOT 行，与官方生涯总计一致",
                  regular["check"]["ok"] and regular["check"]["seasons_counted"] == 2, f"实际 {regular['check']}")
    results.check("生涯数据", "投丢数 = 出手 − 命中（投篮、三分、罚球）",
                  regular["missed"] == {"投篮": 1520, "三分": 294, "罚球": 183}, f"实际 {regular['missed']}")
    playoffs = b.career_section(tables, "playoffs")
    results.check("生涯数据", "季后赛单独一节", playoffs["label"] == "季后赛" and playoffs["career_total"]["GP"] == 13
                  and playoffs["check"]["ok"], f"实际 {playoffs['check']}")
    bad = copy.deepcopy(payload)
    bad["resultSets"][1]["rowSet"][0][CAREER_TOTAL_HEADERS.index("PTS")] = 3000
    check = b.career_section(b.result_sets(bad), "regular")["check"]
    results.check("生涯数据", "逐季合计和官方总计对不上时指出是哪一项",
                  not check["ok"] and check["differences"] == [{"stat": "PTS", "season_sum": 3224, "career_total": 3000}],
                  f"实际 {check}")
    results.check("生涯数据", "球员 ID 可以给数字，也可以给球员页网址",
                  b.player_id_of("https://www.nba.com/player/2544/lebron-james") == "2544" and b.player_id_of("2544") == "2544", "")

    html_page = b"<!DOCTYPE html><html><body>We are unable to process your request at the moment</body></html>"
    try:
        b.decode_stats_body(html_page, "text/html", None)
        blocked = ""
    except ValueError as err:
        blocked = str(err)
    results.check("生涯数据", "返回 200 却是网页（被拦截）时明确报出来，不当成数据", "拦截" in blocked, blocked)
    import gzip

    packed = gzip.compress(json.dumps(payload).encode("utf-8"))
    results.check("生涯数据", "gzip 压缩的回复能解开", b.decode_stats_body(packed, "application/json", "gzip") == payload, "")
    results.check("生涯数据", "请求头给全（Referer、Origin、x-nba-stats-origin / token、User-Agent）",
                  {"Referer", "Origin", "x-nba-stats-origin", "x-nba-stats-token", "User-Agent", "Accept"} <= set(b.STATS_HEADERS),
                  f"实际 {sorted(b.STATS_HEADERS)}")

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        log = tmp / "career.jsonl"
        b.LEDGER.update(path=str(log), label="K1")
        with patched(urllib.request, "urlopen", lambda request, timeout=None: FakeBytes(html_page, headers={"Content-Type": "text/html"})):
            try:
                b.fetch_stats("playercareerstats", {"PlayerID": 2544, "PerMode": "Totals", "LeagueID": "00"})
                exited = False
            except SystemExit:
                exited = True
        entries = read_ledger(log)
        results.check("账本", "stats.nba.com 被拦截也记一行（blocked: true）",
                      exited and entries and entries[-1]["status"] == 200 and entries[-1].get("blocked") is True,
                      f"实际 {entries}")
        b.LEDGER.update(path=None, label="")

        raw = tmp / "career.json"
        with contextlib.redirect_stderr(io.StringIO()):
            b.save_raw(str(raw), "https://stats.nba.com/stats/playercareerstats?PlayerID=1", payload, key="payload")
        done = run_script("nba_boxscore", "career", "1", "--from-raw", str(raw))
        results.check("生涯数据", "命令行 career --from-raw 离线跑通（逐季、TOT 标注、生涯总计、投丢、核对）",
                      done.returncode == 0 and "生涯总计" in done.stdout and "投丢" in done.stdout
                      and "不计入合计" in done.stdout and "与官方生涯总计一致" in done.stdout,
                      f"退出码 {done.returncode} {done.stdout[-400:]} {done.stderr[-200:]}")
        done = run_script("nba_boxscore", "career", "1", "--season-type", "playoffs", "--json", "--from-raw", str(raw))
        try:
            data = json.loads(done.stdout)
        except json.JSONDecodeError:
            data = {}
        results.check("生涯数据", "--season-type playoffs --json 只给季后赛",
                      done.returncode == 0 and data.get("playoffs", {}).get("missed", {}).get("投篮") == 163
                      and "regular" not in data, f"退出码 {done.returncode} {done.stderr[-200:]}")


def run_script(name: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(SCRIPTS / f"{name}.py"), *args],
                          capture_output=True, text=True, encoding="utf-8", errors="replace")


def test_review_packet(results: Results) -> None:
    path = EXAMPLES / "torre-investigation.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    out = run_script("review_packet", str(path)).stdout
    leaked = [p["finding"][:20] for p in data["points"] if p["finding"][:20] in out]
    leaked += [k for k in ("overall_scope", "headline") if str(data.get(k) or data["display"].get(k, ""))[:15] in out]
    results.check("盲审包", "不泄露作者的结论和 display", not leaked, f"泄露了：{leaked}")
    notes = [e["note"][:20] for e in data["evidence"].values() if e.get("note") and e["note"][:20] in out]
    results.check("盲审包", "默认不带证据的 note", not notes, f"带出了：{notes}")
    results.check("盲审包", "带上被核查的断言和归因/事实标注",
                  "被核查的断言" in out and "【归因】" in out and "【事实】" in out, "")


def test_review_compare(results: Results) -> None:
    path = EXAMPLES / "teleprompter-quote.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    verdicts = [p["verdict"] for p in data["points"]]

    def compare(points, missing=None):
        result = {"reviewer": "selftest", "reviewed_at": "2026-10-19T00:00+08:00",
                  "points": points, "missing_points": missing or []}
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False, encoding="utf-8") as fh:
            yaml.safe_dump(result, fh, allow_unicode=True)
        try:
            return run_script("review_compare", str(path), fh.name)
        finally:
            Path(fh.name).unlink(missing_ok=True)

    agree = [{"index": i, "verdict": v, "reason": "r", "opened": ["e1"]} for i, v in enumerate(verdicts, 1)]
    results.check("复核比对", "全部一致时通过", compare(agree).returncode == 0, "")
    differ = copy.deepcopy(agree); differ[0]["verdict"] = "无法证实"
    results.check("复核比对", "有一点不一致就不通过", compare(differ).returncode == 1, "")
    results.check("复核比对", "非阻断的漏点不影响通过",
                  compare(agree, [{"item": "外围细节", "blocking": False}]).returncode == 0, "")
    results.check("复核比对", "只写字符串的漏点按阻断处理", compare(agree, ["某个漏点"]).returncode == 1, "")
    no_opened = copy.deepcopy(agree); no_opened[0]["opened"] = []
    results.check("复核比对", "有证据的点必须记录打开过什么", compare(no_opened).returncode == 1, "")


# ---------------------------------------------------------------- 主程序

def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("-k", help="只跑名字里含这段文字的检查器用例（其他组照常跑）")
    args = parser.parse_args()

    results = Results(args.verbose)
    checker = load_module("check_verdict")
    print("== 编译"); test_compile(results)
    print("== 示例"); bases = test_examples(results, checker)
    print("== 检查器用例"); test_cases(results, checker, bases, args.k)
    print("== 其他脚本")
    test_zh_anchors(results)
    test_boxscore(results)
    test_boxscore_periods(results)
    test_speakers(results)
    test_speakers_lines(results)
    test_ledgers(results)
    test_captions_sources(results)
    test_speakers_audio(results)
    test_speakers_others(results)
    test_career(results)
    test_review_packet(results)
    test_review_compare(results)

    total = results.passed + len(results.failed)
    print(f"\n共 {total} 项：通过 {results.passed}，失败 {len(results.failed)}")
    sys.exit(1 if results.failed else 0)


if __name__ == "__main__":
    main()

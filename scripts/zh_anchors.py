"""把中文转述稿逐句拆开，列出可以拿去找英文源的锚点，并标出需要重点核的句子。

这是中文说法核查的第一步（见 SKILL.md "中文说法：先找英文源"）。脚本只做机械抽取，
不判断真假：它告诉你每一句"靠什么去找英文源""哪里可能走样"，找和判由你来做。

每句标注的内容：
  数字      金额、年限、日期、百分比——原样保留在英文里的概率最高，是最稳的检索词
  外文      人名、媒体名、原文片段
  引号      引号里的话：必须回英文原话核对（"引语重写"走样）
  信源      "据 XX 报道""来源：XX""XX："这类交代
  模糊归因  美媒、名记、曝、据悉、消息人士……：没有指向任何人，要追到具体的人
  强断言    将、已、正式、官宣、确定……：对照英文情态，看是否拔高
  因果      因、由于、所以、导致……：检查是原文说的，还是转述者推断的（"推断写成事实"）

用法：
    python zh_anchors.py 文章.txt
    python zh_anchors.py --text "曝波普和灰熊商讨买断 加盟76人联手詹姆斯"
    python search.py fetch URL | python zh_anchors.py -
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

# 不把队名里的数字当锚点："76人"的 76 不是要核的数字；但"15000人"这类人数照常是锚点
# 时刻和日期整体当一个锚点："7:30"不能拆成 7 和 30，"10月8日"不能拆成 10月 和 8日
DATETIME = re.compile(r"\d{1,2}[:：]\d{2}|\d{1,2}月\d{1,2}日")
NUMBER = re.compile(
    # 后面紧跟的逗号、句号是标点（"+950, +900"），只有"逗号/句点+数字"才是同一个数
    r"(?<![\d.,])(?!76人)\d+(?:[.,]\d+)*(?!\d|[.,]\d)\s*"
    # 长单位排在前面："16分钟"不能截成"16分"（读成得分），"5篮板"不能只剩"5"
    r"(?:分钟|篮板|助攻|抢断|盖帽|失误|万|亿|%|％|年|月|日|号|分|板|助|断|帽|岁|场|秒|美元|元|人|次|名|支)?"
)
# 中文数字加量词："五支球队""两年合同""十五年前"；不收"一"，"一个""一次"太常见
ZH_NUMBER = re.compile(r"[两二三四五六七八九十百千万]+(?:支|名|位|届|座|项|场|年|次|个)")
LATIN = re.compile(r"[A-Za-z][A-Za-z0-9.'’&\-]*(?:\s+[A-Za-z][A-Za-z0-9.'’&\-]*)*")
QUOTE = re.compile(r"“([^”]{2,})”|「([^」]{2,})」|『([^』]{2,})』|\"([^\"]{2,})\"")
# 跨段引语：中文习惯每段只开引号、最后一段才收，前面几段里没有成对的引号
QUOTE_OPEN_TAIL = re.compile(r"[“「『]([^”」』]{2,})$")
# "篮网：杰明11分1助……"是数据行，冒号前的球队不是信源
STAT_LINE = re.compile(r"\s*[一-鿿·\-A-Za-z]{1,12}\d+(?:分|板|助|断|帽)")
URL = re.compile(r"https?://\S+|www\.\S+")
SOURCE_LINE = re.compile(r"来源[:：]\s*([^\s，。；]{1,24})")
SOURCE_PATTERNS = [
    # "美媒Basketball Ground分享数据""名记 Shams 报道"：模糊称呼后面跟着具体名字，名字才是信源
    re.compile(
        r"(?:美媒|外媒|名记|美记|记者|媒体|博主)\s*([A-Z][A-Za-z0-9.&'’\- ]{1,40}?)"
        r"(?=\s*(?:分享|报道|透露|称|表示|指出|发文|爆料|晒出|发布|：|:|，|,|$))"
    ),
    # 句中直接以外文名做主语："ESPN 报道""Shams Charania 透露"
    re.compile(r"(?:^|[，,。；;\s])([A-Z][A-Za-z0-9.&'’\-]+(?:\s+[A-Z][A-Za-z0-9.&'’\-]+)*)\s*(?:分享|报道|透露|称|表示|指出|发文|爆料)"),
    re.compile(r"据([^，。；：:、]{2,24}?)(?:报道|消息|透露|报导|爆料|披露|称|表示|公布|统计|数据显示)"),
    # "Shams：……""名记：……"只认句首，句中的冒号多半是"最关键的一点是："这类；"TA："是 The Athletic 的缩写
    re.compile(r"^[#\s]*(?:\[[^\]]{1,10}\])?[^：:\s]{0,12}?([A-Z]{2,4}|[A-Za-z][A-Za-z.\s]{2,30}|[一-鿿·]{2,8})(?:记者)?[:：]"),
    re.compile(r"(?:援引|引述|转引)([^，。；]{2,24})"),
    # "东契奇接受Sports Illustrated专访"：做采访的媒体才是要追的原始出处
    re.compile(r"接受([^，。；：:]{2,30}?)(?:的)?(?:专访|采访|访问)"),
    # "ESPN发布了对埃奇库姆的采访"：发布采访的媒体也是要追的原始出处
    re.compile(r"(?:^|[，,。；;\s])(?:今日，?|今天，?)?([A-Za-z一-鿿·\s]{2,30}?)发布了?(?:对[^，。；]{1,30}?的)?(?:专访|采访|视频)"),
    # 署名："作者：迈克尔·杜阿尔特"，信源是作者名，不是"作者"
    re.compile(r"^\s*(?:作者|编辑|撰文|文)[:：]\s*([^\s，。；]{2,20})"),
    # 说话人："卡特率先发文澄清""詹姆斯表示"——谁说的，也是要核的信源
    # 从分句开头抓到动词前，再去掉"商业合作伙伴""经纪人"这类头衔
    # 引语后面的归因（"……生涯，”詹姆斯坦言"）前面是闭引号，也算分句开头
    # "76人球员勒布朗-詹姆斯在《球场大脑》节目中表示"：名字里有连字符、前面带身份，中间还可能插场合
    re.compile(
        r"(?:^|[，,。；;\s”」』])([0-9一-鿿·\-A-Za-z\s]{2,24}?)(?:在[^，。；：:]{1,20}?(?:中|上|里))?"
        r"(?:率先|随后|也|则|亲自|公开|再次)?"
        r"(?:发文|表示|透露|直言|指出|强调|回应|写道|坦言|澄清|说道|报道称|补充道|晒出|发帖|更新社媒)"
    ),
]
JU_PATTERN = SOURCE_PATTERNS[2]
COLON_PATTERN = SOURCE_PATTERNS[3]
ROLE_PREFIX = re.compile(
    r"^.*(?:伙伴|经纪人|记者|主帅|主教练|总经理|总裁|老板|发言人|队友|球星|球员|名宿|前锋|后卫|中锋|官方)"
)
# 抽出来的"名字"里出现这些字，多半不是人名或机构名
NOT_A_NAME = re.compile(
    r"[是的了这那此其该点他她]|一点|方面|团队解释|^(?:明确|正式|公开|随后|对此|同时|并且?|还|也|来源|作者|编辑|撰文)$"
)
# 只写了平台或本站的出处行（"来源：X""来源：虎扑"）只是线索，不是点名的信源
PLATFORM_OR_SITE = re.compile(
    r"^(?:X|Twitter|推特|Instagram|IG|ins|Threads|微博|YouTube|油管|TikTok|抖音|"
    r"虎扑|直播吧|懂球帝|新浪|搜狐|网易|腾讯|界面|百家号)$",
    re.I,
)
# "……目标。”萨瓦娜此次投资……"：闭引号后面紧跟一句新陈述，要断开，否则编辑自己的解释会混进引语那一项；
# 但"……。”詹姆斯说道"这种后置归因不断
QUOTE_THEN_NEW = re.compile(r"(?<=[。！？!?][”」』])(?=[^，,。；;\s”」』])")
ATTRIBUTION_AFTER = re.compile(r"^[^，。；“”「」]{1,14}?(?:说道|表示|补充道|坦言|透露|写道|强调|指出|回应|说|称)")
VAGUE = re.compile(r"美媒|外媒|名记|美记|知情人士|消息人士|内部人士|人士|据悉|据称|网传|传闻|疑似|曝光|曝|爆料")
STRONG = re.compile(r"官宣|正式|确定|敲定|已经|已|将会|将|必然|肯定|彻底|铁定")
CAUSAL = re.compile(r"因为|因此|由于|所以|导致|以致|随后即|次日即|为此|因")
SENTENCE_END = re.compile(r"(?<=[。！？!?；;])|\n+")

# "已""将"太常见，只在和动作词搭配时才算强断言，避免"已经41岁"这类误报
STRONG_CONTEXT = re.compile(
    r"(?:官宣|正式|确定|敲定|绝对|毫无疑问|百分百|确诊|结果确认|赛季报销)"
    r"|(?:已|将|将会|已经)(?:签|加盟|交易|裁|买断|达成|退役|离开|加入|完成|确认|决定|拒绝|缺席|报销)"
)
# 事件动词：这类句子是在断言"发生了某件事"，没有信源交代时必须标出来；伤病和撤稿也算
EVENT = re.compile(
    r"(?:遭|被)?(?:解雇|开除|辞退|裁掉|裁员|签下|签约|加盟|交易|买断|退役|辟谣|否认|承认|澄清|处罚|罚款|禁赛|调查|起诉"
    r"|撕裂|骨折|扭伤|拉伤|报销|伤停|缺阵|缺席|撤回|撤稿|更正)"
)
# 英文稿误入：本脚本的断句、信源规则只适用于中文
MOSTLY_LATIN = re.compile(r"[A-Za-z]")
# 署名行："体坛周报、北青体育 2026-07-23 12:07:20" 这类，名字就是信源
BYLINE = re.compile(r"^\s*([一-鿿A-Za-z、，\s]{2,40}?)\s+\d{4}-\d{2}-\d{2}")


def clean(text: str) -> str:
    # 编码损坏时人名中间的"·"常变成"?"（"勒布朗?詹姆斯"），不还原会被当成句末断开
    text = re.sub(r"(?<=[一-鿿])\?(?=[一-鿿])", "·", text)
    kept = []
    for line in text.splitlines():
        # 页面尾部的赛程、直播列表、"相关报道"之类不是正文
        if re.match(r"^\s*[-•·]\s", line) or re.search(r"\bVS\b.*\d{4}-\d{2}-\d{2}", line):
            continue
        if re.match(r"^\s*(相关报道|今日直播|热门推荐|相关阅读)", line):
            break
        kept.append(line)
    return "\n".join(kept)


OPEN_QUOTES, CLOSE_QUOTES = "“「『", "”」』"


def split_outside_quotes(text: str) -> list[tuple[str, bool]]:
    """按句末标点和换行断句，但引号里的"！""。"不断——否则引语会和它的归因（"某某称"）分到两句。

    返回 (文字, 是否独占一整行)。独占一行的短文字多半是栏目标签（"双方数据"），不能并进上一句。
    """
    parts, buf, depth, line_start, quoted_lines = [], [], 0, True, 0
    continuing = False
    for ch in text:
        if ch == "\n":
            parts.append(("".join(buf), line_start)); buf = []; line_start = True
            # 跨段引语：开引号在前几行、闭引号在后几行，中间的段落仍在引语里，深度要带过去；
            # 超过 10 行还没闭合，多半是稿件里落单的引号，不再当引语
            quoted_lines = quoted_lines + 1 if depth else 0
            if quoted_lines > 10:
                depth, quoted_lines = 0, 0
            continuing = depth > 0
            continue
        if continuing and not ch.isspace():
            continuing = False
            if ch in OPEN_QUOTES:
                # 中文习惯续段重新打一个开引号，它接的是同一段引语，深度不加
                buf.append(ch)
                continue
            # 续段没有开引号时补一个，后面抽引语时才认得出它是原话
            buf.append(OPEN_QUOTES[0])
        buf.append(ch)
        if ch in OPEN_QUOTES:
            depth += 1
        elif ch in CLOSE_QUOTES and depth:
            depth -= 1
        elif ch in "。！？!?；;" and depth == 0:
            parts.append(("".join(buf), False)); buf = []; line_start = False
    parts.append(("".join(buf), line_start))
    return parts


def split_sentences(text: str) -> list[str]:
    text = clean(text)
    parts = [(p.strip(), whole) for p, whole in split_outside_quotes(text) if p and p.strip()]
    merged: list[str] = []
    for part, whole_line in parts:
        # "来源：X"这类出处行即使很短也单独成句，否则会被并进上一句、出处被吞掉
        if len(part) < 6 and not SOURCE_LINE.match(part):
            if whole_line:
                # 独占一行的短文字（"双方数据""打开""直播吧"）是标签或页面残留，没有可核的内容
                if not re.search(r"\d", part):
                    continue
            elif merged:
                # 同一行里过短的碎片（标点、单个词）并到前一句
                merged[-1] += part
                continue
        merged.append(part)
    return [piece for part in merged for piece in split_after_quote(part)]


def split_after_quote(part: str) -> list[str]:
    pieces, start = [], 0
    for m in QUOTE_THEN_NEW.finditer(part):
        if ATTRIBUTION_AFTER.match(part[m.start():]):
            continue
        pieces.append(part[start:m.start()])
        start = m.start()
    pieces.append(part[start:])
    return [p for p in pieces if p.strip()]


def sources_in(sentence: str) -> list[str]:
    found: list[str] = []
    byline = BYLINE.match(sentence)
    if byline:
        found += [n.strip() for n in re.split(r"[、，,\s]+", byline.group(1)) if len(n.strip()) >= 2]
    line = SOURCE_LINE.search(sentence)
    if line:
        # 出处行允许单个字符（"来源：X"）；X 只是平台，不等于找到了原始信源
        found.append(line.group(1).strip())
    for pattern in SOURCE_PATTERNS:
        for match in pattern.finditer(sentence):
            if pattern is COLON_PATTERN and STAT_LINE.match(sentence[match.end():]):
                continue
            # 去掉名字里带的日期（"北青体育22日"）；以"报道"结尾的是误抓的动词
            raw = re.sub(r"\d+日$", "", match.group(1).strip()).strip()
            # "Sportico的库尔特-巴登豪森"：机构和记者都是信源，拆开各算一个
            # 只在"据 XX 的 YY 统计"里拆；"最关键的一点是："这类不是信源，不能拆出"最关键"
            split = pattern is JU_PATTERN and raw.count("的") == 1
            for name in raw.split("的") if split else [raw]:
                add_source(found, name)
    # 去掉被更长名字包含的碎片（"Ground" 是 "Basketball Ground" 的一部分）
    return [n for n in found if not any(n != other and n in other for other in found)]


def add_source(found: list[str], name: str) -> None:
    # "而詹姆斯当众指出"：去掉开头的连词、结尾的副词，只留人名
    # 句首冒号那条会把"而詹姆斯当众指出："整段抓进来，所以末尾的说话动词也要去掉
    name = re.sub(r"^(?:而|但|并|且|又|也|还|则|随后)", "", name.strip())
    name = re.sub(r"(?:发文|表示|透露|直言|指出|强调|回应|写道|坦言|澄清|说道|报道称|配文道|配文|称|说)$", "", name)
    name = re.sub(r"(?:当众|公开|当面|随即|亲自|直接|再次|已经|曾经)$", "", name).strip()
    stripped = ROLE_PREFIX.sub("", name).strip()
    if len(stripped) >= 2:
        name = stripped
    if (
        len(name) >= 2
        and name not in found
        and not name.endswith(("报道", "报导"))
        and not VAGUE.search(name)
        and not NOT_A_NAME.search(name)
    ):
        found.append(name)


def analyse(sentence: str) -> dict:
    # 网址单独列出，不参与信源、数字、外文名的抽取（否则 https 会被当成信源，帖子 ID 被当成数字）
    links = URL.findall(sentence)
    sentence = URL.sub(" ", sentence)
    quotes = ["".join(g for g in m.groups() if g) for m in QUOTE.finditer(sentence)]
    open_tail = QUOTE_OPEN_TAIL.search(sentence)
    if open_tail:
        quotes.append(open_tail.group(1))
    latin = [m.group(0).strip() for m in LATIN.finditer(sentence) if len(m.group(0).strip()) > 1]
    datetimes = DATETIME.findall(sentence)
    rest = DATETIME.sub(" ", sentence)
    numbers = datetimes + [n.strip() for n in NUMBER.findall(rest)] + ZH_NUMBER.findall(rest)
    return {
        "links": links,
        "numbers": numbers,
        "latin": list(dict.fromkeys(latin)),
        "quotes": quotes,
        "sources": sources_in(sentence),
        "vague": list(dict.fromkeys(VAGUE.findall(sentence))),
        "strong": list(dict.fromkeys(m.group(0) for m in STRONG_CONTEXT.finditer(sentence))),
        "events": list(dict.fromkeys(m.group(0) for m in EVENT.finditer(sentence))),
        "causal": list(dict.fromkeys(CAUSAL.findall(sentence))),
    }


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    parser.add_argument("file", nargs="?", help="中文稿文本文件；- 表示从标准输入读")
    parser.add_argument("--text", help="直接给一段文字（例如只核一个标题）")
    args = parser.parse_args()

    if args.text:
        text = args.text
    elif args.file == "-":
        sys.stdin.reconfigure(encoding="utf-8")
        text = sys.stdin.read()
    elif args.file:
        text = Path(args.file).read_text(encoding="utf-8")
    else:
        parser.error("需要文件、- 或 --text")

    cjk = len(re.findall(r"[一-鿿]", text))
    if len(MOSTLY_LATIN.findall(text)) > 3 * max(cjk, 1):
        print("[提醒] 这看起来是英文稿。本脚本的断句和信源识别只适用于中文，英文稿的信源（told XX、"
              "said in a post）和数字请人工逐句列出，下面的输出只作参考。\n")
    sentences = split_sentences(text)
    rows = [(i, s, analyse(s)) for i, s in enumerate(sentences, 1)]

    all_sources, all_vague, all_numbers, all_latin, all_quotes = [], [], [], [], []
    print("== 逐句核查清单 ==")
    print("每一句事实陈述都要能对应到英文原文或官方来源；对应不上的在状态块里单列：说不出任何来源判“查无出处”，来源认得出但证据不够判“无法证实”。\n")
    last_speaker: list[str] = []
    for i, sentence, a in rows:
        # 引语的续段（"他说：“……"之后另起一段的“……"）沿用上文的说话人，不算无源
        if a["sources"]:
            last_speaker = [s for s in a["sources"] if not PLATFORM_OR_SITE.match(s)] or last_speaker
        elif a["quotes"] and last_speaker and re.match(r"\s*[“「『]", sentence):
            a["inherited"] = last_speaker
        flags = []
        if a["quotes"]:
            flags.append("引号→回原话核对")
        if a["vague"] and a["sources"]:
            flags.append(f"模糊称呼后点了名：{'、'.join(a['sources'])}→找到它的原帖或原文，确认它真的这样说过")
        elif a["vague"]:
            flags.append("模糊归因→追到具体的人")
        if a["strong"]:
            flags.append("强断言→对照英文情态")
        if a["causal"]:
            flags.append("因果→是原文说的还是推断")
        is_byline = bool(BYLINE.match(sentence))
        events = "、".join(a["events"])
        if a["events"] and a["sources"]:
            # 信源只覆盖它自己说的那部分："热火澄清是操作失误，涉事员工遭解雇"里，
            # 解雇不是热火说的。有信源也要确认它说的就是这件事。
            flags.append(f"断言了事件：{events}→确认「{'、'.join(a['sources'])}」说的就是这件事，而不只是同句的其他部分")
        elif (not a["sources"] and not a.get("inherited") and not is_byline
              and (a["numbers"] or a["strong"] or a["quotes"] or a["events"])):
            flags.append("本句无信源交代" + (f"（断言了事件：{events}）" if events else ""))
        print(f"[{i}] {sentence}")
        detail = []
        if a.get("inherited"):
            detail.append(f"信源：承上文（{'、'.join(a['inherited'])}）")
        for label, key in (("信源", "sources"), ("链接", "links"), ("模糊", "vague"), ("数字", "numbers"),
                           ("外文", "latin"), ("强断言", "strong"), ("因果", "causal")):
            if a[key]:
                detail.append(f"{label}：{'、'.join(a[key])}")
        if a["quotes"]:
            detail.append("引号：" + " | ".join(a["quotes"]))
        if detail:
            print("    " + "；".join(detail))
        if flags:
            print("    ⚑ " + "；".join(flags))
        print()
        all_sources += a["sources"]
        all_vague += a["vague"]
        all_numbers += a["numbers"]
        all_latin += a["latin"]
        all_quotes += a["quotes"]

    print("== 找英文源的线索（按优先级）==")
    sources = [s for s in dict.fromkeys(all_sources) if not PLATFORM_OR_SITE.match(s)]
    platforms = [s for s in dict.fromkeys(all_sources) if PLATFORM_OR_SITE.match(s)]
    if sources:
        print("1. 稿件点名的信源：" + "、".join(sources))
    else:
        print("1. 稿件没有点名任何信源" + ("，只有模糊归因：" + "、".join(dict.fromkeys(all_vague)) if all_vague else ""))
    if platforms:
        print("   出处行只写了平台或本站：" + "、".join(platforms)
              + "（只当线索，不单列核查点；写的是本站，多半是编辑自己整理的，比赛数据就直接查官方）")
    if all_quotes:
        print("2. 引号里的话（回译成英文关键词，加球员名和时间范围去搜）：")
        for q in dict.fromkeys(all_quotes):
            print(f"   - {q}")
    numbers = list(dict.fromkeys(n for n in all_numbers if re.search(r"\d", n)))
    if numbers:
        print("3. 数字（英文里通常原样保留，换算单位后搜）：" + "、".join(numbers))
    latin = list(dict.fromkeys(all_latin))
    if latin:
        print("4. 已有的外文名：" + "、".join(latin))
    print("5. 中文人名、队名请自行回译成英文后组合检索；只找到其他中文稿互相引用、又说不出任何具体来源时，判“查无出处”。")

    flagged = sum(1 for _, _, a in rows if a["quotes"] or a["vague"] or a["strong"] or a["causal"] or a["events"])
    print(f"\n共 {len(rows)} 句，其中 {flagged} 句带有需要重点核的标记。")


if __name__ == "__main__":
    main()

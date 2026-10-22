"""检查核查状态块：必填字段、标签取值、display 段的版式字段与字数、display 有没有凭空多出来的数字。

规则来源：assets/verdict-template.md。检查不过（有"错误"）时退出码为 1。

用法：
    python check_verdict.py 状态块.yaml
    python check_verdict.py 状态块.json
    python check_verdict.py assets/examples/*.yaml

YAML 需要 PyYAML；没装时把状态块存成 JSON 再检查（字段完全相同）。
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

CURRENT_CRITERIA = "nba-fact-check/8"
"""当前判据版本。SKILL.md 里的判据（标签含义、取证规则、汇总规则）一改就递增，并同步改这里。"""

VERDICTS = {"准确", "措辞放大", "误导", "虚假", "查无出处", "无法证实", "不可核查"}
NEGATIVE_VERDICTS = {"误导", "虚假", "查无出处", "无法证实", "措辞放大"}
# 这些判定都要靠正面证据成立（证明它对、或证明它和原文不一样），所以都受"不能只有 L3"约束；
# 查无出处、无法证实、不可核查本来就是"证据不够"的结论，不受这条约束
POSITIVE_EVIDENCE_VERDICTS = {"准确", "措辞放大", "误导", "虚假"}
# display 里出现的外文专名（大写开头的词组）必须在核查结果里出现过；中文专名脚本核不了，要人工核
LATIN_ENTITY = re.compile(r"\b[A-Z][A-Za-z0-9&.'’\-]*(?:\s+[A-Z][A-Za-z0-9&.'’\-]*)*")
# 中文机构名的粗略识别：以这些字收尾的词。只是启发式，人名和不带这些后缀的机构仍要人工核
ZH_ORG = re.compile(r"[一-鿿]{1,10}(?:机构|媒体|公司|集团|报社|日报|晚报|周报|网站|频道|电视台|通讯社|协会|直播网)")
# 泛指而非具体机构的说法："多家媒体""中文媒体""具体媒体"
GENERIC_ORG = re.compile(r"(?:具体|多家|各家|几家|中文|英文|外国|美国|主流|正规|其他|该|这家|那家|知名|相关|官方)(?:机构|媒体|网站|频道)$")
# 只有这些部分算"核查结果"：数字和专名只能从这里搬到 display。原始说法、备注不算
VERIFIED_KEYS = ("points", "evidence", "english_source", "earliest_source", "claim_source", "timeline", "event")
# claim_source 是我们亲眼看到的"这条说法登在哪里"（发布者、时间），属于已观察到的事实；claim 正文不算
TIERS = {"official", "insider", "beat", "outlet", "aggregate", "farm", "none"}
MODALITY = {"在谈", "有望", "已达成", "已完成", "官宣"}
STATUS = {"传闻中", "多方跟进", "已证实", "本人辟谣", "已证伪", "不了了之"}
LAYOUTS = {"quote", "chain", "ladder", "number", "plain"}
ACCESS = {"full", "L1", "L2", "L3"}
TIMEZONES = {"ET", "PT", "北京", "UTC"}
REVIEW_STATUS = {"passed", "pending", "failed"}
SEQUENCE = re.compile(r"因为|因|由于|导致|之后|随后|次日|此前|after|before|then", re.IGNORECASE)
# "没"单字就够（没有、没说、没被……），"无"后面接名词时不算否定式（"无人能挡"之类），只认"无关""无此"
NEGATIVE_HEADLINE = re.compile(r"^(?:没|并非|不是|并未|未|无关|无此|否认|不)")
SPREAD_WORDS = re.compile(r"广为流传|刷屏|热传|疯传|全网")

COMMON_LIMITS = {"headline": 20, "claim_short": 24, "why": 40, "sources_line": 60}
QUOTE_LIMITS = {"speaker": 20, "original": 220, "faithful": 80, "relayed": 80, "drift": 30, "where": 40}
NUMBER_LIMITS = {"label": 12, "claimed": 20, "actual": 20, "unit_note": 20, "basis": 40}

URL_PATTERN = re.compile(r"https?://|www\.|\.(com|cn|net|org)\b", re.IGNORECASE)
NUMBER_PATTERN = re.compile(r"\d+(?:\.\d+)?")


def load(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() == ".json":
        return json.loads(text)
    try:
        import yaml
    except ImportError:
        sys.exit("读 YAML 需要 PyYAML（pip install pyyaml）；或者把状态块存成 .json 再检查。")
    return yaml.safe_load(text)


class Report:
    def __init__(self) -> None:
        self.errors: list[str] = []
        self.warnings: list[str] = []

    def error(self, message: str) -> None:
        self.errors.append(message)

    def warn(self, message: str) -> None:
        self.warnings.append(message)


def display_width(text: str) -> float:
    """按渲染宽度计数：汉字和全角符号算 1，英文字母、数字、半角符号算 0.5。

    按字符数算会冤枉英文名——"Maverick Carter" 是 15 个字符，排出来却只占七八个汉字宽。
    """
    return sum(0.5 if ord(ch) < 0x2E80 else 1 for ch in text)


def check_length(report: Report, where: str, value, limit: int) -> None:
    if value is None:
        return
    width = display_width(str(value))
    if width > limit:
        report.error(f"{where} 宽度 {width:g}，超过上限 {limit}：{value}")


def strings_in(node) -> list[str]:
    if isinstance(node, str):
        return [node]
    if isinstance(node, dict):
        return [s for v in node.values() for s in strings_in(v)]
    if isinstance(node, list):
        return [s for v in node for s in strings_in(v)]
    if node is None or isinstance(node, bool):
        return []
    return [str(node)]


def without_urls(node):
    if isinstance(node, dict):
        return {k: without_urls(v) for k, v in node.items() if k != "url"}
    if isinstance(node, list):
        return [without_urls(v) for v in node]
    if isinstance(node, str):
        return URL_PATTERN.sub(" ", node) if node.startswith("http") else node
    return node


def numbers_in(text: str) -> set[str]:
    # 证据编号（"见 e2、e3"、复核者补的 r1）不是数字，先去掉
    text = re.sub(r"(?<![A-Za-z])[er]\d+[a-z]?(?![A-Za-z\d])", " ", text)
    # 去掉千分位逗号，"4,070,355" 和 "4070355" 视为同一个数
    return set(NUMBER_PATTERN.findall(text.replace(",", "")))


def check_core(data: dict, report: Report) -> None:
    for key in ("criteria_version", "checked_at", "claim", "points", "overall", "evidence", "display"):
        if data.get(key) in (None, "", [], {}):
            report.error(f"缺少必填字段 {key}")

    evidence = data.get("evidence") or {}
    if not isinstance(evidence, dict):
        report.error("evidence 应该是以证据编号为键的映射")
        evidence = {}
    for eid, item in evidence.items():
        if not isinstance(item, dict):
            report.error(f"evidence.{eid} 应该是映射")
            continue
        if not item.get("url") and not item.get("publisher"):
            report.error(f"evidence.{eid} 既没有 url 也没有 publisher，无从查证")
        if item.get("kind") not in ("primary", "secondary"):
            report.error(f"evidence.{eid}.kind 应为 primary 或 secondary，现在是 {item.get('kind')!r}")
        if item.get("access") not in ACCESS:
            report.error(f"evidence.{eid}.access 应为 {sorted(ACCESS)} 之一，现在是 {item.get('access')!r}")

    # 同一句 asserts、同一种 scope 出现在两个点里：结论不同时读者会看到同一句话"既准确又虚假"
    seen_asserts: dict = {}
    for i, point in enumerate(data.get("points") or [], 1):
        key = (re.sub(r"\s+", "", str(point.get("asserts") or "")), point.get("scope"))
        if not key[0]:
            continue
        if key in seen_asserts:
            j, verdict = seen_asserts[key]
            if verdict != point.get("verdict"):
                report.error(f"points 第 {j} 条和第 {i} 条摘的是同一句 asserts、同一种 scope，结论却不同"
                             f"（{verdict} / {point.get('verdict')}）：把要判的那一段单独摘出来")
            else:
                report.warn(f"points 第 {j} 条和第 {i} 条摘的是同一句 asserts、同一种 scope，可以合并")
        else:
            seen_asserts[key] = (i, point.get("verdict"))

    for i, point in enumerate(data.get("points") or [], 1):
        if point.get("verdict") not in VERDICTS:
            report.error(f"points 第 {i} 条的 verdict 不在标签表里：{point.get('verdict')!r}")
        if not point.get("question") or not point.get("finding"):
            report.error(f"points 第 {i} 条缺少 question 或 finding")
        if not point.get("asserts"):
            report.error(f"points 第 {i} 条缺少 asserts：照原说法摘录被核查的那句断言")
        scope = point.get("scope")
        if scope not in ("fact", "attribution"):
            report.error(f"points 第 {i} 条的 scope 应为 fact（核事实）或 attribution（核归因），现在是 {scope!r}")
        basis = point.get("basis")
        if basis is not None and basis not in ("primary", "reports"):
            report.error(f"points 第 {i} 条的 basis 应为 primary 或 reports，现在是 {basis!r}")
        if point.get("verdict") == "准确" and scope == "fact":
            used = [evidence[r] for r in point.get("evidence") or [] if isinstance(evidence.get(r), dict)]
            if basis is None:
                report.error(f"points 第 {i} 条是事实点、判为准确，必须写 basis：primary（有一手证据）或 reports（两个以上独立报道一致）")
            elif basis == "primary" and not any(e.get("kind") == "primary" for e in used):
                report.error(f"points 第 {i} 条 basis 是 primary，但引用的证据里没有一手证据（kind: primary）")
            elif basis == "reports" and len({str(e.get("publisher")) for e in used}) < 2:
                report.error(f"points 第 {i} 条 basis 是 reports，但引用的证据不足两个不同的发布者")
            if basis == "reports" and not point.get("basis_note"):
                # 脚本只能数发布者；同一匿名稿被两家转载也是"两个发布者"，独立性要作者写明、复核者判断
                report.error(f"points 第 {i} 条 basis 是 reports，必须写 basis_note：哪几家、谁具名、为什么互相独立")
        refs = point.get("evidence") or []
        if not refs and point.get("verdict") in ("查无出处", "无法证实") and not point.get("searched"):
            report.warn(f"points 第 {i} 条没有引用证据：写 searched 列出搜过的检索词，否则分不清查了没查到还是没查")
        if not refs and point.get("verdict") not in ("查无出处", "不可核查", "无法证实"):
            report.error(f"points 第 {i} 条判为「{point.get('verdict')}」却没有引用任何证据")
        for ref in refs:
            if ref not in evidence:
                report.error(f"points 第 {i} 条引用了不存在的证据 {ref}")
        levels = [evidence[r].get("access") for r in refs if isinstance(evidence.get(r), dict)]
        if point.get("verdict") in POSITIVE_EVIDENCE_VERDICTS and levels and all(level == "L3" for level in levels):
            if point.get("l3_ok"):
                l3_problems = check_l3_ok(point["l3_ok"], refs, evidence)
                if l3_problems:
                    for problem in l3_problems:
                        report.error(f"points 第 {i} 条的 l3_ok：{problem}")
                else:
                    report.warn(f"points 第 {i} 条只有转述（L3）支撑，按 l3_ok 放行（{point['l3_ok'].get('platform')}）；复核时按三条硬条件人工确认")
            else:
                report.error(
                    f"points 第 {i} 条判为「{point.get('verdict')}」，但引用的证据全是转述（L3）：补一条独立来源，或改判无法证实；"
                    "确属工具够不着原帖（例如 X 上的帖子只能看媒体逐字收录）时，在该点写 l3_ok 说明理由"
                )

    if data.get("overall") and data["overall"] not in VERDICTS:
        report.error(f"overall 不在标签表里：{data['overall']!r}")

    out_of_scope = data.get("out_of_scope")
    if out_of_scope is not None and not isinstance(out_of_scope, list):
        report.error("out_of_scope 应该是列表：每条写一个没打算核的断言和原因")
    elif out_of_scope:
        for j, item in enumerate(out_of_scope, 1):
            if not isinstance(item, dict) or not item.get("asserts") or not item.get("reason"):
                report.error(f"out_of_scope 第 {j} 条要写成 {{asserts: 断言, reason: 为什么不核}}，两项都必填")

    if data.get("criteria_version") and data["criteria_version"] != CURRENT_CRITERIA:
        report.error(f"criteria_version 是 {data['criteria_version']!r}，当前判据版本是 {CURRENT_CRITERIA!r}：按当前判据重核后再改")

    # 结论混合时（有准确、也有误导/查无出处等），总标签一个词说不清，必须写 overall_scope
    verdicts = {p.get("verdict") for p in data.get("points") or []}
    if "准确" in verdicts and verdicts & NEGATIVE_VERDICTS and not data.get("overall_scope"):
        report.error(
            "核查点里既有准确、也有" + "/".join(sorted(verdicts & NEGATIVE_VERDICTS)) +
            "：必须写 overall_scope，说明总标签管的是哪部分、其余部分是什么结论"
        )

    # 复查：supersedes 不为空时必须说明是更正还是更新
    if data.get("supersedes"):
        change = data.get("change")
        if not isinstance(change, dict) or change.get("type") not in ("更正", "更新") or not change.get("note"):
            report.error("填了 supersedes 就必须填 change：{type: 更正 或 更新, note: 改了什么、为什么}")

    # 时间线：含先后或因果的说法必须有
    timeline = data.get("timeline")
    if timeline:
        for i, step in enumerate(timeline, 1):
            if not isinstance(step, dict) or not step.get("at") or not step.get("event"):
                report.error(f"timeline 第 {i} 条缺少 at 或 event")
                continue
            if step.get("tz") not in TIMEZONES:
                report.error(f"timeline 第 {i} 条的 tz 应为 {sorted(TIMEZONES)} 之一，现在是 {step.get('tz')!r}")
            if step.get("evidence") and step["evidence"] not in evidence:
                report.error(f"timeline 第 {i} 条引用了不存在的证据 {step['evidence']}")
        zones = {step.get("tz") for step in timeline if isinstance(step, dict)}
        if len(zones) > 1:
            report.warn(f"timeline 里混用了多个时区 {sorted(z for z in zones if z)}：比较先后前请换算到同一时区")
    elif SEQUENCE.search(str(data.get("claim") or "")):
        report.warn("说法里有先后或因果关系（因、之后、次日……），建议填 timeline 把真实顺序摊开")

    # 中文说法必须先找英文源（SKILL.md "中文说法：先找英文源"）
    if len(re.findall(r"[一-鿿]", str(data.get("claim") or ""))) >= 2:
        english = data.get("english_source")
        if not isinstance(english, dict):
            report.error("说法是中文，必须填 english_source：找到了写出处和英文原文，没找到写 found: false 和搜过的检索词")
        elif not isinstance(english.get("found"), bool):
            report.error("english_source.found 必须是 true 或 false")
        elif "directly_accessed" in english and not isinstance(english["directly_accessed"], bool):
            report.error("english_source.directly_accessed 必须是 true 或 false")
        elif "independent_primary" in english and not isinstance(english["independent_primary"], bool):
            report.error("english_source.independent_primary 必须是 true 或 false")
        elif english["found"] is False and not isinstance(english.get("independent_primary"), bool):
            report.error("english_source.found 为 false 时必须填 independent_primary（true 或 false）：有没有找到能独立核事实的一手来源")
        elif english["found"]:
            # 认出了出处但没读到原文（原稿撤了、社媒打不开）时没有原句可引，不能逼执行者拿转述冒充原句
            if english.get("directly_accessed") is False:
                if english.get("excerpt") and not english.get("excerpt_source"):
                    report.warn("没直接读到原文，excerpt 却写了内容：写明 excerpt_source（这句是从哪篇转述里摘的），"
                                "免得被当成原文")
            elif not english.get("excerpt"):
                report.error("english_source.found 为 true 时必须填 excerpt（英文原文里对应的那一句）；"
                             "认出了出处但没读到原文时，把 directly_accessed 记成 false")
            elif not re.search(r"[A-Za-z]{3,}", str(english["excerpt"])):
                report.warn("english_source.excerpt 里没有英文：这里应放英文原文，确认来源确实是英文或官方一手")
            if not english.get("who") and not english.get("url"):
                report.error("english_source.found 为 true 时至少要写 who 或 url")
        else:
            if not english.get("searched"):
                report.error("english_source.found 为 false 时必须列出 searched（搜过的英文检索词）")
            if data.get("overall") in ("准确", "措辞放大") and not english.get("independent_primary"):
                report.warn(
                    f"没找到英文源，overall 却是「{data.get('overall')}」：没有源头时通常只能判查无出处、无法证实，"
                    "或在有反证时判虚假"
                )

    source = data.get("earliest_source")
    if isinstance(source, dict) and source.get("tier") not in TIERS:
        report.error(f"earliest_source.tier 取值不对：{source.get('tier')!r}")

    event = data.get("event")
    if isinstance(event, dict):
        if event.get("modality") not in MODALITY | {None, "none"}:
            report.error(f"event.modality 取值不对：{event.get('modality')!r}")
        if event.get("status") not in STATUS:
            report.error(f"event.status 取值不对：{event.get('status')!r}")


L3_PLATFORMS = {"X", "Instagram", "Threads"}


def check_l3_ok(l3_ok, refs: list, evidence: dict) -> list[str]:
    """核对 l3_ok 的结构：三条硬条件各有着落（语义仍由复核者判断）。"""
    if not isinstance(l3_ok, dict):
        return ["要写成结构化的四项（platform、verbatim、handle、counter_evidence），见模板；一段自由文本无法核对三条硬条件"]
    problems = []
    if l3_ok.get("platform") not in L3_PLATFORMS:
        problems.append(f"platform 应为 {sorted(L3_PLATFORMS)} 之一（只有工具进不去的社媒平台才能用这个例外），现在是 {l3_ok.get('platform')!r}")
    verbatim = l3_ok.get("verbatim") or []
    if not isinstance(verbatim, list) or len(verbatim) < 2:
        problems.append("verbatim 至少要列两条逐字收录帖子的证据编号")
    else:
        missing = [v for v in verbatim if v not in refs]
        if missing:
            problems.append(f"verbatim 里的 {missing} 不在本点引用的证据里")
        publishers = {str((evidence.get(v) or {}).get("publisher")) for v in verbatim if v in evidence}
        if len(publishers) < 2:
            problems.append("verbatim 的证据要来自至少两个不同的发布者")
    if not l3_ok.get("handle"):
        problems.append("handle 必填：每一家怎么点明发帖人和平台；能对上具体账号就写出来，对不上就写\"账号未直接核对\"")
    if not l3_ok.get("counter_evidence"):
        problems.append("counter_evidence 必填：反证检索做了什么、结果如何（帖子删除或 Story 过期不算反证）")
    return problems


def check_display(data: dict, report: Report) -> None:
    display = data.get("display")
    if not isinstance(display, dict):
        return

    layout = display.get("layout")
    if layout not in LAYOUTS:
        report.error(f"display.layout 取值不对：{layout!r}，应为 {sorted(LAYOUTS)}")

    for key in ("headline", "claim_short", "stamp", "why", "sources_line", "cutoff"):
        if not display.get(key):
            report.error(f"display 缺少 {key}")
    for key, limit in COMMON_LIMITS.items():
        check_length(report, f"display.{key}", display.get(key), limit)

    if display.get("stamp") and display["stamp"] not in VERDICTS:
        report.error(f"display.stamp 不在标签表里：{display['stamp']!r}")
    if display.get("stamp") and data.get("overall") and display["stamp"] != data["overall"]:
        report.warn(f"display.stamp（{display['stamp']}）和 overall（{data['overall']}）不同，确认是有意为之")

    headline = str(display.get("headline") or "")
    if headline.rstrip().endswith(("?", "？")):
        report.error("display.headline 不能是问句：问句会把被核查的说法再传播一遍")
    if headline and headline == display.get("claim_short"):
        report.error("display.headline 和 claim_short 一样：headline 应写核查后的真相，不是原说法")

    checked_at = str(data.get("checked_at") or "")
    stamp_time = re.search(r"\d{4}-(\d{2})-(\d{2})T(\d{2}):(\d{2})", checked_at)
    if stamp_time and display.get("cutoff"):
        expected = f"{stamp_time[1]}-{stamp_time[2]} {stamp_time[3]}:{stamp_time[4]}"
        if expected not in str(display["cutoff"]):
            report.error(f"display.cutoff（{display['cutoff']}）和 checked_at 对不上，应包含 {expected}")

    review = data.get("review") if isinstance(data.get("review"), dict) else {}
    if review and review.get("status") not in REVIEW_STATUS:
        report.error(f"review.status 应为 {sorted(REVIEW_STATUS)} 之一，现在是 {review.get('status')!r}")
    if not isinstance(display.get("publishable"), bool):
        report.error("display.publishable 必须是 true 或 false")
    elif display["publishable"] is False and not display.get("review_reason"):
        report.error("display.publishable 为 false 时必须写 review_reason")
    elif display["publishable"] is True and review.get("status") != "passed":
        report.error("display.publishable 为 true 时 review.status 必须是 passed：对外发布前要经过独立复核")
    if review.get("status") == "passed" and not review.get("by"):
        report.error("review.status 为 passed 时必须写 review.by（谁复核的）")
    if review.get("status") == "passed" and not review.get("at"):
        report.error("review.status 为 passed 时必须写 review.at（什么时候复核的）")
    disagreements = [
        str(p.get("index")) for p in review.get("points") or [] if isinstance(p, dict) and p.get("agree") is False
    ]
    if review.get("status") == "passed" and disagreements:
        report.error(f"review.points 里第 {'、'.join(disagreements)} 点作者和复核者判定不一致，review.status 不能是 passed")

    texts = strings_in({k: v for k, v in display.items() if k not in ("quote_pair",)})
    for text in texts:
        if "假新闻" in text:
            report.error(f"display 里不用“假新闻”这个词，判定只用标签表里的词：{text}")
        if SPREAD_WORDS.search(text) and not re.search(r"\d", text):
            report.warn(f"写了“广为流传/刷屏”一类的话却没有传播数据：{text}")
    if NEGATIVE_HEADLINE.match(headline):
        report.warn(f"display.headline 是否定式（{headline}）：尽量写具体事实，否定式会让读者先记住被否定的说法")
    check_length(report, "display.review_reason", display.get("review_reason"), 40)

    for text in strings_in(display):
        if URL_PATTERN.search(text):
            report.error(f"display 里不放网址：{text}")

    if layout == "quote":
        pair = display.get("quote_pair")
        if not isinstance(pair, dict):
            report.error("layout 为 quote 时必须填 quote_pair")
        else:
            for key in ("speaker", "original", "faithful", "relayed", "drift", "where"):
                if not pair.get(key):
                    report.error(f"quote_pair 缺少 {key}")
            for key, limit in QUOTE_LIMITS.items():
                check_length(report, f"quote_pair.{key}", pair.get(key), limit)
            if pair.get("original") and re.search(r"[一-鿿]", str(pair["original"])):
                report.warn("quote_pair.original 里有中文：这里应放英文原话，译文放 faithful")

    elif layout == "chain":
        chain = display.get("chain")
        if not isinstance(chain, list) or not 2 <= len(chain) <= 5:
            report.error("layout 为 chain 时 chain 必须是 2 到 5 个节点的列表")
        else:
            for i, node in enumerate(chain, 1):
                if not node.get("who"):
                    report.error(f"chain 第 {i} 个节点缺少 who")
                check_length(report, f"chain 第 {i} 个节点的 who", node.get("who"), 12)
                check_length(report, f"chain 第 {i} 个节点的 note", node.get("note"), 16)
                if node.get("tier") not in TIERS:
                    report.error(f"chain 第 {i} 个节点的 tier 取值不对：{node.get('tier')!r}")
            if not any(node.get("drifted") for node in chain) and chain[-1].get("who") != "查无出处":
                report.error("chain 里没有任何节点标 drifted: true，最后一个节点也不是「查无出处」——那这张图想说明什么？")

    elif layout == "ladder":
        ladder = display.get("ladder")
        if not isinstance(ladder, dict):
            report.error("layout 为 ladder 时必须填 ladder")
        else:
            if not ladder.get("subject"):
                report.error("ladder 缺少 subject")
            check_length(report, "ladder.subject", ladder.get("subject"), 16)
            if ladder.get("level") not in MODALITY | {"none"}:
                report.error(f"ladder.level 取值不对：{ladder.get('level')!r}（套不上五格时写 none）")
            if ladder.get("status") not in STATUS:
                report.error(f"ladder.status 取值不对：{ladder.get('status')!r}")
            history = ladder.get("history") or []
            if not history:
                report.error("ladder.history 至少要有一条")
            for i, step in enumerate(history, 1):
                if not step.get("date") or not step.get("note"):
                    report.error(f"ladder.history 第 {i} 条缺少 date 或 note")
                check_length(report, f"ladder.history 第 {i} 条的 note", step.get("note"), 16)
            if len(history) > 5:
                report.warn("ladder.history 超过 5 条，渲染时只显示最近 5 条")

    elif layout == "number":
        number = display.get("number")
        if not isinstance(number, dict):
            report.error("layout 为 number 时必须填 number")
        else:
            for key in ("label", "claimed", "actual", "basis"):
                if not number.get(key):
                    report.error(f"number 缺少 {key}")
            for key, limit in NUMBER_LIMITS.items():
                check_length(report, f"number.{key}", number.get(key), limit)

    # 只搬运、不新增。display 的字段分两类：
    #   展示"原说法"的字段（claim_short、number.claimed、quote_pair.relayed）——可以用原说法里的内容；
    #   展示"核实结果"的字段（其余全部）——数字和专名只能来自已核实的结果。
    # 已核实的结果 = 各核查点的 finding、证据、英文源的 who/excerpt、最早出处、说法登载处、时间线、事件。
    # 核查点的 question（待核的问题）和 english_source.searched（检索词）不算；网址也不算。
    verified_text = " ".join(strings_in(without_urls(verified_part(data))))
    claim_text = " ".join(strings_in(without_urls({"claim": data.get("claim"), "src": data.get("claim_source")})))
    verified_numbers = numbers_in(verified_text)
    claim_numbers = numbers_in(claim_text)

    check_quote_original(display, verified_text, report)
    truth, claim_side = display_fields(display)
    for where, texts, numbers, text_pool in (
        ("核实结果", truth, verified_numbers, verified_text),
        ("原说法", claim_side, verified_numbers | claim_numbers, verified_text + " " + claim_text),
    ):
        for text in texts:
            extra = numbers_in(text) - numbers
            if extra:
                report.error(f"display 里展示{where}的字段出现了数字 {sorted(extra)}，但它不在{'已核实的结果' if where == '核实结果' else '原说法和核实结果'}里：{text}")
            for name in LATIN_ENTITY.findall(text):
                if len(name) > 1 and name not in text_pool:
                    report.error(f"display 里的外文专名「{name}」在{'已核实的结果' if where == '核实结果' else '原说法和核实结果'}里没出现过：{text}")
            for name in zh_orgs(text):
                if name not in text_pool:
                    report.error(f"display 里的机构名「{name}」在{'已核实的结果' if where == '核实结果' else '原说法和核实结果'}里没出现过：{text}")


def check_quote_original(display: dict, verified_text: str, report: Report) -> None:
    """quote_pair.original 必须逐字出自证据：按省略号切段，每段都要在核查结果里找得到。"""
    pair = display.get("quote_pair")
    if not isinstance(pair, dict) or not pair.get("original"):
        return

    def norm(s: str) -> str:
        s = s.replace("\\", "")
        # 媒体转录用方括号补字（"give [himself] the opportunity"），括号本身不算原话
        s = re.sub(r"[‘’“”\"'\[\]]", "", s)
        return re.sub(r"\s+", " ", s).strip().lower()

    pool = norm(verified_text)
    for segment in re.split(r"\.\.\.|…", str(pair["original"])):
        piece = norm(segment).strip(" .,")
        if len(piece) >= 8 and piece not in pool:
            report.error(f"quote_pair.original 的这一段在证据和英文源里找不到原文：{segment.strip()}"
                         "（原文里不相连的几句，中间用 ... 隔开；每一段都要逐字出现在 excerpt 或证据 note 里）")


def verified_part(data: dict) -> dict:
    points = [{"finding": p.get("finding")} for p in data.get("points") or [] if isinstance(p, dict)]
    english = data.get("english_source") if isinstance(data.get("english_source"), dict) else {}
    english = {k: v for k, v in english.items() if k in ("who", "excerpt")}
    claim_source = data.get("claim_source") if isinstance(data.get("claim_source"), dict) else {}
    return {
        "points": points,
        "evidence": data.get("evidence"),
        "english": english,
        "earliest_source": data.get("earliest_source"),
        "claim_source": {k: claim_source.get(k) for k in ("publisher", "published")},
        "timeline": data.get("timeline"),
        "event": data.get("event"),
    }


def display_fields(display: dict) -> tuple[list[str], list[str]]:
    """把 display 拆成"展示核实结果"和"展示原说法"两组文字。cutoff 和阶梯里的日期是时间标记，不查。"""
    truth, claim_side = [], []
    for key, value in display.items():
        if key in ("layout", "stamp", "cutoff", "publishable", "review_reason"):
            continue
        if key == "claim_short":
            claim_side += strings_in(value)
        elif key == "number" and isinstance(value, dict):
            claim_side += strings_in(value.get("claimed"))
            truth += strings_in({k: v for k, v in value.items() if k != "claimed"})
        elif key == "quote_pair" and isinstance(value, dict):
            # relayed 是原说法；original 另由 check_quote_original 逐段比对证据，不在这里查专名
            claim_side += strings_in(value.get("relayed"))
            truth += strings_in({k: v for k, v in value.items() if k not in ("relayed", "original")})
        elif key == "ladder" and isinstance(value, dict):
            truth += strings_in(value.get("subject"))
            truth += [s.get("note") for s in value.get("history") or [] if isinstance(s, dict) and s.get("note")]
        else:
            truth += strings_in(value)
    return truth, claim_side


def zh_orgs(text: str) -> list[str]:
    """中文机构名（启发式）。"美联社等媒体"这类先按"等、和、与、及、、"切开，只看最后一段。"""
    names = []
    for raw in ZH_ORG.findall(text):
        name = re.split(r"[等和与及、]", raw)[-1]
        if len(name) < 3 or GENERIC_ORG.search(name) or name in ("媒体", "机构", "网站", "频道"):
            continue
        names.append(name)
    return names


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    failed = False
    for name in sys.argv[1:]:
        path = Path(name)
        report = Report()
        try:
            data = load(path)
        except Exception as err:  # noqa: BLE001 — 解析失败要报给用户，不是程序 bug
            print(f"✗ {path}：解析失败：{err}")
            failed = True
            continue
        if not isinstance(data, dict):
            print(f"✗ {path}：顶层应该是映射")
            failed = True
            continue
        check_core(data, report)
        check_display(data, report)
        mark = "✗" if report.errors else "✓"
        print(f"{mark} {path}：{len(report.errors)} 个错误，{len(report.warnings)} 个提醒")
        for message in report.errors:
            print(f"   错误：{message}")
        for message in report.warnings:
            print(f"   提醒：{message}")
        failed = failed or bool(report.errors)
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()

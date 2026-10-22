"""生成盲审包：给独立复核者看的材料，**去掉作者的判定和结论**。

复核流程见 references/review.md。盲审包里有：原始说法与出处、每个核查点的问题、
每个点作者用到了哪些证据、证据清单（链接、发布者、日期、取证层级）、英文源链接。
没有：各点判定、finding、overall、overall_scope、display、notes、missing、timeline。

证据的 note 默认也去掉——它常常带着作者的解读（"只算一个信源""证明了……"），
会把复核者往作者的结论上带。确实需要定位线索时用 --with-notes。

用法：
    python review_packet.py 状态块.yaml > packet.md
    python review_packet.py 状态块.yaml --with-notes > packet.md
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def load(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() == ".json":
        return json.loads(text)
    try:
        import yaml
    except ImportError:
        sys.exit("读 YAML 需要 PyYAML（pip install pyyaml）；或者把状态块存成 .json")
    return yaml.safe_load(text)


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    parser.add_argument("status_block")
    parser.add_argument("--with-notes", action="store_true", help="附上证据的 note（可能带作者解读，慎用）")
    args = parser.parse_args()
    data = load(Path(args.status_block))

    out = ["# 盲审包", ""]
    out.append("你是独立复核者。先读 skill 的 SKILL.md（铁律、判定标签、L3 规则）和 references/review.md，"
               "再按下面的材料**自己**判断每个核查点。这里刻意没有给出作者的判定。")
    out += ["", "## 原始说法", "", f"> {str(data.get('claim', '')).strip()}", ""]
    src = data.get("claim_source") or {}
    if src:
        out.append(f"出处：{src.get('publisher', '')}，{src.get('published', '')}，{src.get('url', '')}")
    out.append(f"核查截止时间：{data.get('checked_at', '')}")

    english = data.get("english_source") or {}
    if english:
        out += ["", "## 作者找到的英文源（链接，自己打开核对）", ""]
        out.append(f"- 链接：{english.get('url') or '（作者没有找到上游）'}")
        if english.get("who"):
            out.append(f"- 来源：{english['who']}")
        if english.get("searched"):
            out.append("- 作者搜过的检索词：" + "；".join(map(str, english["searched"])))

    out += ["", "## 核查点", ""]
    out.append("每个点都写明了核的是归因（某人是否这样说过）还是事实（这件事是否属实）；请按同样的口径判。"
               "**判的是\"被核查的断言\"这句话本身**：断言的措辞比原话强就是措辞放大，哪怕问题问得更宽。")
    out.append("")
    for i, point in enumerate(data.get("points") or [], 1):
        refs = "、".join(point.get("evidence") or []) or "（无）"
        scope = {"fact": "事实", "attribution": "归因"}.get(point.get("scope"), "未标明")
        out.append(f"{i}. 【{scope}】{point.get('question', '')}")
        if point.get("asserts"):
            out.append(f"   被核查的断言：{point['asserts']}")
        out.append(f"   作者引用的证据：{refs}")
        if point.get("searched"):
            out.append("   作者为此搜过：" + "；".join(map(str, point["searched"])))
    if data.get("out_of_scope"):
        out += ["", "## 作者声明不核的断言（不必再提为漏点，除非你认为它不该被排除）", ""]
        for item in data["out_of_scope"]:
            if isinstance(item, dict):
                out.append(f"- {item.get('asserts', '')}——不核的原因：{item.get('reason', '')}")
            else:
                out.append(f"- {item}")

    out += ["", "## 证据清单", ""]
    for eid, item in (data.get("evidence") or {}).items():
        if not isinstance(item, dict):
            continue
        line = (f"- **{eid}**：{item.get('publisher', '')}｜{item.get('published', '')}｜"
                f"{item.get('kind', '')}｜取证层级 {item.get('access', '')}｜{item.get('url') or '（无链接）'}")
        out.append(line)
        extra = [f"位置：{item['locator']}" if item.get("locator") else "",
                 f"取得方式：{item['access_method']}" if item.get("access_method") else "",
                 f"作者取得时间：{item['retrieved_at']}" if item.get("retrieved_at") else ""]
        extra = [e for e in extra if e]
        if extra:
            out.append("    " + "｜".join(extra) + "（作者取得的层级不代表你能取得，打不开要记进 failed_opens）")
        if args.with_notes and item.get("note"):
            out.append(f"    作者摘录（仅供定位，自己打开核对）：{item['note']}")

    out += ["", "## 交卷格式", "", "把结果写成 YAML（字段说明见 references/review.md）：", "", "```yaml",
            "reviewer: \"\"", "reviewed_at: \"\"", "points:"]
    for i, _ in enumerate(data.get("points") or [], 1):
        out += [f"  - index: {i}", "    verdict: ", "    reason: \"\"", "    opened: []"]
    out += ["additional_evidence: []   # 你自己补找的证据：每条写 id（例如 r1）、url、publisher、它支撑哪个点",
            "failed_opens: []          # 打不开的证据编号及原因",
            "missing_points: []        # 每条写 item 和 blocking（会改变核心结论为 true，外围细节为 false）",
            "notes: \"\"", "```"]
    print("\n".join(out))


if __name__ == "__main__":
    main()

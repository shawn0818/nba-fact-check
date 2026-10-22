"""比对作者的判定和独立复核者的盲审结果，并把结论写进状态块的 review 字段。

复核流程见 references/review.md。规则：
  - 每个核查点双方判定都一致、复核者没有提出漏点 → review.status = passed
  - 任何一点不一致、复核者漏判某点、或者提出了漏点 → review.status = failed，note 里列出分歧
不取平均、不投票：分歧要回到证据本身解决，作者修改后重新复核。

通过之后，复核者还要人工看一遍 display 段的语义（脚本查不了），有问题手动改成 failed。

用法：
    python review_compare.py 状态块.yaml 复核结果.yaml                      # 只打印比对
    python review_compare.py 状态块.yaml 复核结果.yaml --write 复核后.yaml  # 写出带 review 的新状态块
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

VERDICTS = {"准确", "措辞放大", "误导", "虚假", "查无出处", "无法证实", "不可核查"}


def load(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() == ".json":
        return json.loads(text)
    try:
        import yaml
    except ImportError:
        sys.exit("读 YAML 需要 PyYAML（pip install pyyaml）；或者存成 .json")
    return yaml.safe_load(text)


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    parser.add_argument("status_block")
    parser.add_argument("review_result")
    parser.add_argument("--write", help="把带 review 字段的状态块写到这个文件（不覆盖原文件）")
    args = parser.parse_args()

    data = load(Path(args.status_block))
    result = load(Path(args.review_result))
    author_points = data.get("points") or []
    reviewer_points = {int(p.get("index", 0)): p for p in result.get("points") or [] if isinstance(p, dict)}

    rows, problems = [], []
    for i, point in enumerate(author_points, 1):
        mine = point.get("verdict")
        theirs_item = reviewer_points.get(i)
        theirs = theirs_item.get("verdict") if theirs_item else None
        if theirs is None:
            agree = False
            problems.append(f"第 {i} 点复核者没有给判定")
        elif theirs not in VERDICTS:
            agree = False
            problems.append(f"第 {i} 点复核者的判定「{theirs}」不在标签表里")
        else:
            agree = theirs == mine
            if not agree:
                problems.append(f"第 {i} 点：作者「{mine}」，复核者「{theirs}」——{(theirs_item or {}).get('reason', '')}")
        # 作者这一点本来就没有证据（例如否定性说法"没立案"），复核者无从打开，不要求 opened
        if theirs_item and point.get("evidence") and not theirs_item.get("opened"):
            problems.append(f"第 {i} 点复核者没有记录自己打开过哪些证据（opened 为空）")
        rows.append({"index": i, "author": mine, "reviewer": theirs, "agree": agree})

    extra = sorted(set(reviewer_points) - set(range(1, len(author_points) + 1)))
    if extra:
        problems.append(f"复核结果里有状态块中不存在的核查点编号：{extra}")
    follow_ups = []
    for missing in result.get("missing_points") or []:
        # 漏点分两种：会改变核心结论的（阻断）和外围的（非阻断）。只写字符串的按阻断处理，宁严勿松。
        if isinstance(missing, dict) and missing.get("blocking") is False:
            follow_ups.append(str(missing.get("item", "")))
        else:
            text = missing.get("item", missing) if isinstance(missing, dict) else missing
            problems.append(f"复核者认为漏了或问偏了、且会影响核心结论的核查点：{text}")

    status = "passed" if not problems else "failed"
    extras = result.get("additional_evidence") or []
    failed = result.get("failed_opens") or []
    print(f"{'核查点':<6}{'作者':<10}{'复核者':<10}一致")
    for r in rows:
        print(f"{r['index']:<8}{str(r['author']):<10}{str(r['reviewer']):<10}{'✓' if r['agree'] else '✗'}")
    print()
    if extras:
        print(f"复核者补找了 {len(extras)} 条证据（作者可以考虑并入状态块）：")
        for e in extras:
            print(f"  - {e.get('id', '')} {e.get('publisher', '')} {e.get('url', '')} → 支撑第 {e.get('supports', '')} 点")
    if failed:
        print("复核者打不开的证据：")
        for f in failed:
            print(f"  - {f}")
    if follow_ups:
        print("非阻断的漏点（不影响本次通过，作者后续补核或写进 out_of_scope）：")
        for f in follow_ups:
            print(f"  - {f}")
    if extras or failed or follow_ups:
        print()
    if problems:
        print("复核未通过：")
        for p in problems:
            print(f"  - {p}")
    else:
        print("各点判定一致，没有阻断性漏点：复核通过。别忘了再人工看一遍 display 段的措辞（见 references/review.md）。")

    if args.write:
        try:
            import yaml
        except ImportError:
            sys.exit("写 YAML 需要 PyYAML")
        data["review"] = {
            "status": status,
            "by": result.get("reviewer") or None,
            "at": str(result.get("reviewed_at")) if result.get("reviewed_at") else None,
            "note": ("；".join(problems) if problems else "各点判定一致，没有阻断性漏点")
                    + (("；非阻断漏点：" + "；".join(follow_ups)) if follow_ups else ""),
            "points": rows,
        }
        Path(args.write).write_text(
            yaml.safe_dump(data, allow_unicode=True, sort_keys=False, width=1000), encoding="utf-8"
        )
        print(f"\n已写出：{args.write}（原文件未改动）")
    sys.exit(0 if status == "passed" else 1)


if __name__ == "__main__":
    main()

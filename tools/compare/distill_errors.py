#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
错题集蒸馏器

读取 logs/error_notebook.jsonl，输出：
  - logs/error_guide.md          供校对 LLM 注入 system prompt 的认知地图
  - logs/few_shot_examples.jsonl 有代表性的上下文示例，供 few-shot 用

设计原则：
  不输出强制替换规则。只告诉 LLM「Qwen 在哪些地方容易走神、走神时的样子」，
  结合具体上下文，由 LLM 自行判断是否需要修改。
"""

import json
import re
from collections import Counter, defaultdict
from pathlib import Path

# ── 配置 ────────────────────────────────────────────────────────────────────
_PROJECT_ROOT = Path(__file__).parent.parent.parent
INPUT_JSONL   = _PROJECT_ROOT / "logs" / "error_notebook.jsonl"
OUTPUT_GUIDE  = _PROJECT_ROOT / "logs" / "error_guide.md"
OUTPUT_SHOTS  = _PROJECT_ROOT / "logs" / "few_shot_examples.jsonl"

# 出现 N 次以上才进入「高频混淆」列表
HIGH_FREQ_THRESHOLD = 5
# 每个高频 pair 最多保留几条上下文示例
MAX_EXAMPLES_PER_PAIR = 3
# few-shot 文件里最多保留多少条
MAX_FEW_SHOTS = 60
# ────────────────────────────────────────────────────────────────────────────


def load_entries(path: Path) -> list[dict]:
    entries = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                entries.append(json.loads(line))
    return entries


def build_pair_index(entries: list[dict]) -> dict:
    """
    返回 {(qwen, human): {"count": N, "category": str, "examples": [...]}}
    """
    index: dict[tuple, dict] = {}
    for e in entries:
        key = (e["qwen"], e["human"])
        if key not in index:
            index[key] = {
                "count":    0,
                "category": e["category"],
                "examples": [],
            }
        index[key]["count"] += 1
        if len(index[key]["examples"]) < MAX_EXAMPLES_PER_PAIR:
            index[key]["examples"].append({
                "before": e["before"],
                "after":  e["after"],
                "video_date": e.get("video_date", ""),
            })
    return index


def is_symmetric(q: str, h: str, index: dict) -> bool:
    """检查 (q→h) 和 (h→q) 是否都高频出现（双向混淆）"""
    reverse = index.get((h, q), {}).get("count", 0)
    forward = index.get((q, h), {}).get("count", 0)
    return reverse >= HIGH_FREQ_THRESHOLD and forward >= HIGH_FREQ_THRESHOLD


def format_example(q: str, h: str, ex: dict) -> str:
    before = ex["before"].strip()
    after  = ex["after"].strip()
    return (
        f"  Qwen: …{before}[{q}]{after}…\n"
        f"  精校: …{before}[{h}]{after}…"
    )


def build_guide(entries: list[dict], index: dict) -> str:
    lines = []
    lines.append("# Qwen3-ASR 中文转录常见混淆模式")
    lines.append("")
    lines.append("本文档总结 Qwen3-ASR 在本频道内容上的系统性识别弱点。")
    lines.append("**校对时请结合上下文判断，不要机械替换。**")
    lines.append(f"（基于 {len(entries)} 条对比样本，{len(index)} 个不同错误对）")
    lines.append("")

    # ── 按类别分组，只保留高频 pair ─────────────────────────────────────────
    by_cat: dict[str, list] = defaultdict(list)
    for (q, h), info in sorted(index.items(), key=lambda x: -x[1]["count"]):
        if info["count"] >= HIGH_FREQ_THRESHOLD:
            by_cat[info["category"]].append((q, h, info))

    cat_labels = {
        "single_char_cn": "## 1. 单字同音/近音混淆（中文）",
        "multi_char_cn":  "## 2. 多字中文混淆",
        "english_word":   "## 3. 英文词拼写偏差",
        "cn_en_mix":      "## 4. 中英文混排混淆",
        "other":          "## 5. 其他",
    }

    for cat_key, cat_title in cat_labels.items():
        pairs = by_cat.get(cat_key, [])
        if not pairs:
            continue
        lines.append(cat_title)
        lines.append("")

        for q, h, info in pairs[:30]:   # 每类最多展示30条
            sym = is_symmetric(q, h, index)
            reverse_cnt = index.get((h, q), {}).get("count", 0)

            # 频率标注
            if sym:
                freq_note = f"（双向混淆：{q}→{h} ×{info['count']}，{h}→{q} ×{reverse_cnt}）"
            else:
                freq_note = f"（×{info['count']}）"

            lines.append(f"### `{q}` ↔ `{h}` {freq_note}")

            if sym:
                lines.append(f"> Qwen 在 `{q}` 和 `{h}` 之间随机浮动，需凭上下文判断正确形式。")
            else:
                lines.append(f"> Qwen 倾向于把 `{h}` 识别成 `{q}`，但不总是如此，请看上下文确认。")

            for ex in info["examples"][:2]:
                lines.append(format_example(q, h, ex))

            lines.append("")

    # ── 频道专有词汇提示（从 cn_en_mix / multi_char_cn 里挑出明显的专有名词）──
    lines.append("## 6. 频道专有名词提示")
    lines.append("")
    lines.append(
        "以下词汇在本频道高频出现，Qwen 有时识别偏差。"
        "**请务必结合上下文判断**——同一个发音可能对应不同含义。"
    )
    lines.append("")

    # 挑出疑似专有名词：qwen 或 human 含大写字母，或 human 是已知频道词汇
    proper_hint_pairs = []
    for (q, h), info in sorted(index.items(), key=lambda x: -x[1]["count"]):
        if info["count"] < 3:
            continue
        has_upper = bool(re.search(r'[A-Z]', q + h))
        long_cn   = len(q) >= 2 and len(h) >= 2 and bool(re.search(r'[\u4e00-\u9fff]', q + h))
        if has_upper or (long_cn and info["category"] == "multi_char_cn"):
            proper_hint_pairs.append((q, h, info))

    for q, h, info in proper_hint_pairs[:20]:
        ex = info["examples"][0] if info["examples"] else {}
        hint = format_example(q, h, ex) if ex else ""
        lines.append(f"- `{q}` / `{h}`（出现 {info['count']} 次）")
        if hint:
            lines.append(hint)
        lines.append("")

    lines.append("---")
    lines.append("*本文档由 `tools/compare/distill_errors.py` 自动生成，可定期重新生成。*")
    return "\n".join(lines)


def build_few_shots(entries: list[dict], index: dict) -> list[dict]:
    """
    挑选最有代表性的 (qwen_chunk, human_chunk) 对作为 few-shot 示例。
    策略：高频 pair、上下文明确能看出差别的、类别均衡。
    """
    # 按 pair 频率降序，每类各取若干条
    cat_quota = {
        "single_char_cn": 15,
        "multi_char_cn":  15,
        "english_word":   10,
        "cn_en_mix":      10,
        "other":          10,
    }
    cat_taken: Counter = Counter()
    seen_pairs: set = set()
    shots = []

    # 先按频率排序
    sorted_entries = sorted(
        entries,
        key=lambda e: index.get((e["qwen"], e["human"]), {}).get("count", 0),
        reverse=True,
    )

    for e in sorted_entries:
        cat = e["category"]
        if cat_taken[cat] >= cat_quota.get(cat, 10):
            continue
        pair = (e["qwen"], e["human"])
        if pair in seen_pairs:
            continue
        # 确保上下文不为空（有上下文才能体现"因地制宜"）
        if len(e.get("before", "")) + len(e.get("after", "")) < 8:
            continue
        seen_pairs.add(pair)
        cat_taken[cat] += 1
        shots.append({
            "category":   cat,
            "pair_count": index[pair]["count"],
            "qwen":       e["qwen"],
            "human":      e["human"],
            "before":     e["before"],
            "after":      e["after"],
            "video_date": e.get("video_date", ""),
        })
        if sum(cat_taken.values()) >= MAX_FEW_SHOTS:
            break

    return shots


def build_compact_guide(index: dict) -> str:
    """
    生成注入 LLM prompt 用的紧凑版指南（目标 400-600 tokens）。
    分类是人工设计的语义分组，count 从数据自动填充。
    """
    def cnt(q: str, h: str) -> int:
        return index.get((q, h), {}).get("count", 0)

    lines = [
        "# Qwen3-ASR 转录混淆速查（本频道）",
        "校对时结合上下文判断，不要机械替换。",
        "",
        "## 代词/指代",
        f"- 它/他/她（×{cnt('它','他')+cnt('他','它')+cnt('他','她')+cnt('她','他')}）"
        "：它=物/概念，他=男性人物，她=女性；Qwen 随机浮动",
        "",
        "## 近音虚词（需凭语法/逻辑判断）",
        f"- 在/再（×{cnt('在','再')+cnt('再','在')}）：在=存在/正在，再=再次/继续",
        f"- 这/就（×{cnt('这','就')+cnt('就','这')}）：这=指示词，就=副词/强调",
        f"- 边/面（×{cnt('边','面')+cnt('面','边')}）：里边/里面，看说话人习惯",
        f"- 嘛/吗（×{cnt('嘛','吗')+cnt('吗','嘛')}）：嘛=陈述语气，吗=真实疑问",
        f"- 那/这（×{cnt('那','这')+cnt('这','那')}）：那=远指，这=近指",
        f"- 说/是（×{cnt('说','是')+cnt('是','说')}）：说=表述动作，是=判断系词",
        f"- 您/你（×{cnt('您','你')+cnt('你','您')}）：您=敬称，看场合正式度",
        "",
        "## 量词与逻辑词",
        f"- 些/期（×{cnt('些','期')}）：「这些视频」=复数，「这期视频」=本集",
        f"- 当然/但是（×{cnt('当然','但是')+cnt('但是','当然')}）：当然=顺承，但是=转折",
        f"- 作为/做（×{cnt('作为','做')+cnt('做','作为')}）：作为X=角色，做X=动作",
        "",
        "## 频道专有词汇（软提示，必须凭上下文）",
        f"- 亚哥/鸭哥（×{cnt('亚','鸭')}）：本频道合作者「鸭哥」；后接「哥」时大概率是「鸭」",
        f"- 麦克/卖课（×{cnt('麦克','卖课')}）：麦克风 vs 课程产品，看语境",
        f"- 欢迎/会员（×{cnt('欢迎','会员')}）：开场语；看是公开还是会员视频",
        f"- Static/Statsig（×{cnt('c','g')}）：英文品牌名，c↔g 混淆",
        "",
        "## 英文拼写偏差",
        "- 英文单词中 a/e/i/o 容易互换（如 incumbents→incumbants）",
        "- 词尾截断（如 fine tune→fine tuning，portraying→portraining）",
        "- 遇英文拼写异常，推断发音最接近的正确单词",
    ]
    return "\n".join(lines)


def build_candidates_dict(index: dict, min_count: int = 5) -> dict:
    """
    构建候选词典，用于扫描新 SRT 文件中的疑似混淆位置。
    格式: {qwen_form: {"alternatives": [...], "count": N, "hint": "..."}}
    """
    # 手工定义的高价值候选（单字或短语）
    candidates = {}

    # 从 index 中提取高频、有意义的单向候选
    skip_trivial = frozenset("呃啊嗯哦哎耶唉哇哈咦哟喂噢哼嘿的地得了嘛吧呢哈么")

    for (q, h), info in sorted(index.items(), key=lambda x: -x[1]["count"]):
        if info["count"] < min_count:
            continue
        # 跳过两边都是纯语气/助词的
        if all(c in skip_trivial for c in q) and all(c in skip_trivial for c in h):
            continue
        # 跳过纯英文单字母（e/a 等，这类上下文太短没价值）
        if len(q) == 1 and q.isascii() and q.isalpha():
            continue
        if q not in candidates:
            candidates[q] = {"alternatives": [], "count": 0, "hint": ""}
        candidates[q]["alternatives"].append(h)
        candidates[q]["count"] = max(candidates[q]["count"], info["count"])

    # 补充手工规则（不在统计里的高置信度条目）
    manual = {
        "亚哥":  {"alternatives": ["鸭哥"],  "count": 21, "hint": "本频道合作者鸭哥"},
        "麦克":  {"alternatives": ["卖课"],  "count": 6,  "hint": "麦克风 vs 卖课"},
        "欢迎":  {"alternatives": ["会员"],  "count": 11, "hint": "开场语类型"},
        "振兴":  {"alternatives": ["震惊"],  "count": 0,  "hint": "同音字"},
        "花旗":  {"alternatives": ["话题"],  "count": 0,  "hint": "同音词"},
    }
    for k, v in manual.items():
        if k not in candidates:
            candidates[k] = v

    return candidates


OUTPUT_COMPACT = _PROJECT_ROOT / "logs" / "error_guide_compact.txt"
OUTPUT_CANDIDATES = _PROJECT_ROOT / "logs" / "correction_candidates.json"


def run() -> None:
    print(f"读取 {INPUT_JSONL} ...")
    entries = load_entries(INPUT_JSONL)
    print(f"共 {len(entries)} 条错误记录")

    index = build_pair_index(entries)
    high_freq = sum(1 for v in index.values() if v["count"] >= HIGH_FREQ_THRESHOLD)
    print(f"不同错误对: {len(index)}，高频(≥{HIGH_FREQ_THRESHOLD}次): {high_freq}")

    # 全量 guide
    guide = build_guide(entries, index)
    OUTPUT_GUIDE.write_text(guide, encoding="utf-8")
    print(f"error_guide_full 已写入: {OUTPUT_GUIDE}  ({len(guide)} 字符)")

    # 紧凑版 guide（用于 LLM prompt）
    compact = build_compact_guide(index)
    OUTPUT_COMPACT.write_text(compact, encoding="utf-8")
    print(f"error_guide_compact 已写入: {OUTPUT_COMPACT}  ({len(compact)} 字符, ~{len(compact)//4} tokens)")

    # 候选词典（用于候选扫描）
    candidates = build_candidates_dict(index)
    OUTPUT_CANDIDATES.write_text(
        json.dumps(candidates, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"correction_candidates 已写入: {OUTPUT_CANDIDATES}  ({len(candidates)} 个条目)")

    # few-shot examples
    shots = build_few_shots(entries, index)
    with open(OUTPUT_SHOTS, "w", encoding="utf-8") as f:
        for s in shots:
            f.write(json.dumps(s, ensure_ascii=False) + "\n")
    print(f"few_shot_examples 已写入: {OUTPUT_SHOTS}  ({len(shots)} 条)")


if __name__ == "__main__":
    run()

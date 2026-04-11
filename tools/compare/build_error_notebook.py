#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
全量错题本生成器

扫描所有有 .qwen.srt + 精校字幕的视频，提取 Qwen 转录错误对，
汇总写入 logs/error_notebook.jsonl。

仅处理上传日期 >= 2025-01-01 的视频（视频文件夹命名格式 YYYYMMDD_...）。
之前的视频字幕质量无法保证，不用于训练。

用法：
  python3 tools/compare/build_error_notebook.py

输出：
  logs/error_notebook.jsonl     每行一个错误 pair
  logs/error_notebook_stats.json  频率统计 + 分类
"""

import json
import re
import sys
import difflib
from collections import Counter
from pathlib import Path
from datetime import datetime

# ── 配置 ────────────────────────────────────────────────────────────────────
_PROJECT_ROOT = Path(__file__).parent.parent.parent

DIRS = [
    _PROJECT_ROOT / "archive" / "有人工字幕",
    _PROJECT_ROOT / "archive" / "会员视频",
]
MIN_DATE      = "20250101"   # 只用 2025 年以后的精校字幕
HUMAN_LANGS   = ("zh", "zh-Hans", "zh-Hant", "en-zh", "en-orig")
CONTEXT_CHARS = 20
OUTPUT_JSONL  = _PROJECT_ROOT / "logs" / "error_notebook.jsonl"
OUTPUT_STATS  = _PROJECT_ROOT / "logs" / "error_notebook_stats.json"
# ────────────────────────────────────────────────────────────────────────────

# ── 归一化/过滤工具（与 compare_single.py 保持一致）────────────────────────
_PUNCT   = re.compile(
    r'[\s，。！？、：；\u201c\u201d\u2018\u2019（）【】《》…—·「」.,!?;:()\[\]"\'\-～~]'
)
_CN_NUM  = str.maketrans("零一二三四五六七八九", "0123456789")
_NUMERIC = re.compile(r'^[\d零一二三四五六七八九十百千万亿两x倍分之/]+$', re.IGNORECASE)
_FILLER  = frozenset("呃啊嗯哦哎耶唉哇哈咦哟喂噢哼嘿")
_PARTICLE = frozenset("的地得了嘛吧呢哈么")
_EMOJI   = re.compile(
    "[\U00010000-\U0010ffff\U0001F300-\U0001F9FF\u2600-\u27BF\uFE0F]",
    flags=re.UNICODE,
)


def _is_trivial(s: str) -> bool:
    stripped = s.strip()
    return not stripped or all(c in _FILLER or c in _PARTICLE for c in stripped)


def _is_numeric_like(s: str) -> bool:
    return bool(_NUMERIC.match(s))


def build_pos_map(raw: str) -> tuple[str, list[int]]:
    cleaned, pos_map = [], []
    for i, ch in enumerate(raw):
        if _PUNCT.match(ch):
            continue
        cleaned.append(ch.translate(_CN_NUM).lower())
        pos_map.append(i)
    return "".join(cleaned), pos_map


def parse_srt(path: Path) -> str:
    content = path.read_text(encoding="utf-8")
    blocks  = re.split(r'\n{2,}', content.strip())
    parts   = []
    for block in blocks:
        lines = block.strip().splitlines()
        text_lines = [l for l in lines if not l.strip().isdigit() and "-->" not in l]
        parts.append("".join(text_lines))
    return "".join(parts)


def extract_diffs(q_raw: str, h_raw: str) -> list[dict]:
    q_clean, q_map = build_pos_map(q_raw)
    h_clean, h_map = build_pos_map(h_raw)

    if not q_map or not h_map:
        return []

    matcher = difflib.SequenceMatcher(None, q_clean, h_clean, autojunk=False)
    results = []

    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag != "replace":
            continue

        q_frag = q_clean[i1:i2]
        h_frag = h_clean[j1:j2]

        if not q_frag or not h_frag:
            continue
        if _is_numeric_like(q_frag) and _is_numeric_like(h_frag):
            continue
        if _is_trivial(q_frag) and _is_trivial(h_frag):
            continue

        big  = max(len(q_frag), len(h_frag))
        small = min(len(q_frag), len(h_frag))
        if small == 0 or big / small > 4:
            continue

        raw_i1 = q_map[i1]
        raw_i2 = q_map[i2 - 1] + 1
        raw_j1 = h_map[j1]
        raw_j2 = h_map[j2 - 1] + 1

        ctx_start = q_map[max(0, i1 - CONTEXT_CHARS)]
        ctx_end   = q_map[min(len(q_map) - 1, i2 + CONTEXT_CHARS - 1)] + 1

        q_display = q_raw[raw_i1:raw_i2]
        h_display = h_raw[raw_j1:raw_j2]

        q_no_emoji = _EMOJI.sub("", q_display).strip()
        h_no_emoji = _EMOJI.sub("", h_display).strip()
        if q_no_emoji == h_no_emoji:
            continue

        results.append({
            "qwen":    q_display,
            "human":   h_display,
            "before":  q_raw[ctx_start:raw_i1],
            "after":   q_raw[raw_i2:ctx_end],
            "pair_key": (q_no_emoji, h_no_emoji),
        })

    # 同视频内去重
    seen: set = set()
    deduped = []
    for r in results:
        if r["pair_key"] not in seen:
            seen.add(r["pair_key"])
            deduped.append(r)
    return deduped


# ── 扫描 + 提取 ──────────────────────────────────────────────────────────────

def find_video_date(folder_name: str) -> str | None:
    """从文件夹名 YYYYMMDD_xxx 提取日期字符串"""
    m = re.match(r'^(\d{8})_', folder_name)
    return m.group(1) if m else None


def find_pairs(dirs: list[Path]) -> list[tuple[Path, Path, str, str]]:
    """返回 (qwen_srt, human_srt, video_id, video_date) 列表"""
    pairs = []
    for base in dirs:
        if not base.exists():
            continue
        for video_dir in sorted(base.iterdir()):
            if not video_dir.is_dir():
                continue
            date = find_video_date(video_dir.name)
            if date is None or date < MIN_DATE:
                continue

            qwen_srts = list(video_dir.glob("*.qwen.srt"))
            if not qwen_srts:
                continue
            qwen_path = qwen_srts[0]
            stem = qwen_path.stem.removesuffix(".qwen")

            human_path = None
            for lang in HUMAN_LANGS:
                cand = video_dir / f"{stem}.{lang}.srt"
                if cand.exists():
                    human_path = cand
                    break
            if human_path is None:
                continue

            # 从文件夹名提取 video_id（最后一个 _ 后面的部分）
            parts = video_dir.name.rsplit("_", 1)
            video_id = parts[-1] if len(parts) == 2 else video_dir.name

            pairs.append((qwen_path, human_path, video_id, date))
    return pairs


def categorize(q: str, h: str) -> str:
    """简单分类错误类型"""
    q_has_cn  = bool(re.search(r'[\u4e00-\u9fff]', q))
    h_has_cn  = bool(re.search(r'[\u4e00-\u9fff]', h))
    q_has_en  = bool(re.search(r'[a-zA-Z]', q))
    h_has_en  = bool(re.search(r'[a-zA-Z]', h))

    if q_has_en and h_has_en:
        return "english_word"
    if q_has_cn and h_has_cn and not q_has_en and not h_has_en:
        if len(q) == 1 and len(h) == 1:
            return "single_char_cn"
        return "multi_char_cn"
    if (q_has_cn and h_has_en) or (q_has_en and h_has_cn):
        return "cn_en_mix"
    return "other"


def run() -> None:
    pairs = find_pairs(DIRS)
    print(f"找到视频对: {len(pairs)} 个（日期 >= {MIN_DATE}）")

    OUTPUT_JSONL.parent.mkdir(parents=True, exist_ok=True)

    all_entries    = []
    pair_counter   = Counter()
    category_counter = Counter()
    video_count    = 0
    error_count    = 0

    with open(OUTPUT_JSONL, "w", encoding="utf-8") as f_out:
        for qwen_path, human_path, video_id, date in pairs:
            try:
                q_raw = parse_srt(qwen_path)
                h_raw = parse_srt(human_path)
            except Exception as e:
                print(f"  跳过（读取失败）: {qwen_path.parent.name}  {e}")
                continue

            diffs = extract_diffs(q_raw, h_raw)
            video_count += 1
            error_count += len(diffs)

            for d in diffs:
                cat = categorize(d["qwen"], d["human"])
                entry = {
                    "video_id":   video_id,
                    "video_date": date,
                    "qwen":       d["qwen"],
                    "human":      d["human"],
                    "before":     d["before"],
                    "after":      d["after"],
                    "category":   cat,
                }
                f_out.write(json.dumps(entry, ensure_ascii=False) + "\n")
                pair_counter[(d["qwen"], d["human"])] += 1
                category_counter[cat] += 1

            print(f"  [{date}] {video_id}  →  {len(diffs)} 条差异", flush=True)

    # 写统计文件
    stats = {
        "generated_at": datetime.now().isoformat(),
        "videos_processed": video_count,
        "total_errors": error_count,
        "category_counts": dict(category_counter),
        "top_pairs": [
            {"qwen": q, "human": h, "count": cnt}
            for (q, h), cnt in pair_counter.most_common(100)
        ],
    }
    OUTPUT_STATS.write_text(json.dumps(stats, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"\n{'='*60}")
    print(f"处理视频: {video_count}")
    print(f"总错误对: {error_count}")
    print(f"分类统计:")
    for cat, cnt in category_counter.most_common():
        print(f"  {cat}: {cnt}")
    print(f"\n出现 ≥3 次的高频错误对 (top 20):")
    for (q, h), cnt in pair_counter.most_common(20):
        if cnt >= 2:
            print(f"  「{q}」→「{h}」  ×{cnt}")
    print(f"\n输出: {OUTPUT_JSONL}")
    print(f"统计: {OUTPUT_STATS}")
    print(f"{'='*60}")


if __name__ == "__main__":
    run()

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
校对精确度评估 — 测量每条 correction 的对错

除了 CER（整体误差率）以外，还需要知道：
  - Precision: 我们做的修改中，有多少是正确的？
    correct_change / total_changes
  - Recall: 原始错误中，有多少被修正了？
    correct_change / total_fixable_errors
  - Net CER delta per video (= CER_after - CER_before, 越负越差)

判断每条 correction (O→C) 的对错：
  GOOD:    C 出现在 human_text 且 O 不出现（真正修复了一个错误）
  BAD:     O 出现在 human_text 且 C 不出现（破坏了正确的内容）
  NEUTRAL: 两者都在 human_text，或都不在（无法确定）

为什么这比 CER 更有用：
  CER 度量的是整个文件的字符距离变化，无法区分哪些改动是好的哪些是坏的。
  Precision 能直接指出「改错方向」的比例，驱动 prompt/候选词典的迭代。

用法：
  python3 tools/correct/precision_eval.py
"""

import json
import re
from pathlib import Path
from statistics import mean, median

_PROJECT_ROOT = Path(__file__).parent.parent.parent
DIRS = [
    _PROJECT_ROOT / "archive" / "有人工字幕",
    _PROJECT_ROOT / "archive" / "会员视频",
]
HUMAN_LANGS = ("zh", "zh-Hans", "zh-Hant", "en-zh", "en-orig")
TEST_DATE_FROM = "20260101"

# 格式规范化模式：这类修改几乎总是正确的（中文数字→阿拉伯数字）
_FORMAT_NORM_PAIRS = {
    ("两百", "200"), ("两千", "2000"), ("百分之十", "10%"),
    ("百分之百", "100%"), ("幺幺", "11"),
}

_PUNCT = re.compile(
    r'[\s，。！？、：；\u201c\u201d\u2018\u2019（）【】《》…—·「」.,!?;:()\[\]"\'～~]'
)
_CN_NUM = str.maketrans("零一二三四五六七八九", "0123456789")


def normalize(s: str) -> str:
    return _PUNCT.sub("", s).translate(_CN_NUM).lower()


def parse_srt_text(path: Path) -> str:
    content = path.read_text(encoding="utf-8")
    blocks = re.split(r'\n{2,}', content.strip())
    parts = []
    for block in blocks:
        lines = block.strip().splitlines()
        text_lines = [l for l in lines if not l.strip().isdigit() and "-->" not in l]
        parts.append("".join(text_lines))
    return normalize("".join(parts))


def detect_qwen_lang(path: Path) -> str:
    try:
        sample = path.read_text(encoding="utf-8")[:800]
    except Exception:
        return "zh"
    cn_chars = sum(1 for c in sample if '\u4e00' <= c <= '\u9fff')
    en_chars  = sum(1 for c in sample if c.isascii() and c.isalpha())
    return "en" if en_chars > cn_chars * 2 else "zh"


def find_video_date(name: str):
    m = re.match(r'^(\d{8})_', name)
    return m.group(1) if m else None


def find_test_videos():
    videos = []
    for base in DIRS:
        if not base.exists():
            continue
        for vdir in sorted(base.iterdir()):
            if not vdir.is_dir():
                continue
            date = find_video_date(vdir.name)
            if date is None or date < TEST_DATE_FROM:
                continue
            qwen_srts = list(vdir.glob("*.qwen.srt"))
            if not qwen_srts:
                continue
            qwen_path = qwen_srts[0]
            stem = qwen_path.stem.removesuffix(".qwen")

            qwen_lang = detect_qwen_lang(qwen_path)
            ref_langs = ("en-orig", "en") if qwen_lang == "en" else HUMAN_LANGS

            human_path = None
            for lang in ref_langs:
                cand = vdir / f"{stem}.{lang}.srt"
                if cand.exists():
                    human_path = cand
                    break
            if human_path is None:
                continue

            corrected_path = vdir / f"{stem}.corrected.srt"
            if not corrected_path.exists():
                continue

            videos.append({
                "video_id": vdir.name.rsplit("_", 1)[-1],
                "date":     date,
                "qwen":     qwen_path,
                "human":    human_path,
                "corrected": corrected_path,
            })
    return videos


def diff_corrections(qwen_text: str, corrected_text: str) -> list[tuple[str, str]]:
    """
    比较 qwen 和 corrected 文本，提取所有 (original, corrected) 对。
    逐块（SRT block）比较，返回差异片段对。
    """
    diffs = []
    if qwen_text == corrected_text:
        return diffs

    # 找最长公共子序列中的差异块
    import difflib
    sq = difflib.SequenceMatcher(None, qwen_text, corrected_text, autojunk=False)
    for tag, i1, i2, j1, j2 in sq.get_opcodes():
        if tag == "replace":
            diffs.append((qwen_text[i1:i2], corrected_text[j1:j2]))
        elif tag == "delete":
            diffs.append((qwen_text[i1:i2], ""))
        elif tag == "insert":
            diffs.append(("", corrected_text[j1:j2]))
    return diffs


def eval_precision(v: dict) -> dict | None:
    """对单视频计算 precision/recall 及变化明细"""
    try:
        qwen_raw     = parse_srt_text(v["qwen"])
        corrected_raw= parse_srt_text(v["corrected"])
        human_raw    = parse_srt_text(v["human"])
    except Exception as e:
        return None

    diffs = diff_corrections(qwen_raw, corrected_raw)
    if not diffs:
        return {
            "video_id": v["video_id"],
            "total_changes": 0,
            "good": 0, "bad": 0, "neutral": 0,
            "precision": None, "recall": None,
        }

    good = bad = neutral = 0
    format_norm_count = 0
    details = []
    for orig, corr in diffs:
        if not orig and not corr:
            continue
        is_format_norm = (orig, corr) in _FORMAT_NORM_PAIRS
        orig_in_human = bool(orig) and (orig in human_raw)
        corr_in_human = bool(corr) and (corr in human_raw)

        if is_format_norm:
            # 格式规范化：直接按 CER 改善判断，这里标记为特殊类型
            # corr (如 "200") 在 human 中出现视为 GOOD
            verdict = "GOOD" if corr_in_human else "NEUTRAL"
            format_norm_count += 1
            if verdict == "GOOD":
                good += 1
            else:
                neutral += 1
        elif corr_in_human and not orig_in_human:
            verdict = "GOOD"
            good += 1
        elif orig_in_human and not corr_in_human:
            verdict = "BAD"
            bad += 1
        else:
            verdict = "NEUTRAL"
            neutral += 1
        details.append({"orig": orig, "corr": corr, "verdict": verdict,
                        "format_norm": is_format_norm})

    total = good + bad + neutral
    precision = good / (good + bad) if (good + bad) > 0 else None

    # 计算可修复错误数（qwen 有但 human 没有的片段）
    total_fixable = sum(
        1 for orig, _ in diffs
        if orig and orig not in human_raw
    )
    recall = good / total_fixable if total_fixable > 0 else None

    return {
        "video_id":       v["video_id"],
        "total_changes":  total,
        "good":           good,
        "bad":            bad,
        "neutral":        neutral,
        "format_norm":    format_norm_count,
        "precision":      precision,
        "recall":         recall,
        "details":        details[:8],  # 保存前8条样例
    }


def run():
    videos = find_test_videos()
    print(f"测试集（已有 corrected.srt）: {len(videos)} 个")
    if not videos:
        print("未找到可评估视频。")
        return

    results = []
    for v in videos:
        r = eval_precision(v)
        if r:
            results.append(r)

    if not results:
        print("无可用结果。")
        return

    # ── 汇总 ──
    has_changes = [r for r in results if r["total_changes"] > 0]
    print(f"有修改的视频: {len(has_changes)}/{len(results)}")
    print()

    if has_changes:
        precisions = [r["precision"] for r in has_changes if r["precision"] is not None]
        total_good = sum(r["good"] for r in results)
        total_bad  = sum(r["bad"]  for r in results)
        total_neut = sum(r["neutral"] for r in results)
        total_changes = sum(r["total_changes"] for r in results)

        total_fmt_norm = sum(r.get("format_norm", 0) for r in results)
        print(f"{'='*60}")
        print(f"总修改条数: {total_changes}")
        print(f"  GOOD    (确认修正了错误): {total_good}  ({total_good/total_changes*100:.1f}%)")
        print(f"  BAD     (破坏了正确内容): {total_bad}   ({total_bad/total_changes*100:.1f}%)")
        print(f"  NEUTRAL (无法判断):       {total_neut}  ({total_neut/total_changes*100:.1f}%)")
        print(f"  其中格式规范化修改:        {total_fmt_norm}  (中文数字→阿拉伯数字)")
        print()
        print(f"Precision (GOOD/GOOD+BAD): {total_good/(total_good+total_bad)*100:.1f}%" if (total_good+total_bad) > 0 else "Precision: N/A")
        if precisions:
            print(f"每视频 precision 中位数: {median(precisions)*100:.1f}%")
        print(f"{'='*60}")
        print()
        print(f"{'视频ID':<20} {'改动':>4} {'GOOD':>5} {'BAD':>5} {'Prec':>6}")
        print(f"{'-'*50}")
        for r in sorted(has_changes, key=lambda x: -(x["precision"] or 0)):
            prec = f"{r['precision']*100:.0f}%" if r["precision"] is not None else " N/A"
            print(f"{r['video_id']:<20} {r['total_changes']:>4} {r['good']:>5} {r['bad']:>5} {prec:>6}")

        # 打印 BAD 修改样例
        bad_samples = []
        for r in results:
            for d in r.get("details", []):
                if d["verdict"] == "BAD":
                    bad_samples.append((r["video_id"], d["orig"], d["corr"]))
        if bad_samples:
            print(f"\n--- BAD corrections 样例 (前10条) ---")
            for vid, orig, corr in bad_samples[:10]:
                print(f"  [{vid}] '{orig}' → '{corr}'")

        # 打印 GOOD 修改样例
        good_samples = []
        for r in results:
            for d in r.get("details", []):
                if d["verdict"] == "GOOD":
                    good_samples.append((r["video_id"], d["orig"], d["corr"]))
        if good_samples:
            print(f"\n--- GOOD corrections 样例 (前10条) ---")
            for vid, orig, corr in good_samples[:10]:
                print(f"  [{vid}] '{orig}' → '{corr}'")

    # ── 候选词 recall 分析 ──
    cand_path = _PROJECT_ROOT / "logs" / "correction_candidates.json"
    if cand_path.exists():
        candidates = json.loads(cand_path.read_text(encoding="utf-8"))
        print(f"\n{'='*60}")
        print("候选词 Recall（在测试集中的覆盖率和修正率）")
        print(f"{'='*60}")
        print(f"{'候选词':<12} {'出现视频':>6} {'已修正':>6} {'Recall':>8}  类型")
        print(f"{'-'*52}")

        # 分类：格式规范化 vs 其他
        fmt_patterns = {p for p, _ in _FORMAT_NORM_PAIRS}

        for pat, info in candidates.items():
            # 扫描测试集：qwen.srt 里含有该词的视频
            videos_with_pat = 0
            videos_corrected = 0
            for v in find_test_videos():
                try:
                    qwen_text = parse_srt_text(v["qwen"])
                    if pat not in qwen_text:
                        continue
                    videos_with_pat += 1
                    corrected_text = parse_srt_text(v["corrected"])
                    # 词被修正 = qwen 有但 corrected 没有（或数量减少）
                    if pat not in corrected_text:
                        videos_corrected += 1
                except Exception:
                    continue

            recall_str = f"{videos_corrected/videos_with_pat*100:.0f}%" if videos_with_pat > 0 else "  —"
            cat = "格式规范" if pat in fmt_patterns else "专有/同音"
            marker = "" if videos_with_pat == 0 else ("✓" if videos_with_pat > 0 and videos_corrected == videos_with_pat else ("△" if videos_corrected > 0 else "✗"))
            print(f"{pat:<12} {videos_with_pat:>6} {videos_corrected:>6} {recall_str:>8}  {cat}  {marker}")

        print(f"{'='*60}")
        print("  ✓=全部修正  △=部分修正  ✗=未修正任何  —=测试集无此词")

    # 保存
    out = _PROJECT_ROOT / "logs" / "precision_eval.json"
    out.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n详细结果: {out}")


if __name__ == "__main__":
    run()

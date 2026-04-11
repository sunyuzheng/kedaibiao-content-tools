#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
校对效果评估

对有 .qwen.srt + .zh.srt + .corrected.srt 的视频，计算：
  - CER_before  = edit_distance(qwen_text, human_text) / len(human_text)
  - CER_after   = edit_distance(corrected_text, human_text) / len(human_text)
  - improvement = (CER_before - CER_after) / CER_before
  - false_pos   = 改了反而更差的 chunk 比例（可选精细化）

测试集策略：
  只评估 2026-01-01 之后的视频（error_guide 用 2025 数据训练，此为 held-out 集）

用法：
  # 先对测试集视频生成 .corrected.srt
  python3 tools/correct/batch_correct_srt.py --test-only

  # 再评估
  python3 tools/correct/eval_correction.py
"""

import difflib
import json
import re
from pathlib import Path
from statistics import mean, median

# ── 配置 ────────────────────────────────────────────────────────────────────
_PROJECT_ROOT = Path(__file__).parent.parent.parent
DIRS = [
    _PROJECT_ROOT / "archive" / "有人工字幕",
    _PROJECT_ROOT / "archive" / "会员视频",
]
HUMAN_LANGS = ("zh", "zh-Hans", "zh-Hant", "en-zh", "en-orig")
TEST_DATE_FROM = "20260101"   # held-out 测试集起始日期
# ────────────────────────────────────────────────────────────────────────────

_PUNCT = re.compile(
    r'[\s，。！？、：；\u201c\u201d\u2018\u2019（）【】《》…—·「」.,!?;:()\[\]"\'～~]'
)
_CN_NUM = str.maketrans("零一二三四五六七八九", "0123456789")


def parse_srt_text(path: Path) -> str:
    """SRT → 去标点归一化文本（与 compare_single.py 一致）"""
    content = path.read_text(encoding="utf-8")
    blocks  = re.split(r'\n{2,}', content.strip())
    parts   = []
    for block in blocks:
        lines = block.strip().splitlines()
        text_lines = [l for l in lines if not l.strip().isdigit() and "-->" not in l]
        parts.append("".join(text_lines))
    raw = "".join(parts)
    return _PUNCT.sub("", raw).translate(_CN_NUM).lower()


def cer(hypothesis: str, reference: str) -> float:
    """Character Error Rate = edit_distance / len(reference)"""
    if not reference:
        return 0.0
    # difflib 的 ratio() = 2 * matches / total_chars
    # edit_distance ≈ (1 - ratio) * (len_a + len_b) / 2，近似但足够用于比较
    ratio = difflib.SequenceMatcher(None, hypothesis, reference, autojunk=False).ratio()
    edit_approx = (1 - ratio) * (len(hypothesis) + len(reference)) / 2
    return edit_approx / len(reference)


def find_video_date(folder_name: str) -> str | None:
    m = re.match(r'^(\d{8})_', folder_name)
    return m.group(1) if m else None


def detect_qwen_lang(path: Path) -> str:
    """检测 qwen.srt 的主要语言：'zh' 或 'en'"""
    try:
        sample = path.read_text(encoding="utf-8")[:800]
    except Exception:
        return "zh"
    cn_chars = sum(1 for c in sample if '\u4e00' <= c <= '\u9fff')
    en_chars  = sum(1 for c in sample if c.isascii() and c.isalpha())
    return "en" if en_chars > cn_chars * 2 else "zh"


def find_test_videos() -> list[dict]:
    """找出测试集视频（date >= TEST_DATE_FROM，有三类 SRT）"""
    videos = []
    for base in DIRS:
        if not base.exists():
            continue
        for video_dir in sorted(base.iterdir()):
            if not video_dir.is_dir():
                continue
            date = find_video_date(video_dir.name)
            if date is None or date < TEST_DATE_FROM:
                continue

            qwen_srts = list(video_dir.glob("*.qwen.srt"))
            if not qwen_srts:
                continue
            qwen_path = qwen_srts[0]
            stem = qwen_path.stem.removesuffix(".qwen")

            # 根据 Qwen 语言选择对应的人工参考字幕
            qwen_lang = detect_qwen_lang(qwen_path)
            if qwen_lang == "en":
                ref_langs = ("en-orig", "en")
            else:
                ref_langs = HUMAN_LANGS

            human_path = None
            for lang in ref_langs:
                cand = video_dir / f"{stem}.{lang}.srt"
                if cand.exists():
                    human_path = cand
                    break
            if human_path is None:
                continue

            corrected_path = video_dir / f"{stem}.corrected.srt"

            parts = video_dir.name.rsplit("_", 1)
            video_id = parts[-1] if len(parts) == 2 else video_dir.name

            videos.append({
                "video_id":      video_id,
                "date":          date,
                "dir":           video_dir,
                "qwen":          qwen_path,
                "human":         human_path,
                "qwen_lang":     qwen_lang,
                "corrected":     corrected_path if corrected_path.exists() else None,
            })
    return videos


def eval_video(v: dict) -> dict | None:
    """计算单视频的 CER before/after"""
    try:
        qwen_text    = parse_srt_text(v["qwen"])
        human_text   = parse_srt_text(v["human"])
        cer_before   = cer(qwen_text, human_text)
    except Exception as e:
        return None

    result = {
        "video_id":    v["video_id"],
        "date":        v["date"],
        "cer_before":  cer_before,
        "cer_after":   None,
        "improvement": None,
        "corrected_exists": v["corrected"] is not None,
    }

    if v["corrected"] is not None:
        try:
            corr_text = parse_srt_text(v["corrected"])
            ca = cer(corr_text, human_text)
            imp = (cer_before - ca) / cer_before if cer_before > 0 else 0
            result.update({"cer_after": ca, "improvement": imp})
        except Exception:
            pass

    return result


def run() -> None:
    videos = find_test_videos()
    print(f"测试集视频（{TEST_DATE_FROM}起）: {len(videos)} 个")

    corrected_count = sum(1 for v in videos if v["corrected"] is not None)
    print(f"已有 .corrected.srt: {corrected_count}/{len(videos)}")
    print()

    results = []
    for v in videos:
        r = eval_video(v)
        if r:
            results.append(r)

    if not results:
        print("没有可评估的视频。")
        return

    # ── 基线（qwen vs human）──
    baselines = [r["cer_before"] for r in results]
    print(f"{'='*60}")
    print(f"基线 CER（Qwen vs 精校）")
    print(f"  平均:   {mean(baselines):.4f}  ({mean(baselines)*100:.2f}%)")
    print(f"  中位数: {median(baselines):.4f}")
    print(f"  最低:   {min(baselines):.4f}  最高: {max(baselines):.4f}")
    print()

    # ── 校对效果（有 corrected 的子集）──
    corrected_results = [r for r in results if r["cer_after"] is not None]
    if corrected_results:
        afters = [r["cer_after"] for r in corrected_results]
        imps   = [r["improvement"] for r in corrected_results]
        fp_rate = sum(1 for r in corrected_results if r["improvement"] < 0) / len(corrected_results)

        print(f"校对效果（{len(corrected_results)} 个视频）")
        print(f"  CER after 平均: {mean(afters):.4f}  ({mean(afters)*100:.2f}%)")
        print(f"  改善率 平均:    {mean(imps)*100:.1f}%")
        print(f"  改善率 中位数:  {median(imps)*100:.1f}%")
        print(f"  负改善率（改差了）: {fp_rate*100:.1f}%")
        print()

        print(f"{'─'*60}")
        print(f"{'视频ID':<15} {'日期':<10} {'CER_before':>10} {'CER_after':>10} {'改善%':>8}")
        print(f"{'─'*60}")
        for r in sorted(corrected_results, key=lambda x: -x["improvement"]):
            flag = "↓" if r["improvement"] < 0 else " "
            print(
                f"{r['video_id']:<15} {r['date']:<10} "
                f"{r['cer_before']*100:>9.2f}% {r['cer_after']*100:>9.2f}% "
                f"{r['improvement']*100:>7.1f}%{flag}"
            )
    else:
        print("还没有 .corrected.srt 文件，请先运行校对。")
        print("  python3 tools/correct/batch_correct_srt.py --test-only")

    print(f"{'='*60}")

    # 保存结果
    out = _PROJECT_ROOT / "logs" / "eval_results.json"
    out.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n详细结果: {out}")


if __name__ == "__main__":
    run()

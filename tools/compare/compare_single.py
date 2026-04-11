#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
单视频对比：.qwen.srt vs 精校字幕，提取 Qwen 转录错误

用法：
  python3 tools/compare/compare_single.py <视频目录路径>

  # 例：
  python3 tools/compare/compare_single.py \
    "archive/会员视频/20260202_\"80 分陷阱\"：你越忙，越拿不到真正的结果_7-1cro5Okeg"

输出：
  - 控制台：diff 报告（错误列表 + 统计）
  - <视频目录>/diff_report.txt：同内容，方便复查
"""

import re
import sys
import difflib
from pathlib import Path

# ── 配置 ────────────────────────────────────────────────────────────────────
HUMAN_LANGS   = ("zh", "zh-Hans", "zh-Hant", "en-zh", "en-orig")
CONTEXT_CHARS = 20   # 每条 diff 前后各保留多少字
MAX_DISPLAY   = 80   # 控制台最多展示多少条差异
# ────────────────────────────────────────────────────────────────────────────

# 用于「去噪比较」的标点集合（去掉后做 diff，减少标点差异干扰）
_PUNCT = re.compile(r'[\s，。！？、：；\u201c\u201d\u2018\u2019（）【】《》…—·「」.,!?;:()\[\]"\'\-～~]')

# 纯数字（阿拉伯或中文，含分数斜杠）—— 用于过滤纯数字格式差异
_NUMERIC = re.compile(r'^[\d零一二三四五六七八九十百千万亿/]+$')

# 中文数字 → 阿拉伯数字映射（用于比较前归一化）
_CN_NUM = str.maketrans("零一二三四五六七八九", "0123456789")

# 匹配"中文数字序列"，用于归一化
_CN_NUM_SEQ = re.compile(r'[零一二三四五六七八九十百千万亿]+')

# 语气词/助词集合 —— 这类替换对语义无影响，从错题本中排除
# 规则：如果 qwen 和 human 片段都只由这类字符组成，跳过
_FILLER_CHARS = frozenset("呃啊嗯哦哎耶唉哇哈咦哟喂噢哼嘿")
_PARTICLE_CHARS = frozenset("的地得了嘛吧呢哈么")

def _is_trivial(s: str) -> bool:
    """判断片段是否只含语气词/助词（对语义无影响的差异）"""
    stripped = s.strip()
    if not stripped:
        return True
    return all(c in _FILLER_CHARS or c in _PARTICLE_CHARS for c in stripped)

# emoji 过滤（用于处理人工字幕中插入的 emoji 注释）
_EMOJI = re.compile(
    "[\U00010000-\U0010ffff"   # 4字节 Unicode（大部分 emoji）
    "\U0001F300-\U0001F9FF"    # 补充符号
    "\u2600-\u27BF"            # 杂项符号
    "\uFE0F]",                 # emoji 变体选择符
    flags=re.UNICODE,
)


def normalize_numbers(text: str) -> str:
    """把中文数字序列转成阿拉伯数字（仅用于比较，不改变展示）
    简单映射：零→0 一→1 ... 九→9，十→10 等复杂组合暂不展开。
    目的是让 '2025' 和 '二零二五' 比较时不产生 diff。
    """
    return text.translate(_CN_NUM)


def parse_srt(path: Path) -> str:
    """SRT → 纯文字字符串（去掉序号行 + 时间戳行，块间不加分隔）"""
    content = path.read_text(encoding="utf-8")
    blocks  = re.split(r'\n{2,}', content.strip())
    parts   = []
    for block in blocks:
        lines = block.strip().splitlines()
        text_lines = [
            l for l in lines
            if not l.strip().isdigit()      # 跳过序号
            and "-->" not in l              # 跳过时间戳
        ]
        parts.append("".join(text_lines))
    return "".join(parts)


def clean(text: str) -> str:
    """去标点/空白 + 中文数字归一化 + 转小写，用于对齐比较（不用于展示）"""
    t = _PUNCT.sub("", text)
    t = normalize_numbers(t)
    return t.lower()


def _is_numeric_like(s: str) -> bool:
    """判断字符串是否只含数字/中文数字/分数相关字符（用于过滤格式差异）"""
    return bool(re.match(r'^[\d零一二三四五六七八九十百千万亿两x倍分之/]+$', s, re.IGNORECASE))


def build_pos_map(raw: str) -> tuple[str, list[int]]:
    """
    返回 (cleaned, pos_map)
    pos_map[i] = cleaned[i] 在 raw 中的原始位置
    用于把 clean 文本的 diff 位置映射回原始带标点文本取上下文
    """
    cleaned_chars = []
    pos_map = []
    for i, ch in enumerate(raw):
        # 标点/空白：跳过
        if _PUNCT.match(ch):
            continue
        # 数字归一化 + 小写
        normalized = ch.translate(_CN_NUM).lower()
        cleaned_chars.append(normalized)
        pos_map.append(i)
    return "".join(cleaned_chars), pos_map


def extract_diffs(q_raw: str, h_raw: str) -> list[dict]:
    """
    1. 对「去标点+归一化」版做字符级 SequenceMatcher
    2. 提取所有 replace 操作
    3. 通过 pos_map 把位置映射回原始文本，取带标点的上下文展示

    返回 list[dict]:
      qwen, human, before_orig, after_orig
    """
    q_clean, q_map = build_pos_map(q_raw)
    h_clean, h_map = build_pos_map(h_raw)

    matcher = difflib.SequenceMatcher(None, q_clean, h_clean, autojunk=False)

    results = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag != "replace":
            continue

        q_frag = q_clean[i1:i2]
        h_frag = h_clean[j1:j2]

        if not q_frag or not h_frag:
            continue

        # 过滤：纯数字/分数格式差异（2025 vs 二零二五 / 两百 vs 200 / 1/10 vs 十分之一）
        if _is_numeric_like(q_frag) and _is_numeric_like(h_frag):
            continue

        # 过滤：纯语气词/助词替换（呃/啊/嗯 及 的/地/得/了/嘛 等）
        if _is_trivial(q_frag) and _is_trivial(h_frag):
            continue

        # 过滤：长度比太悬殊（>4x），更可能是内容差异而非识别错误
        big, small = max(len(q_frag), len(h_frag)), min(len(q_frag), len(h_frag))
        if small == 0 or big / small > 4:
            continue

        # 用 pos_map 取原始带标点上下文
        raw_i1 = q_map[i1]
        raw_i2 = q_map[i2 - 1] + 1 if i2 > 0 else raw_i1
        raw_j1 = h_map[j1]
        raw_j2 = h_map[j2 - 1] + 1 if j2 > 0 else raw_j1

        ctx_start = q_map[max(0, i1 - CONTEXT_CHARS)]
        ctx_end   = q_map[min(len(q_map) - 1, i2 + CONTEXT_CHARS - 1)] + 1

        q_display = q_raw[raw_i1:raw_i2]
        h_display = h_raw[raw_j1:raw_j2]

        # 过滤：人工字幕里有时含 emoji 注释（如 🤔），去掉后如果一样就跳过
        q_no_emoji = _EMOJI.sub("", q_display).strip()
        h_no_emoji = _EMOJI.sub("", h_display).strip()
        if q_no_emoji == h_no_emoji:
            continue

        results.append({
            "qwen":   q_display,
            "human":  h_display,
            "before": q_raw[ctx_start:raw_i1],
            "after":  q_raw[raw_i2:ctx_end],
            "q_len":  len(q_frag),
            "h_len":  len(h_frag),
            "pair":   (q_no_emoji, h_no_emoji),  # 用于去重
        })

    # 每个 (qwen, human) pair 只保留第一次出现（同pattern在一个视频里去重）
    seen: set[tuple] = set()
    deduped = []
    for r in results:
        key = r["pair"]
        if key not in seen:
            seen.add(key)
            deduped.append(r)

    return deduped


def find_srt_pair(video_dir: Path) -> tuple[Path, Path] | None:
    """在目录里找 (.qwen.srt, 精校.srt) 对"""
    qwen_srts = list(video_dir.glob("*.qwen.srt"))
    if not qwen_srts:
        return None
    qwen_path = qwen_srts[0]
    stem = qwen_path.stem.removesuffix(".qwen")  # e.g. "视频标题"

    for lang in HUMAN_LANGS:
        cand = video_dir / f"{stem}.{lang}.srt"
        if cand.exists():
            return (qwen_path, cand)
    return None


def report(diffs: list[dict], qwen_path: Path, human_path: Path,
           q_len: int, h_len: int) -> str:
    lines = []
    lines.append("=" * 70)
    lines.append(f"Qwen SRT   : {qwen_path.name}  ({q_len} chars)")
    lines.append(f"精校 SRT   : {human_path.name}  ({h_len} chars)")
    lines.append(f"差异总数   : {len(diffs)}")
    lines.append("=" * 70)

    # 按 Qwen片段长度排序（短的更可能是字词错误，优先展示）
    sorted_diffs = sorted(diffs, key=lambda d: d["q_len"])

    for i, d in enumerate(sorted_diffs[:MAX_DISPLAY], 1):
        lines.append(
            f"\n[{i:03d}] Qwen: 「{d['before']}【{d['qwen']}】{d['after']}」"
        )
        lines.append(
            f"      精校: 「{d['before']}【{d['human']}】{d['after']}」"
        )

    if len(diffs) > MAX_DISPLAY:
        lines.append(f"\n... 还有 {len(diffs) - MAX_DISPLAY} 条未展示，见 diff_report.txt")

    lines.append("\n" + "=" * 70)
    lines.append("长度统计:")
    lines.append(f"  Qwen  去标点文本长度: {q_len}")
    lines.append(f"  精校  去标点文本长度: {h_len}")
    lines.append(f"  字符差异率 (replace ops / max_len): "
                 f"{len(diffs) / max(q_len, h_len) * 100:.2f}%")
    lines.append("=" * 70)
    return "\n".join(lines)


def main():
    if len(sys.argv) < 2:
        print("用法: python3 tools/compare/compare_single.py <视频目录路径>")
        sys.exit(1)

    video_dir = Path(sys.argv[1])
    if not video_dir.is_dir():
        print(f"错误: 目录不存在: {video_dir}")
        sys.exit(1)

    pair = find_srt_pair(video_dir)
    if pair is None:
        print(f"错误: 在 {video_dir} 中未找到 .qwen.srt + 精校字幕对")
        sys.exit(1)

    qwen_path, human_path = pair
    print(f"Qwen  : {qwen_path.name}")
    print(f"精校  : {human_path.name}")

    q_raw = parse_srt(qwen_path)
    h_raw = parse_srt(human_path)
    q_clean = clean(q_raw)
    h_clean = clean(h_raw)

    print(f"文本长度  Qwen={len(q_clean)}  精校={len(h_clean)}")
    print("正在对齐比较（可能需要几秒）...")

    diffs = extract_diffs(q_raw, h_raw)

    text = report(diffs, qwen_path, human_path, len(q_clean), len(h_clean))
    print(text)

    out_path = video_dir / "diff_report.txt"
    out_path.write_text(text, encoding="utf-8")
    print(f"\n报告已保存: {out_path}")


if __name__ == "__main__":
    main()

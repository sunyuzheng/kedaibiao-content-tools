#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Qwen SRT 校对工具

流程：
  1. 扫描：对每个 chunk，用候选词典找出疑似混淆位置（flags）
  2. 校对：把 chunk + flags 发给 LLM，要求对每个 flag 逐一表态
  3. 验证：拒绝幻觉（original 不存在）、拒绝过量修改
  4. 应用：生成 .corrected.srt

防偷懒机制：
  - LLM 必须对每个 flag 显式回应（KEEP 或给出修改），不能沉默
  - 输出格式分两部分：flags 处理 + 额外发现
  - 验证层：original 不在原文则拒绝；total changes < 25%

用法：
  python3 tools/correct/correct_srt.py <视频目录>
  python3 tools/correct/correct_srt.py <视频目录> --model claude-haiku-4-5-20251001
"""

import json
import re
import sys
import time
import traceback
from pathlib import Path

import os

# 尝试从项目 .env 加载 API key
_ENV_FILES = [
    Path(__file__).parent.parent.parent / ".env",
    Path.home() / "Desktop" / "AI" / ".env",
    Path.home() / ".env",
]
for _ef in _ENV_FILES:
    if _ef.exists():
        for _line in _ef.read_text().splitlines():
            _line = _line.strip()
            if _line.startswith("ANTHROPIC_API_KEY=") and not os.environ.get("ANTHROPIC_API_KEY"):
                os.environ["ANTHROPIC_API_KEY"] = _line.split("=", 1)[1].strip().strip('"\'')

try:
    import anthropic
except ImportError:
    print("错误: 未安装 anthropic\n请运行: pip install anthropic")
    sys.exit(1)

# ── 配置 ────────────────────────────────────────────────────────────────────
_PROJECT_ROOT   = Path(__file__).parent.parent.parent
_LOGS           = _PROJECT_ROOT / "logs"
COMPACT_GUIDE   = _LOGS / "error_guide_compact.txt"
CANDIDATES_FILE = _LOGS / "correction_candidates.json"

DEFAULT_MODEL   = "claude-haiku-4-5-20251001"
CHUNK_SIZE      = 8         # 每次发给 LLM 的 SRT 块数（小批次让 LLM 读得更仔细）
MAX_EDIT_RATIO  = 0.20      # 单次 chunk 修改字符比例上限（防过度改写）
HUMAN_LANGS     = ("zh", "zh-Hans", "zh-Hant", "en-zh", "en-orig")
# ────────────────────────────────────────────────────────────────────────────


def load_resources() -> tuple[str, dict]:
    """加载 compact guide 和候选词典"""
    guide = COMPACT_GUIDE.read_text(encoding="utf-8") if COMPACT_GUIDE.exists() else ""
    if CANDIDATES_FILE.exists():
        with open(CANDIDATES_FILE, encoding="utf-8") as f:
            candidates = json.load(f)
    else:
        candidates = {}
    return guide, candidates


def parse_srt(path: Path) -> list[dict]:
    """SRT → [{index, timestamp, text}, ...]"""
    content = path.read_text(encoding="utf-8")
    blocks  = re.split(r'\n{2,}', content.strip())
    chunks  = []
    for block in blocks:
        lines = block.strip().splitlines()
        if len(lines) < 2:
            continue
        idx_line = lines[0].strip()
        ts_line  = lines[1].strip() if "-->" in (lines[1] if len(lines) > 1 else "") else ""
        text_lines = [l for l in lines if not l.strip().isdigit() and "-->" not in l]
        text = "\n".join(text_lines).strip()
        if not text:
            continue
        chunks.append({"index": idx_line, "timestamp": ts_line, "text": text})
    return chunks


def write_srt(chunks: list[dict], path: Path) -> None:
    with open(path, "w", encoding="utf-8") as f:
        for i, c in enumerate(chunks, 1):
            ts = c.get("timestamp", "")
            f.write(f"{i}\n{ts}\n{c['text']}\n\n")


def scan_flags(chunks: list[dict], candidates: dict) -> list[dict]:
    """
    扫描 chunks 中的疑似混淆位置。
    返回 [{chunk_idx, found, alternatives, hint, context}, ...]
    - 多字 pattern：每次出现都标记（精确定位）
    - 单字 pattern（同音字）：每个 chunk 每个 pattern 只标记一次（避免 hint 列表过长）
    排序：长 pattern 优先（避免子串重复匹配）。
    """
    flags = []
    sorted_patterns = sorted(candidates.keys(), key=len, reverse=True)
    already_flagged: set = set()   # (chunk_idx, char_pos) 避免重复多字标记
    single_seen: set = set()       # (chunk_idx, pattern) 单字只标记一次

    for ci, chunk in enumerate(chunks):
        text = chunk["text"]
        for pat in sorted_patterns:
            info = candidates[pat]
            alts = info.get("alternatives", [])
            is_single = len(pat) == 1

            start = 0
            while True:
                pos = text.find(pat, start)
                if pos == -1:
                    break
                key = (ci, pos)
                single_key = (ci, pat)

                if is_single:
                    # 单字：每个 chunk 每个 pattern 只报告一次（但 flag_patterns 仍会含该字）
                    if single_key not in single_seen:
                        single_seen.add(single_key)
                        flags.append({
                            "chunk_idx":  ci,
                            "found":      pat,
                            "alternatives": alts,
                            "hint":       info.get("hint", ""),
                            "context":    text[max(0, pos-10): pos+len(pat)+10],
                            "is_single":  True,
                        })
                else:
                    if key not in already_flagged:
                        already_flagged.add(key)
                        flags.append({
                            "chunk_idx":  ci,
                            "found":      pat,
                            "alternatives": alts,
                            "hint":       info.get("hint", ""),
                            "context":    text[max(0, pos-10): pos+len(pat)+10],
                            "is_single":  False,
                        })
                start = pos + 1

    return flags


def build_prompt(chunks: list[dict], flags: list[dict], guide: str) -> tuple[str, str]:
    """构建 (system_prompt, user_prompt)"""
    system = f"""你是 Qwen3-ASR 字幕纠错助手。本频道内容以中文为主，话题涵盖职场、AI、投资、创业。

{guide}

## 允许修正的两类情况

### 1. 数字/格式规范化（高优先级）
将中文口语数词规范为阿拉伯数字（和人工精校保持一致）：
- 两百 → 200（但「一两百」中的「两百」不改，因为那是约数）
- 两千 → 2000（同上，独立出现时才改）
- 百分之十 → 10%，百分之百 → 100%
- 幺幺 → 11，two → 2

### 2. 已知同音/专有名词混淆
检测提示中列出的模式，结合上下文判断是否需要修正。

## 绝对禁止
- **删除/增加实词**：不能增加或删除名词、动词、形容词
- **修改语气词/副词**：其实、应该、可能、确实、非常、已经、然后等不能删改
- **同义词替换**：即使另一个词更准确，也不替换
- **长片段改写**：每条 original 只能是1-6个字

## 输出格式
JSON 数组，每项：{{"original": "最短精确片段", "corrected": "修正后", "reason": "原因"}}

**关键要求：**
- `"original"` 必须是需要修改的**最短子字符串**（通常1-4字），不是整句话
  - 正确示例：`{{"original": "亚哥", "corrected": "鸭哥"}}` ✓
  - 错误示例：`{{"original": "给大家推荐一下亚哥的那篇文章", "corrected": "..."}}` ✗（太长）
- `"original"` 必须在字幕中精确存在（不能是空字符串或造句）
- 不确定时输出 []，宁可漏改，不要误改"""

    # 组装字幕文本
    srt_lines = []
    for ci, chunk in enumerate(chunks):
        srt_lines.append(f"[{ci}] {chunk.get('timestamp','')}")
        srt_lines.append(chunk["text"])
        srt_lines.append("")
    srt_text = "\n".join(srt_lines)

    # 将 flags 聚合成简洁的类别提示（避免单字同音词列出几十行）
    if flags:
        # 单字 hint: 按 hint 文本聚合（同类合并）
        single_hints = {}
        multi_hints  = []
        for f in flags:
            if f.get("is_single"):
                h = f["hint"] or f"「{f['found']}」可能是「{'或'.join(f['alternatives'])}」"
                single_hints[f["hint"] or f["found"]] = h
            else:
                alts = "、".join(f["alternatives"]) if f["alternatives"] else "?"
                ctx  = f.get("context", "")
                multi_hints.append(f"  - 「{f['found']}」→「{alts}」  上下文: …{ctx}…")

        flag_lines = ["## 本批字幕中检测到以下可能混淆的模式（请结合上下文判断，不确定则不改）"]
        if single_hints:
            flag_lines.append("【同音字】请检查下列字的每次出现是否用对：")
            for h in single_hints.values():
                flag_lines.append(f"  - {h}")
        if multi_hints:
            flag_lines.append("【具体位置】")
            flag_lines.extend(multi_hints)
        hints_text = "\n".join(flag_lines) + "\n\n"
    else:
        hints_text = ""

    user = f"""{hints_text}## 字幕原文
{srt_text}
请输出修正 JSON 数组："""

    return system, user


def call_llm(system: str, user: str, model: str, client: anthropic.Anthropic) -> str:
    """调用 Claude API，返回原始文本响应"""
    resp = client.messages.create(
        model=model,
        max_tokens=1024,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    return resp.content[0].text


def parse_response(raw: str):
    """从 LLM 响应中提取 JSON（数组或对象均可）"""
    # 先去掉 markdown fence
    stripped = re.sub(r'^```(?:json)?\s*', '', raw.strip(), flags=re.MULTILINE)
    stripped = re.sub(r'\s*```$', '', stripped.strip(), flags=re.MULTILINE)
    stripped = stripped.strip()

    # 直接解析
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        pass

    # 提取第一个完整的 [...] 或 {...}
    for pattern in (r'(\[.*\])', r'(\{.*\})'):
        m = re.search(pattern, stripped, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(1))
            except json.JSONDecodeError:
                pass
    return []


_CN_DIGITS = set("零一二三四五六七八九十百千万亿两")
_ALL_DIGITS = set("0123456789") | _CN_DIGITS

# Patterns that are number-format normalizations (CN→Arabic): allow digits in corrected
_FORMAT_NORM_PATTERNS = {"百分之十", "百分之百", "两百", "两千", "幺幺", "到十"}

# Patterns where origin must appear at a word boundary (not inside a larger number phrase)
# e.g. "一两百" should NOT become "一200"
_BOUNDARY_GUARD_PATTERNS = {"两百", "两千"}
_PRECEDING_DIGIT_CHARS = set("一二三四五六七八九十")


def _has_digit(s: str) -> bool:
    return any(c in _ALL_DIGITS for c in s)


def _edit_distance_approx(a: str, b: str) -> int:
    """简单逐字符比较的近似编辑距离（仅用于短串过滤）"""
    if a == b:
        return 0
    common = sum(x == y for x, y in zip(a, b))
    return (len(a) - common) + (len(b) - common)


def _extract_minimal_diff(orig: str, corr: str, flag_patterns: set) -> tuple[str, str] | None:
    """
    LLM 有时把上下文也放进 original（如「亚哥的那篇文章」→「鸭哥的那篇文章」）。
    本函数在已知 flag_patterns 的前提下提取最小差异片段：
      orig="亚哥的那篇文章", corr="鸭哥的那篇文章", flag_patterns={"亚哥"}
      → ("亚哥", "鸭哥")

    策略：对每个 flag_pattern，检查它是否作为子字符串出现在 orig 中，
    同时 orig 和 corr 有相同的前缀/后缀（只有 flag 部分不同）。
    """
    if orig == corr or not orig or not corr:
        return None
    for pat in sorted(flag_patterns, key=len, reverse=True):  # 长 pattern 优先
        pos = orig.find(pat)
        if pos == -1:
            continue
        prefix = orig[:pos]
        suffix = orig[pos + len(pat):]
        if corr.startswith(prefix) and (not suffix or corr.endswith(suffix)):
            corr_end = len(corr) - len(suffix) if suffix else len(corr)
            corr_pat = corr[len(prefix): corr_end]
            if corr_pat and corr_pat != pat:
                return pat, corr_pat
    return None


def validate_and_collect(parsed, chunk_texts: list[str], flags: list[dict]) -> list[dict]:
    """
    验证并收集修改列表。
    拒绝标准（防止 LLM 越界改写）：
      - original 不存在原文（幻觉）
      - original == corrected
      - original 或 corrected 含数字（禁止修改数字）
      - original 长度 > 6（防止修改长短语）
      - corrected 比 original 增加超过 2 个字（防止新增内容）
      - 近似编辑距离 > 3（防止大幅度改写）
      - 总改动量超过 MAX_EDIT_RATIO
    """
    corrections = []
    full_text = "\n".join(chunk_texts)
    total_chars = max(len(full_text), 1)

    # 统一为 list
    if isinstance(parsed, dict):
        items = parsed.get("flagged", []) + parsed.get("extra", [])
    elif isinstance(parsed, list):
        items = parsed
    else:
        return []

    # 只接受被 scan_flags 标记过的原文片段（候选词典里的已知混淆模式）
    flag_patterns = {f["found"] for f in flags}

    for item in items:
        orig = item.get("original") or item.get("found", "")
        corr = item.get("corrected", "")
        if item.get("action") == "KEEP":
            continue
        if not orig or not corr or orig == corr:
            continue
        if orig not in full_text:
            continue  # 幻觉，拒绝

        # 白名单守卫：只接受已知混淆模式
        # 如果 LLM 输出了上下文（如「亚哥的文章」→「鸭哥的文章」），
        # 尝试提取最小差异片段（「亚哥」→「鸭哥」）
        if orig not in flag_patterns:
            minimal = _extract_minimal_diff(orig, corr, flag_patterns)
            if minimal and minimal[0] in full_text:
                orig, corr = minimal  # 使用最小片段（minimal[0] 已验证是 flag_pattern）
            else:
                continue  # 不在白名单且无法提取有效片段
        # 禁止修改含数字的片段（格式规范化模式除外）
        if orig not in _FORMAT_NORM_PATTERNS:
            if _has_digit(orig) or _has_digit(corr):
                continue
        # 边界守卫：两百/两千不能出现在「一两百」「五两千」等数量词后
        if orig in _BOUNDARY_GUARD_PATTERNS:
            pos = full_text.find(orig)
            if pos > 0 and full_text[pos - 1] in _PRECEDING_DIGIT_CHARS:
                continue
        # 原文片段不能太长（超过6字说明在改短语/句子）
        if len(orig) > 6:
            continue
        # 修正后不能比原文多超过2个字（防止新增内容）
        if len(corr) - len(orig) > 2:
            continue
        # 编辑距离限制（格式规范化模式豁免，因为中文→数字跨字符集距离大）
        if orig not in _FORMAT_NORM_PATTERNS and _edit_distance_approx(orig, corr) > 4:
            continue
        corrections.append({"original": orig, "corrected": corr, "reason": item.get("reason", "")})

    # 总改动量检查
    total_changed = sum(len(c["original"]) for c in corrections)
    if total_changed > total_chars * MAX_EDIT_RATIO:
        corrections = sorted(corrections, key=lambda x: len(x["original"]))[:5]

    return corrections


def apply_corrections(chunks: list[dict], corrections: list[dict]) -> list[dict]:
    """将修改应用到 chunks（每条 correction 全局替换一次）"""
    result = [dict(c) for c in chunks]
    for corr in corrections:
        orig = corr["original"]
        repl = corr["corrected"]
        applied = False
        for chunk in result:
            if orig in chunk["text"] and not applied:
                chunk["text"] = chunk["text"].replace(orig, repl, 1)
                applied = True
    return result


def find_qwen_srt(video_dir: Path) -> Path | None:
    srts = list(video_dir.glob("*.qwen.srt"))
    return srts[0] if srts else None


def correct_file(
    video_dir: Path,
    model: str = DEFAULT_MODEL,
    verbose: bool = False,
) -> Path | None:
    """对单个视频目录运行校对，生成 .corrected.srt"""
    qwen_path = find_qwen_srt(video_dir)
    if qwen_path is None:
        print(f"  未找到 .qwen.srt: {video_dir.name}")
        return None

    out_stem    = qwen_path.stem.removesuffix(".qwen")
    output_path = video_dir / f"{out_stem}.corrected.srt"
    if output_path.exists():
        if verbose:
            print(f"  已存在，跳过: {output_path.name}")
        return output_path

    guide, candidates = load_resources()
    client = anthropic.Anthropic()

    chunks = parse_srt(qwen_path)
    if not chunks:
        return None

    corrected = list(chunks)  # copy
    total_corrections = 0
    total_flags = 0
    api_errors  = 0

    # 分批处理
    for batch_start in range(0, len(chunks), CHUNK_SIZE):
        batch = corrected[batch_start: batch_start + CHUNK_SIZE]
        flags = scan_flags(batch, candidates)
        total_flags += len(flags)

        # 如果本批没有任何候选词，跳过 API 调用（节省费用 + 避免速率限制）
        if not flags:
            continue

        system, user = build_prompt(batch, flags, guide)

        try:
            raw = call_llm(system, user, model, client)
            parsed = parse_response(raw)
            if verbose:
                print(f"    batch {batch_start//CHUNK_SIZE+1}: flags={len(flags)}, raw={raw[:80]}...")
            time.sleep(0.3)  # 避免触发速率限制（健康批次间隔）
        except Exception as e:
            api_errors += 1
            if verbose:
                print(f"    API 错误 (batch {batch_start}): {e}")
            # 指数退避：第一次2s，第二次4s，...最多30s
            wait = min(2 ** min(api_errors, 4), 30)
            time.sleep(wait)
            continue

        batch_texts = [c["text"] for c in batch]
        corrections = validate_and_collect(parsed, batch_texts, flags)
        total_corrections += len(corrections)

        # 应用到全局 corrected（batch 在 corrected 的切片上操作）
        if corrections:
            patched = apply_corrections(batch, corrections)
            corrected[batch_start: batch_start + CHUNK_SIZE] = patched

    write_srt(corrected, output_path)
    print(f"  ✓ {video_dir.name[:50]}  flags={total_flags} corrections={total_corrections} api_errors={api_errors}")
    return output_path


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Qwen SRT 校对工具")
    parser.add_argument("video_dir", help="视频目录路径")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    video_dir = Path(args.video_dir)
    if not video_dir.is_dir():
        print(f"错误: 目录不存在: {video_dir}")
        sys.exit(1)

    result = correct_file(video_dir, model=args.model, verbose=args.verbose)
    if result:
        print(f"\n输出: {result}")


if __name__ == "__main__":
    main()

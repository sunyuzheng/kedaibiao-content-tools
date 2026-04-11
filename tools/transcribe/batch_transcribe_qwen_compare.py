#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Qwen3-ASR 对比转录脚本（错题本数据集）

针对「已有精校字幕」的视频再跑一遍 Qwen3-ASR，输出保存为 .qwen.srt。
.qwen.srt vs 原 .zh.srt / .srt = 成对训练数据，供后续 LLM 校对使用。

扫描目标：
  - archive/有人工字幕/    （公开视频，info.json subtitles 字段有人工字幕）
  - archive/会员视频/      （有 .zh.srt 的会员视频）

跳过条件：已存在 .qwen.srt（可续跑）

用法：
  python3 tools/transcribe/batch_transcribe_qwen_compare.py
"""

import json
import sys
import time
import traceback
from pathlib import Path

try:
    from mlx_qwen3_asr import Session
except ImportError:
    print("错误: 未安装 mlx-qwen3-asr\n请运行: pip install mlx-qwen3-asr")
    sys.exit(1)

try:
    from tqdm import tqdm
except ImportError:
    print("错误: 未安装 tqdm\n请运行: pip install tqdm")
    sys.exit(1)

# ── 配置 ────────────────────────────────────────────────────────────────────
_PROJECT_ROOT = Path(__file__).parent.parent.parent

MODEL_ID   = "Qwen/Qwen3-ASR-1.7B"
LANGUAGE   = "Chinese"
AUDIO_EXTS = {".m4a", ".mp3", ".wav", ".mp4", ".flac"}

DIRS = {
    "public":  _PROJECT_ROOT / "archive" / "有人工字幕",
    "members": _PROJECT_ROOT / "archive" / "会员视频",
}
PROGRESS_FILE = _PROJECT_ROOT / "logs" / "transcribe_progress_qwen_compare.json"
# ────────────────────────────────────────────────────────────────────────────


def has_human_srt(audio_path: Path) -> bool:
    """检查是否有 YouTube 上传的人工字幕（带语言代码，如 .zh.srt / .en.srt）
    不包括 Batch1 生成的无语言码 .srt，也不包括我们生成的 .qwen.srt"""
    stem = audio_path.stem
    parent = audio_path.parent
    for lang in ("zh", "zh-Hans", "zh-Hant", "en", "en-zh", "en-orig"):
        if (parent / f"{stem}.{lang}.srt").exists():
            return True
    return False


def has_qwen_srt(audio_path: Path) -> bool:
    return (audio_path.parent / (audio_path.stem + ".qwen.srt")).exists()


def find_candidates(dirs: list[Path]) -> list[Path]:
    """找出：有精校字幕 且 还没有 .qwen.srt 的音频文件"""
    files = []
    for d in dirs:
        if not d.exists():
            print(f"警告: 目录不存在，跳过: {d}")
            continue
        for f in sorted(d.rglob("*")):
            if f.is_file() and f.suffix.lower() in AUDIO_EXTS:
                if has_human_srt(f) and not has_qwen_srt(f):
                    files.append(f)
    return files


def load_progress() -> dict:
    if PROGRESS_FILE.exists():
        with open(PROGRESS_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {"completed": [], "failed": []}


def save_progress(progress: dict) -> None:
    PROGRESS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(PROGRESS_FILE, "w", encoding="utf-8") as f:
        json.dump(progress, f, ensure_ascii=False, indent=2)


def _fmt_ts(seconds: float) -> str:
    ms = max(0, int(round(seconds * 1000)))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1_000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def write_qwen_srt(chunks: list, audio_path: Path) -> Path:
    srt_path = audio_path.parent / (audio_path.stem + ".qwen.srt")
    with open(srt_path, "w", encoding="utf-8") as f:
        for i, chunk in enumerate(chunks, 1):
            f.write(f"{i}\n{_fmt_ts(chunk['start'])} --> {_fmt_ts(chunk['end'])}\n{chunk['text'].strip()}\n\n")
    return srt_path


def transcribe_one(session: Session, audio_path: Path) -> dict:
    try:
        print(f"  → {audio_path.name}", flush=True)
        result = session.transcribe(
            str(audio_path),
            language=LANGUAGE,
            return_chunks=True,
            verbose=False,
        )
        chunks = result.chunks or []
        srt_path = write_qwen_srt(chunks, audio_path)
        return {
            "status": "success",
            "audio": str(audio_path),
            "qwen_srt": str(srt_path),
            "chunks": len(chunks),
            "language": result.language,
        }
    except Exception as e:
        return {
            "status": "error",
            "audio": str(audio_path),
            "error": str(e),
            "traceback": traceback.format_exc(),
        }


def run() -> None:
    progress = load_progress()
    completed_set = set(progress["completed"])
    failed_set    = set(progress["failed"])

    candidates = find_candidates(list(DIRS.values()))
    todo = [f for f in candidates
            if str(f) not in completed_set and str(f) not in failed_set]

    print(f"\n扫描完成:")
    print(f"  有精校字幕、待对比转录: {len(candidates)}")
    print(f"  已完成: {len(completed_set)},  已失败: {len(failed_set)}")
    print(f"  本次待处理: {len(todo)}")

    if not todo:
        print("\n所有文件均已处理完毕！")
        return

    print(f"\n正在加载模型: {MODEL_ID}\n")
    session = Session(MODEL_ID)

    batch_start  = time.time()
    success_count = 0

    with tqdm(todo, desc="对比转录进度", unit="文件") as bar:
        for audio_path in bar:
            t0     = time.time()
            result = transcribe_one(session, audio_path)
            elapsed = time.time() - t0

            if result["status"] == "success":
                progress["completed"].append(result["audio"])
                success_count += 1
                bar.set_postfix({
                    "成功": success_count,
                    "失败": len(progress["failed"]),
                    "耗时": f"{elapsed:.0f}s",
                    "chunks": result.get("chunks", "?"),
                })
            else:
                progress["failed"].append(result["audio"])
                tqdm.write(f"\n失败: {audio_path.name}")
                tqdm.write(f"  错误: {result['error']}")

            if (success_count + len(progress["failed"])) % 10 == 0:
                save_progress(progress)

    save_progress(progress)

    total = time.time() - batch_start
    print("\n" + "=" * 60)
    print("完成统计:")
    print(f"  成功生成 .qwen.srt: {len(progress['completed'])}")
    print(f"  失败: {len(progress['failed'])}")
    print(f"  总耗时: {total / 3600:.1f} 小时")
    print("=" * 60)
    print("\n下一步: 运行 tools/compare/build_error_notebook.py 生成错题本 JSON")


if __name__ == "__main__":
    print("=" * 60)
    print("Qwen3-ASR 对比转录（错题本数据集）")
    print(f"模型: {MODEL_ID}  语言: {LANGUAGE}")
    print("输出: 每个视频生成 .qwen.srt，供与精校字幕对比")
    print("=" * 60)
    run()

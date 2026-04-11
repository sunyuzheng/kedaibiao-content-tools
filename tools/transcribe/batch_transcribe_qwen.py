#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
批量音频转SRT字幕脚本 - Qwen3-ASR 版本
使用 mlx-qwen3-asr（Apple MLX 原生），中文效果优于 Whisper large-v3
模型：Qwen/Qwen3-ASR-1.7B（q8量化，M4 Pro约25x实时速度）

用法：
  # 转录「无人工字幕」目录（默认）
  python3 tools/transcribe/batch_transcribe_qwen.py

  # 转录「会员视频」目录
  python3 tools/transcribe/batch_transcribe_qwen.py --target members

  # 转录两个目录
  python3 tools/transcribe/batch_transcribe_qwen.py --target all

安装依赖：
  pip install mlx-qwen3-asr tqdm
"""

import argparse
import json
import sys
import time
import traceback
from pathlib import Path

try:
    from mlx_qwen3_asr import Session
except ImportError:
    print("错误: 未安装 mlx-qwen3-asr")
    print("请运行: pip install mlx-qwen3-asr")
    sys.exit(1)

try:
    from tqdm import tqdm
except ImportError:
    print("错误: 未安装 tqdm")
    print("请运行: pip install tqdm")
    sys.exit(1)

# ── 配置 ────────────────────────────────────────────────────────────────────
_PROJECT_ROOT = Path(__file__).parent.parent.parent

MODEL_ID   = "Qwen/Qwen3-ASR-1.7B"
LANGUAGE   = "Chinese"
AUDIO_EXTS = {".m4a", ".mp3", ".wav", ".mp4", ".flac", ".ogg", ".webm"}

DIRS = {
    "no_sub":  _PROJECT_ROOT / "archive" / "无人工字幕",
    "members": _PROJECT_ROOT / "archive" / "会员视频",
}
PROGRESS_FILE = _PROJECT_ROOT / "logs" / "transcribe_progress_qwen.json"
# ────────────────────────────────────────────────────────────────────────────


def load_progress() -> dict:
    if PROGRESS_FILE.exists():
        with open(PROGRESS_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {"completed": [], "failed": []}


def save_progress(progress: dict) -> None:
    PROGRESS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(PROGRESS_FILE, "w", encoding="utf-8") as f:
        json.dump(progress, f, ensure_ascii=False, indent=2)


def has_any_srt(audio_path: Path) -> bool:
    """检查是否已有任何 .srt 文件（含 .zh.srt / .en-zh.srt 等）"""
    stem = audio_path.stem
    return any(audio_path.parent.glob(f"{stem}*.srt"))


def find_audio_files(dirs: list[Path]) -> list[Path]:
    """找出所有还没有任何 .srt 文件的音频文件"""
    files = []
    for d in dirs:
        if not d.exists():
            print(f"警告: 目录不存在，跳过: {d}")
            continue
        for f in sorted(d.rglob("*")):
            if f.is_file() and f.suffix.lower() in AUDIO_EXTS:
                if not has_any_srt(f):
                    files.append(f)
    return files


def write_srt_from_chunks(chunks: list, srt_path: Path) -> None:
    """用 chunk 级别时间戳写 SRT（每个 chunk 已是自然句子边界）"""
    with open(srt_path, "w", encoding="utf-8") as f:
        for i, chunk in enumerate(chunks, 1):
            start = _fmt_ts(chunk["start"])
            end   = _fmt_ts(chunk["end"])
            text  = chunk["text"].strip()
            f.write(f"{i}\n{start} --> {end}\n{text}\n\n")


def _fmt_ts(seconds: float) -> str:
    """秒 → SRT 时间戳 HH:MM:SS,mmm"""
    ms = max(0, int(round(seconds * 1000)))
    h, ms  = divmod(ms, 3_600_000)
    m, ms  = divmod(ms,    60_000)
    s, ms  = divmod(ms,     1_000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def transcribe_one(session: Session, audio_path: Path) -> dict:
    """转录单个文件，返回结果字典"""
    srt_path = audio_path.with_suffix(".srt")
    try:
        print(f"  → {audio_path.name}", flush=True)
        result = session.transcribe(
            str(audio_path),
            language=LANGUAGE,
            return_chunks=True,   # chunk 级别时间戳，无需 forced aligner
            verbose=False,
        )
        chunks = result.chunks or []
        write_srt_from_chunks(chunks, srt_path)
        return {
            "status": "success",
            "audio": str(audio_path),
            "srt": str(srt_path),
            "language": result.language,
            "text_length": len(result.text),
            "chunks": len(chunks),
        }
    except Exception as e:
        return {
            "status": "error",
            "audio": str(audio_path),
            "error": str(e),
            "traceback": traceback.format_exc(),
        }


def run(target_dirs: list[Path]) -> None:
    progress = load_progress()
    completed_set = set(progress["completed"])
    failed_set    = set(progress["failed"])

    all_files = find_audio_files(target_dirs)
    todo = [f for f in all_files if str(f) not in completed_set and str(f) not in failed_set]

    print(f"\n扫描完成:")
    print(f"  需要转录的音频文件总数: {len(all_files)}")
    print(f"  已完成: {len(completed_set)},  已失败: {len(failed_set)}")
    print(f"  本次待处理: {len(todo)}")

    if not todo:
        print("\n所有文件均已处理完毕！")
        return

    print(f"\n正在加载模型: {MODEL_ID}")
    print("（首次运行会从 HuggingFace 下载，约 3-4 GB，请耐心等待）\n")

    session = Session(MODEL_ID)

    batch_start = time.time()
    success_count = 0

    with tqdm(todo, desc="转录进度", unit="文件") as bar:
        for audio_path in bar:
            file_start = time.time()
            result = transcribe_one(session, audio_path)
            elapsed = time.time() - file_start

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

            # 每 10 个文件保存一次进度
            if (success_count + len(progress["failed"])) % 10 == 0:
                save_progress(progress)

    save_progress(progress)

    total_time = time.time() - batch_start
    print("\n" + "=" * 60)
    print("完成统计:")
    print(f"  成功: {len(progress['completed'])}")
    print(f"  失败: {len(progress['failed'])}")
    print(f"  总耗时: {total_time / 3600:.1f} 小时")
    print("=" * 60)

    if progress["failed"]:
        print("\n失败文件列表（前10条）:")
        for p in progress["failed"][:10]:
            print(f"  - {Path(p).name}")
        if len(progress["failed"]) > 10:
            print(f"  ... 还有 {len(progress['failed']) - 10} 条")


def main() -> None:
    parser = argparse.ArgumentParser(description="Qwen3-ASR 批量转录工具")
    parser.add_argument(
        "--target",
        choices=["no_sub", "members", "all"],
        default="no_sub",
        help=(
            "转录目标目录: "
            "no_sub=archive/无人工字幕（默认）, "
            "members=archive/会员视频, "
            "all=两个都转"
        ),
    )
    args = parser.parse_args()

    if args.target == "no_sub":
        target_dirs = [DIRS["no_sub"]]
    elif args.target == "members":
        target_dirs = [DIRS["members"]]
    else:
        target_dirs = [DIRS["no_sub"], DIRS["members"]]

    print("=" * 60)
    print("Qwen3-ASR 批量转录工具（Apple Silicon / MLX）")
    print(f"模型:  {MODEL_ID}")
    print(f"语言:  {LANGUAGE}")
    print(f"目录:  {[str(d) for d in target_dirs]}")
    print("=" * 60)

    run(target_dirs)


if __name__ == "__main__":
    main()

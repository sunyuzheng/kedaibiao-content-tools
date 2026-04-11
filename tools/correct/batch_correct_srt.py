#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
批量校对工具

用法：
  # 只处理测试集（2026年视频，用于评估）
  python3 tools/correct/batch_correct_srt.py --test-only

  # 处理所有有 .qwen.srt 但没有 .corrected.srt 的视频
  python3 tools/correct/batch_correct_srt.py

  # 指定模型
  python3 tools/correct/batch_correct_srt.py --model claude-sonnet-4-6
"""

import argparse
import re
import sys
import time
from pathlib import Path

# 项目路径
_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(_ROOT / "tools" / "correct"))

from correct_srt import correct_file, DEFAULT_MODEL, HUMAN_LANGS

DIRS = [
    _ROOT / "archive" / "有人工字幕",
    _ROOT / "archive" / "会员视频",
]
TEST_DATE_FROM = "20260101"


def find_video_date(folder_name: str) -> str | None:
    m = re.match(r'^(\d{8})_', folder_name)
    return m.group(1) if m else None


def find_targets(test_only: bool) -> list[Path]:
    targets = []
    for base in DIRS:
        if not base.exists():
            continue
        for video_dir in sorted(base.iterdir()):
            if not video_dir.is_dir():
                continue
            date = find_video_date(video_dir.name)
            if date is None:
                continue
            if test_only and date < TEST_DATE_FROM:
                continue

            qwen_srts = list(video_dir.glob("*.qwen.srt"))
            if not qwen_srts:
                continue

            qwen_path = qwen_srts[0]
            stem = qwen_path.stem.removesuffix(".qwen")
            corrected = video_dir / f"{stem}.corrected.srt"
            if corrected.exists():
                continue  # 已完成

            targets.append(video_dir)

    return targets


def run(test_only: bool, model: str) -> None:
    targets = find_targets(test_only)
    scope = "测试集（2026年）" if test_only else "全量"
    print(f"待校对视频（{scope}）: {len(targets)} 个")
    print(f"模型: {model}")
    print()

    success, failed = 0, 0
    for i, video_dir in enumerate(targets, 1):
        print(f"[{i}/{len(targets)}] {video_dir.name[:60]}")
        try:
            result = correct_file(video_dir, model=model, verbose=False)
            if result:
                success += 1
            else:
                failed += 1
        except KeyboardInterrupt:
            print("\n中断")
            break
        except Exception as e:
            failed += 1
            print(f"  ✗ 异常: {e}")
            time.sleep(2)

    print(f"\n完成: 成功={success}  失败={failed}")
    print("\n下一步: python3 tools/correct/eval_correction.py")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--test-only", action="store_true", help="只处理测试集（2026年）")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    args = parser.parse_args()
    run(test_only=args.test_only, model=args.model)

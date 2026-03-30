#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
字幕文件整理脚本
下载完成后，根据是否有字幕文件（特别是人工字幕）来分类视频
"""

import os
import sys
import json
import shutil
from pathlib import Path


def has_manual_subtitle(video_dir):
    """
    检查视频目录是否有字幕文件
    返回: (has_manual_sub, has_auto_sub, subtitle_files)
    """
    video_path = Path(video_dir)
    if not video_path.is_dir():
        return False, False, []
    
    # 查找所有字幕文件
    subtitle_files = list(video_path.glob("*.srt"))
    
    if not subtitle_files:
        return False, False, []
    
    # 检查 info.json 来判断字幕类型（文件名可能与文件夹名不同，用 glob 查找）
    info_jsons = list(video_path.glob("*.info.json"))
    info_json = info_jsons[0] if info_jsons else None
    has_manual = False
    has_auto = False

    if info_json and info_json.exists():
        try:
            with open(info_json, 'r', encoding='utf-8') as f:
                info = json.load(f)
                
            # 检查字幕信息
            subtitles = info.get('subtitles', {})
            auto_captions = info.get('automatic_captions', {})
            
            # 检查是否有手动字幕
            # 优先检查：如果 subtitles 字典不为空，说明有手动字幕
            if subtitles:
                # 检查是否有我们需要的语言（中文或英文）
                target_langs = ['zh', 'zh-Hans', 'zh-Hant', 'en', 'en-US', 'en-GB']
                for lang_code in target_langs:
                    if lang_code in subtitles:
                        has_manual = True
                        break
                # 如果还没有找到，检查是否有任何中文或英文变体
                if not has_manual:
                    for key in subtitles.keys():
                        # 检查是否是中文（zh开头）或英文（en开头）
                        if key.startswith('zh') or key.startswith('en'):
                            has_manual = True
                            break
            
            # 检查是否有自动字幕（但只有在没有手动字幕时才考虑）
            if not has_manual and auto_captions:
                target_langs = ['zh', 'zh-Hans', 'zh-Hant', 'en', 'en-US', 'en-GB']
                for lang_code in target_langs:
                    if lang_code in auto_captions:
                        has_auto = True
                        break
                # 如果还没有找到，检查是否有任何中文或英文变体
                if not has_auto:
                    for key in auto_captions.keys():
                        if key.startswith('zh') or key.startswith('en'):
                            has_auto = True
                            break
        except Exception as e:
            print(f"警告: 无法解析 {info_json}: {e}")
            # 如果无法解析，假设有字幕文件就是手动字幕
            has_manual = True
    
    # 注意：不再用"有 .srt 文件就假设是人工字幕"的逻辑。
    # .srt 可能来自 yt-dlp 下载的自动字幕，或本地 Whisper 转录。
    # 只有 info.json subtitles 字段含 zh/en 语言码才算真正的人工字幕。
    return has_manual, has_auto, subtitle_files


def organize_videos(archive_dir):
    """
    整理视频文件夹，根据是否有字幕分类
    """
    archive_path = Path(archive_dir)
    if not archive_path.exists():
        print(f"错误: 目录不存在: {archive_dir}")
        return
    
    with_subtitles_dir = archive_path / "有人工字幕"
    without_subtitles_dir = archive_path / "无人工字幕"
    
    # 创建分类目录
    with_subtitles_dir.mkdir(exist_ok=True)
    without_subtitles_dir.mkdir(exist_ok=True)
    
    # 统计信息
    stats = {
        'total': 0,
        'with_manual_sub': 0,
        'with_auto_sub_only': 0,
        'without_sub': 0,
        'skipped': 0
    }
    
    # 遍历所有视频文件夹
    for video_dir in archive_path.iterdir():
        # 跳过分类目录和文件
        if not video_dir.is_dir():
            continue
        if video_dir.name in ['有人工字幕', '无人工字幕']:
            continue
        if video_dir.name.startswith('NA_'):
            continue  # 跳过播放列表文件夹
        
        stats['total'] += 1
        
        has_manual, has_auto, subtitle_files = has_manual_subtitle(video_dir)
        
        # 确定目标目录
        if has_manual:
            target_dir = with_subtitles_dir
            stats['with_manual_sub'] += 1
            status = "有人工字幕"
        elif has_auto:
            target_dir = without_subtitles_dir
            stats['with_auto_sub_only'] += 1
            status = "仅自动字幕"
        else:
            target_dir = without_subtitles_dir
            stats['without_sub'] += 1
            status = "无字幕"
        
        # 移动文件夹
        target_path = target_dir / video_dir.name
        if target_path.exists():
            print(f"跳过 (已存在): {video_dir.name} [{status}]")
            stats['skipped'] += 1
        else:
            try:
                shutil.move(str(video_dir), str(target_path))
                sub_info = f" ({len(subtitle_files)} 个字幕文件)" if subtitle_files else ""
                print(f"已分类: {video_dir.name} -> {status}{sub_info}")
            except Exception as e:
                print(f"错误: 无法移动 {video_dir.name}: {e}")
    
    # 打印统计信息
    print("\n" + "=" * 50)
    print("分类统计:")
    print("=" * 50)
    print(f"总视频数: {stats['total']}")
    print(f"  有人工字幕: {stats['with_manual_sub']}")
    print(f"  仅自动字幕: {stats['with_auto_sub_only']}")
    print(f"  无字幕: {stats['without_sub']}")
    print(f"  跳过(已存在): {stats['skipped']}")
    print("=" * 50)


def main():
    if len(sys.argv) < 2:
        archive_dir = "archive"
    else:
        archive_dir = sys.argv[1]
    
    print("=" * 50)
    print("开始整理字幕文件...")
    print("=" * 50)
    print()
    
    organize_videos(archive_dir)


if __name__ == "__main__":
    main()

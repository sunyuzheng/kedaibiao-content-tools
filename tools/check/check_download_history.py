#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
检查下载历史，确保所有已下载的视频都在历史文件中
"""

import re
from pathlib import Path

ARCHIVE_DIR = Path("archive")
HISTORY_FILE = ARCHIVE_DIR / "downloaded_history.txt"

def extract_video_id(folder_name):
    """从文件夹名称中提取视频ID"""
    # 视频ID通常是11个字符，在文件夹名称的最后
    match = re.search(r'([A-Za-z0-9_-]{11})$', folder_name)
    return match.group(1) if match else None

def main():
    print("=" * 50)
    print("检查下载历史完整性...")
    print("=" * 50)
    print()
    
    # 读取下载历史
    if not HISTORY_FILE.exists():
        print(f"错误: 下载历史文件不存在: {HISTORY_FILE}")
        return
    
    with open(HISTORY_FILE, 'r', encoding='utf-8') as f:
        history_lines = f.readlines()
    
    # 提取历史中的视频ID
    history_ids = set()
    for line in history_lines:
        line = line.strip()
        if line.startswith('youtube '):
            video_id = line.replace('youtube ', '').strip()
            if video_id:
                history_ids.add(video_id)
    
    print(f"下载历史中的视频ID数量: {len(history_ids)}")
    print()
    
    # 检查所有视频文件夹
    all_video_ids = set()
    missing_from_history = []
    
    # 检查根目录
    for folder in ARCHIVE_DIR.iterdir():
        if folder.is_dir() and folder.name not in ['有人工字幕', '无人工字幕']:
            if not folder.name.startswith('NA_'):
                video_id = extract_video_id(folder.name)
                if video_id:
                    all_video_ids.add(video_id)
                    if video_id not in history_ids:
                        missing_from_history.append((folder.name, video_id))
    
    # 检查分类目录
    for category_dir in ['有人工字幕', '无人工字幕']:
        category_path = ARCHIVE_DIR / category_dir
        if category_path.exists():
            for folder in category_path.iterdir():
                if folder.is_dir():
                    video_id = extract_video_id(folder.name)
                    if video_id:
                        all_video_ids.add(video_id)
                        if video_id not in history_ids:
                            missing_from_history.append((folder.name, video_id))
    
    print(f"找到的视频文件夹总数: {len(all_video_ids)}")
    print()
    
    # 检查缺失的
    if missing_from_history:
        print(f"⚠ 警告: 发现 {len(missing_from_history)} 个视频不在下载历史中:")
        for folder_name, video_id in missing_from_history[:10]:
            print(f"  - {folder_name} (ID: {video_id})")
        if len(missing_from_history) > 10:
            print(f"  ... 还有 {len(missing_from_history) - 10} 个")
        print()
        print("建议: 将这些视频ID添加到下载历史文件中")
    else:
        print("✓ 所有视频都在下载历史中！")
    
    # 检查历史中是否有不存在的视频
    extra_in_history = history_ids - all_video_ids
    if extra_in_history:
        print()
        print(f"ℹ 历史中有 {len(extra_in_history)} 个视频ID在当前文件夹中找不到")
        print("  （这可能是正常的，如果视频被删除或重命名）")
    
    print()
    print("=" * 50)
    print("检查完成")
    print("=" * 50)

if __name__ == "__main__":
    main()


#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
检查数据完整性 - 为LLM处理做准备
检查空文件夹、缺失文件、数据质量等
"""

import json
import os
from pathlib import Path
from collections import defaultdict

ARCHIVE_DIR = Path("archive")
REPORT_FILE = Path("数据完整性报告.md")

def check_video_folder(folder_path):
    """检查单个视频文件夹的完整性"""
    issues = []
    files = list(folder_path.iterdir())
    
    # 必需文件
    has_audio = any(f.suffix == '.m4a' and '.part' not in f.name for f in files)
    has_info_json = any(f.suffix == '.json' and 'info' in f.name for f in files)
    has_description = any('description' in f.name for f in files)
    has_thumbnail = any(f.suffix in ['.jpg', '.webp', '.png'] for f in files)
    
    # 可选但重要的文件
    has_subtitle = any(f.suffix == '.srt' for f in files)
    
    # 检查问题
    if not has_audio:
        issues.append("缺少音频文件")
    if not has_info_json:
        issues.append("缺少info.json元数据")
    if not has_description:
        issues.append("缺少description文件")
    if not has_thumbnail:
        issues.append("缺少缩略图")
    
    # 检查info.json内容
    info_data = None
    if has_info_json:
        info_file = next((f for f in files if f.suffix == '.json' and 'info' in f.name), None)
        if info_file:
            try:
                with open(info_file, 'r', encoding='utf-8') as f:
                    info_data = json.load(f)
                
                # 检查关键字段
                if not info_data.get('title'):
                    issues.append("info.json缺少title")
                if not info_data.get('description'):
                    issues.append("info.json缺少description")
                if not info_data.get('upload_date'):
                    issues.append("info.json缺少upload_date")
                if not info_data.get('duration'):
                    issues.append("info.json缺少duration")
            except Exception as e:
                issues.append(f"无法解析info.json: {e}")
    
    return {
        'has_audio': has_audio,
        'has_info_json': has_info_json,
        'has_description': has_description,
        'has_thumbnail': has_thumbnail,
        'has_subtitle': has_subtitle,
        'issues': issues,
        'info_data': info_data,
        'file_count': len(files)
    }

def check_all_videos():
    """检查所有视频文件夹"""
    print("检查数据完整性...")
    
    stats = {
        'total_folders': 0,
        'empty_folders': [],
        'missing_audio': [],
        'missing_metadata': [],
        'missing_subtitle': [],
        'incomplete_folders': [],
        'complete_folders': 0,
        'with_subtitles': 0,
        'categories': defaultdict(int)
    }
    
    # 检查所有文件夹
    all_folders = []
    
    # 根目录
    for folder in ARCHIVE_DIR.iterdir():
        if folder.is_dir() and folder.name not in ['有人工字幕', '无人工字幕']:
            if not folder.name.startswith('NA_'):
                all_folders.append(('根目录', folder))
    
    # 分类目录
    for category in ['有人工字幕', '无人工字幕']:
        category_path = ARCHIVE_DIR / category
        if category_path.exists():
            for folder in category_path.iterdir():
                if folder.is_dir():
                    all_folders.append((category, folder))
    
    stats['total_folders'] = len(all_folders)
    
    for category, folder in all_folders:
        stats['categories'][category] += 1
        
        # 检查文件夹
        result = check_video_folder(folder)
        
        if result['file_count'] == 0:
            stats['empty_folders'].append((category, folder.name))
        elif result['issues']:
            stats['incomplete_folders'].append({
                'category': category,
                'folder': folder.name,
                'issues': result['issues']
            })
            if not result['has_audio']:
                stats['missing_audio'].append((category, folder.name))
            if not result['has_info_json']:
                stats['missing_metadata'].append((category, folder.name))
        else:
            stats['complete_folders'] += 1
        
        if result['has_subtitle']:
            stats['with_subtitles'] += 1
    
    return stats

def generate_report(stats):
    """生成报告"""
    md_content = f"""# 数据完整性报告

**生成时间**: {os.popen('date').read().strip()}

## 📊 总体统计

- **总文件夹数**: {stats['total_folders']}
- **完整文件夹**: {stats['complete_folders']} ({stats['complete_folders']*100//stats['total_folders'] if stats['total_folders'] > 0 else 0}%)
- **不完整文件夹**: {len(stats['incomplete_folders'])}
- **空文件夹**: {len(stats['empty_folders'])}
- **有字幕的文件夹**: {stats['with_subtitles']}

## ⚠️ 发现的问题

### 空文件夹 ({len(stats['empty_folders'])} 个)

"""
    
    if stats['empty_folders']:
        for category, folder_name in stats['empty_folders'][:20]:
            md_content += f"- `{folder_name}` ({category})\n"
        if len(stats['empty_folders']) > 20:
            md_content += f"\n*（仅显示前20个，共 {len(stats['empty_folders'])} 个空文件夹）*\n"
    else:
        md_content += "✅ 无空文件夹\n"
    
    md_content += f"""
### 缺少音频文件 ({len(stats['missing_audio'])} 个)

"""
    
    if stats['missing_audio']:
        for category, folder_name in stats['missing_audio'][:20]:
            md_content += f"- `{folder_name}` ({category})\n"
        if len(stats['missing_audio']) > 20:
            md_content += f"\n*（仅显示前20个，共 {len(stats['missing_audio'])} 个）*\n"
    else:
        md_content += "✅ 所有文件夹都有音频文件\n"
    
    md_content += f"""
### 缺少元数据 ({len(stats['missing_metadata'])} 个)

"""
    
    if stats['missing_metadata']:
        for category, folder_name in stats['missing_metadata'][:20]:
            md_content += f"- `{folder_name}` ({category})\n"
        if len(stats['missing_metadata']) > 20:
            md_content += f"\n*（仅显示前20个，共 {len(stats['missing_metadata'])} 个）*\n"
    else:
        md_content += "✅ 所有文件夹都有元数据\n"
    
    md_content += f"""
### 不完整文件夹详情 ({len(stats['incomplete_folders'])} 个)

"""
    
    if stats['incomplete_folders']:
        for item in stats['incomplete_folders'][:30]:
            md_content += f"- **{item['folder']}** ({item['category']})\n"
            for issue in item['issues']:
                md_content += f"  - {issue}\n"
        if len(stats['incomplete_folders']) > 30:
            md_content += f"\n*（仅显示前30个，共 {len(stats['incomplete_folders'])} 个）*\n"
    else:
        md_content += "✅ 所有文件夹都完整\n"
    
    md_content += f"""
## 📁 分类统计

"""
    
    for category, count in sorted(stats['categories'].items()):
        md_content += f"- **{category}**: {count} 个文件夹\n"
    
    md_content += f"""
## ✅ 数据质量评估

### 为LLM处理准备的检查清单

- ✅ **音频文件**: {stats['complete_folders']}/{stats['total_folders']} 个文件夹有音频 ({stats['complete_folders']*100//stats['total_folders'] if stats['total_folders'] > 0 else 0}%)
- ✅ **元数据**: {stats['total_folders'] - len(stats['missing_metadata'])}/{stats['total_folders']} 个文件夹有元数据 ({(stats['total_folders'] - len(stats['missing_metadata']))*100//stats['total_folders'] if stats['total_folders'] > 0 else 0}%)
- ✅ **字幕文件**: {stats['with_subtitles']}/{stats['total_folders']} 个文件夹有字幕 ({stats['with_subtitles']*100//stats['total_folders'] if stats['total_folders'] > 0 else 0}%)

### LLM处理建议

1. **转录优先级**:
   - 优先处理有字幕的视频（{stats['with_subtitles']} 个），可直接使用字幕文本
   - 无字幕的视频（{stats['total_folders'] - stats['with_subtitles']} 个）需要使用Whisper转录

2. **数据完整性**:
   - {len(stats['incomplete_folders'])} 个文件夹需要补充数据
   - {len(stats['empty_folders'])} 个空文件夹需要重新下载

3. **元数据利用**:
   - 所有有info.json的视频都可以提取标题、描述、标签等信息
   - 可用于生成newsletter、shownotes等

---
*最后更新: {os.popen('date').read().strip()}*
"""
    
    with open(REPORT_FILE, 'w', encoding='utf-8') as f:
        f.write(md_content)
    
    print(f"✓ 报告已生成: {REPORT_FILE}")

def main():
    print("=" * 60)
    print("数据完整性检查")
    print("=" * 60)
    print()
    
    stats = check_all_videos()
    generate_report(stats)
    
    print()
    print("=" * 60)
    print("检查完成")
    print("=" * 60)
    print()
    print(f"总文件夹: {stats['total_folders']}")
    print(f"完整文件夹: {stats['complete_folders']}")
    print(f"不完整文件夹: {len(stats['incomplete_folders'])}")
    print(f"空文件夹: {len(stats['empty_folders'])}")
    print(f"有字幕: {stats['with_subtitles']}")

if __name__ == "__main__":
    main()




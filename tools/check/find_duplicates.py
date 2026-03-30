#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
查找Transistor.fm中的重复Episodes

功能：
1. 通过多种方式检测重复（标题、video_url、media_url、duration等）
2. 生成详细的重复报告（JSON和CSV格式）
3. 提供处理建议（保留哪个、删除哪个）
"""

import json
import csv
import re
from pathlib import Path
from typing import List, Dict, Set, Tuple
from collections import defaultdict
from datetime import datetime
import difflib

# 项目路径
PROJECT_ROOT = Path(__file__).parent.parent.parent
EPISODES_FILE = PROJECT_ROOT / "tools/upload/all_episodes.json"
OUTPUT_DIR = PROJECT_ROOT / "tools/upload"
DUPLICATES_JSON = OUTPUT_DIR / "duplicates_report.json"
DUPLICATES_CSV = OUTPUT_DIR / "duplicates_report.csv"


def normalize_title(title: str) -> str:
    """标准化标题，用于比较
    
    1. 去除EP前缀（EP1_, EP2_等）
    2. 去除前后空格
    3. 转换为小写
    """
    if not title:
        return ""
    
    # 去除EP前缀（EP数字_）
    title = re.sub(r'^EP\d+_?\s*', '', title, flags=re.IGNORECASE)
    
    # 去除前后空格并转小写
    return title.strip().lower()


def extract_video_id(video_url: str) -> str:
    """从YouTube URL中提取video ID"""
    if not video_url:
        return ""
    
    # 匹配各种YouTube URL格式
    patterns = [
        r'(?:youtube\.com/watch\?v=|youtu\.be/|youtube\.com/embed/)([a-zA-Z0-9_-]{11})',
    ]
    
    for pattern in patterns:
        match = re.search(pattern, video_url)
        if match:
            return match.group(1)
    
    return ""


def extract_media_id(media_url: str) -> str:
    """从media_url中提取唯一标识"""
    if not media_url:
        return ""
    
    # media_url格式: https://media.transistor.fm/{share_id}/{file_id}.mp3
    # 提取share_id和file_id的组合作为唯一标识
    match = re.search(r'/media\.transistor\.fm/([^/]+)/([^/]+)\.', media_url)
    if match:
        return f"{match.group(1)}_{match.group(2)}"
    
    return ""


def calculate_similarity(str1: str, str2: str) -> float:
    """计算两个字符串的相似度（0-1）"""
    if not str1 or not str2:
        return 0.0
    
    return difflib.SequenceMatcher(None, str1.lower(), str2.lower()).ratio()


def find_duplicates_by_title(episodes: List[Dict]) -> Dict[str, List[Dict]]:
    """通过标题查找重复"""
    title_groups = defaultdict(list)
    
    for ep in episodes:
        title = ep.get("title", "")
        normalized = normalize_title(title)
        if normalized:
            title_groups[normalized].append(ep)
    
    # 只返回有重复的组
    duplicates = {k: v for k, v in title_groups.items() if len(v) > 1}
    return duplicates


def find_duplicates_by_video_url(episodes: List[Dict]) -> Dict[str, List[Dict]]:
    """通过YouTube video URL查找重复"""
    video_groups = defaultdict(list)
    
    for ep in episodes:
        video_url = ep.get("video_url", "")
        video_id = extract_video_id(video_url)
        if video_id:
            video_groups[video_id].append(ep)
    
    # 只返回有重复的组
    duplicates = {k: v for k, v in video_groups.items() if len(v) > 1}
    return duplicates


def find_duplicates_by_media_url(episodes: List[Dict]) -> Dict[str, List[Dict]]:
    """通过音频文件URL查找重复"""
    media_groups = defaultdict(list)
    
    for ep in episodes:
        media_url = ep.get("media_url", "")
        media_id = extract_media_id(media_url)
        if media_id:
            media_groups[media_id].append(ep)
    
    # 只返回有重复的组
    duplicates = {k: v for k, v in media_groups.items() if len(v) > 1}
    return duplicates


def find_duplicates_by_duration(episodes: List[Dict], tolerance: int = 5) -> Dict[int, List[Dict]]:
    """通过时长查找重复（允许一定误差）"""
    duration_groups = defaultdict(list)
    
    for ep in episodes:
        duration = ep.get("duration")
        if duration:
            # 将时长四舍五入到最近的tolerance秒
            rounded = (duration // tolerance) * tolerance
            duration_groups[rounded].append(ep)
    
    # 只返回有重复的组
    duplicates = {k: v for k, v in duration_groups.items() if len(v) > 1}
    return duplicates


def find_similar_titles(episodes: List[Dict], threshold: float = 0.85) -> List[Tuple[Dict, Dict, float]]:
    """查找相似的标题（使用字符串相似度）"""
    similar_pairs = []
    
    for i, ep1 in enumerate(episodes):
        title1 = normalize_title(ep1.get("title", ""))
        if not title1:
            continue
        
        for ep2 in episodes[i+1:]:
            title2 = normalize_title(ep2.get("title", ""))
            if not title2:
                continue
            
            similarity = calculate_similarity(title1, title2)
            if similarity >= threshold:
                similar_pairs.append((ep1, ep2, similarity))
    
    return similar_pairs


def recommend_keep(episodes: List[Dict]) -> Dict:
    """推荐保留哪个episode
    
    优先级：
    1. 有完整信息（thumbnail、描述、video_url）
    2. 状态为published
    3. 编号较小（较早的）
    4. 有transcript
    """
    if not episodes:
        return None
    
    if len(episodes) == 1:
        return episodes[0]
    
    def score(ep: Dict) -> Tuple[int, int, int, int]:
        """计算得分，返回元组用于排序"""
        # 完整性得分（越高越好）
        completeness = 0
        if ep.get("image_url"):
            completeness += 10
        if ep.get("description"):
            completeness += 10
        if ep.get("video_url"):
            completeness += 10
        if ep.get("transcript_url") or ep.get("transcripts"):
            completeness += 5
        
        # 状态得分（published > scheduled > draft）
        status_score = {"published": 3, "scheduled": 2, "draft": 1}.get(ep.get("status"), 0)
        
        # 编号（越小越好，所以用负数）
        number = ep.get("episode_number") or 999999
        
        # 创建时间（越早越好，所以用负数）
        created_at = ep.get("created_at", "")
        created_timestamp = 0
        if created_at:
            try:
                dt = datetime.fromisoformat(created_at.replace('Z', '+00:00'))
                created_timestamp = -int(dt.timestamp())
            except:
                pass
        
        return (-completeness, -status_score, number, created_timestamp)
    
    # 按得分排序
    sorted_episodes = sorted(episodes, key=score)
    return sorted_episodes[0]


def analyze_duplicates(episodes: List[Dict]) -> Dict:
    """分析所有重复"""
    print("🔍 正在分析重复episodes...")
    
    results = {
        "analyzed_at": datetime.now().isoformat(),
        "total_episodes": len(episodes),
        "duplicates_by_title": [],
        "duplicates_by_video_url": [],
        "duplicates_by_media_url": [],
        "duplicates_by_duration": [],
        "similar_titles": [],
        "summary": {}
    }
    
    # 1. 按标题查找重复
    print("  检查标题重复...")
    title_dups = find_duplicates_by_title(episodes)
    for normalized_title, dup_episodes in title_dups.items():
        keep = recommend_keep(dup_episodes)
        results["duplicates_by_title"].append({
            "normalized_title": normalized_title,
            "count": len(dup_episodes),
            "episodes": dup_episodes,
            "recommend_keep": keep.get("episode_id") if keep else None
        })
    
    # 2. 按video URL查找重复
    print("  检查YouTube video URL重复...")
    video_dups = find_duplicates_by_video_url(episodes)
    for video_id, dup_episodes in video_dups.items():
        keep = recommend_keep(dup_episodes)
        results["duplicates_by_video_url"].append({
            "video_id": video_id,
            "count": len(dup_episodes),
            "episodes": dup_episodes,
            "recommend_keep": keep.get("episode_id") if keep else None
        })
    
    # 3. 按media URL查找重复
    print("  检查音频文件重复...")
    media_dups = find_duplicates_by_media_url(episodes)
    for media_id, dup_episodes in media_dups.items():
        keep = recommend_keep(dup_episodes)
        results["duplicates_by_media_url"].append({
            "media_id": media_id,
            "count": len(dup_episodes),
            "episodes": dup_episodes,
            "recommend_keep": keep.get("episode_id") if keep else None
        })
    
    # 4. 按时长查找重复
    print("  检查时长重复...")
    duration_dups = find_duplicates_by_duration(episodes)
    for duration, dup_episodes in duration_dups.items():
        keep = recommend_keep(dup_episodes)
        results["duplicates_by_duration"].append({
            "duration_seconds": duration,
            "duration_formatted": f"{duration//60}:{duration%60:02d}",
            "count": len(dup_episodes),
            "episodes": dup_episodes,
            "recommend_keep": keep.get("episode_id") if keep else None
        })
    
    # 5. 查找相似标题
    print("  检查相似标题...")
    similar = find_similar_titles(episodes)
    for ep1, ep2, similarity in similar:
        # 检查是否已经在其他重复组中
        already_found = False
        for dup_group in results["duplicates_by_title"]:
            ep1_id = ep1.get("episode_id")
            ep2_id = ep2.get("episode_id")
            if any(e.get("episode_id") == ep1_id for e in dup_group["episodes"]) and \
               any(e.get("episode_id") == ep2_id for e in dup_group["episodes"]):
                already_found = True
                break
        
        if not already_found:
            results["similar_titles"].append({
                "similarity": round(similarity, 3),
                "episode1": ep1,
                "episode2": ep2
            })
    
    # 生成摘要
    results["summary"] = {
        "duplicates_by_title_count": len(results["duplicates_by_title"]),
        "duplicates_by_video_url_count": len(results["duplicates_by_video_url"]),
        "duplicates_by_media_url_count": len(results["duplicates_by_media_url"]),
        "duplicates_by_duration_count": len(results["duplicates_by_duration"]),
        "similar_titles_count": len(results["similar_titles"]),
        "total_duplicate_episodes": len(set(
            ep.get("episode_id")
            for group in [
                results["duplicates_by_title"],
                results["duplicates_by_video_url"],
                results["duplicates_by_media_url"],
                results["duplicates_by_duration"]
            ]
            for item in group
            for ep in item["episodes"]
        ))
    }
    
    return results


def export_to_csv(results: Dict, output_file: Path):
    """导出重复报告到CSV"""
    with open(output_file, 'w', newline='', encoding='utf-8') as f:
        writer = csv.writer(f)
        
        # 写入标题行
        writer.writerow([
            "重复类型", "重复标识", "Episode ID", "Episode Number", "标题",
            "状态", "YouTube URL", "音频URL", "时长", "有Thumbnail",
            "有描述", "有Transcript", "发布时间", "建议保留"
        ])
        
        # 写入标题重复
        for dup in results["duplicates_by_title"]:
            for ep in dup["episodes"]:
                writer.writerow([
                    "标题重复",
                    dup["normalized_title"],
                    ep.get("episode_id"),
                    ep.get("episode_number"),
                    ep.get("title"),
                    ep.get("status"),
                    ep.get("video_url", ""),
                    ep.get("media_url", ""),
                    ep.get("duration_in_mmss", ""),
                    "是" if ep.get("image_url") else "否",
                    "是" if ep.get("description") else "否",
                    "是" if ep.get("transcript_url") or ep.get("transcripts") else "否",
                    ep.get("published_at", ""),
                    "是" if ep.get("episode_id") == dup["recommend_keep"] else "否"
                ])
        
        # 写入video URL重复
        for dup in results["duplicates_by_video_url"]:
            for ep in dup["episodes"]:
                writer.writerow([
                    "Video URL重复",
                    dup["video_id"],
                    ep.get("episode_id"),
                    ep.get("episode_number"),
                    ep.get("title"),
                    ep.get("status"),
                    ep.get("video_url", ""),
                    ep.get("media_url", ""),
                    ep.get("duration_in_mmss", ""),
                    "是" if ep.get("image_url") else "否",
                    "是" if ep.get("description") else "否",
                    "是" if ep.get("transcript_url") or ep.get("transcripts") else "否",
                    ep.get("published_at", ""),
                    "是" if ep.get("episode_id") == dup["recommend_keep"] else "否"
                ])
        
        # 写入media URL重复
        for dup in results["duplicates_by_media_url"]:
            for ep in dup["episodes"]:
                writer.writerow([
                    "音频文件重复",
                    dup["media_id"],
                    ep.get("episode_id"),
                    ep.get("episode_number"),
                    ep.get("title"),
                    ep.get("status"),
                    ep.get("video_url", ""),
                    ep.get("media_url", ""),
                    ep.get("duration_in_mmss", ""),
                    "是" if ep.get("image_url") else "否",
                    "是" if ep.get("description") else "否",
                    "是" if ep.get("transcript_url") or ep.get("transcripts") else "否",
                    ep.get("published_at", ""),
                    "是" if ep.get("episode_id") == dup["recommend_keep"] else "否"
                ])
        
        # 写入时长重复
        for dup in results["duplicates_by_duration"]:
            for ep in dup["episodes"]:
                writer.writerow([
                    "时长重复",
                    dup["duration_formatted"],
                    ep.get("episode_id"),
                    ep.get("episode_number"),
                    ep.get("title"),
                    ep.get("status"),
                    ep.get("video_url", ""),
                    ep.get("media_url", ""),
                    ep.get("duration_in_mmss", ""),
                    "是" if ep.get("image_url") else "否",
                    "是" if ep.get("description") else "否",
                    "是" if ep.get("transcript_url") or ep.get("transcripts") else "否",
                    ep.get("published_at", ""),
                    "是" if ep.get("episode_id") == dup["recommend_keep"] else "否"
                ])
        
        # 写入相似标题
        for sim in results["similar_titles"]:
            ep1 = sim["episode1"]
            ep2 = sim["episode2"]
            writer.writerow([
                f"相似标题 (相似度: {sim['similarity']:.1%})",
                "",
                ep1.get("episode_id"),
                ep1.get("episode_number"),
                ep1.get("title"),
                ep1.get("status"),
                ep1.get("video_url", ""),
                ep1.get("media_url", ""),
                ep1.get("duration_in_mmss", ""),
                "是" if ep1.get("image_url") else "否",
                "是" if ep1.get("description") else "否",
                "是" if ep1.get("transcript_url") or ep1.get("transcripts") else "否",
                ep1.get("published_at", ""),
                "否"
            ])
            writer.writerow([
                f"相似标题 (相似度: {sim['similarity']:.1%})",
                "",
                ep2.get("episode_id"),
                ep2.get("episode_number"),
                ep2.get("title"),
                ep2.get("status"),
                ep2.get("video_url", ""),
                ep2.get("media_url", ""),
                ep2.get("duration_in_mmss", ""),
                "是" if ep2.get("image_url") else "否",
                "是" if ep2.get("description") else "否",
                "是" if ep2.get("transcript_url") or ep2.get("transcripts") else "否",
                ep2.get("published_at", ""),
                "否"
            ])


def print_summary(results: Dict):
    """打印摘要信息"""
    summary = results["summary"]
    
    print("\n" + "=" * 60)
    print("📊 重复分析摘要")
    print("=" * 60)
    print(f"总episode数: {results['total_episodes']}")
    print(f"\n重复统计:")
    print(f"  - 标题重复: {summary['duplicates_by_title_count']} 组")
    print(f"  - Video URL重复: {summary['duplicates_by_video_url_count']} 组")
    print(f"  - 音频文件重复: {summary['duplicates_by_media_url_count']} 组")
    print(f"  - 时长重复: {summary['duplicates_by_duration_count']} 组")
    print(f"  - 相似标题: {summary['similar_titles_count']} 对")
    print(f"\n涉及重复的episode总数: {summary['total_duplicate_episodes']}")
    
    if summary['duplicates_by_title_count'] > 0:
        print(f"\n📝 标题重复详情:")
        for dup in results["duplicates_by_title"][:5]:  # 只显示前5个
            print(f"  - \"{dup['normalized_title']}\": {dup['count']} 个episodes")
            for ep in dup["episodes"]:
                keep_mark = "⭐" if ep.get("episode_id") == dup["recommend_keep"] else "  "
                print(f"    {keep_mark} EP{ep.get('episode_number')} - {ep.get('title')}")
    
    if summary['duplicates_by_video_url_count'] > 0:
        print(f"\n🎥 Video URL重复详情:")
        for dup in results["duplicates_by_video_url"][:5]:  # 只显示前5个
            print(f"  - Video ID: {dup['video_id']}: {dup['count']} 个episodes")
            for ep in dup["episodes"]:
                keep_mark = "⭐" if ep.get("episode_id") == dup["recommend_keep"] else "  "
                print(f"    {keep_mark} EP{ep.get('episode_number')} - {ep.get('title')}")


def main():
    """主函数"""
    print("=" * 60)
    print("🔍 查找Transistor.fm重复Episodes")
    print("=" * 60)
    
    # 检查文件是否存在
    if not EPISODES_FILE.exists():
        print(f"❌ 文件不存在: {EPISODES_FILE}")
        print("   请先运行 export_episodes.py 导出所有episodes")
        return
    
    # 读取episodes
    print(f"\n📖 正在读取: {EPISODES_FILE}")
    with open(EPISODES_FILE, 'r', encoding='utf-8') as f:
        data = json.load(f)
    
    episodes = data.get("episodes", [])
    if not episodes:
        print("❌ 未找到episodes数据")
        return
    
    print(f"✅ 已读取 {len(episodes)} 个episodes")
    
    # 分析重复
    results = analyze_duplicates(episodes)
    
    # 保存JSON报告
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    with open(DUPLICATES_JSON, 'w', encoding='utf-8') as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    print(f"\n✅ 已保存JSON报告: {DUPLICATES_JSON}")
    
    # 保存CSV报告
    export_to_csv(results, DUPLICATES_CSV)
    print(f"✅ 已保存CSV报告: {DUPLICATES_CSV}")
    
    # 打印摘要
    print_summary(results)
    
    print(f"\n📄 详细报告已保存:")
    print(f"   - JSON: {DUPLICATES_JSON}")
    print(f"   - CSV: {DUPLICATES_CSV}")


if __name__ == "__main__":
    main()


#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
将有字幕的视频音频上传到 Transistor.fm
严格遵循两步上传法（Pass-through Upload）
"""

import json
import os
import requests
from pathlib import Path
from datetime import datetime
import time
from typing import Dict, List, Optional, Tuple
import re
import subprocess
import sys

# 从 .env 加载环境变量（无需 python-dotenv）
_env_path = Path(__file__).parent.parent.parent / ".env"
if _env_path.exists():
    for _line in _env_path.read_text(encoding="utf-8").splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _, _v = _line.partition("=")
            os.environ.setdefault(_k.strip(), _v.strip())

# Transistor.fm API 配置
TRANSISTOR_API_KEY = os.environ["TRANSISTOR_API_KEY"]
TRANSISTOR_API_BASE = "https://api.transistor.fm/v1"
TRANSISTOR_SHOW_ID = os.environ["TRANSISTOR_SHOW_ID"]

# 项目路径
PROJECT_ROOT = Path(__file__).parent.parent.parent

# 错误日志文件
ERROR_LOG_FILE = PROJECT_ROOT / "logs/upload_errors.log"

# 上传记录文件
UPLOAD_RECORDS_FILE = PROJECT_ROOT / "tools/upload/uploaded_episodes.json"

# 视频文件夹路径
VIDEOS_WITH_SUBS_DIR = PROJECT_ROOT / "archive/有人工字幕"
VIDEOS_WITHOUT_SUBS_DIR = PROJECT_ROOT / "archive/无人工字幕"


def get_shows() -> List[Dict]:
    """获取用户的所有播客节目列表"""
    url = f"{TRANSISTOR_API_BASE}/shows"
    headers = {
        "x-api-key": TRANSISTOR_API_KEY
    }
    
    try:
        response = requests.get(url, headers=headers)
        response.raise_for_status()
        data = response.json()
        return data.get("data", [])
    except Exception as e:
        print(f"❌ 获取播客列表失败: {e}")
        if hasattr(e, 'response') and e.response is not None:
            print(f"响应内容: {e.response.text}")
        return []


def send_notification(title: str, message: str, sound: str = "default"):
    """
    发送macOS系统通知
    
    Args:
        title: 通知标题
        message: 通知内容
        sound: 通知声音（default, Basso, Blow, Bottle, Frog, Funk, Glass, Hero, Morse, Ping, Pop, Purr, Sosumi, Submarine, Tink）
    """
    try:
        script = f'''
        display notification "{message}" with title "{title}" sound name "{sound}"
        '''
        subprocess.run(['osascript', '-e', script], check=False, capture_output=True)
    except Exception as e:
        print(f"⚠️  发送通知失败: {e}")


def log_error(error_type: str, message: str, details: str = ""):
    """
    记录错误到日志文件
    
    Args:
        error_type: 错误类型
        message: 错误消息
        details: 详细信息
    """
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log_entry = f"[{timestamp}] {error_type}: {message}\n"
    if details:
        log_entry += f"  详情: {details}\n"
    log_entry += "\n"
    
    try:
        with open(ERROR_LOG_FILE, 'a', encoding='utf-8') as f:
            f.write(log_entry)
    except Exception as e:
        print(f"⚠️  写入错误日志失败: {e}")


def save_upload_record(episode_data: Dict, folder_name: str, source_type: str = "unknown"):
    """
    保存上传记录到JSON文件
    
    Args:
        episode_data: Episode数据（从API返回的完整数据）
        folder_name: 源文件夹名称
        source_type: 来源类型（"with_subs" 或 "without_subs"）
    """
    try:
        # 读取现有记录
        records = []
        if UPLOAD_RECORDS_FILE.exists():
            with open(UPLOAD_RECORDS_FILE, 'r', encoding='utf-8') as f:
                records = json.load(f)
        
        # 准备记录数据
        episode_id = episode_data.get("id")
        attrs = episode_data.get("attributes", {})
        
        record = {
            "episode_id": episode_id,
            "episode_number": attrs.get("number"),
            "title": attrs.get("title"),
            "status": attrs.get("status"),
            "published_at": attrs.get("published_at"),
            "share_url": attrs.get("share_url"),
            "video_url": attrs.get("video_url"),
            "media_url": attrs.get("media_url"),
            "description": attrs.get("description"),
            "slug": attrs.get("slug"),
            "duration": attrs.get("duration"),
            "duration_in_mmss": attrs.get("duration_in_mmss"),
            "source_folder": folder_name,
            "source_type": source_type,
            "uploaded_at": datetime.now().isoformat(),
            "created_at": attrs.get("created_at"),
            "updated_at": attrs.get("updated_at")
        }
        
        # 检查是否已存在（避免重复）
        existing_ids = {r.get("episode_id") for r in records}
        if episode_id not in existing_ids:
            records.append(record)
        else:
            # 更新现有记录
            for i, r in enumerate(records):
                if r.get("episode_id") == episode_id:
                    records[i] = record
                    break
        
        # 保存记录
        UPLOAD_RECORDS_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(UPLOAD_RECORDS_FILE, 'w', encoding='utf-8') as f:
            json.dump(records, f, ensure_ascii=False, indent=2)
            
    except Exception as e:
        print(f"⚠️  保存上传记录失败: {e}")


def get_max_episode_number(show_id: str) -> int:
    """
    获取指定show中当前最大的episode编号
    
    Returns:
        当前最大的episode编号，如果没有episode则返回0
    """
    url = f"{TRANSISTOR_API_BASE}/episodes"
    headers = {
        "x-api-key": TRANSISTOR_API_KEY
    }
    
    params = {
        "show_id": show_id
    }
    
    try:
        # 处理速率限制（429）重试
        max_retries = 3
        for attempt in range(max_retries):
            response = requests.get(url, headers=headers, params=params, timeout=30)
            if response.status_code != 429:
                break
            wait_time = 60 * (attempt + 1)
            print(f"   ⏸️  速率限制(429)，等待 {wait_time} 秒后重试 ({attempt + 1}/{max_retries})...")
            time.sleep(wait_time)
        response.raise_for_status()
        data = response.json()
        episodes = data.get("data", [])
        
        if not episodes:
            return 0
        
        # 获取所有episode的编号
        numbers = []
        for ep in episodes:
            number = ep.get("attributes", {}).get("number")
            if number is not None:
                numbers.append(number)
        
        if numbers:
            return max(numbers)
        else:
            return 0
            
    except Exception as e:
        print(f"⚠️  获取最大episode编号失败: {e}")
        return 0


def authorize_audio_upload(filename: str) -> Optional[Dict]:
    """
    步骤1: 授权音频文件上传，获取 upload_url 和 audio_url
    
    严格遵循官方文档的两步上传法：
    GET /v1/episodes/authorize_upload?filename=你的文件名.mp3
    
    Returns:
        Dict包含 upload_url, audio_url, content_type，如果失败返回 None
    """
    url = f"{TRANSISTOR_API_BASE}/episodes/authorize_upload"
    headers = {
        "x-api-key": TRANSISTOR_API_KEY
    }
    
    params = {
        "filename": filename
    }
    
    try:
        print(f"   🔐 步骤1: 获取上传授权...")
        response = requests.get(url, headers=headers, params=params, timeout=30)
        
        if response.status_code == 200:
            data = response.json()
            attributes = data.get("data", {}).get("attributes", {})
            
            upload_url = attributes.get("upload_url")
            audio_url = attributes.get("audio_url")
            content_type = attributes.get("content_type", "audio/mpeg")
            
            if not upload_url or not audio_url:
                print(f"   ❌ 授权响应中缺少必要字段")
                print(f"   响应: {json.dumps(data, indent=2, ensure_ascii=False)}")
                return None
            
            print(f"   ✅ 授权成功")
            print(f"      upload_url: {upload_url[:80]}...")
            print(f"      audio_url: {audio_url[:80]}...")
            print(f"      content_type: {content_type}")
            
            return {
                "upload_url": upload_url,
                "audio_url": audio_url,
                "content_type": content_type
            }
        else:
            print(f"   ❌ 授权失败: HTTP {response.status_code}")
            print(f"   响应: {response.text[:500]}")
            return None
            
    except Exception as e:
        print(f"   ❌ 授权请求异常: {e}")
        return None


def upload_audio_file(upload_url: str, content_type: str, audio_file_path: Path) -> bool:
    """
    步骤2: 上传音频文件到 upload_url
    
    严格遵循官方文档：
    - 方法：PUT（绝对不要用POST）
    - Headers：Content-Type 必须与授权返回的 content_type 一致
    - Body：直接发送文件的二进制流（Raw Binary）
    
    Returns:
        True if successful, False otherwise
    """
    try:
        print(f"   📤 步骤2: 上传音频文件（PUT）...")
        
        if not audio_file_path.exists():
            print(f"   ❌ 音频文件不存在: {audio_file_path}")
            return False
        
        file_size_mb = audio_file_path.stat().st_size / 1024 / 1024
        print(f"      文件大小: {file_size_mb:.2f} MB")
        
        # 读取二进制文件
        with open(audio_file_path, 'rb') as audio_file:
            # PUT 请求，直接发送二进制数据
            headers = {
                'Content-Type': content_type
            }
            
            response = requests.put(
                upload_url,
                headers=headers,
                data=audio_file,
                timeout=600  # 大文件可能需要更长时间
            )
            
            # PUT 请求成功通常返回 200 或 204
            if response.status_code in [200, 201, 204]:
                print(f"   ✅ 音频文件上传成功!")
                return True
            else:
                print(f"   ❌ 上传失败: HTTP {response.status_code}")
                print(f"   响应: {response.text[:500]}")
                return False
                
    except Exception as e:
        print(f"   ❌ 上传异常: {e}")
        return False


def srt_to_text(srt_path: Path) -> str:
    """
    将SRT字幕文件转换为纯文本
    
    Args:
        srt_path: SRT文件路径
        
    Returns:
        纯文本内容
    """
    if not srt_path.exists():
        return ""
    
    try:
        with open(srt_path, 'r', encoding='utf-8') as f:
            content = f.read()
        
        # 移除SRT格式标记（序号、时间戳等）
        # 保留字幕文本内容
        lines = content.split('\n')
        text_lines = []
        
        for line in lines:
            line = line.strip()
            # 跳过空行、序号、时间戳
            if not line:
                continue
            if re.match(r'^\d+$', line):  # 序号
                continue
            if re.match(r'^\d{2}:\d{2}:\d{2}', line):  # 时间戳
                continue
            if '-->' in line:  # 时间戳行
                continue
            
            # 保留字幕文本
            text_lines.append(line)
        
        return '\n'.join(text_lines)
    except Exception as e:
        print(f"   ⚠️  读取字幕文件失败: {e}")
        return ""


def create_episode(
    show_id: str,
    title: str,
    description: str,
    audio_url: str,
    video_url: Optional[str] = None,
    transcript: Optional[str] = None,
    published_at: Optional[str] = None,
    number: Optional[int] = None
) -> Optional[Dict]:
    """
    步骤3: 创建 episode
    
    使用步骤1获取的 audio_url 创建 episode
    
    Args:
        show_id: 播客节目 ID
        title: 标题
        description: 描述
        audio_url: 音频URL（从授权步骤获取）
        transcript: 文字稿（可选）
        published_at: 发布时间 (ISO 8601 格式，可选)
        number: episode 编号 (可选)
    
    Returns:
        Episode 资源，如果失败返回 None
    """
    url = f"{TRANSISTOR_API_BASE}/episodes"
    headers = {
        "x-api-key": TRANSISTOR_API_KEY,
        "Content-Type": "application/json"
    }
    
    try:
        print(f"   📝 步骤3: 创建 episode...")
        
        episode_data = {
            "episode": {
                "show_id": show_id,
                "title": title,
                "description": description,
                "audio_url": audio_url,  # 必须使用授权步骤返回的 audio_url
            }
        }
        
        # 添加可选字段
        if number:
            episode_data["episode"]["number"] = number
        
        # video_url可以在创建时设置
        if video_url:
            episode_data["episode"]["video_url"] = video_url
        
        # 注意：transcript 和 published_at 不支持在创建时设置
        # published_at 可能需要通过网页界面设置，或者等待API支持
        
        # 处理速率限制（429）重试
        max_retries = 3
        for attempt in range(max_retries):
            response = requests.post(url, headers=headers, json=episode_data, timeout=30)
            if response.status_code != 429:
                break
            wait_time = 60 * (attempt + 1)
            print(f"   ⏸️  速率限制(429)，等待 {wait_time} 秒后重试 ({attempt + 1}/{max_retries})...")
            time.sleep(wait_time)
        
        if response.status_code in [200, 201]:
            result = response.json()
            episode = result.get("data", {})
            episode_id = episode.get("id")
            
            attrs = episode.get('attributes', {})
            print(f"   ✅ Episode 创建成功!")
            print(f"      Episode ID: {episode_id}")
            print(f"      URL: {attrs.get('share_url', 'N/A')}")
            print(f"      状态: {attrs.get('status', 'N/A')}")
            
            # 步骤4: 更新YouTube Video URL（如果提供）
            # 注意：需要在创建后更新，因为创建时可能不支持
            # 实际上video_url可以在创建时设置，但为了保险起见，我们在创建后也更新一次
            
            return episode
        else:
            print(f"   ❌ 创建失败: HTTP {response.status_code}")
            print(f"   响应: {response.text[:500]}")
            return None
            
    except Exception as e:
        print(f"   ❌ 创建异常: {e}")
        return None


def update_episode_published_at(episode_id: str, published_at: str) -> bool:
    """更新 episode 的发布时间"""
    url = f"{TRANSISTOR_API_BASE}/episodes/{episode_id}"
    headers = {
        "x-api-key": TRANSISTOR_API_KEY,
        "Content-Type": "application/json"
    }
    
    data = {
        "episode": {
            "published_at": published_at
        }
    }
    
    try:
        response = requests.patch(url, headers=headers, json=data, timeout=30)
        if response.status_code in [200, 201]:
            print(f"   ✅ 发布时间更新成功")
            return True
        else:
            print(f"   ⚠️  更新发布时间失败: HTTP {response.status_code}")
            print(f"   响应: {response.text[:200]}")
            return False
    except Exception as e:
        print(f"   ⚠️  更新发布时间异常: {e}")
        return False




def find_files_in_folder(folder_path: Path) -> Tuple[Optional[Path], Optional[Path], Optional[Path], Optional[str]]:
    """
    在文件夹中查找音频、描述和字幕文件
    
    Returns:
        (audio_path, description_path, transcript_path, title)
    """
    audio_path = None
    description_path = None
    transcript_path = None
    title = None
    
    # 查找音频文件（.m4a）
    for file in folder_path.glob("*.m4a"):
        audio_path = file
        # 从文件名提取标题（去掉扩展名）
        title = file.stem
        break
    
    # 查找描述文件（.description）
    for file in folder_path.glob("*.description"):
        description_path = file
        break
    
    # 优先查找中文字幕（.zh-Hans.srt），如果没有则查找其他.srt文件
    for pattern in ["*.zh-Hans.srt", "*.zh-Hant.srt", "*.zh.srt", "*.srt"]:
        matches = list(folder_path.glob(pattern))
        if matches:
            transcript_path = matches[0]
            break
    
    return audio_path, description_path, transcript_path, title


def read_description(description_path: Path) -> str:
    """读取描述文件内容"""
    if not description_path or not description_path.exists():
        return ""
    
    try:
        with open(description_path, 'r', encoding='utf-8') as f:
            return f.read().strip()
    except Exception as e:
        print(f"   ⚠️  读取描述文件失败: {e}")
        return ""


def format_date_for_transistor(date_str: str) -> Optional[str]:
    """将日期字符串转换为 Transistor.fm 需要的格式"""
    try:
        # 输入格式: YYYYMMDD
        dt = datetime.strptime(date_str, "%Y%m%d")
        # 输出格式: ISO 8601 (YYYY-MM-DDTHH:MM:SSZ)
        return dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    except:
        return None


def extract_youtube_video_id(folder_path: Path) -> Optional[str]:
    """
    从文件夹名或info.json中提取YouTube视频ID
    
    文件夹名格式：YYYYMMDD_标题_VIDEO_ID
    """
    folder_name = folder_path.name

    # 方法1: YouTube ID 固定 11 位（可含 _ 和 -），直接取文件夹名末尾 11 个字符
    if len(folder_name) >= 11:
        video_id = folder_name[-11:]
        if re.match(r'^[a-zA-Z0-9_-]{11}$', video_id):
            return video_id
    
    # 方法2: 从info.json中读取
    info_json_files = list(folder_path.glob("*.info.json"))
    if info_json_files:
        try:
            with open(info_json_files[0], 'r', encoding='utf-8') as f:
                # 只读取第一行，因为文件可能很大
                first_line = f.readline()
                if '"id"' in first_line:
                    # 使用正则表达式提取id字段
                    match = re.search(r'"id"\s*:\s*"([^"]+)"', first_line)
                    if match:
                        return match.group(1)
        except Exception as e:
            print(f"   ⚠️  读取info.json失败: {e}")
    
    return None


def update_episode_video_url(episode_id: str, video_url: str) -> bool:
    """更新 episode 的 YouTube Video URL"""
    url = f"{TRANSISTOR_API_BASE}/episodes/{episode_id}"
    headers = {
        "x-api-key": TRANSISTOR_API_KEY,
        "Content-Type": "application/json"
    }
    
    data = {
        "episode": {
            "video_url": video_url
        }
    }
    
    try:
        response = requests.patch(url, headers=headers, json=data, timeout=30)
        if response.status_code in [200, 201]:
            return True
        else:
            print(f"   ⚠️  更新video_url失败: HTTP {response.status_code}")
            return False
    except Exception as e:
        print(f"   ⚠️  更新video_url异常: {e}")
        return False


def publish_episode(episode_id: str) -> bool:
    """
    发布 episode（将状态从draft改为published）
    
    使用特殊的publish端点：
    PATCH /v1/episodes/EPISODE_ID/publish
    """
    url = f"{TRANSISTOR_API_BASE}/episodes/{episode_id}/publish"
    headers = {
        "x-api-key": TRANSISTOR_API_KEY,
        "Content-Type": "application/x-www-form-urlencoded"
    }
    
    # 使用form-encoded格式
    data = "episode[status]=published"
    
    try:
        # 处理速率限制（429）重试
        max_retries = 3
        for attempt in range(max_retries):
            response = requests.patch(url, headers=headers, data=data, timeout=30)
            if response.status_code != 429:
                break
            wait_time = 60 * (attempt + 1)
            print(f"   ⏸️  速率限制(429)，等待 {wait_time} 秒后重试 ({attempt + 1}/{max_retries})...")
            time.sleep(wait_time)
        if response.status_code in [200, 201]:
            return True
        else:
            print(f"   ⚠️  发布失败: HTTP {response.status_code}")
            print(f"   响应: {response.text[:200]}")
            return False
    except Exception as e:
        print(f"   ⚠️  发布异常: {e}")
        return False


def upload_episode_from_folder(
    folder_path: Path,
    show_id: str,
    episode_number: Optional[int] = None
) -> bool:
    """
    从文件夹上传一个完整的 episode
    
    Args:
        folder_path: 包含音频、描述、字幕文件的文件夹
        show_id: 播客节目 ID
        episode_number: episode 编号（可选）
    
    Returns:
        True if successful, False otherwise
    """
    print(f"\n{'='*60}")
    print(f"📁 处理文件夹: {folder_path.name}")
    
    # 查找文件
    audio_path, description_path, transcript_path, title = find_files_in_folder(folder_path)
    
    if not audio_path:
        print(f"❌ 未找到音频文件，跳过")
        return False
    
    if not title:
        title = folder_path.name
    
    # 确保标题包含 E{N}. 前缀（格式：E1. 标题）
    if episode_number and not re.match(rf"^E{episode_number}\. ", title):
        title = f"E{episode_number}. {title}"
    print(f"📝 标题: {title}")
    
    # 读取描述
    description = read_description(description_path) if description_path else ""
    if description:
        print(f"📄 描述长度: {len(description)} 字符")
    else:
        print(f"⚠️  未找到描述文件")
    
    # 读取字幕（transcript）
    transcript = ""
    if transcript_path:
        transcript = srt_to_text(transcript_path)
        if transcript:
            print(f"📜 字幕长度: {len(transcript)} 字符")
        else:
            print(f"⚠️  字幕文件为空")
    else:
        print(f"⚠️  未找到字幕文件")
    
    # 提取YouTube视频ID
    youtube_video_id = extract_youtube_video_id(folder_path)
    youtube_url = None
    if youtube_video_id:
        youtube_url = f"https://www.youtube.com/watch?v={youtube_video_id}"
        print(f"🎥 YouTube视频: {youtube_url}")
    else:
        print(f"⚠️  未找到YouTube视频ID")
    
    # 从文件夹名提取日期（格式：YYYYMMDD_标题）
    published_at = None
    folder_name = folder_path.name
    date_match = re.match(r'^(\d{8})_', folder_name)
    if date_match:
        date_str = date_match.group(1)
        published_at = format_date_for_transistor(date_str)
        if published_at:
            print(f"📅 发布日期: {published_at}")
    
    # 步骤1: 授权上传
    filename = audio_path.name
    auth_result = authorize_audio_upload(filename)
    
    if not auth_result:
        print(f"❌ 授权失败，跳过此文件")
        return False
    
    upload_url = auth_result["upload_url"]
    audio_url = auth_result["audio_url"]
    content_type = auth_result["content_type"]
    
    # 步骤2: 上传音频文件
    if not upload_audio_file(upload_url, content_type, audio_path):
        print(f"❌ 音频上传失败，跳过此文件")
        return False
    
    # 步骤3: 创建 episode
    episode = create_episode(
        show_id=show_id,
        title=title,
        description=description,
        audio_url=audio_url,
        video_url=youtube_url,  # 在创建时设置video_url
        transcript=transcript if transcript else None,
        published_at=published_at,
        number=episode_number
    )
    
    if not episode:
        print(f"❌ 创建 episode 失败")
        return False
    
    episode_id = episode.get("id")
    attrs = episode.get('attributes', {})
    current_status = attrs.get('status', 'unknown')
    
    # 保存上传记录
    source_type = "with_subs" if "有人工字幕" in str(folder_path) else "without_subs"
    save_upload_record(episode, folder_path.name, source_type)
    
    # 步骤4: 检查是否需要更新video_url（如果创建时没有设置成功）
    if youtube_url and episode_id:
        current_video_url = attrs.get('video_url')
        if current_video_url != youtube_url:
            print(f"   🎥 步骤4: 更新YouTube Video URL...")
            if update_episode_video_url(episode_id, youtube_url):
                print(f"   ✅ YouTube Video URL更新成功")
            else:
                print(f"   ⚠️  YouTube Video URL更新失败，但episode已创建")
    
    # 步骤5: 发布episode（如果状态是draft）
    # 使用特殊的publish端点来发布
    if current_status == 'draft' and episode_id:
        print(f"   📢 步骤5: 发布episode...")
        if publish_episode(episode_id):
            print(f"   ✅ Episode已发布!")
        else:
            print(f"   ⚠️  发布失败，episode仍为draft状态")
            print(f"      Episode URL: {attrs.get('share_url', 'N/A')}")
    
    # 步骤6: 关于transcript的说明
    # 根据API测试，transcript字段不被支持，需要通过网页界面手动上传
    if transcript:
        print(f"   ⚠️  提示：transcript需要通过网页界面手动上传（长度: {len(transcript)} 字符）")
        print(f"      Episode URL: {attrs.get('share_url', 'N/A')}")
    
    print(f"✅ 上传完成!")
    return True


def upload_all_episodes(
    show_id: str,
    start_index: int = 0,
    limit: Optional[int] = None,
    dry_run: bool = False,
    source_dir: Optional[Path] = None
):
    """
    批量上传所有episode
    
    Args:
        show_id: 播客节目 ID
        start_index: 开始索引
        limit: 限制上传数量（None 表示全部）
        dry_run: 是否为试运行（不实际上传）
        source_dir: 源文件夹路径（None则使用默认的有人工字幕文件夹）
    """
    if source_dir is None:
        source_dir = VIDEOS_WITH_SUBS_DIR
    
    if not source_dir.exists():
        print(f"❌ 文件夹不存在: {source_dir}")
        return
    
    # 获取所有子文件夹
    folders = [f for f in source_dir.iterdir() if f.is_dir()]
    folders.sort()  # 按名称排序
    
    if not folders:
        print(f"❌ 未找到任何文件夹")
        return
    
    print(f"📋 找到 {len(folders)} 个文件夹")
    
    # 限制范围
    end_index = len(folders)
    if limit:
        end_index = min(start_index + limit, len(folders))
    
    folders_to_upload = folders[start_index:end_index]
    print(f"📤 准备上传 {len(folders_to_upload)} 个episode (索引 {start_index} 到 {end_index-1})")
    
    # 获取当前最大的episode编号，从下一个编号开始
    max_episode_number = get_max_episode_number(show_id)
    start_episode_number = max_episode_number + 1
    print(f"📊 当前最大episode编号: {max_episode_number}")
    print(f"📊 将从编号 {start_episode_number} 开始上传")
    
    if dry_run:
        print("\n🔍 试运行模式 - 不会实际上传\n")
        for idx, folder in enumerate(folders_to_upload, start=start_index):
            audio_path, description_path, transcript_path, title = find_files_in_folder(folder)
            print(f"[{idx+1}/{len(folders_to_upload)}] {folder.name}")
            print(f"  音频: {audio_path.name if audio_path else '未找到'}")
            print(f"  描述: {description_path.name if description_path else '未找到'}")
            print(f"  字幕: {transcript_path.name if transcript_path else '未找到'}")
            print(f"  标题: {title or folder.name}")
        return
    
    success_count = 0
    fail_count = 0
    consecutive_failures = 0
    max_consecutive_failures = 5  # 连续失败5次后停止并报警
    
    for idx, folder in enumerate(folders_to_upload, start=start_index):
        print(f"\n[{idx+1}/{len(folders_to_upload)}]")
        
        # 计算当前episode编号（从最大编号+1开始）
        current_episode_number = start_episode_number + (idx - start_index)
        
        try:
            success = upload_episode_from_folder(
                folder_path=folder,
                show_id=show_id,
                episode_number=current_episode_number
            )
            
            if success:
                success_count += 1
                consecutive_failures = 0  # 重置连续失败计数
            else:
                fail_count += 1
                consecutive_failures += 1
                
                # 记录失败
                log_error(
                    "上传失败",
                    f"文件夹: {folder.name}",
                    f"Episode编号: {current_episode_number}"
                )
                
                # 如果连续失败太多次，停止并报警
                if consecutive_failures >= max_consecutive_failures:
                    error_msg = f"连续失败{max_consecutive_failures}次，上传已停止。请检查错误日志: {ERROR_LOG_FILE}"
                    print(f"\n❌ {error_msg}")
                    log_error("连续失败", error_msg, f"最后失败的文件夹: {folder.name}")
                    send_notification(
                        "上传错误",
                        f"连续失败{max_consecutive_failures}次，上传已停止",
                        "Basso"  # 错误声音
                    )
                    break
                    
        except Exception as e:
            print(f"❌ 处理异常: {e}")
            fail_count += 1
            consecutive_failures += 1
            
            # 记录异常
            log_error(
                "处理异常",
                f"文件夹: {folder.name}",
                str(e)
            )
            
            # 如果连续失败太多次，停止并报警
            if consecutive_failures >= max_consecutive_failures:
                error_msg = f"连续异常{max_consecutive_failures}次，上传已停止"
                print(f"\n❌ {error_msg}")
                log_error("连续异常", error_msg, str(e))
                send_notification(
                    "上传错误",
                    f"连续异常{max_consecutive_failures}次，上传已停止",
                    "Basso"
                )
                break
        
        # 避免请求过快（API 限制：每 10 秒最多 10 个请求）
        # 每个episode需要4-5个请求（授权、上传、创建、更新video_url、发布），所以每次等待3秒
        if idx < len(folders_to_upload) - 1:
            time.sleep(3)
    
    # 上传完成后的总结和通知
    total = success_count + fail_count
    if total > 0:
        completion_msg = f"上传完成: 成功 {success_count}，失败 {fail_count}"
        print(f"\n{'='*60}")
        print(f"✅ 成功: {success_count}")
        print(f"❌ 失败: {fail_count}")
        print(f"📊 总计: {total}")
        
        # 如果有失败，发送通知
        if fail_count > 0:
            send_notification(
                "上传完成（有失败）",
                completion_msg,
                "Glass"
            )
            log_error("上传完成", completion_msg, f"总上传数: {total}")
        else:
            send_notification(
                "上传完成",
                completion_msg,
                "Glass"
            )
    
    print(f"\n{'='*60}")
    print(f"✅ 成功: {success_count}")
    print(f"❌ 失败: {fail_count}")
    print(f"📊 总计: {len(folders_to_upload)}")


def main():
    """主函数"""
    import argparse
    
    parser = argparse.ArgumentParser(description="上传音频到 Transistor.fm（两步上传法）")
    parser.add_argument(
        "--show-id",
        type=str,
        default=TRANSISTOR_SHOW_ID,
        help=f"播客节目 ID (默认: {TRANSISTOR_SHOW_ID})"
    )
    parser.add_argument(
        "--start",
        type=int,
        default=0,
        help="开始索引 (默认: 0)"
    )
    parser.add_argument(
        "--limit",
        type=int,
        help="限制上传数量"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="试运行模式（不实际上传）"
    )
    parser.add_argument(
        "--list-shows",
        action="store_true",
        help="列出所有播客节目"
    )
    parser.add_argument(
        "--no-subs",
        action="store_true",
        help="上传无人工字幕文件夹中的视频"
    )
    parser.add_argument(
        "--upload-only-new",
        action="store_true",
        help="仅上传本地有但 Transistor 已发布列表中没有的视频（需先运行 check_upload_candidates.py 检查）"
    )
    
    args = parser.parse_args()
    
    # 如果只是列出节目
    if args.list_shows:
        print("📻 获取您的播客节目列表...\n")
        shows = get_shows()
        
        if not shows:
            print("❌ 没有找到播客节目，请检查 API key 是否正确")
            return
        
        print(f"找到 {len(shows)} 个播客节目:\n")
        for show in shows:
            attrs = show.get("attributes", {})
            print(f"  ID: {show.get('id')}")
            print(f"  名称: {attrs.get('title', 'N/A')}")
            print(f"  描述: {attrs.get('description', 'N/A')[:100]}...")
            print()
        return
    
    # 选择源文件夹
    source_dir = VIDEOS_WITHOUT_SUBS_DIR if args.no_subs else VIDEOS_WITH_SUBS_DIR
    
    # 仅上传新视频（本地有但 Transistor 没有的）
    if args.upload_only_new:
        import subprocess
        result = subprocess.run(
            [sys.executable, str(Path(__file__).parent / "check_upload_candidates.py"), "--json"],
            capture_output=True,
            text=True,
            cwd=PROJECT_ROOT,
        )
        if result.returncode != 0:
            print(f"❌ 检查待上传列表失败: {result.stderr}")
            return
        try:
            data = json.loads(result.stdout)
        except json.JSONDecodeError as e:
            print(f"❌ 解析待上传列表失败: {e}")
            return
        candidates = data.get("candidates", [])
        if not candidates:
            print("✅ 没有待上传的视频，本地内容已全部同步到 Transistor。")
            return
        folder_names = [c["folder_name"] for c in candidates]
        folders_to_upload = [source_dir / fn for fn in folder_names]
        # 验证文件夹存在
        missing = [fn for fn, fp in zip(folder_names, folders_to_upload) if not fp.exists()]
        if missing:
            print(f"❌ 以下文件夹不存在: {missing}")
            return
        max_ep = get_max_episode_number(args.show_id)
        print(f"📋 待上传: {len(candidates)} 个（按日期升序，旧的先传，新的 EP 号最大）")
        print(f"📊 当前最大 EP: {max_ep}，将从 EP{max_ep + 1} 开始")
        if args.dry_run:
            for i, (folder, cand) in enumerate(zip(folders_to_upload, candidates)):
                print(f"  [{i+1}] EP{max_ep + 1 + i} {cand['folder_name']}")
            return
        success_count = 0
        fail_count = 0
        for i, folder in enumerate(folders_to_upload):
            ep_num = max_ep + 1 + i
            print(f"\n[{i+1}/{len(folders_to_upload)}] EP{ep_num}")
            try:
                if upload_episode_from_folder(folder, args.show_id, episode_number=ep_num):
                    success_count += 1
                else:
                    fail_count += 1
            except Exception as e:
                print(f"❌ 异常: {e}")
                fail_count += 1
            if i < len(folders_to_upload) - 1:
                time.sleep(3)
        print(f"\n✅ 成功: {success_count}   ❌ 失败: {fail_count}")
        return
    
    # 开始上传
    upload_all_episodes(
        show_id=args.show_id,
        start_index=args.start,
        limit=args.limit,
        dry_run=args.dry_run,
        source_dir=source_dir
    )


if __name__ == "__main__":
    main()


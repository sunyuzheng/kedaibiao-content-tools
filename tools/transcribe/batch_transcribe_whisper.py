#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
批量音频转SRT字幕脚本
使用Faster-Whisper进行高精度转录，支持并行处理
Faster-Whisper比OpenAI Whisper快4-10倍，精度相同
"""

import os
import sys
import json
import time
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from tqdm import tqdm
from faster_whisper import WhisperModel
import traceback
import multiprocessing

# 配置
_PROJECT_ROOT = Path(__file__).parent.parent.parent
AUDIO_DIR = str(_PROJECT_ROOT / "archive/无人工字幕")
# 模型选择：medium（平衡，推荐）或 large-v2（更高精度但更慢）
# tiny/base/small/medium/large-v1/large-v2/large-v3
MODEL_SIZE = "medium"  # medium在速度和精度间平衡最好，large-v2精度更高但慢2-3倍
LANGUAGE = "zh"  # 中文
DEVICE = "cpu"  # 或 "cuda"（如果有GPU）
COMPUTE_TYPE = "int8"  # int8速度快，float16精度更高但慢
# 并行处理数量：faster-whisper内部已优化，不需要太多线程
MAX_WORKERS = min(multiprocessing.cpu_count(), 3)  # 限制并行数避免内存不足
PROGRESS_FILE = str(_PROJECT_ROOT / "logs/transcribe_progress.json")

# 支持的音频格式
AUDIO_EXTENSIONS = {'.m4a', '.mp3', '.wav', '.mp4', '.flac', '.ogg', '.webm'}


def load_progress():
    """加载进度文件"""
    if os.path.exists(PROGRESS_FILE):
        with open(PROGRESS_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    return {"completed": [], "failed": []}


def save_progress(progress):
    """保存进度文件"""
    with open(PROGRESS_FILE, 'w', encoding='utf-8') as f:
        json.dump(progress, f, ensure_ascii=False, indent=2)


def find_audio_files(root_dir):
    """查找所有音频文件"""
    audio_files = []
    root_path = Path(root_dir)
    
    for audio_file in root_path.rglob('*'):
        if audio_file.is_file() and audio_file.suffix.lower() in AUDIO_EXTENSIONS:
            # 检查是否已有SRT文件
            srt_file = audio_file.with_suffix('.srt')
            if not srt_file.exists():
                audio_files.append(audio_file)
    
    return sorted(audio_files)


def transcribe_audio(audio_path, model, language):
    """转录单个音频文件"""
    audio_path = Path(audio_path)
    srt_path = audio_path.with_suffix('.srt')
    
    try:
        # 使用Faster-Whisper进行转录
        segments, info = model.transcribe(
            str(audio_path),
            language=language,
            task="transcribe",
            beam_size=5,  # 平衡速度和精度
            vad_filter=True,  # 启用VAD（语音活动检测）提高精度
            vad_parameters=dict(min_silence_duration_ms=500)
        )
        
        # 转换为列表（因为segments是生成器）
        segments_list = list(segments)
        
        # 生成SRT格式字幕
        srt_content = generate_srt(segments_list)
        
        # 保存SRT文件
        with open(srt_path, 'w', encoding='utf-8') as f:
            f.write(srt_content)
        
        duration = info.duration if hasattr(info, 'duration') else 0
        
        return {
            "status": "success",
            "audio": str(audio_path),
            "srt": str(srt_path),
            "duration": duration,
            "language": info.language if hasattr(info, 'language') else language
        }
    except Exception as e:
        return {
            "status": "error",
            "audio": str(audio_path),
            "error": str(e),
            "traceback": traceback.format_exc()
        }


def generate_srt(segments):
    """将Whisper的segments转换为SRT格式"""
    srt_lines = []
    
    for i, segment in enumerate(segments, start=1):
        start_time = format_timestamp(segment.start)
        end_time = format_timestamp(segment.end)
        text = segment.text.strip()
        
        srt_lines.append(f"{i}")
        srt_lines.append(f"{start_time} --> {end_time}")
        srt_lines.append(text)
        srt_lines.append("")  # 空行分隔
        
    return "\n".join(srt_lines)


def format_timestamp(seconds):
    """将秒数转换为SRT时间戳格式 (HH:MM:SS,mmm)"""
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    millis = int((seconds % 1) * 1000)
    
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def process_batch(audio_files, progress):
    """批量处理音频文件"""
    print(f"\n找到 {len(audio_files)} 个需要处理的音频文件")
    print(f"使用模型: {MODEL_SIZE} ({COMPUTE_TYPE})")
    print(f"并行处理数: {MAX_WORKERS}")
    print(f"语言: {LANGUAGE}")
    print(f"设备: {DEVICE}\n")
    
    # 过滤已完成的文件
    completed_set = set(progress["completed"])
    failed_set = set(progress["failed"])
    
    todo_files = [
        f for f in audio_files 
        if str(f) not in completed_set and str(f) not in failed_set
    ]
    
    if not todo_files:
        print("所有文件都已处理完成！")
        return
    
    print(f"待处理文件数: {len(todo_files)}")
    print(f"已完成: {len(completed_set)}, 失败: {len(failed_set)}\n")
    
    # 加载Faster-Whisper模型（只加载一次，所有线程共享）
    print("正在加载Faster-Whisper模型...")
    print(f"首次运行会下载模型，请耐心等待...")
    start_time = time.time()
    
    model = WhisperModel(
        MODEL_SIZE,
        device=DEVICE,
        compute_type=COMPUTE_TYPE,
        download_root=None  # 使用默认缓存目录
    )
    
    load_time = time.time() - start_time
    print(f"模型加载完成！耗时: {load_time:.1f}秒\n")
    
    # 使用线程池并行处理
    results = []
    batch_start_time = time.time()
    
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        # 提交所有任务
        future_to_audio = {
            executor.submit(transcribe_audio, str(audio_file), model, LANGUAGE): audio_file
            for audio_file in todo_files
        }
        
        # 使用tqdm显示进度
        with tqdm(total=len(todo_files), desc="处理进度", unit="文件") as pbar:
            for future in as_completed(future_to_audio):
                audio_file = future_to_audio[future]
                try:
                    result = future.result()
                    results.append(result)
                    
                    if result["status"] == "success":
                        progress["completed"].append(str(audio_file))
                        duration = result.get("duration", 0)
                        pbar.set_postfix({
                            "成功": len(progress["completed"]),
                            "失败": len(progress["failed"]),
                            "时长": f"{duration/60:.1f}min" if duration > 0 else "N/A"
                        })
                    else:
                        progress["failed"].append(str(audio_file))
                        print(f"\n❌ 处理失败: {audio_file.name}")
                        print(f"   错误: {result.get('error', 'Unknown error')}")
                    
                    # 定期保存进度
                    if len(results) % 5 == 0:
                        save_progress(progress)
                    
                except Exception as e:
                    progress["failed"].append(str(audio_file))
                    print(f"\n❌ 处理异常: {audio_file.name}")
                    print(f"   错误: {str(e)}")
                    results.append({
                        "status": "error",
                        "audio": str(audio_file),
                        "error": str(e)
                    })
                
                pbar.update(1)
    
    # 保存最终进度
    save_progress(progress)
    
    # 计算统计信息
    total_time = time.time() - batch_start_time
    successful = [r for r in results if r["status"] == "success"]
    total_duration = sum(r.get("duration", 0) for r in successful)
    
    # 打印统计信息
    print("\n" + "="*60)
    print("处理完成统计:")
    print(f"  总文件数: {len(audio_files)}")
    print(f"  成功: {len(progress['completed'])}")
    print(f"  失败: {len(progress['failed'])}")
    if successful:
        print(f"  处理音频总时长: {total_duration/3600:.1f} 小时")
        print(f"  实际处理时间: {total_time/3600:.1f} 小时")
        print(f"  平均速度: {total_duration/total_time:.2f}x 实时速度")
    print("="*60)
    
    if progress["failed"]:
        print("\n失败的文件:")
        for failed_file in progress["failed"][:10]:  # 只显示前10个
            print(f"  - {Path(failed_file).name}")
        if len(progress["failed"]) > 10:
            print(f"  ... 还有 {len(progress['failed']) - 10} 个失败文件")


def main():
    """主函数"""
    print("="*60)
    print("批量音频转SRT字幕工具 (Faster-Whisper)")
    print("="*60)
    
    # 检查目录是否存在
    if not os.path.exists(AUDIO_DIR):
        print(f"错误: 目录不存在: {AUDIO_DIR}")
        sys.exit(1)
    
    # 加载进度
    progress = load_progress()
    print(f"\n已加载进度: 完成 {len(progress['completed'])} 个, 失败 {len(progress['failed'])} 个")
    
    # 查找所有音频文件
    print(f"\n正在扫描目录: {AUDIO_DIR}")
    audio_files = find_audio_files(AUDIO_DIR)
    
    if not audio_files:
        print("未找到需要处理的音频文件！")
        return
    
    # 批量处理
    process_batch(audio_files, progress)


if __name__ == "__main__":
    main()

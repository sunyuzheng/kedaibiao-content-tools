#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
批量音频转SRT字幕脚本
使用MLX-Whisper进行高精度转录，专为Apple Silicon优化
MLX-Whisper比标准Whisper快很多，且精度相同
"""

import os
import sys
import json
import time
from pathlib import Path
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor, as_completed
from tqdm import tqdm
from mlx_whisper import transcribe as mlx_transcribe
import traceback
import multiprocessing
import threading

# 配置
_PROJECT_ROOT = Path(__file__).parent.parent.parent
AUDIO_DIR = str(_PROJECT_ROOT / "archive/无人工字幕")
# 模型选择：tiny/base/small/medium/large-v2/large-v3
# MLX社区模型：mlx-community/whisper-{size}
MODEL_SIZE = "mlx-community/whisper-medium"  # medium平衡速度和精度
# 或者使用本地路径（如果已下载）
# MODEL_SIZE = "medium"  # 会自动从HF下载
LANGUAGE = "zh"  # 中文
# 并行处理数量：MLX使用GPU，多线程会导致GPU资源冲突
# 限制为2个并行任务，使用锁序列化GPU访问
MAX_WORKERS = 2  # MLX GPU限制，使用锁保护
# GPU访问锁，确保同一时间只有一个任务使用GPU
gpu_lock = threading.Lock()
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


def transcribe_audio(audio_path, model_path, language):
    """转录单个音频文件"""
    audio_path = Path(audio_path)
    srt_path = audio_path.with_suffix('.srt')
    
    try:
        # 使用锁保护GPU访问，避免多线程冲突
        with gpu_lock:
            # 使用MLX-Whisper进行转录
            result = mlx_transcribe(
                str(audio_path),
                path_or_hf_repo=model_path,
                verbose=False,
                word_timestamps=False,  # 使用segment级别的时间戳即可
                temperature=(0.0, 0.2, 0.4, 0.6, 0.8, 1.0),  # 温度采样
                compression_ratio_threshold=2.4,
                logprob_threshold=-1.0,
                no_speech_threshold=0.6,
                condition_on_previous_text=True,
                language=language,  # 语言参数
            )
        
        # 生成SRT格式字幕
        srt_content = generate_srt(result.get("segments", []))
        
        # 保存SRT文件
        with open(srt_path, 'w', encoding='utf-8') as f:
            f.write(srt_content)
        
        # 计算总时长
        segments = result.get("segments", [])
        duration = segments[-1]["end"] if segments else 0
        
        return {
            "status": "success",
            "audio": str(audio_path),
            "srt": str(srt_path),
            "duration": duration,
            "language": result.get("language", language),
            "text_length": len(result.get("text", ""))
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
        start_time = format_timestamp(segment["start"])
        end_time = format_timestamp(segment["end"])
        text = segment["text"].strip()
        
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
    print(f"使用模型: {MODEL_SIZE}")
    print(f"并行处理数: {MAX_WORKERS}")
    print(f"语言: {LANGUAGE}")
    print(f"平台: Apple Silicon (MLX优化)\n")
    
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
    
    # MLX-Whisper模型会在首次使用时自动下载
    print("注意: 首次运行会下载模型文件，请耐心等待...")
    print("模型会缓存在 ~/.cache/huggingface/ 目录\n")
    
    # 使用线程池并行处理，使用锁保护GPU访问
    # 限制并行数量避免GPU资源冲突
    results = []
    batch_start_time = time.time()
    
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        # 提交所有任务
        future_to_audio = {
            executor.submit(transcribe_audio, str(audio_file), MODEL_SIZE, LANGUAGE): audio_file
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
        if total_time > 0:
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
    print("批量音频转SRT字幕工具 (MLX-Whisper)")
    print("专为Apple Silicon优化")
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


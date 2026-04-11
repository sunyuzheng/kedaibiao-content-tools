#!/bin/bash
# 下载 missing_video_ids.txt 中的视频
# 复用现有 download_channel.sh 的配置

set -e
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
IDS_FILE="$SCRIPT_DIR/missing_video_ids.txt"
ARCHIVE_FILE="$PROJECT_ROOT/archive/downloaded_history.txt"

if [ ! -f "$IDS_FILE" ]; then
  echo "❌ $IDS_FILE 不存在，先运行 fetch_all_videos.py"
  exit 1
fi

COUNT=$(wc -l < "$IDS_FILE")
echo "准备下载 $COUNT 个视频..."

yt-dlp \
  --cookies-from-browser chrome \
  --download-archive "$ARCHIVE_FILE" \
  --output "$PROJECT_ROOT/archive/无人工字幕/%(upload_date)s_%(title)s_%(id)s/%(title)s.%(ext)s" \
  --format "bestaudio[ext=m4a]/bestaudio" \
  --write-info-json \
  --write-description \
  --write-thumbnail \
  --write-subs \
  --sub-lang "zh-Hans,zh-Hant,zh,en" \
  --convert-thumbnails webp \
  --no-playlist \
  --batch-file "$IDS_FILE" \
  --sleep-interval 2 \
  --max-sleep-interval 5

echo "✓ 下载完成"
echo "运行 organize_subtitles.py 重新分类..."
python3 "$PROJECT_ROOT/tools/organize/organize_subtitles.py"

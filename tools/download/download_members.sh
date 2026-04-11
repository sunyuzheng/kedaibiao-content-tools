#!/bin/bash

# 课代表立正频道 - 会员专属视频下载脚本
# 频道 ID: UC_5lJHgnMP_lb_VpIiXV0hQ
# 注意：会员视频单独存放，不上传 Transistor

CHANNEL_URL="https://www.youtube.com/channel/UC_5lJHgnMP_lb_VpIiXV0hQ/videos"
BROWSER_COOKIES="chrome"

# 获取项目根目录（脚本在 tools/download/ 目录下）
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
cd "$PROJECT_ROOT" || exit 1

ARCHIVE_DIR="archive"
MEMBERS_DIR="${ARCHIVE_DIR}/会员视频"
ARCHIVE_FILE="${MEMBERS_DIR}/downloaded_history_members.txt"

echo "=========================================="
echo "开始下载会员专属视频：课代表立正"
echo "=========================================="
echo ""

# 检查 yt-dlp 是否安装
if ! command -v yt-dlp &> /dev/null; then
    echo "错误: yt-dlp 未安装。请先安装: brew install yt-dlp"
    exit 1
fi

# 检查 ffmpeg 是否安装
if ! command -v ffmpeg &> /dev/null; then
    echo "错误: ffmpeg 未安装。请先安装: brew install ffmpeg"
    exit 1
fi

# 创建目录
mkdir -p "${MEMBERS_DIR}"

CONCURRENT_FRAGMENTS=4

echo "下载配置:"
echo "  - 只下载: subscriber_only（会员专属）"
echo "  - 输出目录: ${MEMBERS_DIR}/"
echo "  - 音频格式: m4a (AAC)"
echo "  - 字幕: 优先人工字幕，无则自动字幕"
echo "  - 字幕格式: SRT"
echo "  - 字幕语言: 简体中文、繁体中文、英文"
echo "  - 并发片段: $CONCURRENT_FRAGMENTS"
echo "  - Cookies: 从浏览器读取 ($BROWSER_COOKIES)"
echo "  - 历史记录: ${ARCHIVE_FILE}"
echo ""

yt-dlp "${CHANNEL_URL}" \
  --cookies-from-browser "${BROWSER_COOKIES}" \
  --match-filter "availability = subscriber_only" \
  --ignore-errors \
  --format "bestaudio[ext=m4a]/bestaudio" \
  --write-description \
  --write-info-json \
  --write-thumbnail \
  --write-sub \
  --write-auto-sub \
  --sub-lang "zh,zh-Hans,zh-Hant,en.*" \
  --convert-subs srt \
  --concurrent-fragments "${CONCURRENT_FRAGMENTS}" \
  --output "${MEMBERS_DIR}/%(upload_date)s_%(title)s_%(id)s/%(title)s.%(ext)s" \
  --download-archive "${ARCHIVE_FILE}" \
  --no-playlist-reverse \
  --yes-playlist

echo ""
echo "=========================================="
echo "完成！"
echo "=========================================="
echo "会员视频保存在: ${MEMBERS_DIR}/"
echo "下载历史记录: ${ARCHIVE_FILE}"
echo ""
echo "提示："
echo "  - 这些视频不上传 Transistor，仅本地存档"
echo "  - 下次运行将自动跳过已下载的视频"

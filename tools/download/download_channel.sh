#!/bin/bash

# 课代表立正频道 - 本地多模态数据仓库下载脚本 (支持字幕下载)
# 频道 ID: UC_5lJHgnMP_lb_VpIiXV0hQ
# 频道 URL: https://www.youtube.com/channel/UC_5lJHgnMP_lb_VpIiXV0hQ/

CHANNEL_URL="https://www.youtube.com/channel/UC_5lJHgnMP_lb_VpIiXV0hQ/videos"
BROWSER_COOKIES="chrome"

# 获取项目根目录（脚本在 tools/download/ 目录下）
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
cd "$PROJECT_ROOT" || exit 1

ARCHIVE_DIR="archive"
ARCHIVE_FILE="${ARCHIVE_DIR}/downloaded_history.txt"
WITH_SUBTITLES_DIR="${ARCHIVE_DIR}/有人工字幕"
WITHOUT_SUBTITLES_DIR="${ARCHIVE_DIR}/无人工字幕"

echo "=========================================="
echo "开始下载频道: 课代表立正"
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

# 创建存档目录（如果不存在）
mkdir -p "${ARCHIVE_DIR}"
mkdir -p "${WITH_SUBTITLES_DIR}"
mkdir -p "${WITHOUT_SUBTITLES_DIR}"

# 并发下载参数（可以根据需要调整）
CONCURRENT_FRAGMENTS=4  # 单个视频的并发片段数

echo "下载配置:"
echo "  - 音频格式: m4a (AAC)"
echo "  - 字幕: 优先人工字幕，无则自动字幕"
echo "  - 字幕格式: SRT"
echo "  - 字幕语言: 简体中文、繁体中文、英文"
echo "  - 并发片段: $CONCURRENT_FRAGMENTS"
echo "  - Cookies: 从浏览器读取 ($BROWSER_COOKIES)"
echo "  - 跳过会员专属视频"
echo ""

# 执行下载
# --write-sub: 下载人工上传的字幕
# --write-auto-sub: 如果没有人工字幕，下载自动生成的字幕
# --sub-lang: 指定字幕语言
# --convert-subs: 转换为 SRT 格式
yt-dlp "${CHANNEL_URL}" \
  --cookies-from-browser "${BROWSER_COOKIES}" \
  --match-filter "availability != subscriber_only & availability != needs_auth" \
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
  --output "${ARCHIVE_DIR}/%(upload_date)s_%(title)s_%(id)s/%(title)s.%(ext)s" \
  --download-archive "${ARCHIVE_FILE}" \
  --parse-metadata "description:(?s)(?P<meta_summary>.*)" \
  --no-playlist-reverse \
  --yes-playlist

echo ""
echo "=========================================="
echo "下载完成，开始分类整理..."
echo "=========================================="

# 运行 Python 脚本进行后处理分类
# 获取项目根目录（脚本在 tools/download/ 目录下）
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
ORGANIZE_SCRIPT="${PROJECT_ROOT}/tools/organize/organize_subtitles.py"

if command -v python3 &> /dev/null; then
    if [ -f "$ORGANIZE_SCRIPT" ]; then
        python3 "$ORGANIZE_SCRIPT" "${PROJECT_ROOT}/${ARCHIVE_DIR}"
    else
        echo "警告: 未找到整理脚本: $ORGANIZE_SCRIPT"
        echo "请手动运行: python3 tools/organize/organize_subtitles.py"
    fi
else
    echo "警告: 未找到 python3，跳过自动分类。"
    echo "请手动运行: python3 tools/organize/organize_subtitles.py"
fi

echo ""
echo "=========================================="
echo "完成！"
echo "=========================================="
echo "数据保存在: ${ARCHIVE_DIR}/"
echo "  - 有人工字幕: ${WITH_SUBTITLES_DIR}/"
echo "  - 无人工字幕: ${WITHOUT_SUBTITLES_DIR}/"
echo "下载历史记录: ${ARCHIVE_FILE}"
echo ""
echo "提示: 下次运行此脚本将自动跳过已下载的视频，只下载新内容。"
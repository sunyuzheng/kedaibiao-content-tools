# 课代表立正 · 频道内容库

YouTube 频道「课代表立正」的本地内容管理工具集——自动下载视频、分类字幕、上传到 Transistor.fm 播客平台，并维护正确的剧集顺序。

---

## 功能

- **自动下载**：用 yt-dlp 增量下载频道音频、字幕、元数据、缩略图，自动跳过会员专属视频
- **字幕分类**：按 info.json 中的 `subtitles` 字段自动将视频分入「有人工字幕」/「无人工字幕」
- **Whisper 转录**：对无字幕视频批量生成 SRT（支持 Apple Silicon MLX 和 CPU/CUDA）
- **上传播客**：将有字幕的音频上传到 Transistor.fm，自动设置标题、描述、YouTube 链接、缩略图
- **剧集排序**：按 YouTube 发布时间重排 EP 编号（E1 = 最早，E最大 = 最新）
- **对账检查**：实时对比本地 archive 与 Transistor，找出待上传视频

---

## 工作流

```
1. 下载    → ./tools/download/download_channel.sh
2. 对账    → python3 tools/check/check_upload_candidates.py
3. 上传    → python3 tools/upload/upload_to_transistor_v2.py --upload-only-new
4. 排序    → python3 tools/upload/reorder_episodes_by_date.py
```

详细说明见 [docs/核心任务说明.md](docs/核心任务说明.md)。

---

## 项目结构

```
archive/          本地视频资料（音频 + 字幕 + 元数据，gitignored）
  ├── 有人工字幕/   → 可直接上传 Transistor
  └── 无人工字幕/   → 需 Whisper 转录后再上传
docs/             工作流文档
envs/             Whisper 虚拟环境（gitignored）
logs/             操作日志（gitignored）
tools/
  ├── download/   下载脚本
  ├── organize/   字幕分类
  ├── transcribe/ Whisper 转录
  ├── check/      对账 + 诊断
  └── upload/     上传 + 排序 + 修复
```

---

## 配置

复制 `.env.example` 为 `.env` 并填入你的 Transistor API key：

```bash
cp .env.example .env
```

```
TRANSISTOR_API_KEY=your_transistor_api_key_here
TRANSISTOR_SHOW_ID=your_show_id_here
```

API key 从 [Transistor 账户设置](https://dashboard.transistor.fm/account) 获取。

---

## 依赖

```bash
brew install yt-dlp ffmpeg
pip install requests
# Whisper 转录（可选，无字幕视频用）：
pip install faster-whisper        # CPU/CUDA
pip install mlx-whisper           # Apple Silicon
```

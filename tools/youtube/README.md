# YouTube 自动化工具

「课代表立正」YouTube 频道的本地管理工具——覆盖数据拉取、视频描述更新、AI 分类、Playlist 创建/填充的完整流程。

---

## 认证（首次设置）

**OAuth 2.0 用户授权**（Desktop App 类型）

```bash
# 1. 安装依赖
python3 -m venv envs/youtube_env
envs/youtube_env/bin/pip install google-auth-oauthlib google-api-python-client anthropic

# 2. 授权（弹出浏览器，完成一次 Google 账号授权）
envs/youtube_env/bin/python3 tools/youtube/auth.py
```

- 凭证文件：`client_secret_187061917532-....json`（项目根目录，gitignored）
- Token 缓存：`tools/youtube/youtube_token.json`（gitignored，自动刷新）
- Google Cloud 项目：`claude-code-youtube-493002`（Testing 模式，需加入测试名单）

---

## 配额限制

默认 **10,000 units / 天**（按 Google Cloud 项目计算）

| 操作 | 消耗 |
|------|------|
| `videos.list`（批量，最多50个/call） | 1 unit / call |
| `videos.update`（单个视频） | 50 units / call |
| `playlists.insert`（创建一个 playlist） | 10 units |
| `playlistItems.insert`（往 playlist 加一个视频） | 50 units / call |

每天约可更新 **~199 个视频**，或往 playlist 添加 **~200 个视频**。

---

## 工作流一：视频描述更新（嘉宾信息块）

为嘉宾视频末尾追加统一的嘉宾信息块，引用 `lizheng.ai/guests`。

```bash
# 1. 拉取全量视频数据（生成 all_videos_full.json）
envs/youtube_env/bin/python3 tools/youtube/fetch_all_videos.py

# 2. 生成 patch manifest（读取 guests.json + all_videos_full.json）
envs/youtube_env/bin/python3 tools/youtube/build_patch_manifest.py

# 3. 预览变更
envs/youtube_env/bin/python3 tools/youtube/dry_run.py --n 5

# 4. 写入（先小批量验证，再全量）
envs/youtube_env/bin/python3 tools/youtube/apply_patches.py --limit 3
envs/youtube_env/bin/python3 tools/youtube/apply_patches.py
```

追加格式：
```
────────────────────────────────────
嘉宾 / Guest：刘友忠 Richard Liu
Founder @ Huma Finance · 湾区领航计划
更多嘉宾访谈：https://www.lizheng.ai/guests
────────────────────────────────────
```

---

## 工作流二：Playlist 分类 + 创建

用 Claude AI 对全部公开视频做 11 类分类，然后在 YouTube 创建/填充 playlist。

### 11 个分类

| # | 分类 | 内容 |
|---|------|------|
| 1 | AI时代机会 | AI趋势、AGI、AI对职业的冲击 |
| 2 | AI工具实战 | Cursor、Vibe Coding、具体工具使用 |
| 3 | 求职与面试 | 简历、面试、DS/PM/Eng 面试 |
| 4 | 职场技能 | 升职加薪、汇报、沟通、绩效谈判 |
| 5 | 大厂观察 | FAANG 内幕、VP 路径、裁员、大厂文化 |
| 6 | 创业实战 | 创业故事、融资、产品、副业变现 |
| 7 | 财富与投资 | 理财、投资策略、财富自由 |
| 8 | 思维与决策 | 思维模型、决策框架、认知偏差 |
| 9 | 人生设计 | 人生哲学、重大选择、意义感 |
| 10 | 数据科学 | DS/ML 技术、AB 实验、数据分析 |
| 11 | 美国生活 | 生活记录、文化观察、约会、回国vs留美 |

### 常用命令

```bash
# 分类（全量 / 断点续跑 / 重分类过时条目）
envs/youtube_env/bin/python3 tools/youtube/classify_playlists.py
envs/youtube_env/bin/python3 tools/youtube/classify_playlists.py --resume
envs/youtube_env/bin/python3 tools/youtube/classify_playlists.py --reclassify
envs/youtube_env/bin/python3 tools/youtube/classify_playlists.py --stats

# 在 YouTube 创建 playlist（只消耗 110 units，一次性操作）
envs/youtube_env/bin/python3 tools/youtube/create_playlists.py --create

# 填充视频（每天跑一次，自动断点续传，配额耗尽自动停止）
envs/youtube_env/bin/python3 tools/youtube/create_playlists.py --populate

# 预览方案
envs/youtube_env/bin/python3 tools/youtube/create_playlists.py --plan
```

`classify_playlists.py` 需要 `ANTHROPIC_API_KEY`（从项目根 `.env` 自动读取）。

---

## 工作流三：本地数据库

```bash
envs/youtube_env/bin/python3 tools/youtube/build_database.py
# 生成 tools/youtube/channel.db，可用 DB Browser for SQLite 打开
```

包含 `videos`、`guests`、`guest_videos` 三张表，以及 `guest_stats`、`video_detail` 两个视图。

---

## 文件说明

```
tools/youtube/
├── README.md                  本文档
├── auth.py                    OAuth 认证模块（被其他脚本 import）
│
├── fetch_all_videos.py        拉取全量视频元数据 → all_videos_full.json
├── build_database.py          构建本地 SQLite 数据库
│
├── build_patch_manifest.py    生成描述更新清单
├── dry_run.py                 预览描述变更
├── apply_patches.py           写入视频描述（断点续传）
│
├── classify_playlists.py      AI 分类视频 → playlist_manifest.json
└── create_playlists.py        在 YouTube 创建/填充 playlist

# gitignored（敏感数据 / 大文件）：
# youtube_token.json       OAuth token
# patch_manifest.json      描述变更清单（含原始描述）
# all_videos_full.json     全量视频元数据（大文件）
# all_videos_ids.json      视频 ID 列表
# populate_progress.json   playlist 填充进度（断点续传）
# channel.db               本地 SQLite 数据库

# 已提交（分类结果 + playlist ID 映射）：
# playlist_manifest.json   705个视频的 AI 分类结果
# playlist_ids.json        分类名 → YouTube playlist ID 映射
```

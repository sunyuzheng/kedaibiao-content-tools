# YouTube 自动化工具

批量更新「课代表立正」YouTube 视频描述，为每个嘉宾视频追加嘉宾信息块并引用 lizheng.ai/guests。

---

## 快速上手

```bash
# 1. 环境（首次）
python3 -m venv envs/youtube_env
envs/youtube_env/bin/pip install google-auth-oauthlib google-api-python-client

# 2. 授权（首次，之后 token 自动刷新）
envs/youtube_env/bin/python3 tools/youtube/auth.py

# 3. 构建 patch manifest（读取 guests.json + 本地 archive）
envs/youtube_env/bin/python3 tools/youtube/build_patch_manifest.py

# 4. 预览变更
envs/youtube_env/bin/python3 tools/youtube/dry_run.py --n 5

# 5. 写入（先小批量验证）
envs/youtube_env/bin/python3 tools/youtube/apply_patches.py --limit 3
envs/youtube_env/bin/python3 tools/youtube/apply_patches.py          # 全量
```

---

## 认证方式

**OAuth 2.0 用户授权**（Desktop App 类型）

- 凭证文件：`client_secret_187061917532-....json`（项目根目录）
- Token 缓存：`tools/youtube/youtube_token.json`（gitignored，自动刷新）
- 首次运行 `auth.py` 时弹出浏览器，完成一次 Google 账号授权即可
- Token 有效期：OAuth refresh token 长期有效（除非撤销或 7 天内未使用）

**注意**：OAuth 应用目前处于 Testing 模式，只有已加入测试名单的账号才能授权。
测试用户管理：Google Cloud Console → Auth Platform → Audience → Test users
项目：`claude-code-youtube-493002`

---

## 能做什么

通过 YouTube Data API v3（官方），以频道所有者身份操作：

| 操作 | API 方法 | 说明 |
|------|---------|------|
| 读取视频元数据 | `videos.list` | 标题、描述、分类、语言、隐私状态等 |
| **更新视频描述** | `videos.update` | 本工具核心用途 |
| 更新视频标题 | `videos.update` | 同上，snippet 全量更新 |
| 更新隐私状态 | `videos.update` | public / unlisted / private |
| 读取频道信息 | `channels.list` | 订阅数、视频数等 |
| 读取播放列表 | `playlistItems.list` | 遍历 Uploads 播放列表 |

`videos.update` 规则：必须传完整 `snippet`（不能只传 description），
所以每次更新需先 `videos.list` 读取现有 snippet，再 patch，再 update。

---

## 不能做什么

- **无法**上传新视频（需要 `youtube.upload` scope，本工具只申请了 `youtube.force-ssl`）
- **无法**删除视频
- **无法**管理社区帖子
- **无法**访问分析数据（需要 YouTube Analytics API，不同的 API）
- **无法**批量写入：每个视频必须单独调用一次 `videos.update`

---

## 配额限制

默认配额：**10,000 units / 天**（按 Google Cloud 项目计算）

| 操作 | 消耗 |
|------|------|
| `videos.list`（批量，最多50个/call） | 1 unit / call |
| `videos.update`（单个视频） | 50 units / call |

**本次任务**：353 个视频
- 读取阶段：⌈353/50⌉ = 8 calls = **8 units**（批量读，很便宜）
- 更新阶段：353 × 50 = **17,650 units**

每天能跑：(10,000 - 8) / 50 ≈ **199 个视频/天**
353 个视频分 **2 天**跑完（Day 1: ~199，Day 2: ~154）

如需提速，可在 Google Cloud Console 申请配额提升（最高 50,000 units/天）。

---

## 工具说明

### `build_patch_manifest.py`
读取 `guests.json` + 本地 `archive/` 的 `.info.json`，生成 `patch_manifest.json`。
- 幂等：描述里已含 `lizheng.ai/guests` 的视频自动跳过
- `--force`：强制重新生成所有条目

补充：

- `guests.json` 是 guest roster / 嘉宾-视频关系的权威来源
- 视频标题等 metadata 的权威来源是 `tools/youtube/all_videos_full.json`
- 网站侧的数据流说明见 `docs/网站嘉宾数据说明.md`

### `dry_run.py`
预览 before/after diff，不调用 YouTube API。
- `--n N`：只显示前 N 条
- `--guest NAME`：按嘉宾名过滤

### `apply_patches.py`
实际写入 YouTube。断点续传（已完成的标记为 `done`，中断后重跑自动跳过）。
- `--dry-run`：模拟调用，不写入
- `--limit N`：只处理前 N 条（小批量验证用）
- `--guest NAME`：只处理指定嘉宾

### `auth.py`
认证模块，被其他脚本 import，也可直接运行验证授权状态。

---

## 追加的嘉宾信息块格式

```
────────────────────────────────────
嘉宾 / Guest：刘友忠 Richard Liu（Richard Liu）
Founder @ Huma Finance · 湾区领航计划
更多嘉宾访谈：https://www.lizheng.ai/guests
────────────────────────────────────
```

追加在现有描述**末尾**，不修改原有内容。

---

## 文件

```
tools/youtube/
├── README.md              本文档
├── auth.py                OAuth 认证模块
├── build_patch_manifest.py  生成 patch_manifest.json
├── dry_run.py             预览变更
└── apply_patches.py       写入 YouTube

# gitignored（敏感数据）：
# tools/youtube/youtube_token.json   OAuth token
# tools/youtube/patch_manifest.json  变更清单（含原始描述）
```

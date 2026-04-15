# YouTube Playlist 重构复盘

> 时间：2026-04-10 ~ 2026-04-15
> 目标：将频道 705 个公开视频从零散旧 playlist 重组为 MECE 的 11类体系

---

## 背景

原有 playlist 状态混乱：「精选深度访谈」变成了 230 个视频的杂物桶，「硅谷职场攻略」里混了面试/升职/大厂/数据科学，没有有效导航意义。同时「嘉宾集锦」类 playlist 已被 lizheng.ai/guests 替代，可以删除。

---

## 最终分类体系（11类）

| 分类 | 定位 | 视频数 |
|------|------|--------|
| AI时代机会 | AI趋势、AGI、AI对职业冲击，不含具体工具操作 | 101 |
| AI工具实战 | Cursor、Vibe Coding、具体工具使用、prompt技巧 | 16 |
| 求职与面试 | 简历、面试、DS/PM/Eng面试，不含升职/管理 | 28 |
| 职场技能 | 升职加薪方法、汇报、沟通、情绪价值、绩效谈判 | 90 |
| 大厂观察 | FAANG内幕、VP路径、裁员故事、公司衰落分析 | 57 |
| 创业实战 | 创业故事、融资、产品、副业变现、自媒体经营 | 115 |
| 财富与投资 | 理财、投资策略、财富自由、股票/加密/房产 | 41 |
| 思维与决策 | 思维模型、决策框架、认知偏差，侧重「工具性」 | 130 |
| 人生设计 | 人生哲学、重大选择、意义感，侧重「内在探索」 | 120 |
| 数据科学 | DS/ML技术、AB实验、数据科学家职业路径 | 43 |
| 美国生活 | 生活记录、文化观察、约会、回国vs留美 | 43 |

**共 637 个有效分类，68 个归入「其他」**（直播录像/宠物/婚礼/测试内容）

关键判断边界（易混淆）：
- 「财富自由」访谈 → 财富与投资（不是人生设计）
- 「如何汇报/向上沟通/升职」→ 职场技能
- 「大厂 VP路径/裁员故事」→ 大厂观察（不是职场技能）
- 「如何找到人生意义/自洽」→ 人生设计
- 「AI如何影响职业」→ AI时代机会
- 「离职故事/人生转折」→ 人生设计（可兼大厂观察）
- 「数据科学面试」→ 数据科学 + 求职与面试（双分类）

---

## 执行过程

### 第一阶段：AI分类

用 `classify_playlists.py`（Claude Haiku）对 705 个视频跑分类。

**遇到的问题：**

1. **旧类「精选深度访谈」成了 230 个视频的黑洞**  
   → 删掉该类，改为 10 个具体分类重跑，才分散开

2. **「职场晋升与领导力」140 个视频超标**  
   → 拆分为「职场技能」(升职/汇报/沟通) + 「大厂观察」(大厂内幕/VP路径)  
   → 最终：职场技能 90 + 大厂观察 57

3. **高播放视频被误分**（手动修正）：
   - 「如何积累第一桶金」(157K views) → 思维与决策 → 财富与投资
   - 「打工人如何获得财富自由」(144K views) → 人生设计 → 财富与投资
   - 「Onlyfans博主访谈」→ 创业实战 → 美国生活
   - 「郭宇财富自由」→ 其他 → 财富与投资

4. **Claude Haiku 返回 markdown 包裹的 JSON**  
   → 在 `classify_video()` 里加了 strip markdown fences 逻辑

**运行模式：**
```bash
# 全量（第一次）
envs/youtube_env/bin/python3 tools/youtube/classify_playlists.py

# 重分类过时分类（清除旧类）
envs/youtube_env/bin/python3 tools/youtube/classify_playlists.py --reclassify

# 只看统计
envs/youtube_env/bin/python3 tools/youtube/classify_playlists.py --stats
```

### 第二阶段：创建 Playlist

```bash
envs/youtube_env/bin/python3 tools/youtube/create_playlists.py --create
```

一次性操作，消耗 110 units（11 playlist × 10 units）。

### 第三阶段：填充视频（5天）

每天 10,000 units 配额上限，200 videos/天。

| 日期 | 新增 | 进度 |
|------|------|------|
| 4/11 | 171 | 171/784 |
| 4/12 | 200 | 371/784 |
| 4/13 | 200 | 571/784 |
| 4/14 | 200 | 771/784 |
| 4/15 | 13 | 784/784 ✓ |

**设计亮点：**
- `populate_progress.json` 记录已完成的 (playlist, video_id) 对，重跑自动跳过，不浪费配额
- 配额耗尽时优雅退出并提示「明天继续」
- 按 view_count 降序填充，高播放视频优先进 playlist

---

## 脚本文件

| 文件 | 用途 |
|------|------|
| `classify_playlists.py` | AI 分类（Claude Haiku），维护 `playlist_manifest.json` |
| `create_playlists.py` | 创建 playlist + 填充视频（断点续传） |
| `playlist_manifest.json` | 705 视频的分类结果（已提交，是权威来源） |
| `playlist_ids.json` | 分类名 → YouTube playlist ID 映射（已提交） |
| `populate_progress.json` | 填充进度（gitignored，临时状态） |

---

## 经验总结

1. **分类体系最难的地方不是 AI 标注，是边界定义**  
   「职场技能 vs 大厂观察」「思维与决策 vs 人生设计」这类边界，要在 SYSTEM_PROMPT 里给出具体对立例子，Claude 才能稳定区分。

2. **每个 playlist 40-80 个视频是合理上限**  
   超过 100 的要拆。拆的依据是「观众的不同意图」而不是「内容的表面相似」。

3. **配额管理必须有本地进度文件**  
   不然每次重跑都会在已加入的视频上浪费 50 units/个（即使失败也消耗配额）。

4. **高播放误分会严重影响用户体验**  
   分类结束后要专门 review TOP 20 高播放视频的分类是否合理，这类视频被人看到的概率最高。

#!/usr/bin/env python3
"""One-shot fix for the ~6 high-view videos stuck in 其他."""
import json, time
from pathlib import Path
import anthropic

PROJECT_ROOT = Path(__file__).parent.parent.parent
ALL_VIDEOS_FILE = PROJECT_ROOT / "tools/youtube/all_videos_full.json"
MANIFEST_FILE  = PROJECT_ROOT / "tools/youtube/playlist_manifest.json"

api_key = None
for line in (PROJECT_ROOT / ".env").read_text().splitlines():
    if line.startswith("ANTHROPIC_API_KEY="):
        api_key = line.split("=", 1)[1].strip()

client = anthropic.Anthropic(api_key=api_key)

raw = json.loads(ALL_VIDEOS_FILE.read_text())
vid_map = {v["video_id"]: v for v in raw}

manifest = json.loads(MANIFEST_FILE.read_text(encoding="utf-8"))

targets = {vid: info for vid, info in manifest.items()
           if info.get("categories") == ["其他"] and info.get("view_count", 0) > 3000}

SYSTEM = """你是帮助 YouTube 频道「课代表立正」做 playlist 分类的助手。
6 个分类：AI与科技前沿 / 硅谷职场攻略 / 财富与投资 / 创业实战 / 认知与个人成长 / 硅谷生活

「其他」只用于：宠物/测试/直播录像/婚礼/派对这类没有实质内容的视频
实质内容视频必须归入6个分类之一：
- 「世界模型」哲学/认知/AI → 认知与个人成长 或 AI与科技前沿
- 「学会吃苦」励志/成长 → 认知与个人成长
- 「婚礼视频！」→ 其他（例外）
- 「Onlyfans博主访谈」→ 硅谷生活
- 「拍正脸好看秘籍」→ 硅谷生活
- 「喜剧让我们无敌」→ 认知与个人成长

只返回 JSON：{"categories": ["分类名"], "reason": "一句话"}，不要其他文字"""

for vid, info in sorted(targets.items(), key=lambda x: -x[1].get("view_count", 0)):
    v = vid_map.get(vid, {})
    title = info["title"]
    desc = (v.get("description") or "")[:200].strip()
    user_msg = f"标题：{title}\n描述：{desc if desc else '（无）'}"

    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=150,
        system=SYSTEM,
        messages=[{"role": "user", "content": user_msg}],
    )
    text = resp.content[0].text.strip()
    if text.startswith("```"):
        text = text.split("```", 2)[1]
        if text.startswith("json"):
            text = text[4:]
        text = text.strip()
    data = json.loads(text)
    cats = data.get("categories", ["其他"])
    manifest[vid]["categories"] = cats
    manifest[vid]["reason"] = data.get("reason", "")
    print(f"{info['view_count']:>7,}  {title[:50]}  →  {cats}")
    time.sleep(0.1)

MANIFEST_FILE.write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
print("✓ Done")

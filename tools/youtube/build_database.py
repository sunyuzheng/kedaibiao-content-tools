#!/usr/bin/env python3
"""
从 all_videos_full.json + guests.json 构建本地 SQLite 数据库。

表：
  videos        - 所有视频元数据
  guests        - 嘉宾信息
  guest_videos  - 嘉宾 ↔ 视频多对多关系

视图：
  guest_stats   - 嘉宾维度聚合（总播放、平均播放、期数等）
  video_detail  - 视频 + 嘉宾信息 join

用法：
  envs/youtube_env/bin/python3 tools/youtube/build_database.py
  # 生成 tools/youtube/channel.db，可用 DB Browser for SQLite 打开
"""

import json
import re
import sqlite3
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent.parent
DB_PATH = Path(__file__).parent / "channel.db"
VIDEOS_FILE = PROJECT_ROOT / "tools/youtube/all_videos_full.json"
GUESTS_FILE = PROJECT_ROOT / "guests.json"


def parse_duration(iso: str) -> int:
    """ISO 8601 时长 → 秒数"""
    m = re.match(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", iso or "")
    if not m:
        return 0
    h, mn, s = (int(x or 0) for x in m.groups())
    return h * 3600 + mn * 60 + s


def build():
    raw = json.loads(VIDEOS_FILE.read_text())
    # 去重（同一视频可能出现在多个播放列表条目中）
    seen = set()
    videos = []
    for v in raw:
        if v["video_id"] not in seen:
            seen.add(v["video_id"])
            videos.append(v)
    guests = json.loads(GUESTS_FILE.read_text())

    DB_PATH.unlink(missing_ok=True)
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()

    # ── videos ────────────────────────────────────────────────
    cur.execute("""
    CREATE TABLE videos (
        video_id        TEXT PRIMARY KEY,
        title           TEXT,
        description     TEXT,
        published_at    TEXT,
        privacy         TEXT,
        duration_sec    INTEGER,
        view_count      INTEGER,
        like_count      INTEGER,
        comment_count   INTEGER,
        category_id     TEXT,
        default_language TEXT,
        tags            TEXT,       -- JSON array
        has_description INTEGER,    -- 0/1
        desc_length     INTEGER
    )""")

    cur.executemany("""
    INSERT INTO videos VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, [
        (
            v["video_id"], v["title"], v["description"], v["published_at"],
            v["privacy"], parse_duration(v["duration"]),
            v["view_count"], v["like_count"], v["comment_count"],
            v["category_id"], v["default_language"],
            json.dumps(v["tags"], ensure_ascii=False),
            1 if v["description"].strip() else 0,
            len(v["description"].strip()),
        )
        for v in videos
    ])

    # ── guests ─────────────────────────────────────────────────
    cur.execute("""
    CREATE TABLE guests (
        guest_id        INTEGER PRIMARY KEY AUTOINCREMENT,
        guest_name      TEXT,
        guest_en_name   TEXT,
        guest_title     TEXT,
        guest_company   TEXT,
        max_views       INTEGER,
        episode_count   INTEGER,
        primary_video_id TEXT,
        primary_url     TEXT,
        thumbnail_url   TEXT
    )""")

    cur.executemany("""
    INSERT INTO guests
      (guest_name, guest_en_name, guest_title, guest_company,
       max_views, episode_count, primary_video_id, primary_url, thumbnail_url)
    VALUES (?,?,?,?,?,?,?,?,?)
    """, [
        (
            g["guest_name"], g.get("guest_en_name",""), g.get("guest_title",""),
            g.get("guest_company",""), g.get("max_views",0), g["episode_count"],
            g.get("primary_video_id",""), g.get("primary_url",""), g.get("thumbnail_url",""),
        )
        for g in guests
    ])

    # ── guest_videos ───────────────────────────────────────────
    cur.execute("""
    CREATE TABLE guest_videos (
        guest_id  INTEGER REFERENCES guests(guest_id),
        video_id  TEXT    REFERENCES videos(video_id),
        PRIMARY KEY (guest_id, video_id)
    )""")

    # guest_id lookup
    cur.execute("SELECT guest_id, guest_name FROM guests")
    name_to_id = {row[1]: row[0] for row in cur.fetchall()}

    rows = []
    for g in guests:
        gid = name_to_id[g["guest_name"]]
        for vid in g.get("all_video_ids", []):
            rows.append((gid, vid))
    cur.executemany("INSERT OR IGNORE INTO guest_videos VALUES (?,?)", rows)

    # ── views ──────────────────────────────────────────────────
    cur.execute("""
    CREATE VIEW guest_stats AS
    SELECT
        g.guest_id,
        g.guest_name,
        g.guest_en_name,
        g.guest_title,
        g.guest_company,
        g.episode_count,
        g.max_views,
        SUM(v.view_count)                          AS total_views,
        CAST(AVG(v.view_count) AS INTEGER)         AS avg_views_per_ep,
        SUM(v.like_count)                          AS total_likes,
        CAST(AVG(v.like_count) AS INTEGER)         AS avg_likes_per_ep,
        MIN(v.published_at)                        AS first_episode,
        MAX(v.published_at)                        AS last_episode
    FROM guests g
    JOIN guest_videos gv USING (guest_id)
    JOIN videos v USING (video_id)
    GROUP BY g.guest_id
    ORDER BY total_views DESC
    """)

    cur.execute("""
    CREATE VIEW video_detail AS
    SELECT
        v.*,
        GROUP_CONCAT(g.guest_name, ' / ') AS guest_names
    FROM videos v
    LEFT JOIN guest_videos gv USING (video_id)
    LEFT JOIN guests g USING (guest_id)
    GROUP BY v.video_id
    """)

    con.commit()
    con.close()

    print(f"✓ 数据库已构建：{DB_PATH}")
    print(f"  videos: {len(videos)} 条")
    print(f"  guests: {len(guests)} 条")
    print(f"  guest_videos: {len(rows)} 条关联")


if __name__ == "__main__":
    build()

    # 验证几个查询
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    cur = con.cursor()

    print("\n=== Top 10 嘉宾（总播放）===")
    for r in cur.execute("SELECT guest_name, total_views, avg_views_per_ep, episode_count FROM guest_stats LIMIT 10"):
        print(f"  {r['guest_name']:<25} 总计 {r['total_views']:>8,}  均 {r['avg_views_per_ep']:>6,}  {r['episode_count']}期")

    print("\n=== 描述为空的公开视频（前10）===")
    for r in cur.execute("""
        SELECT video_id, title, view_count FROM videos
        WHERE privacy='public' AND has_description=0
        ORDER BY view_count DESC LIMIT 10
    """):
        print(f"  {r['video_id']}  {r['view_count']:>6,}  {r['title'][:50]}")

    print("\n=== 高播放但无标签（前10）===")
    for r in cur.execute("""
        SELECT video_id, title, view_count FROM videos
        WHERE privacy='public' AND tags='[]'
        ORDER BY view_count DESC LIMIT 10
    """):
        print(f"  {r['view_count']:>6,}  {r['title'][:60]}")

    con.close()

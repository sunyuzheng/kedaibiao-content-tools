#!/usr/bin/env python3
"""Plan/review a title-only series migration; applying requires its exact scope hash."""
from __future__ import annotations

import argparse
import csv
import html
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from tools.podcast.core import (atomic_write_json, canonical_json, extract_video_id,
    load_env, require_transistor_config, sha256_text, utc_now, strip_episode_number)
from tools.podcast.series import episode_series_title, load_catalog, SERIES_LABELS, SERIES_PREFIX_RE
from tools.podcast.transistor_client import TransistorClient

PROTECTED_FIELDS = ("number", "status", "published_at", "video_url", "description", "media_url",
    "image_url", "slug", "share_url", "transcript_url", "duration", "season", "type", "explicit",
    "summary", "alternate_url")


def protected_attributes(episode: dict) -> dict:
    return {key: episode["attributes"].get(key) for key in PROTECTED_FIELDS}


def scope_hash(plan: dict) -> str:
    return sha256_text(canonical_json({key: plan[key] for key in
        ("kind", "show_id", "destination", "audience", "actions")}))


def show_of(episode: dict) -> str:
    return str(episode.get("relationships", {}).get("show", {}).get("data", {}).get("id", ""))


def build_plan(episodes: list[dict], show_id: str, catalog: dict) -> dict:
    actions = []
    seen_ids, seen_videos = set(), set()
    for episode in episodes:
        a = episode["attributes"]
        if a.get("status") != "published":
            continue
        if show_of(episode) != show_id:
            raise ValueError(f"Wrong show for {episode['id']}")
        vid = extract_video_id(a.get("video_url"))
        if not vid or episode["id"] in seen_ids or vid in seen_videos:
            raise ValueError("Missing/duplicate episode identity")
        seen_ids.add(episode["id"]); seen_videos.add(vid)
        title = episode_series_title(a["title"], vid, catalog=catalog)
        entry = catalog[vid]
        if entry.get("global_number") != a.get("number"):
            raise ValueError(f"Global number drift: {vid}")
        actions.append({"episode_id": str(episode["id"]), "video_id": vid,
            "before_title": a["title"], "after_title": title,
            "series": entry["series"], "series_number": entry["series_number"],
            "source_date": entry["source_date"], "classification_basis": entry["classification_basis"],
            "protected": protected_attributes(episode)})
    actions.sort(key=lambda row: row["protected"]["number"])
    plan = {"schema_version": 1, "kind": "podcast_series_title_migration", "show_id": show_id,
        "generated_at": utc_now(), "destination": "https://pod.lizheng.ai + Transistor RSS and subscribed podcast apps",
        "audience": "所有公开访问者及播客订阅者", "actions": actions}
    plan["approval_hash"] = scope_hash(plan)
    return plan


def validate_plan(plan: dict, approval: str) -> None:
    if plan.get("kind") != "podcast_series_title_migration" or plan.get("schema_version") != 1:
        raise ValueError("Unsupported migration plan")
    if not approval or approval != plan.get("approval_hash") or approval != scope_hash(plan):
        raise ValueError("Exact reviewed approval hash required")
    identities, videos, slots = set(), set(), set()
    for row in plan["actions"]:
        if row["episode_id"] in identities or row["video_id"] in videos:
            raise ValueError("Duplicate migration identity")
        identities.add(row["episode_id"]); videos.add(row["video_id"])
        slot = (row["series"], row["series_number"])
        if slot in slots:
            raise ValueError("Duplicate migration series number")
        slots.add(slot)
        expected = f"{SERIES_LABELS[row['series']]} {row['series_number']:03d}｜{strip_episode_number(row['before_title'])}"
        if row["after_title"] != expected or not SERIES_PREFIX_RE.fullmatch(row["after_title"]):
            raise ValueError("Migration may only replace the title prefix")
        if set(row["protected"]) != set(PROTECTED_FIELDS) or row["protected"]["status"] != "published":
            raise ValueError("Incomplete published episode preconditions")


def assert_remote(episode: dict, row: dict, show_id: str) -> None:
    if str(episode["id"]) != row["episode_id"] or show_of(episode) != show_id:
        raise ValueError(f"Remote identity drift: {row['episode_id']}")
    if protected_attributes(episode) != row["protected"]:
        raise ValueError(f"Protected episode fields changed: {row['episode_id']}")
    if extract_video_id(episode["attributes"].get("video_url")) != row["video_id"]:
        raise ValueError("Remote video identity drift")
    if episode["attributes"]["title"] not in (row["before_title"], row["after_title"]):
        raise ValueError(f"Remote title changed since review: {row['episode_id']}")


def apply_plan(client, plan: dict, approval: str, ledger: Path) -> dict:
    validate_plan(plan, approval)
    # Check the whole scope before the first write, including when resuming.
    remote = {str(e["id"]): e for e in client.list_episodes(plan["show_id"])}
    for row in plan["actions"]:
        assert_remote(remote[row["episode_id"]], row, plan["show_id"])
    results = []
    ledger.parent.mkdir(parents=True, exist_ok=True)
    with ledger.open("a", encoding="utf-8") as log:
        for i, row in enumerate(plan["actions"], 1):
            current = client.get_episode(row["episode_id"])
            assert_remote(current, row, plan["show_id"])
            status = "already_applied"
            if current["attributes"]["title"] != row["after_title"]:
                client.update_episode(row["episode_id"], {"title": row["after_title"]})
                current = client.get_episode(row["episode_id"])
                assert_remote(current, row, plan["show_id"])
                if current["attributes"]["title"] != row["after_title"]:
                    raise ValueError(f"Title readback mismatch: {row['episode_id']}")
                status = "updated"
            result = {"at": utc_now(), "episode_id": row["episode_id"], "status": status,
                "title": row["after_title"], "approval_hash": approval}
            log.write(json.dumps(result, ensure_ascii=False) + "\n"); log.flush()
            results.append(result)
            if i % 20 == 0 or i == len(plan["actions"]):
                print(f"Verified {i}/{len(plan['actions'])}", flush=True)
    return {"status": "completed", "approval_hash": approval, "count": len(results),
        "counts": dict(Counter(r["status"] for r in results)), "completed_at": utc_now()}


def write_review(plan: dict, directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    atomic_write_json(directory / "plan.json", plan)
    with (directory / "titles.csv").open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f); writer.writerow(["原全局编号", "视频ID", "系列", "系列编号", "发布日期", "原标题", "新标题", "分类依据"])
        for r in plan["actions"]:
            writer.writerow([r["protected"]["number"], r["video_id"], SERIES_LABELS[r["series"]], r["series_number"],
                r["source_date"], r["before_title"], r["after_title"], r["classification_basis"]])
    h = html.escape
    counts = Counter(r["series"] for r in plan["actions"])
    rows = "".join(f'<tr data-series="{r["series"]}"><td>{r["protected"]["number"]}</td>'
        f'<td><a href="https://pod.lizheng.ai/episodes/{h(r["protected"]["slug"])}">{h(r["before_title"])}</a></td>'
        f'<td><strong>{h(r["after_title"])}</strong><small>{h(r["source_date"])} · {h(r["classification_basis"])}</small></td></tr>'
        for r in reversed(plan["actions"]))
    review = '''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>播客系列改名 · 完整改名清单</title><style>
body{font:16px/1.7 system-ui;background:#f6f5f1;color:#202322;margin:0}main{max-width:1280px;margin:auto;padding:36px 24px}h1{font-size:32px;margin:8px 0}p{max-width:900px}a{color:#376455}section{background:white;border:1px solid #ddd;border-radius:12px;padding:20px;margin:20px 0}.tag{font-size:24px;margin-right:28px;font-weight:700}table{border-collapse:collapse;width:100%;background:white}th,td{text-align:left;vertical-align:top;border-bottom:1px solid #ddd;padding:14px}th{position:sticky;top:0;background:#e7ece7}td:nth-child(2){width:43%;color:#666}small{display:block;color:#707570;margin-top:8px}input,select{font:inherit;padding:10px;border:1px solid #bbb;border-radius:6px}input{width:min(60%,650px)}code{overflow-wrap:anywhere;font-size:12px}summary{cursor:pointer}li{margin:6px 0}@media(max-width:700px){main{padding:20px 12px}th,td{padding:8px;font-size:13px}h1{font-size:25px}.tag{font-size:19px}}
</style><main><div>待发布 · 本地审阅</div><h1>对话 / 立正说：独立编号</h1>
<section><span class="tag">对话 COUNT_D 期</span><span class="tag">立正说 COUNT_S 期</span>
<p>按原始发布日期分别编号。标题只替换前缀，保留每期现有正文；Transistor/RSS 的全局期号、音频、简介、链接和发布日期保持原值。</p>
<p><b>发布位置：</b>Transistor（节目 71709）、pod.lizheng.ai、公开 RSS 及后续同步的播客客户端。<br><b>受众：</b>所有公开访问者与订阅者。</p>
<details open><summary><b>分类边界与需要留意的历史特例</b></summary><ul>
<li>嘉宾访谈、反向采访、多人讨论、合作演示归「对话」；立正本人独立讲解、演讲、答疑及生活记录归「立正说」。</li>
<li>「对话」作为嘉宾内容总系列，也包含原 E49、E318、E373、E423、E499 的鸭哥独立讲解/演示，以及 E242 飞呀自我介绍。它们不是立正个人内容；若希望「对话」严格只收多人对谈，这 6 期需要另定归属。</li>
<li>E123 SVTC 职业问答、E505 耶鲁活动主讲、E526 英文假设检验均归「立正说」。E523 大会现场报道含多位嘉宾交流，归「对话」。</li>
<li>每一个已发布音频条目有自己的编号，包括上下集、剪辑片段；已有标题中的上下集等信息保留。</li>
</ul></details><p><a href="titles.csv">下载完整 CSV</a> · <a href="plan.json">查看冻结执行计划</a></p>
<small>审批绑定此版本：<code>APPROVAL_HASH</code></small></section>
<p><input id="q" placeholder="搜索标题、原编号或分类依据"><select id="series"><option value="">全部系列</option><option value="dialogue">对话</option><option value="solo">立正说</option></select> <span id="count"></span></p>
<table><thead><tr><th>原期号</th><th>原标题</th><th>新标题与分类依据</th></tr></thead><tbody>ROWS</tbody></table>
<script>const rows=[...document.querySelectorAll('tbody tr')],q=document.querySelector('#q'),s=document.querySelector('#series');function filter(){let n=0;for(const r of rows){const show=(!s.value||r.dataset.series===s.value)&&r.textContent.toLowerCase().includes(q.value.toLowerCase());r.hidden=!show;if(show)n++}document.querySelector('#count').textContent=n+' 期'}q.addEventListener('input',filter);s.addEventListener('change',filter);filter();</script></main></html>'''
    review = review.replace("COUNT_D", str(counts["dialogue"])).replace("COUNT_S", str(counts["solo"])).replace("APPROVAL_HASH", h(plan["approval_hash"])).replace("ROWS", rows)
    (directory / "index.html").write_text(review, encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path, help="Read-only saved API listing")
    parser.add_argument("--show-id", default="71709")
    parser.add_argument("--review-dir", type=Path, required=True)
    parser.add_argument("--plan", type=Path)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--approval-hash")
    args = parser.parse_args()
    if args.apply:
        if not args.plan or not args.approval_hash:
            parser.error("--apply requires --plan and --approval-hash")
        plan = json.loads(args.plan.read_text(encoding="utf-8"))
        validate_plan(plan, args.approval_hash)
        load_env(); key, show = require_transistor_config()
        if show != plan["show_id"]:
            raise ValueError("Configured show differs from approved destination")
        receipt = apply_plan(TransistorClient(key), plan, args.approval_hash, args.review_dir / "execution.jsonl")
        atomic_write_json(args.review_dir / "receipt.json", receipt)
        print(json.dumps(receipt, ensure_ascii=False))
    else:
        if args.plan:
            plan = json.loads(args.plan.read_text(encoding="utf-8"))
            validate_plan(plan, plan["approval_hash"])
        else:
            if args.snapshot:
                episodes = json.loads(args.snapshot.read_text(encoding="utf-8"))
            else:
                load_env(); key, show = require_transistor_config()
                if show != args.show_id:
                    raise ValueError("Configured show mismatch")
                episodes = TransistorClient(key).list_episodes(show)
            plan = build_plan(episodes, args.show_id, load_catalog())
        write_review(plan, args.review_dir)
        print(json.dumps({"count": len(plan["actions"]), "approval_hash": plan["approval_hash"], "review": str(args.review_dir / "index.html")}, ensure_ascii=False))


if __name__ == "__main__":
    main()

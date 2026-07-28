"""Self-contained, offline review dashboard for an immutable plan."""

from __future__ import annotations

import html
from collections import Counter
from pathlib import Path
from typing import Any


def _escape(value: Any) -> str:
    return html.escape(str(value or ""), quote=True)


def _badge(label: str, tone: str) -> str:
    return f'<span class="badge badge-{tone}">{_escape(label)}</span>'


def _action_card(item: dict[str, Any]) -> str:
    warnings = item.get("warnings") or []
    warning_html = "".join(_badge(value.replace("_", " "), "warning") for value in warnings)
    transcript = (
        f"{int(item.get('transcript_chars') or 0):,} characters"
        if item.get("transcript_path")
        else "Not available"
    )
    search = " ".join(
        [
            str(item.get("video_id") or ""),
            str(item.get("title") or ""),
            " ".join(str(value) for value in warnings),
        ]
    ).lower()
    return f"""\
<article class="item action-item" data-kind="action" data-search="{_escape(search)}">
  <div class="item-marker marker-action" aria-hidden="true"></div>
  <div class="item-body">
    <div class="item-heading">
      <div>
        <p class="eyebrow">ACTION · {_escape(item.get("action"))}</p>
        <h3>{_escape(item.get("title"))}</h3>
      </div>
      {_badge("Ready", "ready")}
    </div>
    <dl class="facts">
      <div><dt>YouTube ID</dt><dd><code>{_escape(item.get("video_id"))}</code></dd></div>
      <div><dt>Publish date</dt><dd>{_escape(str(item.get("published_at") or "")[:10])}</dd></div>
      <div><dt>Transcript</dt><dd>{_escape(transcript)}</dd></div>
      <div><dt>Audio</dt><dd>{int(item.get("audio_bytes") or 0) / 1_048_576:.1f} MB</dd></div>
    </dl>
    <div class="badges">{warning_html or _badge("No warnings", "neutral")}</div>
  </div>
</article>"""


def _blocked_card(item: dict[str, Any]) -> str:
    reasons = item.get("reasons") or ["unspecified"]
    reason_html = "".join(_badge(value.replace("_", " "), "blocked") for value in reasons)
    search = " ".join(
        [
            str(item.get("video_id") or ""),
            str(item.get("title") or ""),
            " ".join(str(value) for value in reasons),
        ]
    ).lower()
    return f"""\
<article class="item blocked-item" data-kind="blocked" data-search="{_escape(search)}">
  <div class="item-marker marker-blocked" aria-hidden="true"></div>
  <div class="item-body">
    <div class="item-heading">
      <div>
        <p class="eyebrow">QUARANTINED · INDEX {_escape(item.get("playlist_index"))}</p>
        <h3>{_escape(item.get("title"))}</h3>
      </div>
      {_badge("No action", "blocked")}
    </div>
    <p class="mono-line">YouTube ID <code>{_escape(item.get("video_id"))}</code></p>
    <div class="badges">{reason_html}</div>
  </div>
</article>"""


def render_review(
    plan: dict[str, Any],
    output_path: Path,
    *,
    apply_command: str,
) -> Path:
    summary = plan.get("summary") or {}
    actions = plan.get("actions") or []
    blocked = plan.get("blocked") or []
    warning_count = sum(len(item.get("warnings") or []) for item in actions)
    reason_counts = Counter(
        reason
        for item in blocked
        for reason in (item.get("reasons") or ["unspecified"])
    )
    if actions and (blocked or warning_count):
        decision_title = "Ready with exceptions"
        decision_tone = "attention"
        decision_copy = "Review the action scope and understand the quarantined items before approval."
    elif actions:
        decision_title = "Ready for approval"
        decision_tone = "ready"
        decision_copy = "All planned actions passed the configured safety checks."
    else:
        decision_title = "Nothing will be written"
        decision_tone = "neutral"
        decision_copy = "There are no executable actions in this plan."

    reason_rows = "".join(
        f"<li><span>{_escape(reason.replace('_', ' '))}</span><strong>{count}</strong></li>"
        for reason, count in reason_counts.most_common()
    ) or "<li><span>No blocked reasons</span><strong>0</strong></li>"
    items_html = "\n".join(
        [*(_action_card(item) for item in actions), *(_blocked_card(item) for item in blocked)]
    ) or '<div class="empty">No action or blocked item is present in this plan.</div>'

    document = f"""\
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="color-scheme" content="light">
  <link rel="icon" href="data:,">
  <title>YouTube to Podcast · Plan review</title>
  <style>
    :root {{
      --bg: #fff7ed;
      --surface: #ffffff;
      --surface-soft: #fffaf5;
      --text: #0f172a;
      --muted: #5f6572;
      --border: #eadfd4;
      --primary: #c2410c;
      --primary-soft: #ffedd5;
      --accent: #1d4ed8;
      --accent-soft: #dbeafe;
      --ready: #166534;
      --ready-soft: #dcfce7;
      --warning: #9a3412;
      --warning-soft: #ffedd5;
      --blocked: #b91c1c;
      --blocked-soft: #fee2e2;
      --shadow: 0 12px 38px rgba(92, 51, 23, .08);
      --radius: 18px;
      font-family: Inter, ui-sans-serif, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }}
    * {{ box-sizing: border-box; }}
    html {{ scroll-behavior: smooth; }}
    body {{ margin: 0; color: var(--text); background: var(--bg); font-size: 16px; line-height: 1.55; }}
    button, input {{ font: inherit; }}
    button {{ min-height: 44px; cursor: pointer; }}
    button:focus-visible, input:focus-visible {{ outline: 3px solid rgba(29, 78, 216, .35); outline-offset: 2px; }}
    .skip-link {{ position: fixed; left: 16px; top: -80px; z-index: 20; background: var(--text); color: white; padding: 10px 14px; border-radius: 8px; }}
    .skip-link:focus {{ top: 16px; }}
    .shell {{ width: min(1180px, calc(100% - 32px)); margin: 0 auto; padding: 32px 0 72px; }}
    .topbar {{ display: flex; justify-content: space-between; gap: 20px; align-items: start; margin-bottom: 24px; }}
    .product {{ color: var(--primary); font-weight: 800; letter-spacing: -.02em; }}
    .muted {{ color: var(--muted); }}
    h1 {{ margin: 6px 0 8px; font-size: clamp(2rem, 5vw, 3.5rem); line-height: 1.02; letter-spacing: -.045em; }}
    h2 {{ margin: 0; font-size: 1.25rem; letter-spacing: -.02em; }}
    h3 {{ margin: 3px 0 0; font-size: 1.05rem; line-height: 1.35; }}
    p {{ margin: 0; }}
    .hash-box {{ max-width: 330px; text-align: right; }}
    code, .mono-line {{ font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }}
    .hash {{ display: block; margin-top: 4px; overflow-wrap: anywhere; font-size: .76rem; color: var(--muted); }}
    .decision {{ display: grid; grid-template-columns: 1.4fr .8fr; gap: 18px; margin-bottom: 18px; }}
    .panel {{ background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius); box-shadow: var(--shadow); }}
    .decision-main {{ padding: 24px; border-left: 6px solid var(--primary); }}
    .decision-main[data-tone="ready"] {{ border-left-color: var(--ready); }}
    .decision-main[data-tone="neutral"] {{ border-left-color: var(--muted); }}
    .eyebrow {{ color: var(--muted); font-size: .72rem; font-weight: 800; letter-spacing: .09em; text-transform: uppercase; }}
    .decision-main h2 {{ margin: 8px 0; font-size: 1.6rem; }}
    .reason-panel {{ padding: 20px; }}
    .reason-list {{ list-style: none; margin: 14px 0 0; padding: 0; }}
    .reason-list li {{ display: flex; justify-content: space-between; gap: 12px; padding: 8px 0; border-top: 1px solid var(--border); font-size: .88rem; }}
    .metrics {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 12px; margin-bottom: 18px; }}
    .metric {{ padding: 18px; }}
    .metric strong {{ display: block; font-size: 1.8rem; line-height: 1; }}
    .metric span {{ display: block; margin-top: 7px; color: var(--muted); font-size: .82rem; }}
    .steps {{ padding: 22px; margin-bottom: 18px; }}
    .step-grid {{ display: grid; grid-template-columns: repeat(3, 1fr); gap: 14px; margin-top: 16px; }}
    .step {{ padding: 16px; border: 1px solid var(--border); background: var(--surface-soft); border-radius: 13px; }}
    .step-number {{ display: grid; place-items: center; width: 28px; height: 28px; margin-bottom: 12px; border-radius: 50%; background: var(--accent-soft); color: var(--accent); font-weight: 800; }}
    .step strong {{ display: block; margin-bottom: 5px; }}
    .step p {{ color: var(--muted); font-size: .88rem; }}
    .approval {{ padding: 22px; margin-bottom: 18px; }}
    .approval-row {{ display: grid; grid-template-columns: 1fr auto; align-items: center; gap: 12px; margin-top: 14px; }}
    .command {{ min-width: 0; padding: 14px; overflow-wrap: anywhere; background: #0f172a; color: #f8fafc; border-radius: 11px; font-size: .8rem; }}
    .copy-button {{ padding: 0 18px; border: 0; border-radius: 10px; background: var(--accent); color: white; font-weight: 750; }}
    .copy-button:hover {{ background: #1e40af; }}
    .toolbar {{ position: sticky; top: 10px; z-index: 10; display: flex; gap: 10px; align-items: center; padding: 12px; margin-bottom: 14px; background: rgba(255, 255, 255, .94); backdrop-filter: blur(8px); }}
    .filter {{ padding: 0 15px; border: 1px solid var(--border); border-radius: 999px; color: var(--text); background: white; }}
    .filter[aria-pressed="true"] {{ color: white; background: var(--text); border-color: var(--text); }}
    .search {{ flex: 1; min-width: 120px; min-height: 44px; padding: 0 14px; border: 1px solid var(--border); border-radius: 10px; color: var(--text); background: white; }}
    .item {{ display: grid; grid-template-columns: 7px 1fr; margin-bottom: 12px; overflow: hidden; background: var(--surface); border: 1px solid var(--border); border-radius: 14px; }}
    .item[hidden] {{ display: none; }}
    .item-marker {{ width: 100%; }}
    .marker-action {{ background: var(--accent); }}
    .marker-blocked {{ background: var(--blocked); }}
    .item-body {{ padding: 18px 20px; }}
    .item-heading {{ display: flex; justify-content: space-between; gap: 16px; align-items: start; }}
    .facts {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 10px; margin: 16px 0 0; }}
    .facts div {{ min-width: 0; }}
    .facts dt {{ color: var(--muted); font-size: .72rem; }}
    .facts dd {{ margin: 3px 0 0; overflow-wrap: anywhere; font-size: .87rem; }}
    .badges {{ display: flex; flex-wrap: wrap; gap: 7px; margin-top: 14px; }}
    .badge {{ display: inline-flex; align-items: center; min-height: 28px; padding: 4px 9px; border-radius: 999px; font-size: .72rem; font-weight: 750; }}
    .badge-ready {{ color: var(--ready); background: var(--ready-soft); }}
    .badge-warning {{ color: var(--warning); background: var(--warning-soft); }}
    .badge-blocked {{ color: var(--blocked); background: var(--blocked-soft); }}
    .badge-neutral {{ color: var(--muted); background: #f1f5f9; }}
    .mono-line {{ margin-top: 12px; color: var(--muted); font-size: .8rem; }}
    .empty {{ padding: 48px 20px; text-align: center; color: var(--muted); background: var(--surface); border: 1px dashed var(--border); border-radius: var(--radius); }}
    .footer {{ margin-top: 28px; color: var(--muted); font-size: .78rem; }}
    .footer code {{ overflow-wrap: anywhere; word-break: break-all; }}
    @media (max-width: 800px) {{
      .topbar, .item-heading {{ display: block; }}
      .hash-box {{ max-width: none; margin-top: 18px; text-align: left; }}
      .decision {{ grid-template-columns: 1fr; }}
      .metrics {{ grid-template-columns: repeat(2, 1fr); }}
      .step-grid, .facts {{ grid-template-columns: 1fr 1fr; }}
      .toolbar {{ flex-wrap: wrap; }}
      .search {{ order: -1; flex-basis: 100%; }}
    }}
    @media (max-width: 480px) {{
      .shell {{ width: min(100% - 20px, 1180px); padding-top: 20px; }}
      .step-grid, .facts, .approval-row {{ grid-template-columns: 1fr; }}
      .copy-button {{ width: 100%; }}
    }}
    @media (prefers-reduced-motion: reduce) {{
      html {{ scroll-behavior: auto; }}
      * {{ transition: none !important; }}
    }}
  </style>
</head>
<body>
  <a class="skip-link" href="#items">Skip to plan items</a>
  <main class="shell">
    <header class="topbar">
      <div>
        <p class="product">YouTube to Podcast</p>
        <h1>Plan review</h1>
        <p class="muted">Generated {_escape(plan.get("generated_at"))} · {_escape(plan.get("mode"))} / {_escape(plan.get("publication"))}</p>
      </div>
      <div class="hash-box">
        <span class="eyebrow">Approval hash</span>
        <code class="hash">{_escape(plan.get("approval_hash"))}</code>
      </div>
    </header>

    <section class="decision">
      <div class="panel decision-main" data-tone="{decision_tone}">
        <p class="eyebrow">Current recommendation</p>
        <h2>{_escape(decision_title)}</h2>
        <p class="muted">{_escape(decision_copy)}</p>
      </div>
      <aside class="panel reason-panel" aria-labelledby="reason-heading">
        <h2 id="reason-heading">Why items are blocked</h2>
        <ul class="reason-list">{reason_rows}</ul>
      </aside>
    </section>

    <section class="metrics" aria-label="Plan summary">
      <div class="panel metric"><strong>{len(actions)}</strong><span>Executable actions</span></div>
      <div class="panel metric"><strong>{len(blocked)}</strong><span>Quarantined items</span></div>
      <div class="panel metric"><strong>{warning_count}</strong><span>Non-blocking warnings</span></div>
      <div class="panel metric"><strong>{int(summary.get("deferred_candidate_count") or 0)}</strong><span>Deferred by batch limit</span></div>
    </section>

    <section class="panel steps" aria-labelledby="steps-heading">
      <h2 id="steps-heading">Make the decision in three passes</h2>
      <div class="step-grid">
        <div class="step"><span class="step-number">1</span><strong>Review actions</strong><p>Confirm title, date, transcript, warnings, and oldest-to-newest order.</p></div>
        <div class="step"><span class="step-number">2</span><strong>Understand exceptions</strong><p>Blocked items will not be written. Historical gaps require explicit backfill mode.</p></div>
        <div class="step"><span class="step-number">3</span><strong>Approve exact scope</strong><p>Copy the command only if this immutable plan matches your decision.</p></div>
      </div>
    </section>

    <section class="panel approval" aria-labelledby="approval-heading">
      <h2 id="approval-heading">Exact execution command</h2>
      <p class="muted">Changing the plan, files, config, or remote state invalidates this approval.</p>
      <div class="approval-row">
        <code class="command" id="apply-command">{_escape(apply_command)}</code>
        <button class="copy-button" type="button" data-copy="apply-command">Copy command</button>
      </div>
      <p class="muted" id="copy-status" role="status" aria-live="polite"></p>
    </section>

    <nav class="panel toolbar" aria-label="Plan filters">
      <button class="filter" type="button" data-filter="all" aria-pressed="true">All ({len(actions) + len(blocked)})</button>
      <button class="filter" type="button" data-filter="action" aria-pressed="false">Actions ({len(actions)})</button>
      <button class="filter" type="button" data-filter="blocked" aria-pressed="false">Quarantined ({len(blocked)})</button>
      <input class="search" id="search" type="search" aria-label="Search plan items" placeholder="Search title, YouTube ID, or reason">
    </nav>

    <section id="items" aria-label="Plan items">
      {items_html}
    </section>

    <footer class="footer">
      <p>Plan hash <code>{_escape(plan.get("plan_hash"))}</code></p>
      <p>Show ID <code>{_escape(plan.get("show_id"))}</code> · This file contains no API key and performs no remote write.</p>
    </footer>
  </main>
  <script>
    (() => {{
      const buttons = [...document.querySelectorAll("[data-filter]")];
      const items = [...document.querySelectorAll(".item")];
      const search = document.querySelector("#search");
      let active = "all";
      const refresh = () => {{
        const query = (search.value || "").trim().toLowerCase();
        for (const item of items) {{
          const kindMatch = active === "all" || item.dataset.kind === active;
          const searchMatch = !query || item.dataset.search.includes(query);
          item.hidden = !(kindMatch && searchMatch);
        }}
      }};
      for (const button of buttons) {{
        button.addEventListener("click", () => {{
          active = button.dataset.filter;
          for (const other of buttons) other.setAttribute("aria-pressed", String(other === button));
          refresh();
        }});
      }}
      search.addEventListener("input", refresh);
      document.querySelector("[data-copy]").addEventListener("click", async (event) => {{
        const value = document.querySelector("#" + event.currentTarget.dataset.copy).textContent;
        const status = document.querySelector("#copy-status");
        try {{
          await navigator.clipboard.writeText(value);
          status.textContent = "Command copied.";
        }} catch {{
          status.textContent = "Copy was blocked by the browser. Select the command manually.";
        }}
      }});
    }})();
  </script>
</body>
</html>
"""
    output_path = output_path.expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(document, encoding="utf-8")
    return output_path

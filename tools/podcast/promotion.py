"""Deterministic rendering of the owner-approved evergreen promotion template.

The plain episode source, approved config bytes, and fallback title are locked
in each plan. This format does not change the meaning of portable_html_v1.
"""
from __future__ import annotations

import html
import hashlib
import json
import re
from pathlib import Path
from typing import Any

from tools.podcast.core import PROJECT_ROOT, sha256_file
from tools.podcast.show_notes import (
    placeholder_tokens, render_portable_show_notes_html, validate_show_notes,
)

PROMOTION_HTML_FORMAT = "promotion_html_v1"
CONFIG_FILES = {
    "promotion": "podcast_show_notes/PROMOTION.json",
    "legacy": "tools/podcast/config/legacy-promotion.json",
}
# Changing the unattended public payload requires a new reviewed config pin.
APPROVED_CONFIG_HASHES = {
    "promotion": "fb89b8571cd6d8b0241c3c45653cf94659e456e55154b091bb8a07651abe172a",
    "legacy": "052363b3882a6db616c129d050be0940bbf1016c8586f4470abd66c284e58b5e",
}
HOST = "孙煜征（课代表立正），康奈尔大学经济学博士、Superlinear Academy创始人。曾任Amazon经济学家、Meta数据科学家和腾讯IEG副总监，也是OpenAI收购团队早期成员。"
PROGRAM = "《课代表立正》关注AI如何改变工作、职业与商业，以及人在变化中最需要保留的判断力。"
URLS = {"https://www.superlinear.academy", "https://www.superlinear.academy/"}
EPISODE_HEADING = "<p><strong>本期内容</strong></p>"


def promotion_render_inputs(title: str, project_root: Path = PROJECT_ROOT) -> dict[str, Any]:
    """Capture only public renderer inputs for an immutable publication plan."""
    inputs: dict[str, Any] = {"title": title, "config_sha256": {}}
    for name, relative in CONFIG_FILES.items():
        digest = sha256_file(project_root / relative)
        if digest != APPROVED_CONFIG_HASHES[name]:
            raise ValueError(f"Approved {name} config changed: {relative}")
        inputs["config_sha256"][name] = digest
    return inputs


def _load_config(inputs: dict[str, Any], project_root: Path) -> tuple[dict, dict]:
    if not isinstance(inputs, dict) or set(inputs) != {"title", "config_sha256"}:
        raise ValueError("Promotion descriptions require immutable renderer inputs")
    if not isinstance(inputs["title"], str) or not inputs["title"].strip():
        raise ValueError("Promotion descriptions require a fallback title")
    if inputs["config_sha256"] != APPROVED_CONFIG_HASHES:
        raise ValueError("Promotion config hashes differ from the immutable plan")
    configs = []
    for name in ("promotion", "legacy"):
        raw = (project_root / CONFIG_FILES[name]).read_bytes()
        if hashlib.sha256(raw).hexdigest() != inputs["config_sha256"][name]:
            raise ValueError(f"Approved {name} config changed: {CONFIG_FILES[name]}")
        configs.append(json.loads(raw))
    return configs[0], configs[1]


def text_of(fragment: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", fragment)).strip()


def blocks_of(description: str) -> tuple[list[str], bool]:
    if not re.search(r"<(?:p|div)\b", description, re.I):
        if re.search(r"<\w+\b", description):
            raise ValueError("Unrecognized HTML description structure")
        return [b.strip() for b in re.split(r"\n\s*\n", description) if b.strip()], False
    pattern = re.compile(r"<(p|div)\b[^>]*>.*?</\1>", re.S | re.I)
    matches = list(pattern.finditer(description))
    if pattern.sub("", description).strip():
        raise ValueError("Content outside recognized HTML blocks")
    return [m.group() for m in matches], True


def _link_paragraph(label: str, url: str) -> str:
    return (f"<p>{html.escape(label)}</p>\n"
            f'<p><a href="{html.escape(url, quote=True)}">{html.escape(url)}</a></p>')


def promotion_parts(p: dict, community_cta: str = "") -> tuple[str, str]:
    intro = f"<p>{html.escape(p['intro'])}</p>\n" + _link_paragraph(p["personal_label"], p["personal_url"])
    footer = []
    for name in ("ask", "community", "course"):
        footer.append(f"<p><strong>{html.escape(p[name + '_heading'])}</strong></p>")
        footer.append(f"<p>{html.escape(p[name])}</p>")
        if name == "community" and community_cta:
            footer.append(f"<p>{html.escape(community_cta)}</p>")
        url = p[name + "_url"]
        footer.append(f'<p><a href="{html.escape(url, quote=True)}">{html.escape(url)}</a></p>')
    return intro, "\n\n".join(footer)


def validate_promotion_source(source: str) -> dict[str, Any]:
    """An empty new-episode source uses its locked title and remains visible."""
    quality = validate_show_notes(source)
    if not source.strip():
        quality["errors"].remove("empty")
        quality["warnings"].append("empty_source")
    return quality


def _unwrap_template(blocks: list[str], is_html: bool, promotion: dict) -> tuple[list[str], str] | None:
    """Recognize only the complete approved wrapper, anchored at both ends."""
    if not is_html and blocks and blocks[0] == promotion["intro"]:
        # The owned batch writer keeps label/URL and footer text/URL pairs in
        # one plain-text block. Split only those exact wrapper pairs so its
        # episode paragraphs keep their original grouping.
        pairs = {promotion["personal_label"] + "\n" + promotion["personal_url"]:
                 [promotion["personal_label"], promotion["personal_url"]]}
        for name in ("ask", "community", "course"):
            pairs[promotion[name] + "\n" + promotion[name + "_url"]] = [
                promotion[name], promotion[name + "_url"],
            ]
        normalized = []
        for index, block in enumerate(blocks):
            if block in pairs:
                normalized.extend(pairs[block])
            elif (index == len(blocks) - 3
                  and blocks[index + 1] == promotion["course_heading"]
                  and block.endswith("\n" + promotion["community_url"])):
                normalized.extend([block[:-(len(promotion["community_url"]) + 1)],
                                   promotion["community_url"]])
            else:
                normalized.append(block)
        blocks = normalized
    intro, footer = promotion_parts(promotion)
    prefix = blocks_of(intro)[0] + [EPISODE_HEADING]
    suffix = blocks_of(footer)[0]
    key = (lambda block: block) if is_html else text_of
    if not blocks or key(blocks[0]) != key(prefix[0]):
        return None
    for cta_count in (0, 1):
        if len(blocks) < len(prefix) + len(suffix) + cta_count + 1:
            continue
        tail = blocks[-(len(suffix) + cta_count):]
        cta = text_of(tail[5]) if cta_count else ""
        expected_suffix = blocks_of(promotion_parts(promotion, cta)[1])[0]
        if ([key(b) for b in blocks[:4]] == [key(b) for b in prefix]
                and [key(b) for b in tail] == [key(b) for b in expected_suffix]):
            return blocks[4:-(len(suffix) + cta_count)], cta
    raise ValueError("Unrecognized or incomplete approved promotion wrapper")


def refresh(description: str, title: str, promotion: dict, legacy: dict) -> dict:
    """Keep episode content and remove only finite, approved legacy fragments."""
    quality = validate_promotion_source(description)
    if quality["errors"]:
        raise ValueError("Invalid Show Notes: " + ", ".join(quality["errors"]))
    blocks, is_html = blocks_of(description)
    wrapped = _unwrap_template(blocks, is_html, promotion)
    if is_html and wrapped is None:
        # Plain-text inputs remain fail-closed. Only a complete, byte-approved
        # wrapper may arrive as HTML; arbitrary HTML was never a valid source.
        raise ValueError("Promotion renderer requires plain-text Show Notes or the approved wrapper")
    if wrapped:
        blocks, community_cta = wrapped
    else:
        community_cta = ""
    exact = {b["text"] for b in legacy["blocks"]}
    lines = set(legacy["lines"])
    kept, removed = [], []
    i = 0
    while i < len(blocks):
        block = blocks[i]
        text = text_of(block) if is_html else block
        course_lines = text.splitlines() if not is_html else []
        if (len(course_lines) == 2
                and course_lines[0] in {"AI Builders课程：", "AI Builders 课程："}
                and course_lines[1] == promotion["course_url"]):
            removed.extend(course_lines)
            i += 1
            continue
        if text == "加入Superlinear Academy免费社区":
            # Sidecars use either separate CTA/URL blocks or a two-line block.
            if i + 1 >= len(blocks):
                raise ValueError("Unrecognized legacy community section")
            next_lines = blocks[i + 1].splitlines() if not is_html else []
            if len(next_lines) == 2 and next_lines[-1].strip() in URLS:
                cta, consumed = next_lines[0].strip(), 2
            elif i + 2 < len(blocks) and text_of(blocks[i + 2]) in URLS:
                cta, consumed = text_of(blocks[i + 1]), 3
            else:
                raise ValueError("Unrecognized legacy community section")
            if cta not in legacy.get("generic_community_ctas", []):
                if community_cta and community_cta != cta:
                    raise ValueError("Multiple episode community questions")
                community_cta = cta
            removed.extend(text_of(b) for b in blocks[i:i + consumed])
            i += consumed
            continue
        if text in {"关于主播与节目", HOST, PROGRAM} or text in exact:
            removed.append(text)
            if text in {"AI Builders课程：", "AI Builders 课程："}:
                if i + 1 >= len(blocks) or text_of(blocks[i + 1]) != promotion["course_url"]:
                    raise ValueError("Unrecognized legacy course section")
                removed.append(text_of(blocks[i + 1]))
                i += 1
        elif not is_html and any(line.strip() in lines for line in block.splitlines()):
            remaining = []
            for line in block.splitlines():
                if line.strip() in lines:
                    removed.append(line.strip())
                else:
                    remaining.append(line)
            if "\n".join(remaining).strip():
                kept.append("\n".join(remaining).strip())
        else:
            kept.append(block)
        i += 1
    plain_body = "\n\n".join(text_of(b) if is_html else b for b in kept)
    if is_html:
        # An exact wrapper cannot make arbitrary HTML a valid source. Its body
        # must still consist of paragraphs producible by the portable renderer.
        canonical_blocks = blocks_of(render_portable_show_notes_html(plain_body))[0]
        if canonical_blocks != kept:
            raise ValueError("Approved wrapper contains nonportable episode HTML")
    body = "\n\n".join(kept) if is_html else render_portable_show_notes_html(plain_body)
    if not plain_body.strip():
        body = f"<p>{html.escape(title)}</p>"
    intro, footer = promotion_parts(promotion, community_cta)
    after = intro + "\n\n" + EPISODE_HEADING + "\n\n" + body + "\n\n" + footer
    before_tags, before_errors = placeholder_tokens(description)
    after_tags, after_errors = placeholder_tokens(after)
    if after_errors or before_errors or before_tags != after_tags:
        raise ValueError("Dynamic placeholder validation/preservation failed")
    if len(after) > 10000:
        raise ValueError("Description exceeds Transistor's 10,000 character limit")
    # A composed HTML source must remain exactly as reviewed, including spacing.
    if is_html and wrapped:
        prefix = intro + "\n\n" + EPISODE_HEADING + "\n\n"
        expected = prefix + "\n\n".join(blocks) + "\n\n" + footer
        if after != expected:
            raise ValueError("Approved wrapper contains unrefreshed legacy promotion")
        after = description
    return {"description": after, "body": plain_body, "removed": removed,
            "community_cta": community_cta}


def render_promoted_show_notes_html(
    source: str, inputs: dict[str, Any] | None, project_root: Path = PROJECT_ROOT,
) -> str:
    promotion, legacy = _load_config(inputs, project_root)
    return refresh(source, inputs["title"], promotion, legacy)["description"]

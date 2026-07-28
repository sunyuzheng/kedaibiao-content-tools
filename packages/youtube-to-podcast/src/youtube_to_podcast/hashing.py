"""Canonical hashing for immutable, reviewable execution plans."""

from __future__ import annotations

import hashlib
import json
from typing import Any


class PlanError(RuntimeError):
    """Raised when a plan or approval precondition is unsafe."""


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def sha256_file(path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def approval_scope(plan: dict[str, Any]) -> dict[str, Any]:
    return {
        "kind": "youtube_to_podcast_apply",
        "show_id": plan.get("show_id"),
        "channel_url": plan.get("channel_url"),
        "mode": plan.get("mode"),
        "publication": plan.get("publication"),
        "actions": plan.get("actions", []),
    }


def approval_hash(plan: dict[str, Any]) -> str:
    return sha256_text(canonical_json(approval_scope(plan)))


def plan_hash(plan: dict[str, Any]) -> str:
    payload = {key: value for key, value in plan.items() if key != "plan_hash"}
    return sha256_text(canonical_json(payload))


def seal_plan(plan: dict[str, Any]) -> dict[str, Any]:
    sealed = dict(plan)
    sealed["approval_hash"] = approval_hash(sealed)
    sealed["plan_hash"] = plan_hash(sealed)
    return sealed


def verify_plan(plan: dict[str, Any], supplied_approval_hash: str | None = None) -> None:
    if plan.get("kind") != "youtube_to_podcast_plan":
        raise PlanError(f"Unexpected plan kind: {plan.get('kind')!r}")
    expected_plan_hash = plan.get("plan_hash")
    actual_plan_hash = plan_hash(plan)
    if not expected_plan_hash or expected_plan_hash != actual_plan_hash:
        raise PlanError(
            "Plan hash mismatch: "
            f"embedded={expected_plan_hash!r} actual={actual_plan_hash!r}"
        )
    expected_approval = plan.get("approval_hash")
    actual_approval = approval_hash(plan)
    if not expected_approval or expected_approval != actual_approval:
        raise PlanError(
            "Approval scope hash mismatch: "
            f"embedded={expected_approval!r} actual={actual_approval!r}"
        )
    if supplied_approval_hash is not None and supplied_approval_hash != actual_approval:
        raise PlanError(
            "Approval hash does not match this exact action scope: "
            f"expected={actual_approval} supplied={supplied_approval_hash}"
        )

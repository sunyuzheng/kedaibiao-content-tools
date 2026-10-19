"""Preserve published global numbers and append only new incremental episodes."""

from __future__ import annotations

from typing import Any


def project_global_numbers(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return a complete projection, or reject ambiguous historical numbering.

    A withdrawn episode can leave a legitimate gap. Existing positive, unique
    numbers must remain strictly increasing in the canonical date/video order;
    new episodes can only follow that history and start above its highest number.
    """
    ordered = sorted(rows, key=lambda row: (row["date"], row["video_id"]))
    history = [row for row in ordered if not row.get("planned_publish")]
    numbers = [row.get("current_number") for row in history]
    if any(type(number) is not int or number < 1 for number in numbers):
        raise ValueError("published_episode_number_invalid")
    if len(numbers) != len(set(numbers)):
        raise ValueError("duplicate_published_episode_numbers")
    if numbers != sorted(numbers):
        raise ValueError("published_episode_numbers_out_of_order")

    if history:
        latest_key = (history[-1]["date"], history[-1]["video_id"])
        if any(
            row.get("planned_publish")
            and (row["date"], row["video_id"]) <= latest_key
            for row in ordered
        ):
            raise ValueError("new_episode_not_after_published_history")

    next_number = max(numbers, default=0) + 1
    projected = []
    for row in ordered:
        if row.get("planned_publish"):
            target_number = next_number
            next_number += 1
        else:
            target_number = row["current_number"]
        projected.append({**row, "target_number": target_number})
    return projected

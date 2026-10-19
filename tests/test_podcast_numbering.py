"""Global-number gap regressions; no network or service credentials."""

from __future__ import annotations

import unittest
from datetime import date, timedelta

from tools.podcast.numbering import project_global_numbers


class GlobalNumberingTests(unittest.TestCase):
    def test_559_published_episodes_keep_gap_557_and_new_batch_starts_at_561(self) -> None:
        history = [{
            "video_id": f"{number:011d}",
            "date": (date(2020, 1, 1) + timedelta(days=number)).strftime("%Y%m%d"),
            "planned_publish": False,
            "current_number": number,
        } for number in range(1, 561) if number != 557]
        new = [{"video_id": "new-episode", "date": "20261019", "planned_publish": True},
               {"video_id": "nextepisode", "date": "20261020", "planned_publish": True}]
        projected = project_global_numbers([*reversed(history), *reversed(new)])
        self.assertEqual(len(history), 559)
        self.assertEqual([r["target_number"] for r in projected[:-2]],
                         [r["current_number"] for r in history])
        self.assertEqual([r["target_number"] for r in projected[-2:]], [561, 562])
        self.assertNotIn(557, [r["target_number"] for r in projected])
        self.assertTrue(all("target_number" not in r for r in history))

    def test_empty_history_starts_at_one(self) -> None:
        rows = [{"video_id": "new-episode", "date": "20261019", "planned_publish": True}]
        self.assertEqual(project_global_numbers(rows)[0]["target_number"], 1)


if __name__ == "__main__":
    unittest.main()

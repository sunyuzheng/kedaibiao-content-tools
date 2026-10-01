#!/usr/bin/env python3
"""Record a reviewed series assignment locally before planning a new episode."""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from tools.podcast.core import atomic_write_json
from tools.podcast.series import CATALOG_PATH, load_catalog, next_assignment, validate_catalog


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--video-id", required=True)
    parser.add_argument("--series", choices=("dialogue", "solo"), required=True)
    parser.add_argument("--date", required=True, help="Original YouTube date, YYYYMMDD")
    parser.add_argument("--basis", required=True, help="Evidence checked in the description/transcript")
    parser.add_argument("--catalog", type=Path, default=CATALOG_PATH, help="Private operational catalog after cloud cutover")
    args = parser.parse_args()
    data = json.loads(args.catalog.read_text(encoding="utf-8"))
    entries = load_catalog(args.catalog)
    if args.video_id in entries:
        raise ValueError("Assignment already exists; changing history requires a separate reviewed migration")
    assignment = next_assignment(entries, args.series, args.date, args.basis)
    entries[args.video_id] = assignment
    validate_catalog(entries)
    data["episodes"] = entries
    atomic_write_json(args.catalog, data)
    print(json.dumps({"video_id": args.video_id, **assignment}, ensure_ascii=False))


if __name__ == "__main__":
    main()

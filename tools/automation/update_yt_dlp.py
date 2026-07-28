#!/usr/bin/env python3
"""Best-effort upgrade of the project yt-dlp to the newest PyPI pre-release.

The pinned requirement remains the known-good bootstrap baseline. A scheduled
run checks for a newer stable/nightly build before contacting YouTube. Network
or index failures are warnings when the already-installed binary still works.
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[2]
YTDLP_BIN = PROJECT_ROOT / ".venv-podcast" / "bin" / "yt-dlp"
UPDATE_TIMEOUT_SECONDS = 180


def probe_version() -> str | None:
    """Return the runnable project yt-dlp version, not package metadata alone."""
    if not YTDLP_BIN.is_file():
        return None
    try:
        result = subprocess.run(
            [str(YTDLP_BIN), "--version"],
            cwd=PROJECT_ROOT,
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode:
        return None
    version = result.stdout.strip()
    return version or None


def emit(status: str, **data: Any) -> None:
    print(
        json.dumps(
            {
                "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "event": "yt_dlp_update",
                "status": status,
                **data,
            },
            ensure_ascii=False,
            sort_keys=True,
        ),
        flush=True,
    )


def main() -> int:
    before = probe_version()
    command = [
        sys.executable,
        "-m",
        "pip",
        "install",
        "--disable-pip-version-check",
        "--no-input",
        "--upgrade",
        "--pre",
        "--no-deps",
        "--retries",
        "2",
        "--timeout",
        "30",
        "yt-dlp",
    ]
    try:
        result = subprocess.run(
            command,
            cwd=PROJECT_ROOT,
            text=True,
            capture_output=True,
            timeout=UPDATE_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired:
        after = probe_version()
        if after:
            emit(
                "update_failed_using_current",
                version_before=before,
                version_after=after,
                reason=f"pip_timeout_after_{UPDATE_TIMEOUT_SECONDS}s",
            )
            return 0
        emit(
            "unavailable",
            version_before=before,
            version_after=None,
            reason=f"pip_timeout_after_{UPDATE_TIMEOUT_SECONDS}s",
        )
        return 1
    except OSError as exc:
        after = probe_version()
        if after:
            emit(
                "update_failed_using_current",
                version_before=before,
                version_after=after,
                reason=f"{type(exc).__name__}: {exc}",
            )
            return 0
        emit(
            "unavailable",
            version_before=before,
            version_after=None,
            reason=f"{type(exc).__name__}: {exc}",
        )
        return 1

    after = probe_version()
    if result.returncode == 0 and after:
        emit(
            "updated" if before != after else "already_latest",
            version_before=before,
            version_after=after,
        )
        return 0

    reason_lines = (result.stderr or result.stdout).strip().splitlines()
    reason = reason_lines[-1][-500:] if reason_lines else f"pip_exit_{result.returncode}"
    if after:
        emit(
            "update_failed_using_current",
            version_before=before,
            version_after=after,
            reason=reason,
        )
        return 0
    emit(
        "unavailable",
        version_before=before,
        version_after=None,
        reason=reason,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

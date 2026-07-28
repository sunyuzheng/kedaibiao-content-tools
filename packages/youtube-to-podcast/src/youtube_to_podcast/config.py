"""Configuration parsing and starter-project generation."""

from __future__ import annotations

import json
import os
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse


class ConfigError(RuntimeError):
    """Raised when local configuration is incomplete or unsafe."""


@dataclass(frozen=True)
class Policy:
    mode: str = "incremental"
    publication: str = "draft"
    max_actions: int = 3
    max_candidates: int = 12
    include_live: bool = False
    download_subtitles: bool = True
    subtitle_languages: tuple[str, ...] = ("en.*", "zh.*")


@dataclass(frozen=True)
class Config:
    path: Path
    channel_url: str
    show_id: str
    work_dir: Path
    policy: Policy

    @property
    def root(self) -> Path:
        return self.path.parent


STARTER_CONFIG = """\
# YouTube to Podcast never stores secrets in this file.
# Put TRANSISTOR_API_KEY in your shell or a gitignored .env file.
schema_version = 1
work_dir = ".youtube-to-podcast"

[youtube]
channel_url = {channel_url_json}

[transistor]
show_id = "{show_id}"

[policy]
# incremental: only videos newer than the newest linked Transistor episode.
# backfill: historical gaps may become drafts, but are never auto-published.
mode = "incremental"

# draft is the safe default. Change to publish only after reviewing a plan.
publication = "draft"
max_actions = 3
# Bounds per-video YouTube checks while allowing blocked items not to starve
# newer valid actions. Must be at least max_actions.
max_candidates = 12
include_live = false
download_subtitles = true
subtitle_languages = ["en.*", "zh.*"]
"""


def _validate_channel_url(value: str) -> str:
    parsed = urlparse(value)
    hostname = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or hostname not in {
        "youtube.com",
        "www.youtube.com",
        "m.youtube.com",
    }:
        raise ConfigError("youtube.channel_url must be an https://youtube.com URL")
    normalized_path = parsed.path.rstrip("/")
    if not normalized_path.endswith("/videos"):
        raise ConfigError(
            "youtube.channel_url must point to a channel's /videos tab; "
            "arbitrary playlists are not chronological enough for safe incrementals"
        )
    if parsed.query or parsed.fragment:
        raise ConfigError("youtube.channel_url must not contain a query or fragment")
    return value.rstrip("/")


def _required_table(data: dict, key: str) -> dict:
    value = data.get(key)
    if not isinstance(value, dict):
        raise ConfigError(f"Missing [{key}] table")
    return value


def load_config(path: Path) -> Config:
    path = path.expanduser().resolve()
    if not path.exists():
        raise ConfigError(f"Config file does not exist: {path}")
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    if data.get("schema_version") != 1:
        raise ConfigError("Only schema_version = 1 is supported")

    youtube = _required_table(data, "youtube")
    transistor = _required_table(data, "transistor")
    policy_data = data.get("policy") or {}
    if not isinstance(policy_data, dict):
        raise ConfigError("[policy] must be a table")

    channel_url = _validate_channel_url(str(youtube.get("channel_url") or "").strip())
    show_id = str(transistor.get("show_id") or "").strip()
    if not re.fullmatch(r"\d+", show_id):
        raise ConfigError("transistor.show_id must be a numeric Transistor show ID")

    mode = str(policy_data.get("mode") or "incremental").strip()
    if mode not in {"incremental", "backfill"}:
        raise ConfigError("policy.mode must be incremental or backfill")
    publication = str(policy_data.get("publication") or "draft").strip()
    if publication not in {"draft", "publish"}:
        raise ConfigError("policy.publication must be draft or publish")
    if mode == "backfill" and publication == "publish":
        raise ConfigError(
            "backfill + publish is intentionally unsupported; backfill to drafts first"
        )

    max_actions = int(policy_data.get("max_actions", 3))
    if not 1 <= max_actions <= 100:
        raise ConfigError("policy.max_actions must be between 1 and 100")
    max_candidates = int(policy_data.get("max_candidates", 12))
    if not max_actions <= max_candidates <= 500:
        raise ConfigError(
            "policy.max_candidates must be between max_actions and 500"
        )

    languages = policy_data.get("subtitle_languages", ["en.*", "zh.*"])
    if not isinstance(languages, list) or not languages or not all(
        isinstance(item, str) and item.strip() for item in languages
    ):
        raise ConfigError("policy.subtitle_languages must be a non-empty string array")

    work_dir_value = str(data.get("work_dir") or ".youtube-to-podcast")
    work_dir = (path.parent / work_dir_value).resolve()
    try:
        work_dir.relative_to(path.parent)
    except ValueError as exc:
        raise ConfigError("work_dir must stay inside the config directory") from exc

    return Config(
        path=path,
        channel_url=channel_url,
        show_id=show_id,
        work_dir=work_dir,
        policy=Policy(
            mode=mode,
            publication=publication,
            max_actions=max_actions,
            max_candidates=max_candidates,
            include_live=bool(policy_data.get("include_live", False)),
            download_subtitles=bool(
                policy_data.get("download_subtitles", True)
            ),
            subtitle_languages=tuple(item.strip() for item in languages),
        ),
    )


def write_starter_config(
    path: Path,
    *,
    channel_url: str,
    show_id: str,
    force: bool = False,
) -> Path:
    path = path.expanduser().resolve()
    channel_url = _validate_channel_url(channel_url.strip())
    if not re.fullmatch(r"\d+", show_id.strip()):
        raise ConfigError("--show-id must be numeric")
    if path.exists() and not force:
        raise ConfigError(f"Refusing to overwrite existing config: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        STARTER_CONFIG.format(
            channel_url_json=json.dumps(channel_url, ensure_ascii=False),
            show_id=show_id.strip(),
        ),
        encoding="utf-8",
    )
    return path


def load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key.strip() != "TRANSISTOR_API_KEY":
            continue
        clean_value = value.strip()
        if (
            len(clean_value) >= 2
            and clean_value[0] == clean_value[-1]
            and clean_value[0] in {"'", '"'}
        ):
            clean_value = clean_value[1:-1]
        os.environ.setdefault("TRANSISTOR_API_KEY", clean_value)


def transistor_api_key(config: Config) -> str:
    load_env_file(config.root / ".env")
    value = os.environ.get("TRANSISTOR_API_KEY", "").strip()
    if not value:
        raise ConfigError(
            "TRANSISTOR_API_KEY is missing; export it or put it in a gitignored .env"
        )
    return value

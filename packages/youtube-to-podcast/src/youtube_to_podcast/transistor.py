"""Minimal Transistor API client with bounded retries and readback support."""

from __future__ import annotations

import random
import re
import time
from collections import defaultdict
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

import requests


VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")


class TransistorError(RuntimeError):
    """Raised when the Transistor API fails or returns an unsafe shape."""


def extract_video_id(value: str | None) -> str | None:
    parsed = urlparse(value or "")
    host = (parsed.hostname or "").lower()
    candidate: str | None = None
    if host in {"youtu.be", "www.youtu.be"}:
        candidate = parsed.path.strip("/").split("/", 1)[0]
    elif host in {"youtube.com", "www.youtube.com", "m.youtube.com"}:
        if parsed.path.rstrip("/") == "/watch":
            candidate = (parse_qs(parsed.query).get("v") or [None])[0]
        else:
            parts = parsed.path.strip("/").split("/")
            if len(parts) >= 2 and parts[0] in {"shorts", "live", "embed"}:
                candidate = parts[1]
    if candidate and VIDEO_ID_RE.fullmatch(candidate):
        return candidate
    return None


class TransistorClient:
    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = "https://api.transistor.fm/v1",
        session: requests.Session | None = None,
        min_interval: float = 1.1,
        max_attempts: int = 5,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.session = session or requests.Session()
        self.session.headers.update({"x-api-key": api_key})
        self.min_interval = min_interval
        self.max_attempts = max_attempts
        self.sleep = sleep
        self.monotonic = monotonic
        self._last_request_at: float | None = None

    def _throttle(self) -> None:
        if self._last_request_at is None:
            return
        remaining = self.min_interval - (self.monotonic() - self._last_request_at)
        if remaining > 0:
            self.sleep(remaining)

    def request(
        self,
        method: str,
        path: str,
        *,
        expected: tuple[int, ...] = (200,),
        **kwargs: Any,
    ) -> requests.Response:
        url = path if path.startswith("http") else f"{self.base_url}/{path.lstrip('/')}"
        kwargs.setdefault("timeout", (10, 60))
        last_error: Exception | None = None
        for attempt in range(1, self.max_attempts + 1):
            self._throttle()
            try:
                response = self.session.request(method, url, **kwargs)
                self._last_request_at = self.monotonic()
            except requests.RequestException as exc:
                last_error = exc
                if attempt == self.max_attempts:
                    break
                self.sleep(min(30.0, 2 ** (attempt - 1)))
                continue
            if response.status_code in expected:
                return response
            retryable = response.status_code == 429 or response.status_code >= 500
            if retryable and attempt < self.max_attempts:
                retry_after = response.headers.get("Retry-After")
                try:
                    delay = float(retry_after) if retry_after else 2 ** (attempt - 1)
                except ValueError:
                    delay = 2 ** (attempt - 1)
                self.sleep(min(30.0, max(1.0, delay)) + random.uniform(0, 0.25))
                continue
            body = response.text[:500].replace("\n", " ")
            raise TransistorError(
                f"{method} {path} -> HTTP {response.status_code}: {body}"
            )
        raise TransistorError(f"{method} {path} failed: {last_error}")

    def get_show(self, show_id: str) -> dict[str, Any]:
        return self.request("GET", f"/shows/{show_id}").json().get("data", {})

    def list_episodes(self, show_id: str) -> list[dict[str, Any]]:
        episodes: list[dict[str, Any]] = []
        page = 1
        while True:
            payload = self.request(
                "GET",
                "/episodes",
                params={
                    "show_id": show_id,
                    "pagination[page]": page,
                    "pagination[per]": 50,
                },
            ).json()
            episodes.extend(payload.get("data", []))
            total_pages = int((payload.get("meta") or {}).get("totalPages") or 1)
            if page >= total_pages:
                break
            page += 1
        return episodes

    def episodes_by_video_id(
        self, show_id: str
    ) -> tuple[list[dict[str, Any]], dict[str, list[dict[str, Any]]]]:
        episodes = self.list_episodes(show_id)
        mapping: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for episode in episodes:
            video_id = extract_video_id(
                (episode.get("attributes") or {}).get("video_url")
            )
            if video_id:
                mapping[video_id].append(episode)
        return episodes, dict(mapping)

    def authorize_upload(self, filename: str) -> dict[str, str]:
        attrs = (
            self.request(
                "GET",
                "/episodes/authorize_upload",
                params={"filename": filename},
            )
            .json()
            .get("data", {})
            .get("attributes", {})
        )
        required = ("upload_url", "audio_url", "content_type")
        if any(not attrs.get(key) for key in required):
            raise TransistorError("authorize_upload response is incomplete")
        return {key: str(attrs[key]) for key in required}

    def upload_audio(
        self, upload_url: str, content_type: str, path: Path
    ) -> None:
        with path.open("rb") as handle:
            response = requests.put(
                upload_url,
                headers={"Content-Type": content_type},
                data=handle,
                timeout=(20, 900),
            )
        if response.status_code not in (200, 201, 204):
            raise TransistorError(
                f"audio upload -> HTTP {response.status_code}: {response.text[:300]}"
            )

    def create_episode(self, episode: dict[str, Any]) -> dict[str, Any]:
        return (
            self.request(
                "POST",
                "/episodes",
                expected=(200, 201),
                json={"episode": episode},
            )
            .json()
            .get("data", {})
        )

    def get_episode(self, episode_id: str) -> dict[str, Any]:
        return (
            self.request("GET", f"/episodes/{episode_id}")
            .json()
            .get("data", {})
        )

    def update_episode(
        self, episode_id: str, episode: dict[str, Any]
    ) -> dict[str, Any]:
        return (
            self.request(
                "PATCH",
                f"/episodes/{episode_id}",
                expected=(200, 201),
                json={"episode": episode},
            )
            .json()
            .get("data", {})
        )

    def publish_episode(self, episode_id: str, published_at: str) -> dict[str, Any]:
        return (
            self.request(
                "PATCH",
                f"/episodes/{episode_id}/publish",
                expected=(200, 201),
                data={
                    "episode[status]": "published",
                    "episode[published_at]": published_at,
                },
            )
            .json()
            .get("data", {})
        )

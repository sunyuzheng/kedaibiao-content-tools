#!/usr/bin/env python3
"""Small Transistor API client with bounded retries and rate limiting."""

from __future__ import annotations

import random
import time
from collections import defaultdict
from pathlib import Path
from typing import Any, Callable

import requests

from .core import TRANSISTOR_API_BASE, extract_video_id


class TransistorError(RuntimeError):
    pass


class TransistorClient:
    """A single-session client that stays below Transistor's 10 requests/10s limit."""

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = TRANSISTOR_API_BASE,
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
                self.sleep(min(30.0, 2 ** (attempt - 1)) + random.uniform(0, 0.25))
                continue

            if response.status_code in expected:
                return response
            if response.status_code == 429:
                if attempt == self.max_attempts:
                    raise TransistorError(
                        f"{method} {path} remained rate limited after {attempt} attempts"
                    )
                retry_after = response.headers.get("Retry-After")
                try:
                    wait = max(10.0, float(retry_after)) if retry_after else 10.0
                except ValueError:
                    wait = 10.0
                self.sleep(wait + random.uniform(0, 0.5))
                continue
            if response.status_code >= 500 and attempt < self.max_attempts:
                self.sleep(min(30.0, 2 ** (attempt - 1)) + random.uniform(0, 0.25))
                continue
            body = response.text[:500].replace("\n", " ")
            raise TransistorError(f"{method} {path} -> HTTP {response.status_code}: {body}")

        raise TransistorError(f"{method} {path} failed: {last_error}")

    def list_episodes(self, show_id: str) -> list[dict[str, Any]]:
        episodes: list[dict[str, Any]] = []
        page = 1
        while True:
            response = self.request(
                "GET",
                "/episodes",
                params={
                    "show_id": show_id,
                    "pagination[page]": page,
                    "pagination[per]": 50,
                },
            )
            payload = response.json()
            episodes.extend(payload.get("data", []))
            meta = payload.get("meta", {})
            total_pages = int(meta.get("totalPages") or 1)
            if page >= total_pages:
                break
            page += 1
        return episodes

    def episodes_by_video_id(
        self,
        show_id: str,
    ) -> tuple[list[dict[str, Any]], dict[str, list[dict[str, Any]]]]:
        episodes = self.list_episodes(show_id)
        mapping: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for episode in episodes:
            attrs = episode.get("attributes", {})
            video_id = extract_video_id(attrs.get("video_url"))
            if video_id:
                mapping[video_id].append(episode)
        return episodes, dict(mapping)

    def get_episode(self, episode_id: str) -> dict[str, Any]:
        return self.request("GET", f"/episodes/{episode_id}").json().get("data", {})

    def authorize_upload(self, filename: str) -> dict[str, str]:
        data = self.request(
            "GET",
            "/episodes/authorize_upload",
            params={"filename": filename},
        ).json().get("data", {}).get("attributes", {})
        required = ("upload_url", "audio_url", "content_type")
        missing = [key for key in required if not data.get(key)]
        if missing:
            raise TransistorError(f"authorize_upload missing fields: {', '.join(missing)}")
        return {key: str(data[key]) for key in required}

    def upload_audio(
        self,
        upload_url: str,
        content_type: str,
        path: Path,
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
        return self.request(
            "POST",
            "/episodes",
            expected=(200, 201),
            json={"episode": episode},
        ).json().get("data", {})

    def update_episode(self, episode_id: str, episode: dict[str, Any]) -> dict[str, Any]:
        return self.request(
            "PATCH",
            f"/episodes/{episode_id}",
            expected=(200, 201),
            json={"episode": episode},
        ).json().get("data", {})

    def publish_episode(self, episode_id: str, published_at: str) -> dict[str, Any]:
        return self.request(
            "PATCH",
            f"/episodes/{episode_id}/publish",
            expected=(200, 201),
            data={
                "episode[status]": "published",
                "episode[published_at]": published_at,
            },
        ).json().get("data", {})

"""Stream URL resolution and background prefetch caching via yt-dlp."""

from __future__ import annotations

import json
import threading
import time
from typing import Any

import yt_dlp

from ytm.config import STREAM_CACHE_FILE, ensure_dirs

_CACHE_TTL_SECONDS = 1800


class StreamResolver:
    """Resolves YouTube video IDs to direct audio stream URLs with disk caching."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._ydl_opts: dict[str, Any] = {
            "format": "bestaudio/best",
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
        }

    def _load_cache(self) -> dict[str, dict[str, Any]]:
        if not STREAM_CACHE_FILE.is_file():
            return {}
        try:
            data = json.loads(STREAM_CACHE_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        except (OSError, json.JSONDecodeError):
            pass
        return {}

    def _save_cache(self, cache: dict[str, dict[str, Any]]) -> None:
        ensure_dirs()
        now = time.time()
        pruned = {
            vid: entry
            for vid, entry in cache.items()
            if isinstance(entry, dict) and float(entry.get("expires_at", 0)) > now
        }
        try:
            STREAM_CACHE_FILE.write_text(json.dumps(pruned, indent=2), encoding="utf-8")
        except OSError:
            pass

    def get_cached(self, video_id: str) -> str | None:
        """Return a non-expired cached stream URL if present."""
        with self._lock:
            cache = self._load_cache()
            entry = cache.get(video_id)
            if isinstance(entry, dict) and float(entry.get("expires_at", 0)) > time.time():
                url = entry.get("url")
                if isinstance(url, str) and url:
                    return url
        return None

    def resolve(self, video_id: str) -> str | None:
        """Resolve a video_id to a direct audio stream URL, using cache when valid."""
        if not video_id:
            return None
        cached = self.get_cached(video_id)
        if cached:
            return cached

        watch_url = f"https://www.youtube.com/watch?v={video_id}"
        try:
            with yt_dlp.YoutubeDL(self._ydl_opts) as ydl:
                info = ydl.extract_info(watch_url, download=False)
        except Exception:
            return None

        if not isinstance(info, dict):
            return None
        stream_url = info.get("url")
        if not isinstance(stream_url, str) or not stream_url:
            return None

        with self._lock:
            cache = self._load_cache()
            cache[video_id] = {
                "url": stream_url,
                "expires_at": time.time() + _CACHE_TTL_SECONDS,
            }
            self._save_cache(cache)

        return stream_url

    def prefetch_async(self, video_ids: list[str]) -> threading.Thread:
        """Warm the stream URL cache for upcoming tracks in a background daemon thread."""
        unique_ids = [vid for vid in dict.fromkeys(video_ids) if vid]

        def _worker() -> None:
            for vid in unique_ids:
                if not self.get_cached(vid):
                    self.resolve(vid)

        thread = threading.Thread(target=_worker, name="ytm-prefetch", daemon=True)
        thread.start()
        return thread

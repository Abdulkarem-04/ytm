"""Single-worker polite background cache and gapless preload coordinator."""

from __future__ import annotations

import re
import threading
from collections import deque
from typing import Any, Callable

from ytm.downloader import TrackDownloader
from ytm.liked import LikedStore
from ytm.models import Track
from ytm.player import MPVPlayer
from ytm.playlists import PlaylistStore
from ytm.settings import SettingsStore

_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def clean_error_text(exc: Exception, max_len: int = 100) -> str:
    """Strip ANSI escape sequences and collapse whitespace for crisp status errors."""
    text = _ANSI_ESCAPE_RE.sub("", str(exc))
    text = " ".join(text.split())
    if len(text) <= max_len:
        return text
    return text[: max_len - 3] + "..."


class BackgroundCacheManager:
    """Coordinates Priority-1 stream preloads and Priority-2 polite Liked Music caching."""

    def __init__(
        self,
        downloader: TrackDownloader,
        player: MPVPlayer,
        liked_store: LikedStore,
        playlist_store: PlaylistStore,
        settings_store: SettingsStore,
        on_track_cached: Callable[[Track, str], None] | None = None,
        inter_track_delay: float = 1.5,
    ) -> None:
        self.downloader = downloader
        self.player = player
        self.liked_store = liked_store
        self.playlist_store = playlist_store
        self.settings_store = settings_store
        self.on_track_cached = on_track_cached
        self.inter_track_delay = max(0.0, inter_track_delay)

        self._lock = threading.Lock()
        self._preload_queue: deque[tuple[int, Track]] = deque()
        self._seen_preloads: set[tuple[int, str]] = set()
        self._failed_liked_ids: set[str] = set()

        self._stop_event = threading.Event()
        self._wake_event = threading.Event()
        self._thread: threading.Thread | None = None

        self._caching_active: bool = False
        self._current_title: str | None = None
        self._preloading_title: str | None = None
        self._last_error: str | None = None

    def start(self) -> None:
        """Start the single background coordinator thread if not already running."""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop_event.clear()
            self._wake_event.clear()
            self._thread = threading.Thread(
                target=self._worker_loop,
                name="ytm-cache-worker",
                daemon=True,
            )
            self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        """Signal the coordinator thread to stop and wait up to timeout seconds."""
        self._stop_event.set()
        self._wake_event.set()
        with self._lock:
            thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=timeout)
        with self._lock:
            self._thread = None
            self._caching_active = False
            self._current_title = None
            self._preloading_title = None

    def wake(self) -> None:
        """Wake the background coordinator immediately (e.g. after settings change)."""
        self._wake_event.set()

    def request_preload(self, track: Track, queue_index: int) -> None:
        """Enqueue an upcoming track for Priority-1 gapless preload."""
        if not track.video_id or track.local_path:
            return
        if not self.settings_store.load().preload_next:
            return
        key = (queue_index, track.video_id)
        with self._lock:
            if key in self._seen_preloads:
                return
            self._seen_preloads.add(key)
            self._preload_queue.append((queue_index, track))
        self._wake_event.set()

    def get_progress(self) -> dict[str, Any]:
        """Return current cache progress and worker state snapshot."""
        settings = self.settings_store.load()
        liked_tracks = self.liked_store.list_tracks()
        total_liked = len(liked_tracks)
        cached_liked = sum(
            1
            for t in liked_tracks
            if t.video_id and self.downloader.find_existing(t.video_id) is not None
        )
        with self._lock:
            return {
                "auto_cache_enabled": settings.auto_cache_liked,
                "caching_active": self._caching_active,
                "current_title": self._current_title,
                "preloading_title": self._preloading_title,
                "cached_liked": cached_liked,
                "total_liked": total_liked,
                "last_error": self._last_error,
            }

    def _pop_preload(self) -> tuple[int, Track] | None:
        with self._lock:
            if self._preload_queue:
                return self._preload_queue.popleft()
        return None

    def _run_preload_item(self, queue_index: int, track: Track) -> None:
        settings = self.settings_store.load()
        if not settings.preload_next:
            return
        with self._lock:
            self._preloading_title = track.title
        try:
            saved_path = self.downloader.preload(
                track,
                max_files=settings.preload_limit,
                audio_format=settings.ytdlp_format,
            )
            self.player.replace_upcoming_entry(
                queue_index, str(saved_path), expected_video_id=track.video_id
            )
        except Exception as exc:
            with self._lock:
                self._last_error = clean_error_text(exc)
        finally:
            with self._lock:
                self._preloading_title = None

    def _next_uncached_liked(self) -> Track | None:
        for track in self.liked_store.list_tracks():
            if not track.video_id:
                continue
            with self._lock:
                if track.video_id in self._failed_liked_ids:
                    continue
            if self.downloader.find_existing(track.video_id) is not None:
                continue
            return track
        return None

    def _hot_swap_matching_upcoming(self, video_id: str, local_str: str) -> None:
        if not self.player.is_running():
            return
        status = self.player.get_status()
        if not status.get("running"):
            return
        active_pos = int(status.get("playlist_pos", -1))
        queue: list[Track] = status.get("queue") or []
        for idx, item in enumerate(queue):
            if idx > active_pos and item.video_id == video_id:
                self.player.replace_upcoming_entry(idx, local_str, expected_video_id=video_id)

    def _run_one_liked_cache(self, track: Track) -> None:
        settings = self.settings_store.load()
        with self._lock:
            self._caching_active = True
            self._current_title = track.title
        try:
            saved_path = self.downloader.download(track, audio_format=settings.ytdlp_format)
            local_str = str(saved_path)
            self.liked_store.update_local_path(track.video_id, local_str)
            self.playlist_store.update_local_path(track.video_id, local_str)
            self.player.sync_track_local_path(track.video_id, local_str)
            self._hot_swap_matching_upcoming(track.video_id, local_str)
            if self.on_track_cached is not None:
                try:
                    self.on_track_cached(track, local_str)
                except Exception:
                    pass
        except Exception as exc:
            with self._lock:
                self._failed_liked_ids.add(track.video_id)
                self._last_error = clean_error_text(exc)
        finally:
            with self._lock:
                self._caching_active = False
                self._current_title = None

    def _worker_loop(self) -> None:
        while not self._stop_event.is_set():
            # Priority 1: Next-track stream preload
            preload_item = self._pop_preload()
            if preload_item is not None:
                q_idx, track = preload_item
                self._run_preload_item(q_idx, track)
                continue

            # Priority 2: Polite sequential auto-cache for Liked Music
            settings = self.settings_store.load()
            if settings.auto_cache_liked:
                candidate = self._next_uncached_liked()
                if candidate is not None:
                    self._run_one_liked_cache(candidate)
                    if self._stop_event.is_set():
                        break
                    if self.inter_track_delay > 0:
                        self._wake_event.wait(timeout=self.inter_track_delay)
                        self._wake_event.clear()
                    continue

            self._wake_event.wait(timeout=0.5)
            self._wake_event.clear()

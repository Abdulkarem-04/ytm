"""Persistent local Liked Music track store with safe like/unlike and undo support."""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

from ytm.config import LIKED_FILE, UNLIKED_HISTORY_FILE, ensure_dirs
from ytm.models import Track

_MAX_UNDO_HISTORY = 100


class LikedStore:
    """Manages locally persisted liked tracks and an undo history stack."""

    def __init__(
        self,
        liked_file: Path = LIKED_FILE,
        unliked_file: Path | None = None,
    ) -> None:
        self.liked_file = liked_file
        if unliked_file is not None:
            self.unliked_file = unliked_file
        elif liked_file == LIKED_FILE:
            self.unliked_file = UNLIKED_HISTORY_FILE
        else:
            self.unliked_file = liked_file.parent / "unliked_history.json"
        self._lock = threading.Lock()

    def _load_list(self, path: Path) -> list[dict[str, Any]]:
        if not path.is_file():
            return []
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        if not isinstance(data, list):
            return []
        return [item for item in data if isinstance(item, dict) and item.get("video_id")]

    def _save_list(self, path: Path, items: list[dict[str, Any]]) -> None:
        if path in (LIKED_FILE, UNLIKED_HISTORY_FILE):
            ensure_dirs()
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(items, indent=2), encoding="utf-8")

    def _load_raw(self) -> list[dict[str, Any]]:
        return self._load_list(self.liked_file)

    def _save_raw(self, items: list[dict[str, Any]]) -> None:
        self._save_list(self.liked_file, items)

    def _like_unlocked(self, track: Track) -> bool:
        items = self._load_raw()
        if any(item.get("video_id") == track.video_id for item in items):
            track.is_liked = True
            return False
        track.is_liked = True
        new_entry = track.to_dict()
        new_entry["is_liked"] = True
        self._save_raw([new_entry, *items])
        return True

    def _unlike_unlocked(self, track: Track) -> bool:
        items = self._load_raw()
        removed = [item for item in items if item.get("video_id") == track.video_id]
        if not removed:
            return False
        remaining = [item for item in items if item.get("video_id") != track.video_id]
        self._save_raw(remaining)
        track.is_liked = False

        undo_items = [
            item
            for item in self._load_list(self.unliked_file)
            if item.get("video_id") != track.video_id
        ]
        undo_entry = dict(removed[0])
        undo_entry["is_liked"] = False
        self._save_list(self.unliked_file, [undo_entry, *undo_items][:_MAX_UNDO_HISTORY])
        return True

    def list_tracks(self) -> list[Track]:
        """Return all liked tracks, each marked with is_liked=True."""
        with self._lock:
            items = self._load_raw()
        tracks: list[Track] = []
        for item in items:
            track = Track.from_dict(item)
            track.is_liked = True
            tracks.append(track)
        return tracks

    def is_liked(self, video_id: str) -> bool:
        """Return True if video_id exists in the liked store."""
        if not video_id:
            return False
        with self._lock:
            return any(item.get("video_id") == video_id for item in self._load_raw())

    def like(self, track: Track) -> bool:
        """Idempotently add track to Liked Music. Returns True if newly added, False if already liked."""
        if not track.video_id:
            return False
        with self._lock:
            return self._like_unlocked(track)

    def unlike(self, track: Track) -> bool:
        """Remove track from Liked Music and push onto undo stack. Returns True if removed."""
        if not track.video_id:
            return False
        with self._lock:
            return self._unlike_unlocked(track)

    def pop_undo(self) -> Track | None:
        """Restore the most recently unliked track from undo history back to Liked Music."""
        with self._lock:
            undo_items = self._load_list(self.unliked_file)
            if not undo_items:
                return None
            top = undo_items[0]
            self._save_list(self.unliked_file, undo_items[1:])
            track = Track.from_dict(top)
            self._like_unlocked(track)
            return track

    def toggle(self, track: Track) -> bool:
        """Toggle liked status for track; returns new liked state."""
        if not track.video_id:
            return False
        with self._lock:
            if any(item.get("video_id") == track.video_id for item in self._load_raw()):
                self._unlike_unlocked(track)
                return False
            self._like_unlocked(track)
            return True

    def annotate(self, tracks: list[Track]) -> list[Track]:
        """Mark is_liked=True on any track whose video_id is in the liked store."""
        with self._lock:
            liked_ids = {str(item.get("video_id")) for item in self._load_raw()}
        for track in tracks:
            if track.video_id in liked_ids:
                track.is_liked = True
        return tracks

    def merge_tracks(self, tracks: list[Track]) -> int:
        """Merge remote/external liked tracks into the store and return count added."""
        with self._lock:
            items = self._load_raw()
            by_id: dict[str, dict[str, Any]] = {
                str(item["video_id"]): item for item in items
            }
            added = 0
            changed = False
            for track in tracks:
                if not track.video_id:
                    continue
                existing = by_id.get(track.video_id)
                if existing is None:
                    entry = track.to_dict()
                    entry["is_liked"] = True
                    items.append(entry)
                    by_id[track.video_id] = entry
                    added += 1
                    changed = True
                elif track.local_path and existing.get("local_path") != track.local_path:
                    existing["local_path"] = track.local_path
                    changed = True
            if changed:
                self._save_raw(items)
            return added

    def replace_all(self, tracks: list[Track]) -> int:
        """Replace liked tracks with tracks while preserving local_path metadata."""
        with self._lock:
            existing_paths = {
                str(item["video_id"]): str(item["local_path"])
                for item in self._load_raw()
                if item.get("local_path")
            }
            new_items: list[dict[str, Any]] = []
            seen: set[str] = set()
            for track in tracks:
                if not track.video_id or track.video_id in seen:
                    continue
                seen.add(track.video_id)
                if not track.local_path and track.video_id in existing_paths:
                    track.local_path = existing_paths[track.video_id]
                track.is_liked = True
                entry = track.to_dict()
                entry["is_liked"] = True
                new_items.append(entry)
            self._save_raw(new_items)
            return len(new_items)

    def update_local_path(self, video_id: str, local_path: str) -> None:
        """Update local_path for a liked track if it exists in the store."""
        if not video_id or not local_path:
            return
        with self._lock:
            items = self._load_raw()
            changed = False
            for item in items:
                if item.get("video_id") == video_id and item.get("local_path") != local_path:
                    item["local_path"] = local_path
                    changed = True
            if changed:
                self._save_raw(items)

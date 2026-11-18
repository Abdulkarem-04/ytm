"""Persistent custom user playlists store for ytm."""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

from ytm.config import PLAYLISTS_FILE, ensure_dirs
from ytm.models import Track

_DEFAULT_PLAYLIST_NAME = "Favorites"


class PlaylistStore:
    """Manages named user playlists persisted in ~/.config/ytm/playlists.json."""

    def __init__(self, playlists_file: Path = PLAYLISTS_FILE) -> None:
        self.playlists_file = playlists_file
        self._lock = threading.Lock()

    def _load(self) -> dict[str, Any]:
        if not self.playlists_file.is_file():
            return {"playlists": {}, "active": None}
        try:
            data = json.loads(self.playlists_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"playlists": {}, "active": None}
        if not isinstance(data, dict):
            return {"playlists": {}, "active": None}
        raw_pl = data.get("playlists")
        playlists: dict[str, list[dict[str, Any]]] = {}
        if isinstance(raw_pl, dict):
            for name, items in raw_pl.items():
                if isinstance(name, str) and isinstance(items, list):
                    playlists[name] = [
                        it for it in items if isinstance(it, dict) and it.get("video_id")
                    ]
        active = data.get("active")
        if not isinstance(active, str) or active not in playlists:
            active = next(iter(playlists), None)
        return {"playlists": playlists, "active": active}

    def _save(self, data: dict[str, Any]) -> None:
        if self.playlists_file == PLAYLISTS_FILE:
            ensure_dirs()
        else:
            self.playlists_file.parent.mkdir(parents=True, exist_ok=True)
        self.playlists_file.write_text(json.dumps(data, indent=2), encoding="utf-8")

    def list_names(self) -> list[str]:
        """Return all custom playlist names."""
        with self._lock:
            return list(self._load()["playlists"].keys())

    def get_active_name(self) -> str | None:
        """Return the currently active playlist name, or None if none exist."""
        with self._lock:
            return self._load()["active"]

    def set_active_name(self, name: str) -> None:
        """Set the active playlist if it exists."""
        with self._lock:
            data = self._load()
            if name in data["playlists"]:
                data["active"] = name
                self._save(data)

    def cycle_active(self, delta: int = 1) -> str | None:
        """Cycle the active playlist forward or backward and return the new active name."""
        with self._lock:
            data = self._load()
            names = list(data["playlists"].keys())
            if not names:
                return None
            current = data["active"]
            idx = names.index(current) if current in names else 0
            new_name = names[(idx + delta) % len(names)]
            data["active"] = new_name
            self._save(data)
            return new_name

    def create(self, name: str) -> str:
        """Create a playlist (if missing), set it active, and return its name."""
        clean = name.strip()
        if not clean:
            raise ValueError("Playlist name cannot be empty.")
        with self._lock:
            data = self._load()
            data["playlists"].setdefault(clean, [])
            data["active"] = clean
            self._save(data)
            return clean

    def delete(self, name: str) -> bool:
        """Delete a playlist by name. Returns True if removed."""
        with self._lock:
            data = self._load()
            if name not in data["playlists"]:
                return False
            del data["playlists"][name]
            if data["active"] == name:
                data["active"] = next(iter(data["playlists"]), None)
            self._save(data)
            return True

    def get_tracks(self, name: str | None = None) -> list[Track]:
        """Return tracks belonging to the given playlist (or active playlist)."""
        with self._lock:
            data = self._load()
            target = name if name is not None else data["active"]
            if not target or target not in data["playlists"]:
                return []
            return [Track.from_dict(item) for item in data["playlists"][target]]

    def add_track(self, track: Track, name: str | None = None) -> tuple[str, bool]:
        """Add track to name (or active playlist, creating 'Favorites' if none exist)."""
        if not track.video_id:
            raise ValueError("Cannot add track without video_id to playlist.")
        with self._lock:
            data = self._load()
            target = name.strip() if name else data["active"]
            if not target:
                target = _DEFAULT_PLAYLIST_NAME
            items = data["playlists"].setdefault(target, [])
            data["active"] = target
            if any(item.get("video_id") == track.video_id for item in items):
                self._save(data)
                return target, False
            items.append(track.to_dict())
            self._save(data)
            return target, True

    def remove_track(self, video_id: str, name: str | None = None) -> bool:
        """Remove video_id from the given playlist (or active playlist)."""
        if not video_id:
            return False
        with self._lock:
            data = self._load()
            target = name if name is not None else data["active"]
            if not target or target not in data["playlists"]:
                return False
            items = data["playlists"][target]
            remaining = [item for item in items if item.get("video_id") != video_id]
            if len(remaining) == len(items):
                return False
            data["playlists"][target] = remaining
            self._save(data)
            return True

    def update_local_path(self, video_id: str, local_path: str) -> None:
        """Update local_path for matching tracks across all playlists."""
        if not video_id or not local_path:
            return
        with self._lock:
            data = self._load()
            changed = False
            for items in data["playlists"].values():
                for item in items:
                    if item.get("video_id") == video_id and item.get("local_path") != local_path:
                        item["local_path"] = local_path
                        changed = True
            if changed:
                self._save(data)

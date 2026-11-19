"""Persistent user settings model and store for ytm."""

from __future__ import annotations

import json
import threading
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from ytm.config import SETTINGS_FILE, ensure_dirs

PRELOAD_LIMIT_CHOICES = (5, 10, 20)
AUDIO_QUALITY_CHOICES = ("best", "saver")
STREAM_BUFFER_CHOICES = (60, 300, 600)
SPECTRUM_MODE_CHOICES = ("fft", "simulated", "off")

_CYCLE_MAP: dict[str, tuple[Any, ...]] = {
    "auto_cache_liked": (False, True),
    "preload_next": (False, True),
    "preload_limit": PRELOAD_LIMIT_CHOICES,
    "audio_quality": AUDIO_QUALITY_CHOICES,
    "stream_buffer_secs": STREAM_BUFFER_CHOICES,
    "mpris_enabled": (False, True),
    "spectrum_mode": SPECTRUM_MODE_CHOICES,
}


@dataclass
class AppSettings:
    """User-configurable playback, caching, and connectivity preferences."""

    auto_cache_liked: bool = False
    preload_next: bool = True
    preload_limit: int = 10
    audio_quality: str = "best"
    stream_buffer_secs: int = 600
    mpris_enabled: bool = True
    spectrum_mode: str = "fft"

    @property
    def ytdlp_format(self) -> str:
        """Return the yt-dlp format selector matching the configured audio quality."""
        if self.audio_quality == "saver":
            return "bestaudio[abr<=96]/bestaudio/best"
        return "bestaudio/best"

    def to_dict(self) -> dict[str, Any]:
        """Serialize settings to a JSON-compatible dictionary."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AppSettings:
        """Deserialize settings while clamping invalid values back to safe defaults."""
        defaults = cls()
        raw_auto = data.get("auto_cache_liked", defaults.auto_cache_liked)
        auto_cache_liked = bool(raw_auto) if isinstance(raw_auto, bool) else defaults.auto_cache_liked

        raw_preload = data.get("preload_next", defaults.preload_next)
        preload_next = bool(raw_preload) if isinstance(raw_preload, bool) else defaults.preload_next

        raw_limit = data.get("preload_limit", defaults.preload_limit)
        try:
            limit_int = int(raw_limit)
        except (TypeError, ValueError):
            limit_int = defaults.preload_limit
        preload_limit = limit_int if limit_int in PRELOAD_LIMIT_CHOICES else defaults.preload_limit

        raw_quality = str(data.get("audio_quality", defaults.audio_quality)).lower()
        audio_quality = raw_quality if raw_quality in AUDIO_QUALITY_CHOICES else defaults.audio_quality

        raw_buffer = data.get("stream_buffer_secs", defaults.stream_buffer_secs)
        try:
            buf_int = int(raw_buffer)
        except (TypeError, ValueError):
            buf_int = defaults.stream_buffer_secs
        stream_buffer_secs = buf_int if buf_int in STREAM_BUFFER_CHOICES else defaults.stream_buffer_secs

        raw_mpris = data.get("mpris_enabled", defaults.mpris_enabled)
        mpris_enabled = bool(raw_mpris) if isinstance(raw_mpris, bool) else defaults.mpris_enabled

        raw_spec = str(data.get("spectrum_mode", defaults.spectrum_mode)).lower()
        spectrum_mode = raw_spec if raw_spec in SPECTRUM_MODE_CHOICES else defaults.spectrum_mode

        return cls(
            auto_cache_liked=auto_cache_liked,
            preload_next=preload_next,
            preload_limit=preload_limit,
            audio_quality=audio_quality,
            stream_buffer_secs=stream_buffer_secs,
            mpris_enabled=mpris_enabled,
            spectrum_mode=spectrum_mode,
        )


class SettingsStore:
    """Thread-safe JSON persistence for AppSettings in ~/.config/ytm/config.json."""

    def __init__(self, settings_file: Path = SETTINGS_FILE) -> None:
        self.settings_file = settings_file
        self._lock = threading.Lock()

    def _load_unlocked(self) -> AppSettings:
        if not self.settings_file.is_file():
            return AppSettings()
        try:
            data = json.loads(self.settings_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return AppSettings()
        if not isinstance(data, dict):
            return AppSettings()
        return AppSettings.from_dict(data)

    def _save_unlocked(self, settings: AppSettings) -> None:
        if self.settings_file == SETTINGS_FILE:
            ensure_dirs()
        else:
            self.settings_file.parent.mkdir(parents=True, exist_ok=True)
        self.settings_file.write_text(
            json.dumps(settings.to_dict(), indent=2),
            encoding="utf-8",
        )

    def load(self) -> AppSettings:
        """Load persisted settings, returning defaults if missing or invalid."""
        with self._lock:
            return self._load_unlocked()

    def save(self, settings: AppSettings) -> None:
        """Persist settings to disk."""
        with self._lock:
            self._save_unlocked(settings)

    def update(self, **kwargs: Any) -> AppSettings:
        """Update specific settings fields, persist, and return the validated result."""
        with self._lock:
            current = self._load_unlocked().to_dict()
            current.update(kwargs)
            updated = AppSettings.from_dict(current)
            self._save_unlocked(updated)
            return updated

    def cycle(self, field_name: str) -> AppSettings:
        """Toggle or cycle a discrete setting field and persist the new value."""
        choices = _CYCLE_MAP.get(field_name)
        if choices is None:
            raise ValueError(f"Unsupported setting field: {field_name!r}")
        with self._lock:
            settings = self._load_unlocked()
            data = settings.to_dict()
            cur = data.get(field_name)
            idx = choices.index(cur) if cur in choices else 0
            data[field_name] = choices[(idx + 1) % len(choices)]
            updated = AppSettings.from_dict(data)
            self._save_unlocked(updated)
            return updated

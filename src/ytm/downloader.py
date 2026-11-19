"""Resilient local audio caching and rolling preload cache backed by yt-dlp."""

from __future__ import annotations

import os
import shutil
import threading
from pathlib import Path
from typing import Any

import yt_dlp

from ytm.config import MUSIC_DIR, PRELOAD_DIR
from ytm.models import Track
from ytm.settings import SettingsStore

_LOCAL_AUDIO_EXTS = (".opus", ".m4a", ".webm", ".mp3", ".ogg", ".flac", ".wav")
_TEMP_SUFFIXES = {".part", ".ytdl"}


def dir_stats(directory: Path) -> tuple[int, int]:
    """Return (completed_audio_file_count, total_bytes) for directory."""
    if not directory.is_dir():
        return 0, 0
    count = 0
    total_bytes = 0
    for item in directory.iterdir():
        if not item.is_file():
            continue
        if item.suffix.lower() in _LOCAL_AUDIO_EXTS:
            count += 1
            try:
                total_bytes += item.stat().st_size
            except OSError:
                pass
    return count, total_bytes


class TrackDownloader:
    """Downloads permanent local tracks to MUSIC_DIR and rolling preloads to PRELOAD_DIR."""

    def __init__(
        self,
        music_dir: Path = MUSIC_DIR,
        preload_dir: Path = PRELOAD_DIR,
        settings_store: SettingsStore | None = None,
    ) -> None:
        self.music_dir = music_dir
        self.preload_dir = preload_dir
        self.settings_store = settings_store or SettingsStore()
        self._io_lock = threading.Lock()

    @staticmethod
    def find_in_dir(directory: Path, video_id: str) -> str | None:
        """Return path string if video_id audio file exists in directory."""
        if not video_id or not directory.is_dir():
            return None
        for ext in _LOCAL_AUDIO_EXTS:
            candidate = directory / f"{video_id}{ext}"
            if candidate.is_file():
                return str(candidate)
        return None

    def find_existing(self, video_id: str) -> str | None:
        """Return existing cached audio path for video_id in music_dir."""
        return self.find_in_dir(self.music_dir, video_id)

    def find_preloaded(self, video_id: str) -> str | None:
        """Return existing preloaded audio path for video_id in preload_dir."""
        return self.find_in_dir(self.preload_dir, video_id)

    def _find_downloaded_file(self, directory: Path, video_id: str) -> Path | None:
        existing = self.find_in_dir(directory, video_id)
        if existing:
            return Path(existing)
        if not directory.is_dir():
            return None
        for candidate in sorted(directory.glob(f"{video_id}.*")):
            if candidate.is_file() and candidate.suffix.lower() not in _TEMP_SUFFIXES:
                return candidate
        return None

    def _download_to_dir(
        self,
        track: Track,
        target_dir: Path,
        audio_format: str | None = None,
    ) -> Path:
        if not track.video_id:
            raise ValueError("Cannot download track without a video_id.")
        target_dir.mkdir(parents=True, exist_ok=True)
        resolved_format = audio_format or self.settings_store.load().ytdlp_format

        opts: dict[str, Any] = {
            "format": resolved_format,
            "outtmpl": str(target_dir / f"{track.video_id}.%(ext)s"),
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "retries": 10,
            "fragment_retries": 10,
            "socket_timeout": 15,
            "continuedl": True,
        }
        url = f"https://www.youtube.com/watch?v={track.video_id}"
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])

        downloaded = self._find_downloaded_file(target_dir, track.video_id)
        if downloaded is None:
            raise RuntimeError(
                f"Download completed but output file missing for {track.video_id}."
            )
        return downloaded

    def download(self, track: Track, audio_format: str | None = None) -> Path:
        """Download or promote track audio into music_dir and update track.local_path."""
        if not track.video_id:
            raise ValueError("Cannot download track without a video_id.")

        self.music_dir.mkdir(parents=True, exist_ok=True)
        with self._io_lock:
            existing = self.find_existing(track.video_id)
            if existing:
                track.local_path = existing
                return Path(existing)

            preloaded = self.find_preloaded(track.video_id)
            if preloaded:
                src = Path(preloaded)
                dest = self.music_dir / src.name
                shutil.move(str(src), str(dest))
                track.local_path = str(dest)
                return dest

        downloaded = self._download_to_dir(track, self.music_dir, audio_format=audio_format)
        track.local_path = str(downloaded)
        return downloaded

    def preload(
        self,
        track: Track,
        max_files: int | None = None,
        audio_format: str | None = None,
    ) -> Path:
        """Preload track audio into preload_dir (reusing music_dir if permanently cached)."""
        if not track.video_id:
            raise ValueError("Cannot preload track without a video_id.")

        limit = max_files if max_files is not None else self.settings_store.load().preload_limit

        existing = self.find_existing(track.video_id)
        if existing:
            return Path(existing)

        with self._io_lock:
            preloaded = self.find_preloaded(track.video_id)
            if preloaded:
                p = Path(preloaded)
                try:
                    os.utime(p, None)
                except OSError:
                    pass
                self.prune_preload(max_files=limit, keep_video_id=track.video_id)
                return p

        downloaded = self._download_to_dir(track, self.preload_dir, audio_format=audio_format)
        try:
            os.utime(downloaded, None)
        except OSError:
            pass
        self.prune_preload(max_files=limit, keep_video_id=track.video_id)
        return downloaded

    def prune_preload(self, max_files: int = 10, keep_video_id: str | None = None) -> int:
        """Delete oldest preloaded files beyond max_files while preserving keep_video_id."""
        if not self.preload_dir.is_dir():
            return 0
        files = [
            item
            for item in self.preload_dir.iterdir()
            if item.is_file()
            and item.suffix.lower() in _LOCAL_AUDIO_EXTS
            and item.suffix.lower() not in _TEMP_SUFFIXES
        ]
        files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        keep_count = max(0, max_files)
        removed = 0
        kept = 0
        for item in files:
            if keep_video_id and item.stem == keep_video_id:
                kept += 1
                continue
            if kept < keep_count:
                kept += 1
                continue
            try:
                item.unlink()
                removed += 1
            except OSError:
                pass
        return removed

    def clear_preload(self) -> int:
        """Delete all files inside preload_dir and return count removed."""
        if not self.preload_dir.is_dir():
            return 0
        removed = 0
        with self._io_lock:
            for item in list(self.preload_dir.iterdir()):
                if item.is_file():
                    try:
                        item.unlink()
                        removed += 1
                    except OSError:
                        pass
        return removed

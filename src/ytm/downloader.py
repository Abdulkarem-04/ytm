"""Local audio caching and download manager backed by yt-dlp."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yt_dlp

from ytm.config import MUSIC_DIR
from ytm.models import Track

_LOCAL_AUDIO_EXTS = (".opus", ".m4a", ".webm", ".mp3", ".ogg", ".flac", ".wav")
_TEMP_SUFFIXES = {".part", ".ytdl"}


class TrackDownloader:
    """Downloads and resolves locally cached audio files in MUSIC_DIR."""

    def __init__(self, music_dir: Path = MUSIC_DIR) -> None:
        self.music_dir = music_dir

    def find_existing(self, video_id: str) -> str | None:
        """Return existing cached audio path for video_id if present in music_dir."""
        if not video_id or not self.music_dir.is_dir():
            return None
        for ext in _LOCAL_AUDIO_EXTS:
            candidate = self.music_dir / f"{video_id}{ext}"
            if candidate.is_file():
                return str(candidate)
        return None

    def _find_downloaded_file(self, video_id: str) -> Path | None:
        existing = self.find_existing(video_id)
        if existing:
            return Path(existing)
        if not self.music_dir.is_dir():
            return None
        for candidate in sorted(self.music_dir.glob(f"{video_id}.*")):
            if candidate.is_file() and candidate.suffix.lower() not in _TEMP_SUFFIXES:
                return candidate
        return None

    def download(self, track: Track) -> Path:
        """Download best audio stream for track into music_dir and update track.local_path."""
        if not track.video_id:
            raise ValueError("Cannot download track without a video_id.")

        self.music_dir.mkdir(parents=True, exist_ok=True)
        existing = self.find_existing(track.video_id)
        if existing:
            track.local_path = existing
            return Path(existing)

        opts: dict[str, Any] = {
            "format": "bestaudio/best",
            "outtmpl": str(self.music_dir / f"{track.video_id}.%(ext)s"),
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
        }
        url = f"https://www.youtube.com/watch?v={track.video_id}"
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])

        downloaded = self._find_downloaded_file(track.video_id)
        if downloaded is None:
            raise RuntimeError(f"Download completed but output file missing for {track.video_id}.")
        track.local_path = str(downloaded)
        return downloaded

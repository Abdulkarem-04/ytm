"""Configuration paths and directory helpers for ytm."""

from __future__ import annotations

import shutil
from pathlib import Path

CONFIG_DIR = Path.home() / ".config" / "ytm"
CACHE_DIR = Path.home() / ".cache" / "ytm"
MUSIC_DIR = Path.home() / "Music" / "ytm"

AUTH_HEADERS_FILE = CONFIG_DIR / "headers_auth.json"
LIKED_FILE = CONFIG_DIR / "liked_tracks.json"
UNLIKED_HISTORY_FILE = CONFIG_DIR / "unliked_history.json"
PLAYLISTS_FILE = CONFIG_DIR / "playlists.json"
SETTINGS_FILE = CONFIG_DIR / "config.json"
STATE_FILE = CACHE_DIR / "state.json"
STREAM_CACHE_FILE = CACHE_DIR / "stream_cache.json"
COVERS_DIR = CACHE_DIR / "covers"
PRELOAD_DIR = CACHE_DIR / "preload"
MPV_SOCKET_PATH = Path("/tmp/ytm-mpv.sock")

_BUNDLED_YTDLP = Path(__file__).resolve().parents[2] / ".venv" / "bin" / "yt-dlp"
_SYSTEM_YTDLP = shutil.which("yt-dlp")
VENV_YTDLP_PATH: Path = (
    _BUNDLED_YTDLP
    if _BUNDLED_YTDLP.exists()
    else (Path(_SYSTEM_YTDLP) if _SYSTEM_YTDLP else _BUNDLED_YTDLP)
)


def get_ytdlp_path() -> str:
    """Return the path to the yt-dlp executable, checking venv then PATH."""
    if _BUNDLED_YTDLP.exists():
        return str(_BUNDLED_YTDLP)
    which_path = shutil.which("yt-dlp")
    if which_path:
        return which_path
    return str(VENV_YTDLP_PATH)


def ensure_dirs() -> None:
    """Create configuration, cache, cover art, preload, and local music directories if missing."""
    for directory in (CONFIG_DIR, CACHE_DIR, COVERS_DIR, PRELOAD_DIR, MUSIC_DIR):
        directory.mkdir(parents=True, exist_ok=True)

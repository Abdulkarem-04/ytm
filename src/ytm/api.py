"""YouTube Music metadata and radio queue client backed by ytmusicapi."""

from __future__ import annotations

from typing import Any

from ytmusicapi import YTMusic

from ytm.auth import sanitize_auth_file
from ytm.config import AUTH_HEADERS_FILE, MUSIC_DIR
from ytm.liked import LikedStore
from ytm.models import Track, format_duration, parse_duration_seconds

_LOCAL_AUDIO_EXTS = (".opus", ".m4a", ".webm", ".mp3", ".ogg", ".flac", ".wav")


def _find_local_track(video_id: str) -> str | None:
    """Return local cached audio file path for video_id if present in MUSIC_DIR."""
    if not video_id or not MUSIC_DIR.exists():
        return None
    for ext in _LOCAL_AUDIO_EXTS:
        candidate = MUSIC_DIR / f"{video_id}{ext}"
        if candidate.is_file():
            return str(candidate)
    return None


def _parse_track(item: dict[str, Any]) -> Track | None:
    """Convert a ytmusicapi search or watch-playlist item into a Track."""
    video_id = item.get("videoId")
    if not video_id:
        return None

    title = str(item.get("title") or "Unknown Title")

    raw_artists = item.get("artists") or []
    artist_names = [
        str(a.get("name", "")).strip()
        for a in raw_artists
        if isinstance(a, dict) and a.get("name")
    ]
    artist = ", ".join(artist_names) if artist_names else "Unknown Artist"

    raw_album = item.get("album")
    album = ""
    if isinstance(raw_album, dict):
        album = str(raw_album.get("name") or "")
    elif isinstance(raw_album, str):
        album = raw_album

    duration_str = str(item.get("duration") or item.get("length") or "").strip()
    duration_secs = item.get("duration_seconds")
    if isinstance(duration_secs, (int, float)) and duration_secs > 0:
        duration_seconds = int(duration_secs)
        if not duration_str:
            duration_str = format_duration(duration_seconds)
    else:
        duration_seconds = parse_duration_seconds(duration_str)
        if not duration_str:
            duration_str = format_duration(duration_seconds)

    like_status = str(item.get("likeStatus") or "").upper()
    is_liked = like_status == "LIKE"

    return Track(
        video_id=str(video_id),
        title=title,
        artist=artist,
        album=album,
        duration=duration_str,
        duration_seconds=duration_seconds,
        is_liked=is_liked,
        local_path=_find_local_track(str(video_id)),
    )


class YTMClient:
    """Wrapper around ytmusicapi for search and radio discovery."""

    def __init__(self, liked_store: LikedStore | None = None) -> None:
        self.liked_store = liked_store or LikedStore()
        if AUTH_HEADERS_FILE.is_file():
            sanitize_auth_file(AUTH_HEADERS_FILE)
            self.yt = YTMusic(str(AUTH_HEADERS_FILE))
            self.authenticated = True
        else:
            self.yt = YTMusic()
            self.authenticated = False

    def search_tracks(self, query: str, limit: int = 15) -> list[Track]:
        """Search YouTube Music for songs, falling back to videos if empty."""
        results = self.yt.search(query, filter="songs", limit=limit)
        tracks = [t for item in (results or []) if (t := _parse_track(item)) is not None]
        if not tracks:
            fallback = self.yt.search(query, filter="videos", limit=limit)
            tracks = [t for item in (fallback or []) if (t := _parse_track(item)) is not None]
        return self.liked_store.annotate(tracks[:limit])

    def get_radio_queue(self, video_id: str, limit: int = 20) -> list[Track]:
        """Fetch an Up Next watch playlist / radio queue seeded by video_id."""
        watch = self.yt.get_watch_playlist(videoId=video_id, limit=limit)
        raw_tracks = watch.get("tracks") or [] if isinstance(watch, dict) else []
        tracks: list[Track] = []
        seen: set[str] = set()
        for item in raw_tracks:
            track = _parse_track(item)
            if track and track.video_id not in seen:
                seen.add(track.video_id)
                tracks.append(track)
        return self.liked_store.annotate(tracks[:limit])

    def fetch_liked_songs(self, limit: int = 200) -> list[Track]:
        """Fetch the authenticated user's Liked Music library from YouTube Music."""
        if not self.authenticated:
            raise RuntimeError("Not authenticated with YouTube Music.")
        data = self.yt.get_liked_songs(limit=limit)
        raw_tracks = data.get("tracks") or [] if isinstance(data, dict) else []
        tracks: list[Track] = []
        for item in raw_tracks:
            track = _parse_track(item)
            if track is not None:
                track.is_liked = True
                tracks.append(track)
        return self.liked_store.annotate(tracks[:limit])

    def rate_track(self, video_id: str, liked: bool) -> bool:
        """Rate a track on the authenticated YouTube Music account (LIKE or INDIFFERENT)."""
        if not self.authenticated or not video_id:
            return False
        rating = "LIKE" if liked else "INDIFFERENT"
        try:
            self.yt.rate_song(video_id, rating)
            return True
        except Exception:
            return False

    def fetch_lyrics(self, video_id: str) -> dict[str, Any] | None:
        """Fetch timed or plain lyrics for video_id from YouTube Music if available."""
        if not video_id:
            return None
        try:
            watch = self.yt.get_watch_playlist(videoId=video_id, limit=1)
        except Exception:
            return None

        browse_id = watch.get("lyrics") if isinstance(watch, dict) else None
        if not isinstance(browse_id, str) or not browse_id.strip():
            return None

        raw: Any = None
        try:
            raw = self.yt.get_lyrics(browse_id, timestamps=True)
        except Exception:
            raw = None
        if raw is None:
            try:
                raw = self.yt.get_lyrics(browse_id, timestamps=False)
            except Exception:
                return None

        if not isinstance(raw, dict):
            return None

        source = str(raw.get("source") or "YouTube Music")
        raw_lyrics = raw.get("lyrics")

        if bool(raw.get("hasTimestamps")) and isinstance(raw_lyrics, list):
            lines: list[dict[str, Any]] = []
            plain_lines: list[str] = []
            for item in raw_lyrics:
                if isinstance(item, dict):
                    text = str(item.get("text") or "")
                    start_ms = item.get("start_time")
                    end_ms = item.get("end_time")
                else:
                    text = str(getattr(item, "text", "") or "")
                    start_ms = getattr(item, "start_time", None)
                    end_ms = getattr(item, "end_time", None)

                if start_ms is None:
                    continue
                try:
                    start_sec = max(0.0, float(start_ms) / 1000.0)
                except (TypeError, ValueError):
                    continue
                end_sec: float | None = None
                if end_ms is not None:
                    try:
                        end_sec = max(start_sec, float(end_ms) / 1000.0)
                    except (TypeError, ValueError):
                        end_sec = None

                lines.append({"start_sec": start_sec, "end_sec": end_sec, "text": text})
                plain_lines.append(text)

            if lines:
                return {
                    "synced": True,
                    "source": source,
                    "lines": lines,
                    "plain_text": "\n".join(plain_lines),
                }

        if isinstance(raw_lyrics, str) and raw_lyrics.strip():
            return {
                "synced": False,
                "source": source,
                "lines": [],
                "plain_text": raw_lyrics.strip(),
            }

        return None

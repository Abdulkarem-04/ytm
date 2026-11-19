"""Multi-source synced lyrics fetcher, LRC parser, and disk cache for ytm."""

from __future__ import annotations

import json
import os
import re
import threading
import urllib.error
import urllib.parse
import urllib.request
from bisect import bisect_right
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ytm.api import YTMClient
from ytm.config import LYRICS_DIR
from ytm.models import Track

_LRC_TAG_RE = re.compile(r"\[(\d{1,3}):(\d{2})(?:[.:](\d{1,3}))?\]")
_WORD_TAG_RE = re.compile(r"<\d{1,3}:\d{2}(?:[.:]\d{1,3})?>")
_USER_AGENT = "ytm/0.1.0 (https://github.com/dezba/ytm)"


@dataclass
class LyricLine:
    """Single timestamped lyric line."""

    start_sec: float
    text: str
    end_sec: float | None = None

    def to_dict(self) -> dict[str, Any]:
        """Serialize LyricLine into a JSON-compatible dictionary."""
        return {
            "start_sec": self.start_sec,
            "end_sec": self.end_sec,
            "text": self.text,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> LyricLine:
        """Deserialize LyricLine from a dictionary."""
        start_val = float(data.get("start_sec", 0.0) or 0.0)
        end_raw = data.get("end_sec")
        end_val = float(end_raw) if end_raw is not None else None
        return cls(
            start_sec=max(0.0, start_val),
            text=str(data.get("text", "")),
            end_sec=end_val,
        )


@dataclass
class LyricsData:
    """Complete synced or plain lyrics payload for a track."""

    video_id: str
    synced: bool
    source: str
    lines: list[LyricLine]
    plain_text: str

    def to_dict(self) -> dict[str, Any]:
        """Serialize LyricsData into a JSON-compatible dictionary."""
        return {
            "video_id": self.video_id,
            "synced": self.synced,
            "source": self.source,
            "lines": [line.to_dict() for line in self.lines],
            "plain_text": self.plain_text,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> LyricsData:
        """Deserialize LyricsData from a dictionary."""
        raw_lines = data.get("lines") or []
        lines = [LyricLine.from_dict(item) for item in raw_lines if isinstance(item, dict)]
        return cls(
            video_id=str(data.get("video_id", "")),
            synced=bool(data.get("synced", False)) and bool(lines),
            source=str(data.get("source", "Unknown")),
            lines=lines,
            plain_text=str(data.get("plain_text", "")),
        )


def parse_lrc(lrc_text: str) -> list[LyricLine]:
    """Parse standard [mm:ss.xx] LRC lyrics text into sorted LyricLine entries."""
    if not lrc_text or not isinstance(lrc_text, str):
        return []

    parsed: list[LyricLine] = []
    for raw_line in lrc_text.splitlines():
        tags = list(_LRC_TAG_RE.finditer(raw_line))
        if not tags:
            continue
        clean_text = _WORD_TAG_RE.sub("", _LRC_TAG_RE.sub("", raw_line)).strip()
        for match in tags:
            mins = int(match.group(1))
            secs = int(match.group(2))
            frac_raw = match.group(3)
            if frac_raw is None:
                frac = 0.0
            elif len(frac_raw) == 1:
                frac = int(frac_raw) / 10.0
            elif len(frac_raw) == 2:
                frac = int(frac_raw) / 100.0
            else:
                frac = int(frac_raw) / 1000.0
            timestamp = max(0.0, mins * 60.0 + secs + frac)
            parsed.append(LyricLine(start_sec=timestamp, text=clean_text))

    parsed.sort(key=lambda item: item.start_sec)
    for idx in range(len(parsed) - 1):
        if parsed[idx].end_sec is None:
            parsed[idx].end_sec = parsed[idx + 1].start_sec
    return parsed


def active_line_index(lines: list[LyricLine], time_pos: float, lead_sec: float = 0.15) -> int:
    """Return index of the active lyric line for time_pos using binary search."""
    if not lines:
        return -1
    starts = [line.start_sec for line in lines]
    pos = bisect_right(starts, max(0.0, float(time_pos)) + lead_sec) - 1
    return max(0, min(pos, len(lines) - 1))


def _sanitize_video_id(video_id: str) -> str:
    return "".join(ch for ch in video_id if ch.isalnum() or ch in ("-", "_"))


class LyricsProvider:
    """Fetches and disk-caches synced lyrics from YouTube Music with LRCLIB fallback."""

    def __init__(
        self,
        client: YTMClient | None = None,
        lyrics_dir: Path = LYRICS_DIR,
    ) -> None:
        self.client = client
        self.lyrics_dir = lyrics_dir
        self._mem_cache: dict[str, LyricsData] = {}
        self._lock = threading.Lock()

    def _cache_path(self, video_id: str) -> Path | None:
        clean = _sanitize_video_id(video_id)
        if not clean:
            return None
        return self.lyrics_dir / f"{clean}.json"

    def get_cached(self, video_id: str) -> LyricsData | None:
        """Return cached LyricsData from memory or disk if available."""
        if not video_id:
            return None
        with self._lock:
            mem_hit = self._mem_cache.get(video_id)
        if mem_hit is not None:
            return mem_hit

        path = self._cache_path(video_id)
        if path is None or not path.is_file():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(payload, dict):
            return None
        data = LyricsData.from_dict(payload)
        if not data.lines and not data.plain_text.strip():
            return None
        with self._lock:
            self._mem_cache[video_id] = data
        return data

    def _save_cache(self, data: LyricsData) -> None:
        with self._lock:
            self._mem_cache[data.video_id] = data
        path = self._cache_path(data.video_id)
        if path is None:
            return
        self.lyrics_dir.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_name(f"{path.name}.tmp-{os.getpid()}-{threading.get_ident()}")
        try:
            tmp_path.write_text(
                json.dumps(data.to_dict(), indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            os.replace(tmp_path, path)
        except OSError:
            try:
                if tmp_path.exists():
                    tmp_path.unlink()
            except OSError:
                pass

    def _http_get_json(self, url: str) -> Any:
        req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=4.0) as resp:
                body = resp.read().decode("utf-8")
            return json.loads(body)
        except (urllib.error.URLError, OSError, TimeoutError, json.JSONDecodeError):
            return None

    def _payload_to_lrclib_lyrics(self, video_id: str, item: dict[str, Any]) -> LyricsData | None:
        synced_raw = item.get("syncedLyrics")
        if isinstance(synced_raw, str) and synced_raw.strip():
            lines = parse_lrc(synced_raw)
            if lines:
                plain = str(item.get("plainLyrics") or "\n".join(line.text for line in lines)).strip()
                return LyricsData(
                    video_id=video_id,
                    synced=True,
                    source="LRCLIB",
                    lines=lines,
                    plain_text=plain,
                )

        plain_raw = item.get("plainLyrics")
        if isinstance(plain_raw, str) and plain_raw.strip():
            return LyricsData(
                video_id=video_id,
                synced=False,
                source="LRCLIB",
                lines=[],
                plain_text=plain_raw.strip(),
            )
        return None

    def _fetch_lrclib(self, track: Track) -> LyricsData | None:
        title = (track.title or "").strip()
        artist = (track.artist or "").strip()
        if not title:
            return None

        params: dict[str, str] = {"track_name": title}
        if artist and artist != "Unknown Artist":
            params["artist_name"] = artist
        if track.album:
            params["album_name"] = track.album
        if track.duration_seconds and track.duration_seconds > 0:
            params["duration"] = str(int(track.duration_seconds))

        exact_url = f"https://lrclib.net/api/get?{urllib.parse.urlencode(params)}"
        payload = self._http_get_json(exact_url)
        if isinstance(payload, dict):
            parsed = self._payload_to_lrclib_lyrics(track.video_id, payload)
            if parsed is not None:
                return parsed

        search_params: dict[str, str] = {"track_name": title}
        if artist and artist != "Unknown Artist":
            search_params["artist_name"] = artist
        search_url = f"https://lrclib.net/api/search?{urllib.parse.urlencode(search_params)}"
        search_payload = self._http_get_json(search_url)
        if isinstance(search_payload, list):
            for candidate in search_payload:
                if not isinstance(candidate, dict):
                    continue
                parsed = self._payload_to_lrclib_lyrics(track.video_id, candidate)
                if parsed is not None and parsed.synced:
                    return parsed
            for candidate in search_payload:
                if not isinstance(candidate, dict):
                    continue
                parsed = self._payload_to_lrclib_lyrics(track.video_id, candidate)
                if parsed is not None:
                    return parsed
        return None

    def fetch(self, track: Track) -> LyricsData | None:
        """Load cached lyrics or fetch from YouTube Music with LRCLIB synced fallback."""
        if not track.video_id:
            return None
        cached = self.get_cached(track.video_id)
        if cached is not None:
            return cached

        plain_fallback: LyricsData | None = None

        if self.client is not None:
            try:
                ytm_raw = self.client.fetch_lyrics(track.video_id)
            except Exception:
                ytm_raw = None
            if isinstance(ytm_raw, dict):
                lines = [
                    LyricLine.from_dict(item)
                    for item in (ytm_raw.get("lines") or [])
                    if isinstance(item, dict)
                ]
                plain_text = str(ytm_raw.get("plain_text") or "").strip()
                source = str(ytm_raw.get("source") or "YouTube Music")
                if bool(ytm_raw.get("synced")) and lines:
                    result = LyricsData(
                        video_id=track.video_id,
                        synced=True,
                        source=source,
                        lines=lines,
                        plain_text=plain_text or "\n".join(line.text for line in lines),
                    )
                    self._save_cache(result)
                    return result
                if plain_text:
                    plain_fallback = LyricsData(
                        video_id=track.video_id,
                        synced=False,
                        source=source,
                        lines=[],
                        plain_text=plain_text,
                    )

        lrclib_res = self._fetch_lrclib(track)
        if lrclib_res is not None:
            if lrclib_res.synced:
                self._save_cache(lrclib_res)
                return lrclib_res
            if plain_fallback is None:
                plain_fallback = lrclib_res

        if plain_fallback is not None:
            self._save_cache(plain_fallback)
            return plain_fallback

        return None

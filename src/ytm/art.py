"""Truecolor Unicode half-block album art renderer backed by ffmpeg."""

from __future__ import annotations

import subprocess
import threading
import urllib.error
import urllib.request
from pathlib import Path

from ytm.config import COVERS_DIR


class CoverArtRenderer:
    """Downloads YouTube thumbnails and renders 14x7 Truecolor half-block cover art."""

    def __init__(self, covers_dir: Path = COVERS_DIR) -> None:
        self.covers_dir = covers_dir
        self._markup_cache: dict[str, str] = {}
        self._lock = threading.Lock()

    @staticmethod
    def placeholder_markup() -> str:
        """Return a 14x7 high-contrast neon disc graphic when no cover art is available."""
        return "\n".join(
            [
                "[#00e5ff]  ▄▄██████▄▄  [/]",
                "[#00e5ff] ▄██[/][#4c6ef5]▀▀▀▀▀▀[/][#00e5ff]██▄ [/]",
                "[#00e5ff]██▀ [/][#ff4d94] ▄██▄ [/][#00e5ff] ▀██[/]",
                "[#00e5ff]██  [/][#ff4d94] █[/][bold #00ff87]██[/][#ff4d94]█ [/][#00e5ff]  ██[/]",
                "[#00e5ff]██▄ [/][#ff4d94] ▀██▀ [/][#00e5ff] ▄██[/]",
                "[#00e5ff] ▀██[/][#4c6ef5]▄▄▄▄▄▄[/][#00e5ff]██▀ [/]",
                "[#00e5ff]  ▀▀██████▀▀  [/]",
            ]
        )

    def get_cached_markup(self, video_id: str) -> str | None:
        """Return in-memory rendered Rich markup for video_id if cached."""
        with self._lock:
            return self._markup_cache.get(video_id)

    def _download_thumbnail(self, video_id: str) -> Path | None:
        self.covers_dir.mkdir(parents=True, exist_ok=True)
        img_path = self.covers_dir / f"{video_id}.jpg"
        if img_path.is_file() and img_path.stat().st_size > 0:
            return img_path

        for variant in ("hqdefault.jpg", "mqdefault.jpg"):
            url = f"https://i.ytimg.com/vi/{video_id}/{variant}"
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            try:
                with urllib.request.urlopen(req, timeout=4.0) as resp:
                    payload = resp.read()
                if payload:
                    img_path.write_bytes(payload)
                    return img_path
            except (urllib.error.URLError, OSError, TimeoutError):
                continue
        return None

    def render_file(self, img_path: Path, width: int = 14, height_cells: int = 7) -> str:
        """Convert a local image file to width x height_cells Truecolor half-block Rich markup."""
        px_h = height_cells * 2
        cmd = [
            "ffmpeg",
            "-v",
            "error",
            "-i",
            str(img_path),
            "-vframes",
            "1",
            "-vf",
            f"crop=min(iw\\,ih):min(iw\\,ih),scale={width}:{px_h}",
            "-f",
            "rawvideo",
            "-pix_fmt",
            "rgb24",
            "-",
        ]
        try:
            raw = subprocess.check_output(cmd, stderr=subprocess.DEVNULL, timeout=5.0)
        except (OSError, subprocess.SubprocessError):
            return self.placeholder_markup()

        expected = width * px_h * 3
        if len(raw) != expected:
            return self.placeholder_markup()

        lines: list[str] = []
        stride = width * 3
        for cell_y in range(height_cells):
            top_off = (cell_y * 2) * stride
            bot_off = (cell_y * 2 + 1) * stride
            cells: list[str] = []
            for x in range(width):
                p1 = top_off + x * 3
                p2 = bot_off + x * 3
                r1, g1, b1 = raw[p1], raw[p1 + 1], raw[p1 + 2]
                r2, g2, b2 = raw[p2], raw[p2 + 1], raw[p2 + 2]
                cells.append(f"[#{r1:02x}{g1:02x}{b1:02x} on #{r2:02x}{g2:02x}{b2:02x}]▀[/]")
            lines.append("".join(cells))
        return "\n".join(lines)

    def fetch_and_render(self, video_id: str, width: int = 14, height_cells: int = 7) -> str:
        """Download thumbnail if needed, render Truecolor half-block markup, and cache in memory."""
        if not video_id:
            return self.placeholder_markup()
        cached = self.get_cached_markup(video_id)
        if cached is not None:
            return cached

        img_path = self._download_thumbnail(video_id)
        if img_path is None:
            return self.placeholder_markup()

        markup = self.render_file(img_path, width=width, height_cells=height_cells)
        if markup != self.placeholder_markup():
            with self._lock:
                self._markup_cache[video_id] = markup
        return markup

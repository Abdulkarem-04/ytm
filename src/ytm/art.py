"""Truecolor half-block and high-resolution square album art renderer."""

from __future__ import annotations

import os
import subprocess
import threading
import urllib.error
import urllib.request
from pathlib import Path

import numpy as np
from PIL import Image, ImageEnhance, ImageOps

from ytm.config import COVERS_DIR


def _trim_letterbox_pil(pil_img: Image.Image) -> Image.Image:
    """Trim dark horizontal letterbox bars common on 4:3 YouTube thumbnails."""
    arr = np.asarray(pil_img, dtype=np.uint8)
    if arr.ndim != 3 or arr.shape[0] < 16 or arr.shape[1] < 16:
        return pil_img
    h, w, _ = arr.shape
    max_trim = int(h * 0.22)
    row_means = arr.mean(axis=(1, 2))
    row_p95 = np.percentile(arr, 95, axis=(1, 2))

    top = 0
    while top < max_trim and row_means[top] < 16.0 and row_p95[top] < 32.0:
        top += 1

    bottom = h
    while (h - bottom) < max_trim and bottom > top and row_means[bottom - 1] < 16.0 and row_p95[bottom - 1] < 32.0:
        bottom -= 1

    if (bottom - top) >= max(1, int(h * 0.5)) and (top > 0 or bottom < h):
        return pil_img.crop((0, top, w, bottom))
    return pil_img


def _crop_and_resize_pil(
    img_path: Path,
    target_w: int,
    target_h: int,
    sharpen: float = 1.18,
    contrast: float = 1.04,
    color: float = 1.0,
) -> Image.Image:
    """Open img_path, strip letterbox bars, center-crop square, and resize with Lanczos."""
    with Image.open(img_path) as opened:
        rgb = opened.convert("RGB")
        trimmed = _trim_letterbox_pil(rgb)
        side = min(trimmed.width, trimmed.height)
        if trimmed.width != trimmed.height and side > 0:
            left = (trimmed.width - side) // 2
            top = (trimmed.height - side) // 2
            square = trimmed.crop((left, top, left + side, top + side))
        else:
            square = trimmed
        fitted = ImageOps.fit(
            square,
            (max(1, target_w), max(1, target_h)),
            method=Image.Resampling.LANCZOS,
        )
        if color != 1.0:
            fitted = ImageEnhance.Color(fitted).enhance(color)
        if contrast != 1.0:
            fitted = ImageEnhance.Contrast(fitted).enhance(contrast)
        if sharpen != 1.0:
            fitted = ImageEnhance.Sharpness(fitted).enhance(sharpen)
        return fitted


def trim_and_crop_square(
    img_path: Path,
    out_path: Path | None = None,
    size: int = 360,
) -> Path | None:
    """Create a crisp square PNG from img_path with YouTube black letterbox bars removed."""
    if not img_path.is_file():
        return None
    target = out_path or img_path.with_name(f"{img_path.stem}_sq.png")
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = target.with_name(f"{target.name}.tmp-{os.getpid()}-{threading.get_ident()}")
    try:
        squared = _crop_and_resize_pil(img_path, size, size, sharpen=1.18, contrast=1.04)
        squared.save(tmp_path, format="PNG")
        os.replace(tmp_path, target)
        return target
    except Exception:
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except OSError:
            pass
        return None


def _rgb_bytes_to_halfblock(raw: bytes, width: int, height_cells: int) -> str:
    """Convert raw RGB24 bytes of size width x (height_cells * 2) into Truecolor half-block Rich markup."""
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


class CoverArtRenderer:
    """Downloads YouTube thumbnails and prepares both square PNGs (Sixel) and half-block markup."""

    def __init__(self, covers_dir: Path = COVERS_DIR) -> None:
        self.covers_dir = covers_dir
        self._markup_cache: dict[tuple[str, int, int], str] = {}
        self._lock = threading.Lock()

    @staticmethod
    def placeholder_markup(width: int = 14, height_cells: int = 7) -> str:
        """Return a centered high-contrast neon disc graphic scaled to width x height_cells."""
        base = [
            "[#00e5ff]  ▄▄██████▄▄  [/]",
            "[#00e5ff] ▄██[/][#4c6ef5]▀▀▀▀▀▀[/][#00e5ff]██▄ [/]",
            "[#00e5ff]██▀ [/][#ff4d94] ▄██▄ [/][#00e5ff] ▀██[/]",
            "[#00e5ff]██  [/][#ff4d94] █[/][bold #00ff87]██[/][#ff4d94]█ [/][#00e5ff]  ██[/]",
            "[#00e5ff]██▄ [/][#ff4d94] ▀██▀ [/][#00e5ff] ▄██[/]",
            "[#00e5ff] ▀██[/][#4c6ef5]▄▄▄▄▄▄[/][#00e5ff]██▀ [/]",
            "[#00e5ff]  ▀▀██████▀▀  [/]",
        ]
        if width <= 14 and height_cells <= 7:
            return "\n".join(base)

        pad_x = max(0, width - 14)
        left_pad = " " * (pad_x // 2)
        right_pad = " " * (pad_x - (pad_x // 2))
        padded_lines = [f"{left_pad}{line}{right_pad}" for line in base]

        pad_y = max(0, height_cells - 7)
        top_pad = pad_y // 2
        bottom_pad = pad_y - top_pad
        blank = " " * max(14, width)
        return "\n".join(([blank] * top_pad) + padded_lines + ([blank] * bottom_pad))

    def get_cached_markup(
        self, video_id: str, width: int = 14, height_cells: int = 7
    ) -> str | None:
        """Return in-memory rendered Rich markup for (video_id, width, height_cells) if cached."""
        with self._lock:
            return self._markup_cache.get((video_id, width, height_cells))

    def _download_thumbnail(self, video_id: str) -> Path | None:
        self.covers_dir.mkdir(parents=True, exist_ok=True)
        img_path = self.covers_dir / f"{video_id}.jpg"
        if img_path.is_file() and img_path.stat().st_size > 0:
            return img_path

        for variant in ("sddefault.jpg", "hqdefault.jpg", "mqdefault.jpg"):
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

    def prepare_square_image(self, video_id: str, size: int = 360) -> Path | None:
        """Return a cached letterbox-trimmed square PNG for video_id, downloading if needed."""
        if not video_id:
            return None
        sq_path = self.covers_dir / f"{video_id}_sq.png"
        if sq_path.is_file() and sq_path.stat().st_size > 0:
            return sq_path

        raw_path = self._download_thumbnail(video_id)
        if raw_path is None:
            return None
        return trim_and_crop_square(raw_path, out_path=sq_path, size=size)

    def render_file(self, img_path: Path, width: int = 14, height_cells: int = 7) -> str:
        """Convert a local image file to width x height_cells Truecolor half-block Rich markup."""
        px_h = height_cells * 2
        expected = width * px_h * 3
        try:
            pil_img = _crop_and_resize_pil(
                img_path,
                target_w=width,
                target_h=px_h,
                sharpen=1.30,
                contrast=1.06,
                color=1.12,
            )
            raw = pil_img.tobytes()
            if len(raw) == expected:
                return _rgb_bytes_to_halfblock(raw, width=width, height_cells=height_cells)
        except Exception:
            pass

        cmd = [
            "ffmpeg",
            "-v",
            "error",
            "-i",
            str(img_path),
            "-vframes",
            "1",
            "-vf",
            f"crop=min(iw\\,ih):min(iw\\,ih),scale={width}:{px_h}:flags=lanczos",
            "-f",
            "rawvideo",
            "-pix_fmt",
            "rgb24",
            "-",
        ]
        try:
            raw = subprocess.check_output(cmd, stderr=subprocess.DEVNULL, timeout=5.0)
        except (OSError, subprocess.SubprocessError):
            return self.placeholder_markup(width=width, height_cells=height_cells)

        if len(raw) != expected:
            return self.placeholder_markup(width=width, height_cells=height_cells)
        return _rgb_bytes_to_halfblock(raw, width=width, height_cells=height_cells)

    def fetch_and_render(
        self, video_id: str, width: int = 14, height_cells: int = 7
    ) -> str:
        """Download thumbnail if needed, prepare square PNG, render half-block markup, and cache."""
        placeholder = self.placeholder_markup(width=width, height_cells=height_cells)
        if not video_id:
            return placeholder
        cached = self.get_cached_markup(video_id, width=width, height_cells=height_cells)
        if cached is not None:
            return cached

        sq_path = self.prepare_square_image(video_id)
        src_path = sq_path if sq_path is not None else self._download_thumbnail(video_id)
        if src_path is None:
            return placeholder

        markup = self.render_file(src_path, width=width, height_cells=height_cells)
        if markup != placeholder:
            with self._lock:
                self._markup_cache[(video_id, width, height_cells)] = markup
        return markup

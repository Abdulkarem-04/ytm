# AGENTS.md - ytm

## Overview
`ytm` is a high-contrast terminal YouTube Music player with both a Textual TUI and a Rich CLI. It streams audio through a persistent background `mpv` JSON-IPC daemon (`/tmp/ytm-mpv.sock`) so playback survives closing the TUI, with offline caching, gapless next-track preloading, playlists, and terminal cover art.

## Tech Stack & Layout
- Python `>=3.11` in `.venv/` (`ytmusicapi`, `yt-dlp`, `textual`, `rich`, `dbus-fast`, `pillow`, `numpy`, `pytest`), symlinked to `~/.local/bin/ytm`.
- System binaries used at runtime: `mpv` (audio daemon), `parec` + `pactl` (PipeWire FFT tap), and `ffmpeg` (fallback cover art).
- `src/ytm/cli.py`: `argparse` + `rich` CLI entry point (`ytm.cli:main`). Running `ytm` with no args opens the TUI in a TTY (`--focus` launches Focus Mode) or prints status otherwise.
- `src/ytm/tui.py`: 6-view Textual TUI (`[1]` Active Queue, `[2]` Search Results, `[3]` Liked Music, `[4]` Local Cache, `[5]` Playlists, `[6]` Settings), plus single-key Focus Mode (`z`) and side-by-side Synced Lyrics (`y`).
- `src/ytm/player.py`: `MPVPlayer` managing the persistent `mpv` daemon, gapless buffers, hot-swap preloads, and queue state persistence.
- `src/ytm/mpris.py`: Event-driven MPRIS2 D-Bus bridge (`org.mpris.MediaPlayer2.ytm`) for global hardware media keys and KDE Plasma controls.
- `src/ytm/spectrum.py`: `PipeWireSpectrumAnalyzer` and `SpectrumDSP` driving live volume-independent beat-reactive FFT bars.
- `src/ytm/lyrics.py`: Multi-source synced lyrics fetcher (`LyricsProvider`: YouTube Music `TimedLyrics` + `lrclib.net` LRC fallback) with offline disk caching.
- `src/ytm/api.py`, `auth.py`, `resolver.py`: YouTube Music API client, header/cURL auth sanitizer, and `yt-dlp` stream resolver.
- `src/ytm/downloader.py`, `cache_worker.py`, `settings.py`: Resilient `yt-dlp` downloads, single-worker preload/auto-cache manager, and persistent settings store.
- `src/ytm/liked.py`, `playlists.py`, `art.py`, `config.py`, `models.py`: Liked store with undo stack, custom playlists, Truecolor half-block cover art renderer, paths, and `Track` model.
- `tests/test_stage1.py` through `tests/test_stage10.py`: Stage-based pytest suite (43 tests).
- Runtime user data (outside repo): `~/.config/ytm/` (auth, liked, playlists, `config.json`, `state.json`), `~/.cache/ytm/` (covers, preloads, lyrics, stream cache), `~/Music/ytm/` (offline tracks).

## Common Commands
- Install editable with dev tools: `.venv/bin/pip install -e ".[dev]"`
- Run test suite: `.venv/bin/pytest -v`
- Launch TUI / inspect CLI: `ytm` or `.venv/bin/ytm --help`
- Check auth / batch-cache liked tracks: `ytm auth --status`, `ytm cache-liked`

## Project Conventions & Gotchas
- **Bundled `yt-dlp`:** `yt-dlp` lives in `.venv/bin/yt-dlp` (not necessarily system PATH). `config.get_ytdlp_path()` passes it to `mpv` via `--script-opts=ytdl_hook-ytdl_path=...`.
- **`mpv` IPC quirks (v0.41+):** Use `["playlist-play-index", idx]` with readiness polling (PipeWire warm-up takes ~200-250ms) rather than `set_property playlist-pos`. `playlist-clear` keeps the currently playing entry at index 0 (used for zero-interruption shuffle). Hot-swapping an upcoming entry uses `loadfile append` + `playlist-move` + `playlist-remove` and should pass `expected_video_id` so stale preloads do not swap reordered tracks.
- **Rich/Textual markup escaping:** Literal square brackets inside markup blocks must be escaped as `\\[` (e.g. `\\[PLAYING]`), and all track metadata must pass through `rich.markup.escape()`.
- **Safe defaults & keybindings:** Keep `auto_cache_liked` disabled by default (`False` in `settings.py`). Keep `l`/`L` strictly add-only in the TUI (`x` removes with debounce, `u` restores via `unliked_history.json`). Playing from `[3]`/`[4]`/`[5]` stays collection-only (`radio_autoplay=False`); `R` starts radio mix.
- **MPRIS D-Bus types (`src/ytm/mpris.py`):** Omit `from __future__ import annotations` and annotate `@dbus_method` / `@dbus_property` directly with `dbus_fast.annotations` types on Python 3.14; isolate tests with `--bus-name`.
- **Auth & privacy:** `auth.sanitize_auth_headers_dict()` strips Chromium HTTP/2 pseudo-headers and `content-encoding` automatically on load and save. Never commit `headers_auth.json`, `state.json`, or `stream_cache.json`.
- **UI design:** Preserve the high-contrast neon palette on `#000000` background.

"""High-contrast btop/lazygit-style Textual TUI for ytm."""

from __future__ import annotations

import math
import threading
import time
from pathlib import Path
from typing import Any

from rich.markup import escape
from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical
from textual.widgets import DataTable, Input, Static

from ytm.api import YTMClient
from ytm.art import CoverArtRenderer
from ytm.cache_worker import BackgroundCacheManager, clean_error_text
from ytm.config import MUSIC_DIR
from ytm.downloader import TrackDownloader, dir_stats
from ytm.liked import LikedStore
from ytm.models import Track, format_duration
from ytm.player import MPVError, MPVPlayer
from ytm.playlists import PlaylistStore
from ytm.settings import SettingsStore

_AUDIO_EXTS = {".opus", ".m4a", ".webm", ".mp3", ".ogg", ".flac", ".wav"}
_VIEW_ORDER = ("queue", "search", "liked", "local", "playlists", "settings")
_SETTINGS_KEYS = (
    "auto_cache_liked",
    "preload_next",
    "preload_limit",
    "audio_quality",
    "stream_buffer_secs",
    "clear_preload",
    "restart_daemon",
    "local_stats",
)
_SPECTRUM_CHARS = " ▁▂▃▄▅▆▇█"
_SPECTRUM_ROWS = 7
_SPECTRUM_COLORS = {
    6: "#ff4d94",
    5: "#ff4d94",
    4: "#ffd43b",
    3: "#ffd43b",
    2: "#00ff87",
    1: "#00ff87",
    0: "#00e5ff",
}
_BAND_COUNT = 24
_REMOVE_COOLDOWN_SECONDS = 0.4
_SEARCH_PLACEHOLDER = "Press / to search YouTube Music, c to create a playlist..."


def _truncate(text: str, max_len: int) -> str:
    """Truncate text to max_len characters with an ASCII ellipsis."""
    if len(text) <= max_len:
        return text
    if max_len <= 3:
        return text[:max_len]
    return text[: max_len - 3] + "..."


def _build_progress_markup(position: float, duration: float, width: int = 34) -> str:
    """Render a high-contrast Rich markup progress bar."""
    ratio = 0.0 if duration <= 0 else max(0.0, min(1.0, position / duration))
    filled = int(round(ratio * (width - 1)))
    left = "━" * filled
    right = "─" * max(0, width - 1 - filled)
    return f"[#00ff87]{left}[/][bold #ffd43b]●[/][#4c6ef5]{right}[/]"


class YTMApp(App[None]):
    """Interactive multi-pane terminal music player powered by Textual."""

    TITLE = "ytm"
    CSS = """
    Screen {
        background: #000000;
        color: #ffffff;
        layout: vertical;
    }

    #top-bar {
        height: 3;
        background: #000000;
        border: round #00e5ff;
        padding: 0 1;
        layout: horizontal;
    }

    #brand-banner {
        width: 1fr;
        content-align: left middle;
    }

    #daemon-badge {
        width: auto;
        content-align: right middle;
    }

    #search-bar {
        height: 3;
        background: #000000;
        border: round #4c6ef5;
        padding: 0 1;
    }

    #search-bar:focus-within {
        border: round #ffd43b;
    }

    #search-input {
        background: #000000;
        color: #ffffff;
        border: none;
        padding: 0;
        height: 1;
    }

    #workspace {
        height: 1fr;
        background: #000000;
        layout: horizontal;
    }

    #sidebar {
        width: 32;
        background: #000000;
        layout: vertical;
    }

    #nav-panel {
        height: auto;
        background: #000000;
        border: round #00e5ff;
        padding: 0 1;
    }

    #keys-panel {
        height: 1fr;
        overflow-y: auto;
        background: #000000;
        border: round #4c6ef5;
        padding: 0 1;
    }

    #main-pane {
        width: 1fr;
        background: #000000;
        border: round #00ff87;
        padding: 0 1;
        layout: vertical;
    }

    #track-table, #settings-table {
        height: 1fr;
        background: #000000;
        color: #ffffff;
    }

    #settings-table {
        display: none;
    }

    #track-table > .datatable--header, #settings-table > .datatable--header {
        background: #0a1428;
        color: #ffd43b;
        text-style: bold;
    }

    #track-table > .datatable--cursor, #settings-table > .datatable--cursor {
        background: #103252;
        color: #ffffff;
        text-style: bold;
    }

    #track-table > .datatable--even-row, #settings-table > .datatable--even-row {
        background: #05080f;
    }

    #track-table > .datatable--odd-row, #settings-table > .datatable--odd-row {
        background: #000000;
    }

    #main-status {
        height: 1;
        background: #070c16;
        color: #00e5ff;
        padding: 0 1;
    }

    #bottom-dock {
        height: 9;
        background: #000000;
        layout: horizontal;
    }

    #cover-box {
        width: 18;
        height: 9;
        background: #000000;
        border: round #00e5ff;
        padding: 0 1;
    }

    #now-playing-box {
        width: 2fr;
        height: 9;
        background: #000000;
        border: round #00ff87;
        padding: 0 1;
    }

    #visualizer-box {
        width: 1fr;
        min-width: 34;
        height: 9;
        background: #000000;
        border: round #ff4d94;
        padding: 0 1;
    }
    """

    BINDINGS = [
        Binding("slash", "focus_search", "Search", priority=True),
        Binding("escape", "focus_table", "Track List", priority=True),
        Binding("1", "view_queue", "Queue"),
        Binding("2", "view_search", "Search"),
        Binding("3", "view_liked", "Liked"),
        Binding("4", "view_local", "Local"),
        Binding("5", "view_playlists", "Playlists"),
        Binding("6", "view_settings", "Settings"),
        Binding("tab", "next_view", "Cycle View", priority=True),
        Binding("j", "cursor_down", "Down"),
        Binding("k", "cursor_up", "Up"),
        Binding("g", "cursor_top", "Top"),
        Binding("G", "cursor_bottom", "Bottom"),
        Binding("o", "play_selected", "Play"),
        Binding("a", "play_view_queue", "Play View"),
        Binding("R", "start_radio", "Radio Mix"),
        Binding("space", "toggle_pause", "Play/Pause"),
        Binding("n", "next_track", "Next"),
        Binding("p", "prev_track", "Prev"),
        Binding("r", "replay_track", "Replay"),
        Binding("l", "like_current", "Like Playing"),
        Binding("L", "like_selected", "Like Selected"),
        Binding("x", "remove_selected", "Remove"),
        Binding("u", "undo_unlike", "Undo Like"),
        Binding("c", "create_playlist", "New Playlist"),
        Binding("m", "add_selected_to_playlist", "Add Selected"),
        Binding("M", "add_current_to_playlist", "Add Playing"),
        Binding("comma", "prev_playlist", "Prev Playlist"),
        Binding("full_stop", "next_playlist", "Next Playlist"),
        Binding("d", "download_current", "Cache Playing"),
        Binding("D", "download_selected", "Cache Selected"),
        Binding("s", "sync_liked", "Sync Likes"),
        Binding("left_square_bracket", "seek_back", "Seek -5s"),
        Binding("right_square_bracket", "seek_forward", "Seek +5s"),
        Binding("minus", "vol_down", "Vol -5%"),
        Binding("plus", "vol_up", "Vol +5%"),
        Binding("equals_sign", "vol_up", "Vol +5%"),
        Binding("q", "detach_quit", "Detach"),
        Binding("Q", "stop_quit", "Stop+Quit"),
    ]

    def __init__(
        self,
        player: MPVPlayer | None = None,
        client: YTMClient | None = None,
        liked_store: LikedStore | None = None,
        playlist_store: PlaylistStore | None = None,
        downloader: TrackDownloader | None = None,
        art_renderer: CoverArtRenderer | None = None,
        settings_store: SettingsStore | None = None,
        cache_manager: BackgroundCacheManager | None = None,
        music_dir: Path = MUSIC_DIR,
    ) -> None:
        super().__init__()
        self.settings_store = settings_store or SettingsStore()
        self.liked_store = liked_store or LikedStore()
        self.playlist_store = playlist_store or PlaylistStore()
        self.player = player or MPVPlayer(
            liked_store=self.liked_store,
            settings_store=self.settings_store,
        )
        self.client = client or YTMClient(liked_store=self.liked_store)
        self.music_dir = music_dir
        self.downloader = downloader or TrackDownloader(
            music_dir=self.music_dir,
            settings_store=self.settings_store,
        )
        self.art_renderer = art_renderer or CoverArtRenderer()
        self.cache_manager = cache_manager or BackgroundCacheManager(
            downloader=self.downloader,
            player=self.player,
            liked_store=self.liked_store,
            playlist_store=self.playlist_store,
            settings_store=self.settings_store,
            on_track_cached=self._on_bg_track_cached,
        )

        self.active_view: str = "queue"
        self.last_query: str = ""
        self.queue_tracks: list[Track] = []
        self.search_tracks: list[Track] = []
        self.liked_tracks: list[Track] = []
        self.local_tracks: list[Track] = []
        self.playlist_tracks: list[Track] = []
        self.active_queue_pos: int = -1
        self.radio_autoplay: bool = self.player.load_radio_autoplay()

        self.last_status: dict[str, Any] = {"running": False}
        self._table_signature: tuple[Any, ...] | None = None
        self._settings_signature: tuple[Any, ...] | None = None
        self._bands: list[float] = [0.03] * _BAND_COUNT
        self._vis_phase: float = 0.0
        self._refilling_for_video_id: str | None = None
        self._input_mode: str = "search"
        self._last_remove_ts: float = 0.0
        self._cover_video_id: str | None = None

    def compose(self) -> ComposeResult:
        with Horizontal(id="top-bar"):
            yield Static(
                "[bold #00e5ff]ytm[/] [bold #ffffff]// TERMINAL MUSIC PLAYER[/]",
                id="brand-banner",
            )
            yield Static("", id="daemon-badge")

        with Container(id="search-bar"):
            yield Input(placeholder=_SEARCH_PLACEHOLDER, id="search-input")

        with Horizontal(id="workspace"):
            with Vertical(id="sidebar"):
                yield Static("", id="nav-panel")
                yield Static(self._render_keys_markup(), id="keys-panel")
            with Vertical(id="main-pane"):
                yield DataTable(id="track-table", cursor_type="row", zebra_stripes=True)
                yield DataTable(id="settings-table", cursor_type="row", zebra_stripes=True)
                yield Static(
                    "[#00e5ff]Ready.[/] Press [bold #ffd43b]/[/] to search, "
                    "[bold #ffd43b]c[/] new playlist, [bold #ffd43b]6[/] settings, [bold #ffd43b]q[/] detach.",
                    id="main-status",
                )

        with Horizontal(id="bottom-dock"):
            yield Static("", id="cover-box")
            yield Static("", id="now-playing-box")
            yield Static("", id="visualizer-box")

    def on_mount(self) -> None:
        self.query_one("#nav-panel", Static).border_title = "|> Views"
        self.query_one("#keys-panel", Static).border_title = "Keybindings"
        cover_box = self.query_one("#cover-box", Static)
        cover_box.border_title = "Cover"
        cover_box.update(self.art_renderer.placeholder_markup())
        self.query_one("#now-playing-box", Static).border_title = "|> Now Playing"
        self.query_one("#visualizer-box", Static).border_title = "Spectrum"

        table = self.query_one("#track-table", DataTable)
        table.add_columns("#", "Title", "Artist", "Album", "Duration")

        settings_table = self.query_one("#settings-table", DataTable)
        settings_table.add_columns("Setting", "Value", "Description")
        settings_table.display = False

        table.focus()

        self._reload_collections()
        self._refresh_status_tick()
        self._tick_visualizer()
        self.cache_manager.start()

        self.set_interval(0.25, self._refresh_status_tick)
        self.set_interval(0.08, self._tick_visualizer)

    def on_unmount(self) -> None:
        self.cache_manager.stop()

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        if isinstance(self.focused, Input) and action != "focus_table":
            return False
        return True

    def _render_keys_markup(self) -> str:
        return "\n".join(
            [
                "[bold #ffd43b]1-6/Tab[/] [#ffffff]Views[/]   [bold #ffd43b]j/k[/] [#ffffff]Move[/]",
                "[bold #ffd43b]g / G[/]   [#ffffff]Top/End[/] [bold #ffd43b]/[/]   [#ffffff]Search[/]",
                "[bold #ffd43b]Enter[/]   [#ffffff]Play[/]    [bold #ffd43b]R[/]   [#ffffff]Radio[/]",
                "[bold #ffd43b]a[/]       [#ffffff]ViewQ[/]   [bold #ffd43b]Spc[/] [#ffffff]Pause[/]",
                "[bold #ffd43b]n / p[/]   [#ffffff]Next / Prev[/]",
                "[bold #ffd43b]r[/]       [#ffffff]Replay (00:00)[/]",
                "[bold #ffd43b]l / L[/]   [#ffffff]Add Like (No Del)[/]",
                "[bold #ffd43b]x / u[/]   [#ffffff]Remove / Undo Like[/]",
                "[bold #ffd43b]c / m[/]   [#ffffff]New / Add Playlist[/]",
                "[bold #ffd43b], / .[/]   [#ffffff]Switch Playlist[/]",
                "[bold #ffd43b]d / s[/]   [#ffffff]Cache / Sync YTM[/]",
                "[bold #ffd43b]\\[ ] - +[/] [#ffffff]Seek/Vol[/] [bold #ffd43b]q/Q[/] [#ffffff]Quit[/]",
            ]
        )

    def _scan_local_tracks(self) -> list[Track]:
        if not self.music_dir.is_dir():
            return []

        known_by_id: dict[str, Track] = {}
        for t in self.queue_tracks + self.liked_tracks + self.playlist_tracks:
            if t.video_id:
                known_by_id[t.video_id] = t

        tracks: list[Track] = []
        for path in sorted(self.music_dir.iterdir()):
            if not path.is_file() or path.suffix.lower() not in _AUDIO_EXTS:
                continue
            vid = path.stem
            meta = known_by_id.get(vid)
            if meta:
                tracks.append(
                    Track(
                        video_id=vid,
                        title=meta.title,
                        artist=meta.artist,
                        album=meta.album,
                        duration=meta.duration,
                        duration_seconds=meta.duration_seconds,
                        is_liked=meta.is_liked,
                        local_path=str(path),
                    )
                )
            else:
                tracks.append(
                    Track(
                        video_id=vid,
                        title=vid,
                        artist="Local File",
                        album="Local Cache",
                        duration="0:00",
                        local_path=str(path),
                    )
                )
        return self.liked_store.annotate(tracks)

    def _reload_collections(self) -> None:
        self.queue_tracks = self.player.load_queue()
        self.liked_tracks = self.liked_store.list_tracks()
        self.playlist_tracks = self.liked_store.annotate(self.playlist_store.get_tracks())
        self.local_tracks = self._scan_local_tracks()
        self.liked_store.annotate(self.search_tracks)

    def _current_view_tracks(self) -> list[Track]:
        if self.active_view == "queue":
            return self.queue_tracks
        if self.active_view == "search":
            return self.search_tracks
        if self.active_view == "liked":
            return self.liked_tracks
        if self.active_view == "playlists":
            return self.playlist_tracks
        return self.local_tracks

    def _view_title(self) -> str:
        if self.active_view == "queue":
            return "|> [1] Active Queue"
        if self.active_view == "search":
            suffix = f': "{self.last_query}"' if self.last_query else ""
            return f"|> [2] Search Results{suffix}"
        if self.active_view == "liked":
            return "|> [3] Liked Music"
        if self.active_view == "playlists":
            pl_name = self.playlist_store.get_active_name() or "None"
            return f"|> [5] Playlist: {pl_name}"
        if self.active_view == "settings":
            return "|> [6] Settings"
        return "|> [4] Local Cache"

    def _update_nav_panel(self) -> None:
        pl_name = self.playlist_store.get_active_name()
        items: list[tuple[str, str, str, int | None]] = [
            ("queue", "1", "Active Queue", len(self.queue_tracks)),
            ("search", "2", "Search Results", len(self.search_tracks)),
            ("liked", "3", "Liked Music", len(self.liked_tracks)),
            ("local", "4", "Local Cache", len(self.local_tracks)),
            ("playlists", "5", "Playlists", len(self.playlist_tracks)),
            ("settings", "6", "Settings", None),
        ]
        lines: list[str] = []
        for key, num, label, count in items:
            count_suffix = f" [bold #ffd43b]({count})[/]" if (count is not None and key == self.active_view) else (
                f" [#4c6ef5]({count})[/]" if count is not None else ""
            )
            if key == self.active_view:
                lines.append(
                    f"[bold #00ff87]|>[/] [bold #ffffff]\\[{num}] {label}[/]{count_suffix}"
                )
            else:
                lines.append(f"   [#00e5ff]\\[{num}] {label}[/]{count_suffix}")
        pl_display = escape(_truncate(pl_name or "(press c to create)", 20))
        lines.append(f"   [bold #ff4d94]PL:[/] [#ffffff]{pl_display}[/]")
        self.query_one("#nav-panel", Static).update("\n".join(lines))

    def _refresh_settings_table(self, force: bool = False) -> None:
        settings = self.settings_store.load()
        prog = self.cache_manager.get_progress()
        preload_count, preload_bytes = dir_stats(self.downloader.preload_dir)
        local_count, local_bytes = dir_stats(self.downloader.music_dir)

        sig = (
            settings.to_dict(),
            prog["cached_liked"],
            prog["total_liked"],
            prog["caching_active"],
            preload_count,
            preload_bytes,
            local_count,
            local_bytes,
        )
        if not force and sig == self._settings_signature:
            return
        self._settings_signature = sig

        main_pane = self.query_one("#main-pane", Vertical)
        main_pane.border_title = "|> [6] Settings"

        table = self.query_one("#settings-table", DataTable)
        prev_row = table.cursor_row
        table.clear()

        auto_val = (
            f"[ON] ({prog['cached_liked']}/{prog['total_liked']})"
            if settings.auto_cache_liked
            else f"[OFF] ({prog['cached_liked']}/{prog['total_liked']})"
        )
        auto_style = "bold #00ff87" if settings.auto_cache_liked else "bold #ff4d94"
        preload_val = "[ON]" if settings.preload_next else "[OFF]"
        preload_style = "bold #00ff87" if settings.preload_next else "bold #ff4d94"
        quality_val = "Data Saver (<=96k)" if settings.audio_quality == "saver" else "Best Audio"
        preload_mb = preload_bytes / (1024 * 1024)
        local_mb = local_bytes / (1024 * 1024)

        rows = [
            (
                "Auto-Cache Liked Music",
                Text(auto_val, style=auto_style),
                "Sequentially cache all Liked songs to ~/Music/ytm",
            ),
            (
                "Gapless Next-Track Preload",
                Text(preload_val, style=preload_style),
                "Pre-download upcoming streamed track to remove gaps",
            ),
            (
                "Preload Temp Cache Cap",
                Text(f"{settings.preload_limit} tracks", style="bold #ffd43b"),
                "Max temporary files in ~/.cache/ytm/preload",
            ),
            (
                "Audio Quality",
                Text(quality_val, style="bold #ffd43b"),
                "Lower bitrate Opus stream for slow connections",
            ),
            (
                "Stream RAM Buffer",
                Text(f"{settings.stream_buffer_secs}s", style="bold #ffd43b"),
                "mpv RAM readahead buffer + auto-reconnect",
            ),
            (
                "[Action] Clear Temp Preload",
                Text(f"{preload_count} files ({preload_mb:.1f} MB)", style="bold #00e5ff"),
                "Delete temporary files in ~/.cache/ytm/preload",
            ),
            (
                "[Action] Restart mpv Daemon",
                Text("[RUN]", style="bold #00ff87"),
                "Apply quality and buffer changes cleanly",
            ),
            (
                "[Info] Permanent Local Cache",
                Text(f"{local_count} files ({local_mb:.1f} MB)", style="bold #00ff87"),
                "~/Music/ytm (auto-cached Liked + manual d/D)",
            ),
        ]
        for label, val_text, desc in rows:
            table.add_row(
                Text(label, style="bold #ffffff"),
                val_text,
                Text(desc, style="#00e5ff"),
            )

        target_row = max(0, min(prev_row, len(rows) - 1))
        table.move_cursor(row=target_row)
        self._update_nav_panel()

    def _refresh_table(self, force: bool = False) -> None:
        tracks = self._current_view_tracks()
        current_track: Track | None = self.last_status.get("current_track")
        current_vid = current_track.video_id if current_track else None
        active_idx = self.active_queue_pos if self.active_view == "queue" else -1
        active_pl = self.playlist_store.get_active_name()
        sig = (
            self.active_view,
            self.last_query,
            active_pl,
            active_idx,
            current_vid if self.active_view != "queue" else None,
            tuple((t.video_id, t.title, t.is_liked, bool(t.local_path)) for t in tracks),
        )
        if not force and sig == self._table_signature:
            return
        self._table_signature = sig

        main_pane = self.query_one("#main-pane", Vertical)
        main_pane.border_title = self._view_title()

        table = self.query_one("#track-table", DataTable)
        prev_row = table.cursor_row
        table.clear()

        for idx, track in enumerate(tracks):
            if self.active_view == "queue":
                is_active = idx == self.active_queue_pos
            else:
                is_active = current_vid is not None and track.video_id == current_vid
            num_text = Text(
                f"|>{idx + 1}" if is_active else str(idx + 1),
                style="bold #00ff87" if is_active else "bold #ffd43b",
            )
            suffix_len = (8 if track.is_liked else 0) + (8 if track.local_path else 0)
            title_display = _truncate(track.title, max(12, 42 - suffix_len))
            title_text = Text(
                title_display,
                style="bold #00ff87" if is_active else "bold #ffffff",
            )
            if track.is_liked:
                title_text.append(" [LIKED]", style="bold #ff4d94")
            if track.local_path:
                title_text.append(" [LOCAL]", style="bold #00e5ff")

            artist_text = Text(_truncate(track.artist, 22), style="#00e5ff")
            album_text = Text(_truncate(track.album or "-", 22), style="#ff4d94")
            dur_text = Text(track.duration, style="#00ff87")
            table.add_row(num_text, title_text, artist_text, album_text, dur_text)

        if tracks:
            target_row = max(0, min(prev_row, len(tracks) - 1))
            table.move_cursor(row=target_row)

        self._update_nav_panel()

    def _set_status(self, message: str) -> None:
        self.query_one("#main-status", Static).update(message)

    def _update_cover_art(self, track: Track | None) -> None:
        cover_box = self.query_one("#cover-box", Static)
        if track is None or not track.video_id:
            if self._cover_video_id is not None:
                self._cover_video_id = None
                cover_box.update(self.art_renderer.placeholder_markup())
            return

        vid = track.video_id
        if vid == self._cover_video_id:
            return
        self._cover_video_id = vid
        cached = self.art_renderer.get_cached_markup(vid)
        if cached is not None:
            cover_box.update(cached)
            return

        cover_box.update(self.art_renderer.placeholder_markup())

        def _fetch_bg() -> None:
            markup = self.art_renderer.fetch_and_render(vid)

            def _apply() -> None:
                if self._cover_video_id == vid:
                    self.query_one("#cover-box", Static).update(markup)

            self._safe_ui(_apply)

        self._run_bg(_fetch_bg)

    def _update_top_and_now_playing(self, status: dict[str, Any]) -> None:
        running = bool(status.get("running"))
        paused = bool(status.get("paused", False))
        track: Track | None = status.get("current_track")

        if not running:
            daemon_badge = "[bold #ff4d94]\\[DAEMON: STOPPED][/]"
        elif paused:
            daemon_badge = "[bold #ffd43b]\\[DAEMON: PAUSED][/]"
        else:
            daemon_badge = "[bold #00ff87]\\[DAEMON: PLAYING][/]"

        auth_badge = (
            "[bold #00ff87]\\[SYNCED][/]"
            if self.client.authenticated
            else "[bold #00e5ff]\\[GUEST MODE][/]"
        )
        prog = self.cache_manager.get_progress()
        cache_badge = (
            f"  [bold #ffd43b]\\[CACHING: {prog['cached_liked']}/{prog['total_liked']}][/]"
            if prog["caching_active"]
            else ""
        )
        self.query_one("#daemon-badge", Static).update(
            f"{daemon_badge}  {auth_badge}{cache_badge}"
        )
        self._update_cover_art(track if running else None)

        now_box = self.query_one("#now-playing-box", Static)
        if not running or track is None:
            state_line = (
                "[bold #ff4d94]\\[STOPPED][/] [#ffffff]No active playback session.[/]"
                if not running
                else "[bold #ffd43b]\\[IDLE][/] [#ffffff]Daemon ready. Select a track and press Enter.[/]"
            )
            now_box.border_title = "|> Now Playing"
            now_box.update(
                "\n".join(
                    [
                        state_line,
                        "[#00e5ff]Tip:[/] Press [bold #ffd43b]/[/] to search, [bold #ffd43b]3[/] Liked Music, or [bold #ffd43b]5[/] Playlists.",
                        f"{_build_progress_markup(0.0, 0.0)}  [bold #ffffff]00:00 / 00:00[/]",
                    ]
                )
            )
            return

        state_tag = "[bold #ffd43b]\\[PAUSED][/]" if paused else "[bold #00ff87]\\[PLAYING][/]"
        source_tag = (
            "[bold #00ff87]\\[LOCAL][/]"
            if track.local_path
            else "[bold #00e5ff]\\[STREAM][/]"
        )
        liked_tag = " [bold #ff4d94]\\[LIKED][/]" if track.is_liked else ""
        mode_tag = (
            "[bold #00e5ff]\\[RADIO AUTO][/]"
            if self.radio_autoplay
            else "[bold #00ff87]\\[COLLECTION ONLY][/]"
        )

        time_pos = float(status.get("time_pos", 0.0) or 0.0)
        duration = float(status.get("duration", 0.0) or 0.0)
        volume = int(round(float(status.get("volume", 100.0) or 100.0)))
        pos_idx = int(status.get("playlist_pos", -1))
        total_count = int(status.get("playlist_count", 0) or 0)
        track_counter = (
            f"{pos_idx + 1}/{total_count}" if pos_idx >= 0 and total_count > 0 else "-/-"
        )

        bar = _build_progress_markup(time_pos, duration)
        timing = f"{format_duration(time_pos)} / {format_duration(duration)}"
        title_safe = escape(track.title)
        artist_safe = escape(track.artist)
        album_safe = escape(track.album or "-")
        pl_active = self.playlist_store.get_active_name()
        pl_info = f"   [bold #ff4d94]PL:[/] [#ffffff]{escape(pl_active)}[/]" if pl_active else ""

        now_box.border_title = "|> Now Playing"
        now_box.update(
            "\n".join(
                [
                    f"{state_tag} {source_tag}{liked_tag}  [bold #ffffff]{title_safe}[/]",
                    f"[bold #00e5ff]Artist:[/] [bold #ffffff]{artist_safe}[/]   [bold #ff4d94]Album:[/] [#ffffff]{album_safe}[/]",
                    f"{bar}  [bold #ffffff]{timing}[/]  [bold #ffd43b]Vol: {volume}%[/]",
                    f"{mode_tag}  [bold #00e5ff]Track: {track_counter}[/]{pl_info}",
                ]
            )
        )

    def _refill_radio_bg(self, seed_video_id: str) -> None:
        try:
            radio = self.client.get_radio_queue(seed_video_id, limit=15)
        except Exception:
            return
        if not radio:
            return
        added = self.player.append_tracks(radio)
        if added <= 0:
            return

        def _done() -> None:
            self._reload_collections()
            self._refresh_table(force=True)
            self._set_status(f"[#00e5ff]Auto-queued {added} more radio tracks.[/]")

        self._safe_ui(_done)

    def _refresh_status_tick(self) -> None:
        status = self.player.get_status()
        self.last_status = status

        if status.get("running"):
            loaded_queue = status.get("queue") or []
            if loaded_queue:
                self.queue_tracks = loaded_queue
            self.radio_autoplay = bool(status.get("radio_autoplay", self.radio_autoplay))
            pos_idx = int(status.get("playlist_pos", -1))
            total_count = int(status.get("playlist_count", 0) or 0)
            self.active_queue_pos = pos_idx

            if (
                self.settings_store.load().preload_next
                and 0 <= pos_idx < len(self.queue_tracks) - 1
            ):
                self.cache_manager.request_preload(
                    self.queue_tracks[pos_idx + 1], pos_idx + 1
                )

            current_track: Track | None = status.get("current_track")
            if (
                self.radio_autoplay
                and total_count > 0
                and pos_idx == total_count - 1
                and current_track is not None
                and current_track.video_id
                and self._refilling_for_video_id != current_track.video_id
            ):
                self._refilling_for_video_id = current_track.video_id
                seed_id = current_track.video_id
                self._run_bg(lambda: self._refill_radio_bg(seed_id))
        else:
            self.active_queue_pos = -1

        self._update_top_and_now_playing(status)
        if self.active_view == "settings":
            self._refresh_settings_table()
        else:
            self._refresh_table()

    def _tick_visualizer(self) -> None:
        status = self.last_status
        running = bool(status.get("running"))
        paused = bool(status.get("paused", False))
        core_idle = bool(status.get("core_idle", True))
        track: Track | None = status.get("current_track")

        active = running and not paused and not core_idle and track is not None
        if active and track is not None:
            self._vis_phase += 0.38
            time_pos = float(status.get("time_pos", 0.0) or 0.0)
            seed = sum(ord(ch) for ch in track.video_id) * 0.13
            t = time_pos * 3.1 + self._vis_phase
            for i in range(_BAND_COUNT):
                freq = 0.35 + (i / _BAND_COUNT) * 1.4
                envelope = 0.72 + 0.28 * math.sin(i * 0.27 + seed)
                wave_a = 0.5 + 0.5 * math.sin(t * freq + i * 0.48 + seed)
                wave_b = 0.5 + 0.5 * math.cos(t * (freq * 0.63) - i * 0.31)
                target = max(0.08, min(1.0, (0.58 * wave_a + 0.42 * wave_b) * envelope))
                self._bands[i] = self._bands[i] * 0.42 + target * 0.58
        else:
            for i in range(_BAND_COUNT):
                self._bands[i] = max(0.02, self._bands[i] * 0.70)

        max_units = _SPECTRUM_ROWS * 8
        rows: list[str] = []
        for row_idx in range(_SPECTRUM_ROWS - 1, -1, -1):
            color = _SPECTRUM_COLORS[row_idx]
            chars: list[str] = []
            for height in self._bands:
                units = max(1, min(max_units, int(round(height * max_units))))
                row_units = max(0, min(8, units - row_idx * 8))
                chars.append(_SPECTRUM_CHARS[row_units])
            rows.append(f"[{color}]{''.join(chars)}[/]")

        self.query_one("#visualizer-box", Static).update("\n".join(rows))

    def _apply_local_path(self, video_id: str, local_str: str) -> None:
        for col in (
            self.queue_tracks,
            self.search_tracks,
            self.liked_tracks,
            self.local_tracks,
            self.playlist_tracks,
        ):
            for item in col:
                if item.video_id == video_id:
                    item.local_path = local_str
        self._reload_collections()
        if self.active_view == "settings":
            self._refresh_settings_table(force=True)
        else:
            self._refresh_table(force=True)

    def _on_bg_track_cached(self, track: Track, local_str: str) -> None:
        self._safe_ui(self._apply_local_path, track.video_id, local_str)

    def _active_table(self) -> DataTable:
        if self.active_view == "settings":
            return self.query_one("#settings-table", DataTable)
        return self.query_one("#track-table", DataTable)

    def _switch_view(self, view: str) -> None:
        if view not in _VIEW_ORDER:
            return
        self.active_view = view
        track_table = self.query_one("#track-table", DataTable)
        settings_table = self.query_one("#settings-table", DataTable)
        if view == "settings":
            track_table.display = False
            settings_table.display = True
            self._refresh_settings_table(force=True)
            settings_table.focus()
        else:
            settings_table.display = False
            track_table.display = True
            self._reload_collections()
            self._refresh_table(force=True)
            track_table.focus()

    def _selected_track(self) -> tuple[int, Track] | None:
        tracks = self._current_view_tracks()
        if not tracks:
            return None
        table = self.query_one("#track-table", DataTable)
        row = table.cursor_row
        if 0 <= row < len(tracks):
            return row, tracks[row]
        return None

    def _run_bg(self, fn) -> None:
        threading.Thread(target=fn, daemon=True).start()

    def _safe_ui(self, fn, *args: Any) -> None:
        if self.is_running:
            try:
                self.call_from_thread(fn, *args)
            except RuntimeError:
                pass

    def _play_track_with_radio_bg(self, seed_track: Track) -> None:
        self._safe_ui(
            self._set_status,
            f"[#00e5ff]Queueing[/] [bold #ffffff]{escape(seed_track.title)}[/] [#00e5ff]+ Up Next radio...[/]",
        )
        queue: list[Track] = [seed_track]
        try:
            radio = self.client.get_radio_queue(seed_track.video_id, limit=15)
        except Exception:
            radio = []

        seen = {seed_track.video_id}
        for item in radio:
            if item.video_id not in seen:
                seen.add(item.video_id)
                queue.append(item)

        try:
            self.player.play_queue(queue, start_index=0, radio_autoplay=True)
        except (MPVError, ValueError) as exc:
            self._safe_ui(self._set_status, f"[bold #ff4d94]Playback error:[/] {escape(str(exc))}")
            return

        def _done() -> None:
            self.radio_autoplay = True
            self._refilling_for_video_id = None
            self._reload_collections()
            self.active_view = "queue"
            self._refresh_status_tick()
            self._refresh_table(force=True)
            self._set_status(
                f"[bold #00ff87]|> Radio Mix:[/] [bold #ffffff]{escape(seed_track.title)}[/] "
                f"[#00e5ff]({len(queue)} tracks)[/]"
            )

        self._safe_ui(_done)

    def _play_queue_bg(
        self,
        tracks: list[Track],
        start_index: int,
        stay_in_view: bool = True,
        radio_autoplay: bool = False,
    ) -> None:
        try:
            self.player.play_queue(tracks, start_index=start_index, radio_autoplay=radio_autoplay)
        except (MPVError, ValueError) as exc:
            self._safe_ui(self._set_status, f"[bold #ff4d94]Playback error:[/] {escape(str(exc))}")
            return

        chosen = tracks[start_index]

        def _done() -> None:
            self.radio_autoplay = radio_autoplay
            self._refilling_for_video_id = None
            self._reload_collections()
            if not stay_in_view:
                self.active_view = "queue"
            self._refresh_status_tick()
            self._refresh_table(force=True)
            self._set_status(
                f"[bold #00ff87]|> Playing collection[/] from [bold #ffffff]{escape(chosen.title)}[/] "
                f"[#00e5ff]({len(tracks)} tracks, radio off)[/]"
            )

        self._safe_ui(_done)

    def _search_bg(self, query: str) -> None:
        try:
            results = self.client.search_tracks(query, limit=25)
        except Exception as exc:
            self._safe_ui(self._set_status, f"[bold #ff4d94]Search error:[/] {escape(str(exc))}")
            return

        def _done() -> None:
            self.last_query = query
            self.search_tracks = results
            self.active_view = "search"
            self._refresh_table(force=True)
            self.query_one("#track-table", DataTable).focus()
            self._set_status(
                f"[bold #00ff87]Found {len(results)} tracks[/] for [bold #ffffff]{escape(query)}[/]. "
                "Press [bold #ffd43b]Enter[/] to play."
            )

        self._safe_ui(_done)

    def _reset_input_to_search(self) -> None:
        self._input_mode = "search"
        inp = self.query_one("#search-input", Input)
        inp.placeholder = _SEARCH_PLACEHOLDER

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id != "search-input":
            return
        val = event.value.strip()
        if self._input_mode == "playlist_create":
            event.input.value = ""
            self._reset_input_to_search()
            self.query_one("#track-table", DataTable).focus()
            if not val:
                self._set_status("[bold #ffd43b]\\[PLAYLIST][/] Cancelled.")
                return
            created = self.playlist_store.create(val)
            self.active_view = "playlists"
            self._reload_collections()
            self._refresh_table(force=True)
            self._set_status(
                f"[bold #00ff87]\\[PLAYLIST][/] Created '[bold #ffffff]{escape(created)}[/]' - "
                "press [bold #ffd43b]m[/] on any song to add tracks."
            )
            return

        if not val:
            self._active_table().focus()
            return
        self._set_status(f"[#00e5ff]Searching YouTube Music for[/] [bold #ffffff]{escape(val)}[/]...")
        self._run_bg(lambda: self._search_bg(val))

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if event.data_table.id == "settings-table":
            self.action_activate_setting()
        elif event.data_table.id == "track-table":
            self.action_play_selected()

    def action_focus_search(self) -> None:
        self._input_mode = "search"
        inp = self.query_one("#search-input", Input)
        inp.placeholder = "Search YouTube Music (Enter to search, Esc to cancel)..."
        inp.focus()

    def action_create_playlist(self) -> None:
        self._input_mode = "playlist_create"
        inp = self.query_one("#search-input", Input)
        inp.value = ""
        inp.placeholder = "New Playlist Name (Enter to create, Esc to cancel)..."
        inp.focus()

    def action_focus_table(self) -> None:
        self._reset_input_to_search()
        self._active_table().focus()

    def action_view_queue(self) -> None:
        self._switch_view("queue")

    def action_view_search(self) -> None:
        self._switch_view("search")

    def action_view_liked(self) -> None:
        self._switch_view("liked")

    def action_view_local(self) -> None:
        self._switch_view("local")

    def action_view_playlists(self) -> None:
        self._switch_view("playlists")

    def action_view_settings(self) -> None:
        self._switch_view("settings")

    def action_next_view(self) -> None:
        idx = _VIEW_ORDER.index(self.active_view)
        self._switch_view(_VIEW_ORDER[(idx + 1) % len(_VIEW_ORDER)])

    def action_cursor_down(self) -> None:
        self._active_table().action_cursor_down()

    def action_cursor_up(self) -> None:
        self._active_table().action_cursor_up()

    def action_cursor_top(self) -> None:
        table = self._active_table()
        if table.row_count > 0:
            table.move_cursor(row=0)

    def action_cursor_bottom(self) -> None:
        table = self._active_table()
        if table.row_count > 0:
            table.move_cursor(row=table.row_count - 1)

    def action_activate_setting(self) -> None:
        table = self.query_one("#settings-table", DataTable)
        row = table.cursor_row
        if not (0 <= row < len(_SETTINGS_KEYS)):
            return
        key = _SETTINGS_KEYS[row]

        if key in (
            "auto_cache_liked",
            "preload_next",
            "preload_limit",
            "audio_quality",
            "stream_buffer_secs",
        ):
            updated = self.settings_store.cycle(key)
            self.cache_manager.wake()
            self._refresh_settings_table(force=True)
            if key in ("audio_quality", "stream_buffer_secs"):
                val_label = (
                    f"{updated.stream_buffer_secs}s"
                    if key == "stream_buffer_secs"
                    else updated.audio_quality
                )
                self._set_status(
                    f"[bold #00ff87]\\[SETTINGS][/] {key} = [bold #ffffff]{val_label}[/] "
                    "(select 'Restart mpv Daemon' below to apply now)."
                )
            else:
                val_str = str(getattr(updated, key))
                self._set_status(
                    f"[bold #00ff87]\\[SETTINGS][/] {key} = [bold #ffffff]{val_str}[/]."
                )
        elif key == "clear_preload":
            removed = self.downloader.clear_preload()
            self._refresh_settings_table(force=True)
            self._set_status(
                f"[bold #00ff87]\\[CACHE][/] Cleared {removed} temporary preload files."
            )
        elif key == "restart_daemon":
            self._set_status("[#00e5ff]Restarting mpv daemon with current settings...[/]")

            def _restart_bg() -> None:
                try:
                    self.player.restart_daemon()
                except MPVError as exc:
                    self._safe_ui(
                        self._set_status,
                        f"[bold #ff4d94]Restart error:[/] {escape(clean_error_text(exc))}",
                    )
                    return

                def _done() -> None:
                    self._refresh_status_tick()
                    self._refresh_settings_table(force=True)
                    self._set_status(
                        "[bold #00ff87]\\[DAEMON][/] Restarted mpv with current audio quality and buffer settings."
                    )

                self._safe_ui(_done)

            self._run_bg(_restart_bg)
        elif key == "local_stats":
            self._set_status(
                f"[#00e5ff]Permanent Local Cache folder:[/] [bold #ffffff]{escape(str(self.downloader.music_dir))}[/]"
            )

    def action_play_selected(self) -> None:
        if self.active_view == "settings":
            self.action_activate_setting()
            return
        selection = self._selected_track()
        if selection is None:
            self._set_status("[bold #ffd43b]No track selected.[/]")
            return
        row_idx, track = selection
        if self.active_view == "queue":
            def _jump() -> None:
                try:
                    self.player.play_index(row_idx)
                except MPVError as exc:
                    self._safe_ui(self._set_status, f"[bold #ff4d94]{escape(str(exc))}[/]")
                    return
                self._safe_ui(self._refresh_status_tick)
                self._safe_ui(
                    self._set_status,
                    f"[bold #00ff87]|> Playing[/] [bold #ffffff]{escape(track.title)}[/]",
                )

            self._run_bg(_jump)
        elif self.active_view in ("liked", "local", "playlists"):
            tracks = list(self._current_view_tracks())
            self.radio_autoplay = False
            self._set_status(
                f"[#00e5ff]Playing collection from[/] [bold #ffffff]{escape(track.title)}[/]..."
            )
            self._run_bg(
                lambda: self._play_queue_bg(
                    tracks, row_idx, stay_in_view=True, radio_autoplay=False
                )
            )
        else:
            self.radio_autoplay = True
            self._run_bg(lambda: self._play_track_with_radio_bg(track))

    def action_play_view_queue(self) -> None:
        tracks = self._current_view_tracks()
        if not tracks:
            self._set_status("[bold #ffd43b]Current view has no tracks to play.[/]")
            return
        selection = self._selected_track()
        start_idx = selection[0] if selection else 0
        self.radio_autoplay = False
        self._set_status(f"[#00e5ff]Loading {len(tracks)} tracks from current view...[/]")
        self._run_bg(
            lambda: self._play_queue_bg(
                list(tracks), start_idx, stay_in_view=True, radio_autoplay=False
            )
        )

    def action_start_radio(self) -> None:
        selection = self._selected_track()
        target = selection[1] if selection is not None else self.last_status.get("current_track")
        if target is None:
            self._set_status("[bold #ffd43b]No track selected for radio mix.[/]")
            return
        self.radio_autoplay = True
        self._run_bg(lambda: self._play_track_with_radio_bg(target))

    def action_toggle_pause(self) -> None:
        try:
            paused = self.player.toggle_pause()
        except MPVError as exc:
            self._set_status(f"[bold #ff4d94]{escape(str(exc))}[/]")
            return
        self._refresh_status_tick()
        self._set_status(
            "[bold #ffd43b]\\[PAUSED][/]" if paused else "[bold #00ff87]\\[PLAYING][/]"
        )

    def action_next_track(self) -> None:
        try:
            track = self.player.next_track()
        except MPVError as exc:
            self._set_status(f"[bold #ffd43b]{escape(str(exc))}[/]")
            return
        self._refresh_status_tick()
        if track:
            self._set_status(f"[bold #00ff87]|> Next:[/] [bold #ffffff]{escape(track.title)}[/]")

    def action_prev_track(self) -> None:
        try:
            track = self.player.prev_track()
        except MPVError as exc:
            self._set_status(f"[bold #ffd43b]{escape(str(exc))}[/]")
            return
        self._refresh_status_tick()
        if track:
            self._set_status(f"[bold #00ff87]<| Previous:[/] [bold #ffffff]{escape(track.title)}[/]")

    def action_replay_track(self) -> None:
        try:
            self.player.replay()
        except MPVError as exc:
            self._set_status(f"[bold #ff4d94]{escape(str(exc))}[/]")
            return
        self._refresh_status_tick()
        self._set_status("[bold #00ff87]\\[REPLAYING][/] Restarted from 00:00")

    def _resolve_target_track(self, prefer_current: bool) -> Track | None:
        if prefer_current:
            current: Track | None = self.last_status.get("current_track")
            if current is not None:
                return current
        selection = self._selected_track()
        return selection[1] if selection is not None else None

    def _set_in_memory_liked(self, video_id: str, liked: bool) -> None:
        for col in (
            self.queue_tracks,
            self.search_tracks,
            self.local_tracks,
            self.playlist_tracks,
        ):
            for item in col:
                if item.video_id == video_id:
                    item.is_liked = liked

    def _like_track_add_only(self, track: Track) -> None:
        newly_added = self.liked_store.like(track)
        self.player.sync_track_like(track.video_id, True)
        self._set_in_memory_liked(track.video_id, True)
        self._reload_collections()
        self._refresh_table(force=True)
        self._refresh_status_tick()
        if newly_added:
            if self.client.authenticated:
                self._run_bg(lambda: self.client.rate_track(track.video_id, True))
            self._set_status(
                f"[bold #ff4d94]\\[LIKED][/] Added [bold #ffffff]{escape(track.title)}[/] to Liked Music."
            )
        else:
            self._set_status(
                f"[bold #ff4d94]\\[LIKED][/] [bold #ffffff]{escape(track.title)}[/] "
                "is already in Liked Music (press [bold #ffd43b]x[/] to remove)."
            )

    def action_like_current(self) -> None:
        target = self._resolve_target_track(prefer_current=True)
        if target is None:
            self._set_status("[bold #ffd43b]No active or selected track to like.[/]")
            return
        self._like_track_add_only(target)

    def action_like_selected(self) -> None:
        target = self._resolve_target_track(prefer_current=False)
        if target is None:
            self._set_status("[bold #ffd43b]No track selected to like.[/]")
            return
        self._like_track_add_only(target)

    def action_remove_selected(self) -> None:
        now = time.monotonic()
        if now - self._last_remove_ts < _REMOVE_COOLDOWN_SECONDS:
            return
        self._last_remove_ts = now

        selection = self._selected_track()
        if selection is None:
            self._set_status("[bold #ffd43b]No track selected to remove.[/]")
            return
        _, track = selection

        if self.active_view == "playlists":
            pl_name = self.playlist_store.get_active_name()
            if not pl_name or not self.playlist_store.remove_track(track.video_id, pl_name):
                self._set_status("[bold #ffd43b]Track is not in active playlist.[/]")
                return
            self._reload_collections()
            self._refresh_table(force=True)
            self._set_status(
                f"[bold #ffd43b]\\[REMOVED][/] Removed [bold #ffffff]{escape(track.title)}[/] "
                f"from '[bold #ffffff]{escape(pl_name)}[/]'."
            )
            return

        if not self.liked_store.unlike(track):
            self._set_status("[bold #ffd43b]Track is not in Liked Music.[/]")
            return

        self.player.sync_track_like(track.video_id, False)
        self._set_in_memory_liked(track.video_id, False)
        if self.client.authenticated:
            self._run_bg(lambda: self.client.rate_track(track.video_id, False))
        self._reload_collections()
        self._refresh_table(force=True)
        self._refresh_status_tick()
        self._set_status(
            f"[bold #ffd43b]\\[REMOVED][/] Removed [bold #ffffff]{escape(track.title)}[/] "
            "from Liked Music (press [bold #ffd43b]u[/] to undo)."
        )

    def action_undo_unlike(self) -> None:
        restored = self.liked_store.pop_undo()
        if restored is None:
            self._set_status("[bold #ffd43b]Nothing to undo.[/]")
            return
        self.player.sync_track_like(restored.video_id, True)
        self._set_in_memory_liked(restored.video_id, True)
        if self.client.authenticated:
            self._run_bg(lambda: self.client.rate_track(restored.video_id, True))
        self._reload_collections()
        self._refresh_table(force=True)
        self._refresh_status_tick()
        self._set_status(
            f"[bold #00ff87]\\[UNDO][/] Restored [bold #ffffff]{escape(restored.title)}[/] to Liked Music!"
        )

    def _add_to_playlist(self, track: Track) -> None:
        pl_name, added = self.playlist_store.add_track(track)
        self._reload_collections()
        self._refresh_table(force=True)
        if added:
            self._set_status(
                f"[bold #00ff87]\\[PLAYLIST][/] Added [bold #ffffff]{escape(track.title)}[/] "
                f"to '[bold #ffffff]{escape(pl_name)}[/]'."
            )
        else:
            self._set_status(
                f"[bold #00e5ff]\\[PLAYLIST][/] [bold #ffffff]{escape(track.title)}[/] "
                f"is already in '[bold #ffffff]{escape(pl_name)}[/]'."
            )

    def action_add_selected_to_playlist(self) -> None:
        target = self._resolve_target_track(prefer_current=False)
        if target is None:
            self._set_status("[bold #ffd43b]No track selected to add.[/]")
            return
        self._add_to_playlist(target)

    def action_add_current_to_playlist(self) -> None:
        target = self._resolve_target_track(prefer_current=True)
        if target is None:
            self._set_status("[bold #ffd43b]No active or selected track to add.[/]")
            return
        self._add_to_playlist(target)

    def _cycle_playlist(self, delta: int) -> None:
        new_name = self.playlist_store.cycle_active(delta)
        if new_name is None:
            self._set_status(
                "[bold #ffd43b]No custom playlists yet. Press [bold #ffffff]c[/] to create one.[/]"
            )
            return
        self.active_view = "playlists"
        self._reload_collections()
        self._refresh_table(force=True)
        self._set_status(
            f"[bold #ff4d94]\\[PLAYLIST][/] Active playlist: [bold #ffffff]{escape(new_name)}[/] "
            f"({len(self.playlist_tracks)} tracks)"
        )

    def action_prev_playlist(self) -> None:
        self._cycle_playlist(-1)

    def action_next_playlist(self) -> None:
        self._cycle_playlist(1)

    def _download_track_bg(self, track: Track) -> None:
        self._safe_ui(
            self._set_status,
            f"[#00e5ff]Caching[/] [bold #ffffff]{escape(track.title)}[/] [#00e5ff]to {escape(str(self.music_dir))}...[/]",
        )
        try:
            saved_path = self.downloader.download(track)
        except Exception as exc:
            self._safe_ui(
                self._set_status,
                f"[bold #ff4d94]Download error:[/] {escape(clean_error_text(exc))}",
            )
            return

        local_str = str(saved_path)
        self.liked_store.update_local_path(track.video_id, local_str)
        self.playlist_store.update_local_path(track.video_id, local_str)
        self.player.sync_track_local_path(track.video_id, local_str)

        def _done() -> None:
            self._apply_local_path(track.video_id, local_str)
            self._refresh_status_tick()
            self._set_status(
                f"[bold #00ff87]\\[CACHED][/] Saved [bold #ffffff]{escape(track.title)}[/] to {escape(str(self.music_dir))}"
            )

        self._safe_ui(_done)

    def action_download_current(self) -> None:
        target = self._resolve_target_track(prefer_current=True)
        if target is None:
            self._set_status("[bold #ffd43b]No active or selected track to cache.[/]")
            return
        self._run_bg(lambda: self._download_track_bg(target))

    def action_download_selected(self) -> None:
        target = self._resolve_target_track(prefer_current=False)
        if target is None:
            self._set_status("[bold #ffd43b]No track selected to cache.[/]")
            return
        self._run_bg(lambda: self._download_track_bg(target))

    def action_sync_liked(self) -> None:
        if not self.client.authenticated:
            self._set_status(
                "[bold #00e5ff]\\[GUEST MODE][/] Run [bold #ffd43b]ytm auth[/] in terminal to link YouTube Music."
            )
            return

        self._set_status("[#00e5ff]Syncing Liked Music from YouTube Music...[/]")

        def _sync_bg() -> None:
            try:
                remote = self.client.fetch_liked_songs(limit=500)
                added = self.liked_store.merge_tracks(remote)
            except Exception as exc:
                self._safe_ui(
                    self._set_status,
                    f"[bold #ff4d94]Sync error:[/] {escape(clean_error_text(exc))}",
                )
                return

            def _done() -> None:
                self._reload_collections()
                self._refresh_table(force=True)
                self._refresh_status_tick()
                self._set_status(
                    f"[bold #00ff87]\\[SYNCED][/] Fetched {len(remote)} remote liked tracks ({added} added)."
                )

            self._safe_ui(_done)

        self._run_bg(_sync_bg)

    def action_seek_back(self) -> None:
        try:
            self.player.seek_relative(-5)
        except MPVError as exc:
            self._set_status(f"[bold #ff4d94]{escape(str(exc))}[/]")
            return
        self._refresh_status_tick()
        self._set_status("[#00e5ff]Seek -5s[/]")

    def action_seek_forward(self) -> None:
        try:
            self.player.seek_relative(5)
        except MPVError as exc:
            self._set_status(f"[bold #ff4d94]{escape(str(exc))}[/]")
            return
        self._refresh_status_tick()
        self._set_status("[#00e5ff]Seek +5s[/]")

    def action_vol_down(self) -> None:
        try:
            vol = self.player.set_volume("-5")
        except (MPVError, ValueError) as exc:
            self._set_status(f"[bold #ff4d94]{escape(str(exc))}[/]")
            return
        self._refresh_status_tick()
        self._set_status(f"[bold #ffd43b]Volume: {int(round(vol))}%[/]")

    def action_vol_up(self) -> None:
        try:
            vol = self.player.set_volume("+5")
        except (MPVError, ValueError) as exc:
            self._set_status(f"[bold #ff4d94]{escape(str(exc))}[/]")
            return
        self._refresh_status_tick()
        self._set_status(f"[bold #ffd43b]Volume: {int(round(vol))}%[/]")

    def action_detach_quit(self) -> None:
        self.cache_manager.stop()
        self.exit()

    def action_stop_quit(self) -> None:
        self.cache_manager.stop()
        self.player.stop()
        self.exit()


def run_tui() -> None:
    """Launch the interactive Textual TUI."""
    YTMApp().run()

"""High-contrast btop/lazygit-style Textual TUI for ytm."""

from __future__ import annotations

import math
import textwrap
import threading
import time
from pathlib import Path
from typing import Any

from rich.markup import escape
from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical
from textual.markup import escape as title_escape
from textual.screen import ModalScreen
from textual.widgets import DataTable, Input, OptionList, Static
from textual.widgets.option_list import Option

from ytm.api import YTMClient
from ytm.art import CoverArtRenderer
from ytm.cache_worker import BackgroundCacheManager, clean_error_text
from ytm.config import MUSIC_DIR
from ytm.downloader import TrackDownloader, dir_stats
from ytm.liked import LikedStore
from ytm.lyrics import LyricsData, LyricsProvider, active_line_index
from ytm.models import Track, format_duration
from ytm.player import MPVError, MPVPlayer
from ytm.playlists import PlaylistStore
from ytm.settings import SOUND_PROFILE_LABELS, SOUND_PROFILE_SHORT, SettingsStore
from ytm.spectrum import PipeWireSpectrumAnalyzer

_AUDIO_EXTS = {".opus", ".m4a", ".webm", ".mp3", ".ogg", ".flac", ".wav"}
_VIEW_ORDER = ("queue", "search", "liked", "local", "playlists", "settings")
_SETTINGS_KEYS = (
    "auto_cache_liked",
    "preload_next",
    "preload_limit",
    "audio_quality",
    "stream_buffer_secs",
    "mpris_enabled",
    "spectrum_mode",
    "sound_profile",
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


_NEW_PLAYLIST_SENTINEL = "\x00new"

_MODAL_CSS = """
PlaylistPickerModal {
    align: center middle;
    background: rgba(0, 0, 0, 0.7);
}

#modal-box {
    width: 52;
    height: auto;
    max-height: 22;
    background: #000000;
    border: round #ff4d94;
    padding: 0 1;
}

#modal-list {
    height: auto;
    max-height: 14;
    background: #000000;
    color: #ffffff;
    border: none;
}

#modal-list > .option-list--option-highlighted {
    background: #103252;
    color: #ffffff;
    text-style: bold;
}

#modal-hint {
    background: #000000;
    color: #00e5ff;
}
"""


class PlaylistPickerModal(ModalScreen[str | None]):
    """Compact high-contrast picker that returns the chosen playlist name."""

    DEFAULT_CSS = _MODAL_CSS
    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
        Binding("j", "down", "Down"),
        Binding("k", "up", "Up"),
        Binding("c", "new_playlist", "New"),
    ] + [Binding(str(n), f"quick_pick({n})", f"Pick {n}") for n in range(1, 10)]

    def __init__(self, names: list[str], active: str | None, track_title: str) -> None:
        super().__init__()
        self.names = names
        self.active = active
        self.track_title = track_title

    def compose(self) -> ComposeResult:
        with Vertical(id="modal-box"):
            options = []
            for idx, name in enumerate(self.names):
                num = f"{idx + 1}" if idx < 9 else " "
                options.append(Option(Text.assemble((f"{num}  ", "bold #ffd43b"), (name, "bold #ffffff"))))
            yield OptionList(*options, id="modal-list")
            yield Static(
                "[bold #ffd43b]j/k[/] move  [bold #ffd43b]Enter[/] add  [bold #ffd43b]1-9[/] quick  "
                "[bold #ffd43b]c[/] new  [bold #ffd43b]Esc[/] cancel",
                id="modal-hint",
            )

    def on_mount(self) -> None:
        box = self.query_one("#modal-box", Vertical)
        box.border_title = f"Add to playlist: {title_escape(_truncate(self.track_title, 30))}"
        option_list = self.query_one("#modal-list", OptionList)
        if self.active in self.names:
            option_list.highlighted = self.names.index(self.active)
        elif self.names:
            option_list.highlighted = 0
        option_list.focus()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        if 0 <= event.option_index < len(self.names):
            self.dismiss(self.names[event.option_index])

    def action_down(self) -> None:
        self.query_one("#modal-list", OptionList).action_cursor_down()

    def action_up(self) -> None:
        self.query_one("#modal-list", OptionList).action_cursor_up()

    def action_cancel(self) -> None:
        self.dismiss(None)

    def action_new_playlist(self) -> None:
        self.dismiss(_NEW_PLAYLIST_SENTINEL)

    def action_quick_pick(self, number: int) -> None:
        idx = int(number) - 1
        if 0 <= idx < len(self.names):
            self.dismiss(self.names[idx])


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

    #track-table, #settings-table, #playlist-folders-table {
        height: 1fr;
        background: #000000;
        color: #ffffff;
    }

    #settings-table, #playlist-folders-table {
        display: none;
    }

    #track-table > .datatable--header, #settings-table > .datatable--header,
    #playlist-folders-table > .datatable--header {
        background: #0a1428;
        color: #ffd43b;
        text-style: bold;
    }

    #track-table > .datatable--cursor, #settings-table > .datatable--cursor,
    #playlist-folders-table > .datatable--cursor {
        background: #103252;
        color: #ffffff;
        text-style: bold;
    }

    #track-table > .datatable--even-row, #settings-table > .datatable--even-row,
    #playlist-folders-table > .datatable--even-row {
        background: #05080f;
    }

    #track-table > .datatable--odd-row, #settings-table > .datatable--odd-row,
    #playlist-folders-table > .datatable--odd-row {
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

    #focus-stage {
        height: 1fr;
        background: #000000;
        layout: vertical;
        display: none;
    }

    #focus-upper {
        height: 16;
        background: #000000;
        layout: horizontal;
    }

    #focus-cover-box {
        width: 32;
        height: 16;
        background: #000000;
        border: round #00e5ff;
        padding: 0 1;
    }

    #focus-now-playing {
        width: 1fr;
        height: 16;
        background: #000000;
        border: round #00ff87;
        padding: 1 2;
    }

    #focus-lyrics-box {
        width: 1.3fr;
        height: 16;
        background: #000000;
        border: round #ffd43b;
        padding: 0 1;
        display: none;
    }

    #focus-spectrum-box {
        height: 1fr;
        min-height: 7;
        background: #000000;
        border: round #ff4d94;
        padding: 0 1;
    }
    """

    BINDINGS = [
        Binding("slash", "focus_search", "Search", priority=True),
        Binding("backslash", "focus_filter", "Filter"),
        Binding("escape", "focus_table", "Track List", priority=True),
        Binding("z", "toggle_focus_mode", "Focus Mode"),
        Binding("y", "toggle_lyrics", "Lyrics"),
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
        Binding("S", "toggle_shuffle", "Shuffle"),
        Binding("w", "cycle_repeat", "Repeat"),
        Binding("E", "cycle_sound_profile", "Sound EQ"),
        Binding("backspace", "go_back", "Back"),
        Binding("h", "go_back", "Back"),
        Binding("e", "rename_playlist", "Rename Playlist"),
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
        lyrics_provider: LyricsProvider | None = None,
        initial_focus: bool = False,
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
        self.lyrics_provider = lyrics_provider or LyricsProvider(client=self.client)
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
        self._focus_cover_video_id: str | None = None
        self.spectrum_analyzer = PipeWireSpectrumAnalyzer(band_count=_BAND_COUNT)

        self.focus_mode: bool = bool(initial_focus)
        self.lyrics_visible: bool = False
        self._current_lyrics: LyricsData | None = None
        self._lyrics_video_id: str | None = None
        self._lyrics_loading_id: str | None = None
        self._lyrics_missing_ids: set[str] = set()
        self._lyrics_scroll_offset: int = 0

        self.shuffle_on: bool = self.player.load_shuffle()
        self.repeat_mode: str = self.player.load_repeat_mode()
        self.playlist_summaries: list[dict[str, Any]] = []
        self._playlist_browse_mode: str = "folders"
        self._folders_signature: tuple[Any, ...] | None = None
        self._filter_text: str = ""
        self._rename_target: str | None = None
        self._pending_add_track: Track | None = None
        self._pending_delete_playlist: tuple[str, float] | None = None

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
                yield DataTable(id="playlist-folders-table", cursor_type="row", zebra_stripes=True)
                yield Static(
                    "[#00e5ff]Ready.[/] Press [bold #ffd43b]/[/] to search, "
                    "[bold #ffd43b]c[/] new playlist, [bold #ffd43b]6[/] settings, [bold #ffd43b]q[/] detach.",
                    id="main-status",
                )

        with Horizontal(id="bottom-dock"):
            yield Static("", id="cover-box")
            yield Static("", id="now-playing-box")
            yield Static("", id="visualizer-box")

        with Vertical(id="focus-stage"):
            with Horizontal(id="focus-upper"):
                yield Static("", id="focus-cover-box")
                yield Static("", id="focus-now-playing")
                yield Static("", id="focus-lyrics-box")
            yield Static("", id="focus-spectrum-box")

    def on_mount(self) -> None:
        self.query_one("#nav-panel", Static).border_title = "|> Views"
        self.query_one("#keys-panel", Static).border_title = "Keybindings"
        cover_box = self.query_one("#cover-box", Static)
        cover_box.border_title = "Cover"
        cover_box.update(self.art_renderer.placeholder_markup())
        self.query_one("#now-playing-box", Static).border_title = "|> Now Playing"
        self.query_one("#visualizer-box", Static).border_title = "Spectrum"

        focus_cover = self.query_one("#focus-cover-box", Static)
        focus_cover.border_title = "Cover"
        focus_cover.update(self.art_renderer.placeholder_markup(width=28, height_cells=14))
        self.query_one("#focus-now-playing", Static).border_title = "|> Focus Player"
        self.query_one("#focus-lyrics-box", Static).border_title = "Lyrics"
        self.query_one("#focus-spectrum-box", Static).border_title = "Spectrum"

        table = self.query_one("#track-table", DataTable)
        table.add_columns("#", "Title", "Artist", "Album", "Duration")

        settings_table = self.query_one("#settings-table", DataTable)
        settings_table.add_columns("Setting", "Value", "Description")
        settings_table.display = False

        folders_table = self.query_one("#playlist-folders-table", DataTable)
        folders_table.add_columns("#", "Playlist", "Tracks", "Local", "Status")
        folders_table.display = False

        if self.focus_mode:
            self._apply_focus_layout()
        else:
            table.focus()

        self._reload_collections()
        self._refresh_status_tick()
        self._tick_visualizer()
        self.cache_manager.start()
        if not self.is_headless and self.settings_store.load().spectrum_mode == "fft":
            self.spectrum_analyzer.start()
        if self.player.is_running():
            self._run_bg(self.player._maybe_start_mpris)

        self.set_interval(0.25, self._refresh_status_tick)
        self.set_interval(0.05, self._tick_visualizer)

    def on_unmount(self) -> None:
        self.spectrum_analyzer.stop()
        self.cache_manager.stop()

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        if isinstance(self.screen, ModalScreen):
            return False
        if isinstance(self.focused, Input) and action != "focus_table":
            return False
        if self.focus_mode and action in {
            "play_selected",
            "play_view_queue",
            "start_radio",
            "like_selected",
            "remove_selected",
            "add_selected_to_playlist",
            "download_selected",
            "open_selected_playlist",
            "rename_playlist",
            "go_back",
            "activate_setting",
        }:
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
                "[bold #ffd43b]S / w[/]   [#ffffff]Shuffle / Repeat[/]",
                "[bold #ffd43b]z / y[/]   [#ffffff]Focus / Lyrics[/]",
                "[bold #ffd43b]\\[/]       [#ffffff]Filter (offline)[/]",
                "[bold #ffd43b]r[/]       [#ffffff]Replay (00:00)[/]",
                "[bold #ffd43b]l / L[/]   [#ffffff]Add Like (No Del)[/]",
                "[bold #ffd43b]x / u[/]   [#ffffff]Remove / Undo Like[/]",
                "[bold #ffd43b]c / m[/]   [#ffffff]New / Add Playlist[/]",
                "[bold #ffd43b], / .[/]   [#ffffff]Switch Playlist[/]",
                "[bold #ffd43b]e / h[/]   [#ffffff]Rename PL / Back[/]",
                "[bold #ffd43b]E[/]       [#ffffff]Sound EQ (Bass/Clarity)[/]",
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
        self.playlist_summaries = self.playlist_store.list_summaries()
        self.local_tracks = self._scan_local_tracks()
        self.liked_store.annotate(self.search_tracks)

    def _in_folders_mode(self) -> bool:
        return self.active_view == "playlists" and self._playlist_browse_mode == "folders"

    def _filter_active(self) -> bool:
        return bool(self._filter_text.strip())

    def _visible_rows(self) -> list[tuple[int, Track]]:
        """Return (real_index, track) pairs for the current view after the offline filter."""
        tracks = self._current_view_tracks()
        needle = self._filter_text.strip().lower()
        if not needle:
            return list(enumerate(tracks))
        return [
            (idx, t)
            for idx, t in enumerate(tracks)
            if needle in t.title.lower() or needle in t.artist.lower() or needle in (t.album or "").lower()
        ]

    def _visible_folders(self) -> list[tuple[int, dict[str, Any]]]:
        needle = self._filter_text.strip().lower()
        rows = list(enumerate(self.playlist_summaries))
        if not needle:
            return rows
        return [(idx, s) for idx, s in rows if needle in str(s["name"]).lower()]

    def _filter_suffix(self, shown: int, total: int) -> str:
        if not self._filter_active():
            return ""
        return f'  // filter "{title_escape(self._filter_text.strip())}" {shown}/{total}'

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
            if self._playlist_browse_mode == "folders":
                return f"|> [5] Playlists ({len(self.playlist_summaries)})"
            pl_name = title_escape(self.playlist_store.get_active_name() or "None")
            return f"|> [5] Playlists / {pl_name}  (Esc: back)"
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
            ("playlists", "5", "Playlists", len(self.playlist_summaries)),
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
        mpris_val = "[ON]" if settings.mpris_enabled else "[OFF]"
        mpris_style = "bold #00ff87" if settings.mpris_enabled else "bold #ff4d94"
        spec_labels = {
            "fft": ("Live Audio (PipeWire FFT)", "bold #00ff87"),
            "simulated": ("Simulated Wave", "bold #ffd43b"),
            "off": ("Off", "bold #ff4d94"),
        }
        spec_val, spec_style = spec_labels.get(
            settings.spectrum_mode, (settings.spectrum_mode, "bold #ffd43b")
        )
        eq_val = SOUND_PROFILE_LABELS.get(settings.sound_profile, settings.sound_profile)
        eq_style = "bold #4c6ef5" if settings.sound_profile == "flat" else "bold #00ff87"
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
                "MPRIS Media Keys",
                Text(mpris_val, style=mpris_style),
                "Global Play/Pause/Next/Prev across keyboards and KDE Plasma",
            ),
            (
                "Spectrum Mode",
                Text(spec_val, style=spec_style),
                "Beat-reactive volume-independent FFT or simulated wave",
            ),
            (
                "Sound Enhancer / EQ",
                Text(eq_val, style=eq_style),
                "Zero-latency bass, sensitivity, and clarity DSP (press E anywhere)",
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

    def _refresh_folders_table(self, force: bool = False) -> None:
        visible = self._visible_folders()
        total = len(self.playlist_summaries)
        sig = (
            self._filter_text.strip().lower(),
            tuple((s["name"], s["count"], s["local"], s["active"]) for s in self.playlist_summaries),
        )
        if not force and sig == self._folders_signature:
            return
        self._folders_signature = sig

        main_pane = self.query_one("#main-pane", Vertical)
        main_pane.border_title = self._view_title() + self._filter_suffix(len(visible), total)

        table = self.query_one("#playlist-folders-table", DataTable)
        prev_row = table.cursor_row
        table.clear()

        for real_idx, item in visible:
            is_active = bool(item["active"])
            num_text = Text(
                f"|>{real_idx + 1}" if is_active else str(real_idx + 1),
                style="bold #00ff87" if is_active else "bold #ffd43b",
            )
            name_text = Text(
                f"[DIR] {_truncate(str(item['name']), 34)}",
                style="bold #00ff87" if is_active else "bold #ffffff",
            )
            count_text = Text(f"{item['count']} tracks", style="#00e5ff")
            local_text = Text(f"{item['local']} local", style="#00ff87")
            status_text = (
                Text("[ACTIVE]", style="bold #ff4d94")
                if is_active
                else Text("-", style="#4c6ef5")
            )
            table.add_row(num_text, name_text, count_text, local_text, status_text)

        if visible:
            target_row = max(0, min(prev_row, len(visible) - 1))
            table.move_cursor(row=target_row)

        self._update_nav_panel()

    def _refresh_table(self, force: bool = False) -> None:
        if self._in_folders_mode():
            self._refresh_folders_table(force=force)
            return

        tracks = self._current_view_tracks()
        visible = self._visible_rows()
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
            self._filter_text.strip().lower(),
            tuple((t.video_id, t.title, t.is_liked, bool(t.local_path)) for t in tracks),
        )
        if not force and sig == self._table_signature:
            return
        self._table_signature = sig

        main_pane = self.query_one("#main-pane", Vertical)
        main_pane.border_title = self._view_title() + self._filter_suffix(len(visible), len(tracks))

        table = self.query_one("#track-table", DataTable)
        prev_row = table.cursor_row
        table.clear()

        for real_idx, track in visible:
            if self.active_view == "queue":
                is_active = real_idx == self.active_queue_pos
            else:
                is_active = current_vid is not None and track.video_id == current_vid
            num_text = Text(
                f"|>{real_idx + 1}" if is_active else str(real_idx + 1),
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

        if visible:
            target_row = max(0, min(prev_row, len(visible) - 1))
            table.move_cursor(row=target_row)

        self._update_nav_panel()

    def _set_status(self, message: str) -> None:
        self.query_one("#main-status", Static).update(message)

    def _apply_focus_layout(self) -> None:
        if not self.is_mounted:
            return
        show_focus = bool(self.focus_mode)
        for widget_id in ("#top-bar", "#search-bar", "#workspace", "#bottom-dock"):
            self.query_one(widget_id).display = not show_focus
        focus_stage = self.query_one("#focus-stage", Vertical)
        focus_stage.display = show_focus
        focus_stage.can_focus = show_focus
        self.query_one("#focus-lyrics-box").display = show_focus and self.lyrics_visible
        self.query_one("#search-input", Input).can_focus = not show_focus
        if show_focus:
            focus_stage.focus()
        else:
            self._apply_table_visibility().focus()
        self.call_after_refresh(self._sync_focus_widgets_after_refresh)

    def _update_cover_art(self, track: Track | None) -> None:
        cover_box = self.query_one("#cover-box", Static)
        focus_cover = self.query_one("#focus-cover-box", Static)
        if track is None or not track.video_id:
            if self._cover_video_id is not None:
                self._cover_video_id = None
                cover_box.update(self.art_renderer.placeholder_markup())
            if self._focus_cover_video_id is not None:
                self._focus_cover_video_id = None
                focus_cover.update(self.art_renderer.placeholder_markup(width=28, height_cells=14))
            return

        vid = track.video_id
        if vid != self._cover_video_id:
            self._cover_video_id = vid
            cached = self.art_renderer.get_cached_markup(vid, width=14, height_cells=7)
            if cached is not None:
                cover_box.update(cached)
            else:
                cover_box.update(self.art_renderer.placeholder_markup())

                def _fetch_small() -> None:
                    markup = self.art_renderer.fetch_and_render(vid, width=14, height_cells=7)

                    def _apply_small() -> None:
                        if self._cover_video_id == vid:
                            self.query_one("#cover-box", Static).update(markup)

                    self._safe_ui(_apply_small)

                self._run_bg(_fetch_small)

        if self.focus_mode and vid != self._focus_cover_video_id:
            self._focus_cover_video_id = vid
            cached_big = self.art_renderer.get_cached_markup(vid, width=28, height_cells=14)
            if cached_big is not None:
                focus_cover.update(cached_big)
            else:
                focus_cover.update(self.art_renderer.placeholder_markup(width=28, height_cells=14))

                def _fetch_big() -> None:
                    markup = self.art_renderer.fetch_and_render(vid, width=28, height_cells=14)

                    def _apply_big() -> None:
                        if self._focus_cover_video_id == vid:
                            self.query_one("#focus-cover-box", Static).update(markup)

                    self._safe_ui(_apply_big)

                self._run_bg(_fetch_big)

    def _ensure_lyrics_for_track(self, track: Track | None) -> None:
        if track is None or not track.video_id:
            self._lyrics_video_id = None
            self._current_lyrics = None
            self._lyrics_loading_id = None
            self._lyrics_scroll_offset = 0
            return

        vid = track.video_id
        if vid != self._lyrics_video_id:
            self._lyrics_video_id = vid
            self._lyrics_scroll_offset = 0
            self._current_lyrics = self.lyrics_provider.get_cached(vid)

        if (
            self.lyrics_visible
            and self._current_lyrics is None
            and self._lyrics_loading_id != vid
            and vid not in self._lyrics_missing_ids
        ):
            cached = self.lyrics_provider.get_cached(vid)
            if cached is not None:
                self._current_lyrics = cached
                return

            self._lyrics_loading_id = vid

            def _fetch_lyrics_bg() -> None:
                data = self.lyrics_provider.fetch(track)

                def _apply_lyrics() -> None:
                    if self._lyrics_loading_id == vid:
                        self._lyrics_loading_id = None
                    if data is None:
                        self._lyrics_missing_ids.add(vid)
                    else:
                        self._lyrics_missing_ids.discard(vid)
                    if self._lyrics_video_id == vid:
                        self._current_lyrics = data
                        self._lyrics_scroll_offset = 0
                        self._render_lyrics_panel(self.last_status)

                self._safe_ui(_apply_lyrics)

            self._run_bg(_fetch_lyrics_bg)

    def _render_lyrics_panel(self, status: dict[str, Any]) -> None:
        if not self.is_mounted or not self.focus_mode or not self.lyrics_visible:
            return
        lyrics_box = self.query_one("#focus-lyrics-box", Static)
        running = bool(status.get("running"))
        track: Track | None = status.get("current_track")

        if not running or track is None or not track.video_id:
            lyrics_box.border_title = "Lyrics"
            lyrics_box.update("\n[#00e5ff]Start playback to view synced lyrics.[/]")
            return

        vid = track.video_id
        if self._lyrics_loading_id == vid and self._current_lyrics is None:
            lyrics_box.border_title = "Lyrics \\[LOADING]"
            lyrics_box.update("\n[#00e5ff]Searching YouTube Music & LRCLIB for lyrics...[/]")
            return

        data = self._current_lyrics
        if data is None:
            lyrics_box.border_title = "Lyrics"
            lyrics_box.update(
                "\n[bold #ffd43b]No lyrics available for this track.[/]\n"
                "[#4c6ef5](Press y to toggle retry)[/]"
            )
            return

        try:
            box_w = int(lyrics_box.content_size.width)
            box_h = int(lyrics_box.content_size.height)
        except Exception:
            box_w, box_h = 44, 14
        inner_w = max(24, box_w if box_w > 4 else 44)
        max_rows = max(6, min(14, box_h if box_h > 2 else 14))
        wrap_w = max(18, inner_w - 3)

        if data.synced and data.lines:
            lyrics_box.border_title = f"Lyrics \\[SYNCED - {title_escape(data.source)}]"
            time_pos = float(status.get("time_pos", 0.0) or 0.0)
            act_idx = active_line_index(data.lines, time_pos)

            visual_rows: list[tuple[int, bool, str]] = []
            act_first_row = 0
            for idx, line in enumerate(data.lines):
                text_body = line.text.strip() or "~"
                wrapped = textwrap.wrap(text_body, width=wrap_w) or ["~"]
                if idx == act_idx:
                    act_first_row = len(visual_rows)
                for seg_idx, seg in enumerate(wrapped):
                    visual_rows.append((idx, seg_idx == 0, seg))

            start_row = max(0, act_first_row - max_rows // 2)
            if start_row + max_rows > len(visual_rows):
                start_row = max(0, len(visual_rows) - max_rows)
            window = visual_rows[start_row : start_row + max_rows]

            rendered: list[str] = []
            for idx, is_first, seg in window:
                safe_seg = escape(seg)
                if idx == act_idx:
                    prefix = "|> " if is_first else "   "
                    rendered.append(f"[bold #00ff87]{prefix}{safe_seg}[/]")
                elif abs(idx - act_idx) == 1:
                    rendered.append(f"[#ffffff]   {safe_seg}[/]")
                elif idx < act_idx:
                    rendered.append(f"[#4c6ef5]   {safe_seg}[/]")
                else:
                    rendered.append(f"[#00e5ff]   {safe_seg}[/]")
            lyrics_box.update("\n".join(rendered))
            return

        lyrics_box.border_title = f"Lyrics \\[PLAIN - {title_escape(data.source)}]"
        plain_lines: list[str] = []
        for raw_line in (data.plain_text or "").splitlines():
            stripped = raw_line.strip()
            if not stripped:
                plain_lines.append("")
            else:
                plain_lines.extend(textwrap.wrap(stripped, width=wrap_w) or [""])

        if not plain_lines:
            lyrics_box.update("\n[bold #ffd43b]No lyrics available for this track.[/]")
            return

        max_offset = max(0, len(plain_lines) - max_rows)
        self._lyrics_scroll_offset = max(0, min(self._lyrics_scroll_offset, max_offset))
        view_rows = plain_lines[self._lyrics_scroll_offset : self._lyrics_scroll_offset + max_rows]
        rendered = [f"[#00e5ff]   {escape(row)}[/]" if row else "" for row in view_rows]
        if max_offset > 0 and len(rendered) == max_rows:
            rendered[-1] = (
                f"[#4c6ef5]   (j/k scroll {self._lyrics_scroll_offset + 1}/{max_offset + 1})[/]"
            )
        lyrics_box.update("\n".join(rendered))

    def _update_focus_now_playing(self, status: dict[str, Any]) -> None:
        focus_now = self.query_one("#focus-now-playing", Static)
        running = bool(status.get("running"))
        paused = bool(status.get("paused", False))
        track: Track | None = status.get("current_track")

        try:
            box_w = int(focus_now.content_size.width)
        except Exception:
            box_w = 50
        inner_w = max(28, box_w if box_w > 6 else 50)
        bar_w = max(12, min(46, inner_w - 22))
        hints = (
            "[bold #ffd43b]Spc[/] Pause  [bold #ffd43b]n/p[/] Next/Prev  "
            "[bold #ffd43b]S/w[/] Shuf/Rep  [bold #ffd43b]E[/] EQ  [bold #ffd43b]y[/] Lyrics  "
            "[bold #ffd43b]z/Esc[/] Full UI"
        )

        if not running or track is None:
            state_line = (
                "[bold #ff4d94]\\[STOPPED][/] [#ffffff]No active playback session.[/]"
                if not running
                else "[bold #ffd43b]\\[IDLE][/] [#ffffff]Daemon ready. Press z/Esc to browse.[/]"
            )
            focus_now.border_title = "|> Focus Player"
            focus_now.update(
                "\n".join(
                    [
                        state_line,
                        "",
                        "[bold #00e5ff]Minimalist Focus Mode[/]",
                        f"{_build_progress_markup(0.0, 0.0, width=bar_w)}  [bold #ffffff]00:00 / 00:00[/]",
                        "",
                        hints,
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
            "[bold #00e5ff]\\[RADIO][/]"
            if self.radio_autoplay
            else "[bold #00ff87]\\[COLLECTION][/]"
        )
        time_pos = float(status.get("time_pos", 0.0) or 0.0)
        duration = float(status.get("duration", 0.0) or 0.0)
        volume = int(round(float(status.get("volume", 100.0) or 100.0)))
        pos_idx = int(status.get("playlist_pos", -1))
        total_count = int(status.get("playlist_count", 0) or 0)
        track_counter = (
            f"{pos_idx + 1}/{total_count}" if pos_idx >= 0 and total_count > 0 else "-/-"
        )
        bar = _build_progress_markup(time_pos, duration, width=bar_w)
        timing = f"{format_duration(time_pos)} / {format_duration(duration)}"
        title_safe = escape(_truncate(track.title, inner_w))
        artist_safe = escape(_truncate(track.artist, max(16, inner_w - 9)))
        album_safe = escape(_truncate(track.album or "-", max(16, inner_w - 9)))
        pl_active = self.playlist_store.get_active_name()
        pl_info = f"  [bold #ff4d94]PL:[/] [#ffffff]{escape(_truncate(pl_active, 16))}[/]" if pl_active else ""
        shuffle_tag = "  [bold #ffd43b]\\[SHUFFLE][/]" if self.shuffle_on else ""
        repeat_tag = (
            f"  [bold #ff4d94]\\[REPEAT: {self.repeat_mode.upper()}][/]"
            if self.repeat_mode in ("all", "one")
            else ""
        )
        eq_profile = self.settings_store.load().sound_profile
        eq_tag = (
            f"  [bold #00ff87]\\[EQ: {SOUND_PROFILE_SHORT.get(eq_profile, eq_profile.upper())}][/]"
            if eq_profile != "flat"
            else ""
        )

        focus_now.border_title = "|> Focus Player"
        focus_now.update(
            "\n".join(
                [
                    f"{state_tag} {source_tag}{liked_tag}",
                    f"[bold #ffffff]{title_safe}[/]",
                    f"[bold #00e5ff]Artist:[/] [bold #ffffff]{artist_safe}[/]",
                    f"[bold #ff4d94]Album:[/]  [#ffffff]{album_safe}[/]",
                    "",
                    f"{bar}  [bold #ffffff]{timing}[/]",
                    f"[bold #ffd43b]Vol: {volume}%[/]  [bold #00e5ff]Track: {track_counter}[/]  {mode_tag}{shuffle_tag}{repeat_tag}{eq_tag}{pl_info}",
                    "",
                    hints,
                ]
            )
        )

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
        self._ensure_lyrics_for_track(track if running else None)
        self._update_focus_now_playing(status)
        self._render_lyrics_panel(status)

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
        shuffle_tag = "  [bold #ffd43b]\\[SHUFFLE][/]" if self.shuffle_on else ""
        repeat_tag = (
            f"  [bold #ff4d94]\\[REPEAT: {self.repeat_mode.upper()}][/]"
            if self.repeat_mode in ("all", "one")
            else ""
        )
        eq_profile = self.settings_store.load().sound_profile
        eq_tag = (
            f"  [bold #00ff87]\\[EQ: {SOUND_PROFILE_SHORT.get(eq_profile, eq_profile.upper())}][/]"
            if eq_profile != "flat"
            else ""
        )

        now_box.border_title = "|> Now Playing"
        now_box.update(
            "\n".join(
                [
                    f"{state_tag} {source_tag}{liked_tag}  [bold #ffffff]{title_safe}[/]",
                    f"[bold #00e5ff]Artist:[/] [bold #ffffff]{artist_safe}[/]   [bold #ff4d94]Album:[/] [#ffffff]{album_safe}[/]",
                    f"{bar}  [bold #ffffff]{timing}[/]  [bold #ffd43b]Vol: {volume}%[/]",
                    f"{mode_tag}{shuffle_tag}{repeat_tag}{eq_tag}  [bold #00e5ff]Track: {track_counter}[/]{pl_info}",
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
            self.shuffle_on = bool(status.get("shuffle", False))
            self.repeat_mode = str(status.get("repeat_mode", "off"))
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
                and self.repeat_mode == "off"
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
            self.shuffle_on = self.player.load_shuffle()
            self.repeat_mode = self.player.load_repeat_mode()

        self._update_top_and_now_playing(status)
        if self.active_view == "settings":
            self._refresh_settings_table()
        elif self._in_folders_mode():
            self._refresh_folders_table()
        else:
            self._refresh_table()

    def _current_spectrum_width(self) -> int:
        box_id = "#focus-spectrum-box" if self.focus_mode else "#visualizer-box"
        try:
            vis_box = self.query_one(box_id, Static)
            w = int(vis_box.content_size.width)
            if w > 4:
                return max(12, min(240, w))
        except Exception:
            pass
        return _BAND_COUNT

    def on_resize(self) -> None:
        if self.is_mounted:
            self._tick_visualizer()
            if self.focus_mode:
                self._update_focus_now_playing(self.last_status)
                self._render_lyrics_panel(self.last_status)

    def _tick_visualizer(self) -> None:
        if not self.is_mounted:
            return
        box_id = "#focus-spectrum-box" if self.focus_mode else "#visualizer-box"
        try:
            vis_box = self.query_one(box_id, Static)
        except Exception:
            return

        status = self.last_status
        running = bool(status.get("running"))
        paused = bool(status.get("paused", False))
        core_idle = bool(status.get("core_idle", True))
        track: Track | None = status.get("current_track")

        band_count = self._current_spectrum_width()
        row_count = _SPECTRUM_ROWS
        if self.focus_mode:
            try:
                h = int(vis_box.content_size.height)
                if h >= _SPECTRUM_ROWS:
                    row_count = min(24, h)
            except Exception:
                row_count = _SPECTRUM_ROWS

        if len(self._bands) != band_count:
            if self._bands:
                old_len = len(self._bands)
                self._bands = [
                    self._bands[min(old_len - 1, int(i * old_len / band_count))]
                    for i in range(band_count)
                ]
            else:
                self._bands = [0.02] * band_count

        spec_mode = self.settings_store.load().spectrum_mode
        active = running and not paused and not core_idle and track is not None
        use_fft = active and spec_mode == "fft" and not self.is_headless
        self.spectrum_analyzer.set_active(use_fft)
        capturing = use_fft and self.spectrum_analyzer.is_capturing()

        if spec_mode == "off":
            self._bands = [0.02] * band_count
            vis_title = "Spectrum \\[OFF]"
        elif capturing:
            self._bands = self.spectrum_analyzer.get_bands(band_count)
            vis_title = "Spectrum \\[LIVE FFT]"
        elif active and track is not None:
            vis_title = "Spectrum \\[FFT]" if spec_mode == "fft" else "Spectrum \\[SIM]"
            self._vis_phase += 0.28
            time_pos = float(status.get("time_pos", 0.0) or 0.0)
            seed = sum(ord(ch) for ch in track.video_id) * 0.13
            t = time_pos * 3.1 + self._vis_phase
            for i in range(band_count):
                freq = 0.35 + (i / band_count) * 1.4
                envelope = 0.72 + 0.28 * math.sin(i * 0.27 + seed)
                wave_a = 0.5 + 0.5 * math.sin(t * freq + i * 0.48 + seed)
                wave_b = 0.5 + 0.5 * math.cos(t * (freq * 0.63) - i * 0.31)
                target = max(0.08, min(1.0, (0.58 * wave_a + 0.42 * wave_b) * envelope))
                self._bands[i] = self._bands[i] * 0.42 + target * 0.58
        else:
            vis_title = "Spectrum \\[FFT]" if spec_mode == "fft" else "Spectrum \\[SIM]"
            for i in range(band_count):
                self._bands[i] = max(0.02, self._bands[i] * 0.70)

        max_units = row_count * 8
        rows: list[str] = []
        for row_idx in range(row_count - 1, -1, -1):
            if row_count == _SPECTRUM_ROWS and row_idx in _SPECTRUM_COLORS:
                color = _SPECTRUM_COLORS[row_idx]
            else:
                frac = row_idx / max(1, row_count - 1)
                if frac >= 0.70:
                    color = "#ff4d94"
                elif frac >= 0.45:
                    color = "#ffd43b"
                elif frac >= 0.16:
                    color = "#00ff87"
                else:
                    color = "#00e5ff"
            chars: list[str] = []
            for height in self._bands:
                units = max(1, min(max_units, int(round(height * max_units))))
                row_units = max(0, min(8, units - row_idx * 8))
                chars.append(_SPECTRUM_CHARS[row_units])
            rows.append(f"[{color}]{''.join(chars)}[/]")

        vis_box.border_title = vis_title
        vis_box.update("\n".join(rows))

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

    def _apply_table_visibility(self) -> DataTable:
        """Show only the table matching active_view / folder browse mode and return it."""
        track_table = self.query_one("#track-table", DataTable)
        settings_table = self.query_one("#settings-table", DataTable)
        folders_table = self.query_one("#playlist-folders-table", DataTable)

        if self.active_view == "settings":
            track_table.display = False
            folders_table.display = False
            settings_table.display = True
            return settings_table
        if self._in_folders_mode():
            track_table.display = False
            settings_table.display = False
            folders_table.display = True
            return folders_table
        settings_table.display = False
        folders_table.display = False
        track_table.display = True
        return track_table

    def _active_table(self) -> DataTable:
        if self.active_view == "settings":
            return self.query_one("#settings-table", DataTable)
        if self._in_folders_mode():
            return self.query_one("#playlist-folders-table", DataTable)
        return self.query_one("#track-table", DataTable)

    def _switch_view(self, view: str) -> None:
        if view not in _VIEW_ORDER:
            return
        if self.focus_mode:
            self.focus_mode = False
            self._apply_focus_layout()
        self.active_view = view
        self._filter_text = ""
        self._pending_delete_playlist = None
        if view == "playlists":
            self._playlist_browse_mode = "folders"

        self._reload_collections()
        tbl = self._apply_table_visibility()
        if view == "settings":
            self._refresh_settings_table(force=True)
        elif self._in_folders_mode():
            self._refresh_folders_table(force=True)
        else:
            self._refresh_table(force=True)
        tbl.focus()

    def _selected_track(self) -> tuple[int, Track] | None:
        if self._in_folders_mode():
            return None
        visible = self._visible_rows()
        if not visible:
            return None
        table = self.query_one("#track-table", DataTable)
        row = table.cursor_row
        if 0 <= row < len(visible):
            return visible[row]
        return None

    def _selected_folder(self) -> tuple[int, dict[str, Any]] | None:
        visible = self._visible_folders()
        if not visible:
            return None
        table = self.query_one("#playlist-folders-table", DataTable)
        row = table.cursor_row
        if 0 <= row < len(visible):
            return visible[row]
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
            self._filter_text = ""
            self._apply_table_visibility()
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
                self._filter_text = ""
                self._apply_table_visibility()
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
            self._filter_text = ""
            tbl = self._apply_table_visibility()
            self._refresh_table(force=True)
            tbl.focus()
            self._set_status(
                f"[bold #00ff87]Found {len(results)} tracks[/] for [bold #ffffff]{escape(query)}[/]. "
                "Press [bold #ffd43b]Enter[/] to play."
            )

        self._safe_ui(_done)

    def _move_folder_cursor_to(self, name: str) -> None:
        """Move #playlist-folders-table cursor to the visible folder matching name."""
        visible = self._visible_folders()
        table = self.query_one("#playlist-folders-table", DataTable)
        for row_idx, (_, item) in enumerate(visible):
            if item["name"] == name:
                table.move_cursor(row=row_idx)
                return

    def _reset_input_to_search(self) -> None:
        self._input_mode = "search"
        self._rename_target = None
        inp = self.query_one("#search-input", Input)
        inp.placeholder = _SEARCH_PLACEHOLDER

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id != "search-input" or self._input_mode != "filter":
            return
        if event.value == self._filter_text:
            return
        self._filter_text = event.value
        tbl = self._active_table()
        if self._in_folders_mode():
            self._refresh_folders_table(force=True)
        else:
            self._refresh_table(force=True)
        if tbl.row_count > 0:
            tbl.move_cursor(row=0)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id != "search-input":
            return
        val = event.value.strip()
        mode = self._input_mode

        if mode == "filter":
            self._filter_text = val
            self._reset_input_to_search()
            event.input.value = ""
            tbl = self._apply_table_visibility()
            if self._in_folders_mode():
                self._refresh_folders_table(force=True)
                total = len(self.playlist_summaries)
                shown = len(self._visible_folders())
            else:
                self._refresh_table(force=True)
                total = len(self._current_view_tracks())
                shown = len(self._visible_rows())
            tbl.focus()
            if val:
                self._set_status(
                    f"[#00e5ff]Filter locked:[/] '[bold #ffffff]{escape(val)}[/]' ({shown}/{total}, Esc to clear)."
                )
            else:
                self._set_status("[#00e5ff]Offline filter cleared.[/]")
            return

        if mode == "playlist_rename":
            target = self._rename_target
            self._reset_input_to_search()
            event.input.value = ""
            folders_tbl = self._apply_table_visibility()
            folders_tbl.focus()
            if not val or not target:
                self._set_status("[bold #ffd43b]\\[PLAYLIST][/] Rename cancelled.")
                return
            try:
                renamed = self.playlist_store.rename(target, val)
            except ValueError as exc:
                self._set_status(f"[bold #ff4d94]Rename error:[/] {escape(str(exc))}")
                return
            self._reload_collections()
            self._refresh_folders_table(force=True)
            self._move_folder_cursor_to(renamed)
            self._set_status(
                f"[bold #00ff87]\\[PLAYLIST][/] Renamed '[bold #ffffff]{escape(target)}[/]' to '[bold #ffffff]{escape(renamed)}[/]'."
            )
            return

        if mode == "playlist_create":
            self._reset_input_to_search()
            event.input.value = ""
            pending = self._pending_add_track
            self._pending_add_track = None
            if not val:
                self._active_table().focus()
                self._set_status("[bold #ffd43b]\\[PLAYLIST][/] Cancelled.")
                return
            created = self.playlist_store.create(val)
            if pending is not None:
                _, added = self.playlist_store.add_track(pending, name=created)
                self._reload_collections()
                tbl = self._apply_table_visibility()
                if self._in_folders_mode():
                    self._refresh_folders_table(force=True)
                else:
                    self._refresh_table(force=True)
                tbl.focus()
                verb = "Created playlist and added" if added else "Created playlist for"
                self._set_status(
                    f"[bold #00ff87]\\[PLAYLIST][/] {verb} [bold #ffffff]{escape(pending.title)}[/] "
                    f"in '[bold #ffffff]{escape(created)}[/]'."
                )
                return

            self.active_view = "playlists"
            self._playlist_browse_mode = "folders"
            self._filter_text = ""
            self._reload_collections()
            folders_tbl = self._apply_table_visibility()
            self._refresh_folders_table(force=True)
            self._move_folder_cursor_to(created)
            folders_tbl.focus()
            self._set_status(
                f"[bold #00ff87]\\[PLAYLIST][/] Created '[bold #ffffff]{escape(created)}[/]' - "
                "press [bold #ffd43b]Enter[/] to open or [bold #ffd43b]m[/] on any song to add tracks."
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
        elif event.data_table.id == "playlist-folders-table":
            self.action_open_selected_playlist()
        elif event.data_table.id == "track-table":
            self.action_play_selected()

    def action_focus_search(self) -> None:
        if self.focus_mode:
            self.focus_mode = False
            self._apply_focus_layout()
        self._input_mode = "search"
        inp = self.query_one("#search-input", Input)
        inp.placeholder = "Search YouTube Music (Enter to search, Esc to cancel)..."
        inp.focus()

    def action_focus_filter(self) -> None:
        if self.focus_mode:
            self.focus_mode = False
            self._apply_focus_layout()
        if self.active_view == "settings":
            self._set_status(
                "[bold #ffd43b]Offline filter is available in track and playlist views \\[1]-\\[5].[/]"
            )
            return
        self._input_mode = "filter"
        inp = self.query_one("#search-input", Input)
        inp.placeholder = "Filter current view offline (live, Enter keep, Esc clear)..."
        inp.value = self._filter_text
        inp.focus()

    def action_create_playlist(self) -> None:
        if self.focus_mode:
            self.focus_mode = False
            self._apply_focus_layout()
        self._input_mode = "playlist_create"
        inp = self.query_one("#search-input", Input)
        inp.value = ""
        inp.placeholder = "New Playlist Name (Enter to create, Esc to cancel)..."
        inp.focus()

    def action_rename_playlist(self) -> None:
        if not self._in_folders_mode():
            self._set_status("[bold #ffd43b]Open \\[5] Playlists folder list and press e to rename.[/]")
            return
        selection = self._selected_folder()
        if selection is None:
            self._set_status("[bold #ffd43b]No playlist folder selected to rename.[/]")
            return
        old_name = str(selection[1]["name"])
        self._rename_target = old_name
        self._input_mode = "playlist_rename"
        inp = self.query_one("#search-input", Input)
        inp.placeholder = f"Rename playlist '{old_name}' (Enter save, Esc cancel)..."
        inp.value = old_name
        inp.focus()

    def action_open_selected_playlist(self) -> None:
        selection = self._selected_folder()
        if selection is None:
            self._set_status("[bold #ffd43b]No playlist folder selected. Press c to create one.[/]")
            return
        name = str(selection[1]["name"])
        self.playlist_store.set_active_name(name)
        self._playlist_browse_mode = "tracks"
        self._filter_text = ""
        self._pending_delete_playlist = None
        self._reload_collections()
        tbl = self._apply_table_visibility()
        self._refresh_table(force=True)
        tbl.focus()
        self._set_status(
            f"[bold #ff4d94]\\[PLAYLIST][/] Opened '[bold #ffffff]{escape(name)}[/]' "
            f"({len(self.playlist_tracks)} tracks, [bold #ffd43b]Esc[/]/[bold #ffd43b]h[/] back to folders)."
        )

    def action_go_back(self) -> None:
        if self.active_view == "playlists" and self._playlist_browse_mode == "tracks":
            active_name = self.playlist_store.get_active_name()
            self._playlist_browse_mode = "folders"
            self._filter_text = ""
            self._pending_delete_playlist = None
            self._reload_collections()
            tbl = self._apply_table_visibility()
            self._refresh_folders_table(force=True)
            if active_name:
                self._move_folder_cursor_to(active_name)
            tbl.focus()
            self._set_status("[#00e5ff]Showing playlist folders (Enter to open, c new, e rename).[/]")

    def _sync_focus_widgets_after_refresh(self) -> None:
        self._tick_visualizer()
        if self.focus_mode:
            self._update_focus_now_playing(self.last_status)
            self._render_lyrics_panel(self.last_status)

    def action_toggle_focus_mode(self) -> None:
        self.focus_mode = not self.focus_mode
        self._apply_focus_layout()
        self._refresh_status_tick()
        self.call_after_refresh(self._sync_focus_widgets_after_refresh)

    def action_toggle_lyrics(self) -> None:
        if not self.focus_mode:
            self.focus_mode = True
            self.lyrics_visible = True
        else:
            self.lyrics_visible = not self.lyrics_visible
        current: Track | None = self.last_status.get("current_track")
        running = bool(self.last_status.get("running"))
        if self.lyrics_visible and running and current is not None and current.video_id:
            self._lyrics_missing_ids.discard(current.video_id)
        self._apply_focus_layout()
        if self.lyrics_visible:
            self._ensure_lyrics_for_track(current if running else None)
        self._refresh_status_tick()
        self.call_after_refresh(self._sync_focus_widgets_after_refresh)

    def action_focus_table(self) -> None:
        if isinstance(self.focused, Input):
            was_filter = self._input_mode == "filter"
            self._reset_input_to_search()
            inp = self.query_one("#search-input", Input)
            inp.value = ""
            self._pending_add_track = None
            if was_filter:
                self._filter_text = ""
                if self._in_folders_mode():
                    self._refresh_folders_table(force=True)
                else:
                    self._refresh_table(force=True)
            self._active_table().focus()
            return
        if self.focus_mode:
            if self.lyrics_visible:
                self.lyrics_visible = False
            else:
                self.focus_mode = False
            self._apply_focus_layout()
            self._refresh_status_tick()
            self.call_after_refresh(self._sync_focus_widgets_after_refresh)
            return
        if self._filter_active():
            self._filter_text = ""
            if self._in_folders_mode():
                self._refresh_folders_table(force=True)
            else:
                self._refresh_table(force=True)
            self._set_status("[#00e5ff]Cleared offline filter.[/]")
            return
        if self.active_view == "playlists" and self._playlist_browse_mode == "tracks":
            self.action_go_back()
            return
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
        if self.focus_mode:
            if (
                self.lyrics_visible
                and self._current_lyrics is not None
                and not self._current_lyrics.synced
            ):
                self._lyrics_scroll_offset += 1
                self._render_lyrics_panel(self.last_status)
            return
        self._active_table().action_cursor_down()

    def action_cursor_up(self) -> None:
        if self.focus_mode:
            if (
                self.lyrics_visible
                and self._current_lyrics is not None
                and not self._current_lyrics.synced
            ):
                self._lyrics_scroll_offset = max(0, self._lyrics_scroll_offset - 1)
                self._render_lyrics_panel(self.last_status)
            return
        self._active_table().action_cursor_up()

    def action_cursor_top(self) -> None:
        if self.focus_mode:
            if (
                self.lyrics_visible
                and self._current_lyrics is not None
                and not self._current_lyrics.synced
            ):
                self._lyrics_scroll_offset = 0
                self._render_lyrics_panel(self.last_status)
            return
        table = self._active_table()
        if table.row_count > 0:
            table.move_cursor(row=0)

    def action_cursor_bottom(self) -> None:
        if self.focus_mode:
            if (
                self.lyrics_visible
                and self._current_lyrics is not None
                and not self._current_lyrics.synced
            ):
                self._lyrics_scroll_offset += 10000
                self._render_lyrics_panel(self.last_status)
            return
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
            "mpris_enabled",
            "spectrum_mode",
            "sound_profile",
        ):
            updated = self.settings_store.cycle(key)
            self.cache_manager.wake()
            self._refresh_settings_table(force=True)
            if key == "sound_profile":
                try:
                    self.player.apply_sound_profile(updated.sound_profile)
                except MPVError as exc:
                    self._set_status(f"[bold #ff4d94]{escape(str(exc))}[/]")
                    return
                self._refresh_status_tick()
                label = SOUND_PROFILE_LABELS.get(updated.sound_profile, updated.sound_profile)
                self._set_status(
                    f"[bold #00ff87]\\[SOUND EQ][/] Profile: [bold #ffffff]{escape(label)}[/]."
                )
            elif key == "mpris_enabled":
                if updated.mpris_enabled:
                    self._run_bg(self.player._maybe_start_mpris)
                    self._set_status(
                        "[bold #00ff87]\\[SETTINGS][/] MPRIS Media Keys = [bold #ffffff]ON[/] "
                        "(global keyboard and KDE controls active)."
                    )
                else:
                    self._run_bg(self.player._maybe_stop_mpris)
                    self._set_status(
                        "[bold #ffd43b]\\[SETTINGS][/] MPRIS Media Keys = [bold #ffffff]OFF[/] "
                        "(MPRIS bridge stopped)."
                    )
            elif key == "spectrum_mode":
                if updated.spectrum_mode == "fft" and not self.is_headless:
                    self.spectrum_analyzer.start()
                else:
                    self.spectrum_analyzer.stop()
                self._tick_visualizer()
                self._set_status(
                    f"[bold #00ff87]\\[SETTINGS][/] Spectrum Mode = [bold #ffffff]{updated.spectrum_mode}[/]."
                )
            elif key in ("audio_quality", "stream_buffer_secs"):
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
        if self._in_folders_mode():
            self.action_open_selected_playlist()
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
        if self._in_folders_mode():
            selection = self._selected_folder()
            if selection is None:
                self._set_status("[bold #ffd43b]No playlist folder selected.[/]")
                return
            name = str(selection[1]["name"])
            self.playlist_store.set_active_name(name)
            tracks = self.liked_store.annotate(self.playlist_store.get_tracks(name))
            if not tracks:
                self._set_status(
                    f"[bold #ffd43b]Playlist '[bold #ffffff]{escape(name)}[/]' has no tracks.[/]"
                )
                return
            self.radio_autoplay = False
            self._set_status(
                f"[#00e5ff]Playing playlist[/] [bold #ffffff]{escape(name)}[/] ({len(tracks)} tracks)..."
            )
            self._run_bg(
                lambda: self._play_queue_bg(
                    tracks, 0, stay_in_view=True, radio_autoplay=False
                )
            )
            return

        if self._filter_active():
            visible = self._visible_rows()
            if not visible:
                self._set_status("[bold #ffd43b]Filtered view has no matching tracks.[/]")
                return
            filtered_tracks = [t for _, t in visible]
            cursor_row = self.query_one("#track-table", DataTable).cursor_row
            start_idx = max(0, min(cursor_row, len(filtered_tracks) - 1))
            self.radio_autoplay = False
            self._set_status(
                f"[#00e5ff]Playing {len(filtered_tracks)} filtered tracks...[/]"
            )
            self._run_bg(
                lambda: self._play_queue_bg(
                    filtered_tracks, start_idx, stay_in_view=True, radio_autoplay=False
                )
            )
            return

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

    def action_toggle_shuffle(self) -> None:
        try:
            enabled = self.player.toggle_shuffle()
        except MPVError as exc:
            self._set_status(f"[bold #ff4d94]{escape(str(exc))}[/]")
            return
        self.shuffle_on = enabled
        self._reload_collections()
        self._refresh_status_tick()
        if self._in_folders_mode():
            self._refresh_folders_table(force=True)
        else:
            self._refresh_table(force=True)
        if enabled:
            self._set_status(
                "[bold #ffd43b]\\[SHUFFLE: ON][/] Upcoming tracks shuffled smoothly."
            )
        else:
            self._set_status(
                "[bold #00e5ff]\\[SHUFFLE: OFF][/] Restored original queue order."
            )

    def action_cycle_repeat(self) -> None:
        try:
            mode = self.player.cycle_repeat()
        except (MPVError, ValueError) as exc:
            self._set_status(f"[bold #ff4d94]{escape(str(exc))}[/]")
            return
        self.repeat_mode = mode
        self._refresh_status_tick()
        self._set_status(f"[bold #ff4d94]\\[REPEAT: {mode.upper()}][/]")

    def action_cycle_sound_profile(self) -> None:
        try:
            profile = self.player.cycle_sound_profile()
        except (MPVError, ValueError) as exc:
            self._set_status(f"[bold #ff4d94]{escape(str(exc))}[/]")
            return
        if self.active_view == "settings":
            self._refresh_settings_table(force=True)
        self._refresh_status_tick()
        label = SOUND_PROFILE_LABELS.get(profile, profile)
        self._set_status(
            f"[bold #00ff87]\\[SOUND EQ][/] Profile: [bold #ffffff]{escape(label)}[/]"
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

        if self._in_folders_mode():
            selection = self._selected_folder()
            if selection is None:
                self._set_status("[bold #ffd43b]No playlist folder selected to delete.[/]")
                return
            name = str(selection[1]["name"])
            if (
                self._pending_delete_playlist is not None
                and self._pending_delete_playlist[0] == name
                and (now - self._pending_delete_playlist[1]) <= 3.5
            ):
                self.playlist_store.delete(name)
                self._pending_delete_playlist = None
                self._reload_collections()
                self._refresh_folders_table(force=True)
                self._set_status(
                    f"[bold #ffd43b]\\[DELETED][/] Deleted playlist '[bold #ffffff]{escape(name)}[/]'."
                )
            else:
                self._pending_delete_playlist = (name, now)
                self._set_status(
                    f"[bold #ff4d94]\\[CONFIRM][/] Press [bold #ffd43b]x[/] again within 3s to delete playlist "
                    f"'[bold #ffffff]{escape(name)}[/]'."
                )
            return

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

    def _add_to_playlist(self, track: Track, playlist_name: str | None = None) -> None:
        if playlist_name is None:
            names = self.playlist_store.list_names()
            if len(names) >= 2:
                def _on_picked(choice: str | None) -> None:
                    if choice is None:
                        self._set_status("[bold #ffd43b]\\[PLAYLIST][/] Add cancelled.")
                        return
                    if choice == _NEW_PLAYLIST_SENTINEL:
                        self._pending_add_track = track
                        self.action_create_playlist()
                        return
                    self._add_to_playlist(track, playlist_name=choice)

                self.push_screen(
                    PlaylistPickerModal(names, self.playlist_store.get_active_name(), track.title),
                    _on_picked,
                )
                return

        pl_name, added = self.playlist_store.add_track(track, name=playlist_name)
        self._reload_collections()
        if self._in_folders_mode():
            self._refresh_folders_table(force=True)
        else:
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
        self._playlist_browse_mode = "tracks"
        self._filter_text = ""
        self._pending_delete_playlist = None
        self._reload_collections()
        tbl = self._apply_table_visibility()
        self._refresh_table(force=True)
        tbl.focus()
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
        self.spectrum_analyzer.stop()
        self.cache_manager.stop()
        self.exit()

    def action_stop_quit(self) -> None:
        self.spectrum_analyzer.stop()
        self.cache_manager.stop()
        self.player.stop()
        self.exit()


def run_tui(focus: bool = False) -> None:
    """Launch the interactive Textual TUI."""
    YTMApp(initial_focus=focus).run()

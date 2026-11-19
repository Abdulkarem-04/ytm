"""High-contrast terminal CLI and TUI entrypoint for ytm."""

from __future__ import annotations

import argparse
import sys
import time
from typing import Any

from rich.console import Console
from rich.markup import escape
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from ytm import __version__
from ytm.api import YTMClient
from ytm.auth import remove_auth, run_interactive_auth
from ytm.cache_worker import clean_error_text
from ytm.config import AUTH_HEADERS_FILE
from ytm.downloader import TrackDownloader
from ytm.liked import LikedStore
from ytm.models import Track, format_duration
from ytm.player import MPVError, MPVPlayer
from ytm.playlists import PlaylistStore
from ytm.settings import SettingsStore
from ytm.tui import run_tui

console = Console()


def _build_progress_bar(position: float, duration: float, width: int = 28) -> str:
    """Render a crisp ASCII/Unicode progress bar with cursor indicator."""
    if duration <= 0:
        ratio = 0.0
    else:
        ratio = max(0.0, min(1.0, position / duration))
    filled = int(round(ratio * (width - 1)))
    left = "━" * filled
    right = "─" * max(0, width - 1 - filled)
    return f"{left}●{right}"


def _render_now_playing(status: dict[str, Any]) -> None:
    """Display the active track Now Playing panel and upcoming tracks."""
    if not status.get("running"):
        console.print(
            Panel(
                "[bold bright_yellow]\\[STOPPED][/bold bright_yellow] "
                "[bright_white]Background player is not running.[/bright_white]\n"
                "[bright_cyan]Start listening:[/bright_cyan] "
                "[bold bright_white]ytm play \"Daft Punk Veridis Quo\"[/bold bright_white] "
                "or [bold bright_white]ytm search \"lofi beats\"[/bold bright_white]",
                title="[bold bright_cyan]ytm status[/bold bright_cyan]",
                border_style="bright_cyan",
            )
        )
        return

    track: Track | None = status.get("current_track")
    paused: bool = bool(status.get("paused", False))
    time_pos: float = float(status.get("time_pos", 0.0) or 0.0)
    duration: float = float(status.get("duration", 0.0) or 0.0)
    volume: float = float(status.get("volume", 100.0) or 100.0)

    if track is None:
        console.print(
            Panel(
                "[bold bright_yellow]\\[IDLE][/bold bright_yellow] "
                "[bright_white]Daemon is running with no active track.[/bright_white]",
                title="[bold bright_cyan]ytm status[/bold bright_cyan]",
                border_style="bright_cyan",
            )
        )
        return

    state_tag = (
        "[bold bright_yellow]\\[PAUSED][/bold bright_yellow]"
        if paused
        else "[bold bright_green]\\[PLAYING][/bold bright_green]"
    )
    liked_tag = " [bold bright_magenta]\\[LIKED][/bold bright_magenta]" if track.is_liked else ""
    source_tag = (
        " [bright_green]\\[LOCAL][/bright_green]"
        if track.local_path
        else " [bright_cyan]\\[STREAM][/bright_cyan]"
    )
    shuffle_tag = " [bold bright_yellow]\\[SHUFFLE][/bold bright_yellow]" if status.get("shuffle") else ""
    repeat_mode = str(status.get("repeat_mode", "off"))
    repeat_tag = (
        f" [bold bright_magenta]\\[REPEAT: {repeat_mode.upper()}][/bold bright_magenta]"
        if repeat_mode in ("all", "one")
        else ""
    )

    bar = _build_progress_bar(time_pos, duration)
    timing = f"{format_duration(time_pos)} / {format_duration(duration)}"
    album_line = (
        f"[bright_magenta]Album:[/bright_magenta] [bright_white]{escape(track.album)}[/bright_white]\n"
        if track.album
        else ""
    )

    body = (
        f"{state_tag}{liked_tag}{source_tag}{shuffle_tag}{repeat_tag}  "
        f"[bold bright_white]{escape(track.title)}[/bold bright_white]\n"
        f"[bright_cyan]Artist:[/bright_cyan] [bold bright_cyan]{escape(track.artist)}[/bold bright_cyan]\n"
        f"{album_line}"
        f"[bright_green]{bar}[/bright_green]  "
        f"[bold bright_white]{timing}[/bold bright_white]  "
        f"[bright_yellow]Vol: {int(round(volume))}%[/bright_yellow]"
    )

    console.print(
        Panel(
            body,
            title="[bold bright_cyan]|> Now Playing[/bold bright_cyan]",
            border_style="bright_green" if not paused else "bright_yellow",
        )
    )

    upcoming: list[Track] = status.get("upcoming") or []
    if upcoming:
        _render_queue_table(
            upcoming[:5],
            title="Up Next (Top 5)",
            active_index=None,
            start_num=1,
        )


def _render_queue_table(
    tracks: list[Track],
    title: str,
    active_index: int | None = None,
    start_num: int = 1,
) -> None:
    """Render a high-contrast Rich table of tracks."""
    table = Table(
        title=f"[bold bright_cyan]{title}[/bold bright_cyan]",
        border_style="bright_blue",
        header_style="bold bright_yellow",
        show_lines=False,
    )
    table.add_column("#", justify="right", style="bold bright_yellow", width=4)
    table.add_column("Title", style="bold bright_white", no_wrap=False)
    table.add_column("Artist", style="bright_cyan", no_wrap=False)
    table.add_column("Album", style="bright_magenta", no_wrap=False)
    table.add_column("Duration", justify="right", style="bright_green", width=8)

    for offset, track in enumerate(tracks):
        idx = start_num + offset
        is_active = active_index is not None and offset == active_index
        marker = f"|>{idx}" if is_active else str(idx)
        title_text = Text(track.title, style="bold bright_green" if is_active else "bold bright_white")
        if track.is_liked:
            title_text.append(" [LIKED]", style="bold bright_magenta")
        table.add_row(
            marker,
            title_text,
            track.artist,
            track.album or "-",
            track.duration,
        )

    console.print(table)


def _start_track_with_radio(
    client: YTMClient,
    player: MPVPlayer,
    seed_track: Track,
    radio_limit: int = 15,
) -> list[Track]:
    """Build a radio queue starting with seed_track and launch playback."""
    queue: list[Track] = [seed_track]
    try:
        radio = client.get_radio_queue(seed_track.video_id, limit=radio_limit)
    except Exception:
        radio = []

    seen = {seed_track.video_id}
    for item in radio:
        if item.video_id not in seen:
            seen.add(item.video_id)
            queue.append(item)

    player.play_queue(queue, start_index=0)
    return queue


def _cmd_play(args: argparse.Namespace) -> int:
    query = " ".join(args.query).strip()
    if not query:
        console.print("[bold bright_red]Error:[/bold bright_red] Provide a search query to play.")
        return 1

    client = YTMClient()
    player = MPVPlayer()

    with console.status(f"[bright_cyan]Searching YouTube Music for {query!r}...[/bright_cyan]"):
        results = client.search_tracks(query, limit=5)

    if not results:
        console.print(f"[bold bright_yellow]No tracks found for {query!r}.[/bold bright_yellow]")
        return 1

    top_track = results[0]
    with console.status(f"[bright_cyan]Queueing {top_track.title!r} + Up Next radio...[/bright_cyan]"):
        _start_track_with_radio(client, player, top_track, radio_limit=15)

    status = player.get_status()
    _render_now_playing(status)
    return 0


def _cmd_search(args: argparse.Namespace) -> int:
    query = " ".join(args.query).strip()
    if not query:
        console.print("[bold bright_red]Error:[/bold bright_red] Provide a search query.")
        return 1

    client = YTMClient()
    with console.status(f"[bright_cyan]Searching YouTube Music for {query!r}...[/bright_cyan]"):
        tracks = client.search_tracks(query, limit=args.limit)

    if not tracks:
        console.print(f"[bold bright_yellow]No tracks found for {query!r}.[/bold bright_yellow]")
        return 1

    _render_queue_table(tracks, title=f"Search Results: {query}")

    chosen_index: int | None = None
    if args.pick is not None:
        if 1 <= args.pick <= len(tracks):
            chosen_index = args.pick - 1
        else:
            console.print(
                f"[bold bright_red]Invalid --pick index {args.pick}. "
                f"Must be between 1 and {len(tracks)}.[/bold bright_red]"
            )
            return 1
    elif not args.no_interactive and sys.stdin.isatty():
        try:
            raw = console.input(
                f"[bold bright_yellow]Enter track # (1-{len(tracks)}) to play, or Enter to cancel: [/bold bright_yellow]"
            ).strip()
        except (EOFError, KeyboardInterrupt):
            console.print()
            return 0
        if not raw:
            return 0
        if not raw.isdigit() or not (1 <= int(raw) <= len(tracks)):
            console.print("[bold bright_red]Invalid selection.[/bold bright_red]")
            return 1
        chosen_index = int(raw) - 1

    if chosen_index is not None:
        player = MPVPlayer()
        selected = tracks[chosen_index]
        with console.status(f"[bright_cyan]Starting {selected.title!r} + Up Next radio...[/bright_cyan]"):
            _start_track_with_radio(client, player, selected, radio_limit=15)
        _render_now_playing(player.get_status())

    return 0


def _cmd_status(_args: argparse.Namespace) -> int:
    player = MPVPlayer()
    _render_now_playing(player.get_status())
    return 0


def _cmd_toggle(_args: argparse.Namespace) -> int:
    player = MPVPlayer()
    paused = player.toggle_pause()
    status = player.get_status()
    track: Track | None = status.get("current_track")
    label = f" - [bold bright_white]{escape(track.title)}[/bold bright_white]" if track else ""
    if paused:
        console.print(f"[bold bright_yellow]\\[PAUSED][/bold bright_yellow]{label}")
    else:
        console.print(f"[bold bright_green]\\[PLAYING][/bold bright_green]{label}")
    return 0


def _cmd_next(_args: argparse.Namespace) -> int:
    player = MPVPlayer()
    track = player.next_track()
    if track:
        console.print(
            f"[bold bright_green]|> Next:[/bold bright_green] "
            f"[bold bright_white]{escape(track.title)}[/bold bright_white] "
            f"[bright_cyan]by {escape(track.artist)}[/bright_cyan]"
        )
    else:
        console.print("[bold bright_yellow]Advanced to next track.[/bold bright_yellow]")
    return 0


def _cmd_prev(_args: argparse.Namespace) -> int:
    player = MPVPlayer()
    track = player.prev_track()
    if track:
        console.print(
            f"[bold bright_green]<| Previous:[/bold bright_green] "
            f"[bold bright_white]{escape(track.title)}[/bold bright_white] "
            f"[bright_cyan]by {escape(track.artist)}[/bright_cyan]"
        )
    else:
        console.print("[bold bright_yellow]Moved to previous track.[/bold bright_yellow]")
    return 0


def _cmd_replay(_args: argparse.Namespace) -> int:
    player = MPVPlayer()
    player.replay()
    status = player.get_status()
    track: Track | None = status.get("current_track")
    label = f" [bold bright_white]{escape(track.title)}[/bold bright_white]" if track else ""
    console.print(f"[bold bright_green]\\[REPLAYING][/bold bright_green]{label} from 00:00")
    return 0


def _cmd_shuffle(args: argparse.Namespace) -> int:
    player = MPVPlayer()
    mode = getattr(args, "mode", "toggle")
    if mode == "on":
        enabled = player.set_shuffle(True)
    elif mode == "off":
        enabled = player.set_shuffle(False)
    else:
        enabled = player.toggle_shuffle()
    if enabled:
        console.print("[bold bright_yellow]\\[SHUFFLE: ON][/bold bright_yellow] Upcoming tracks shuffled.")
    else:
        console.print("[bold bright_cyan]\\[SHUFFLE: OFF][/bold bright_cyan] Original queue order restored.")
    return 0


def _cmd_repeat(args: argparse.Namespace) -> int:
    player = MPVPlayer()
    mode = getattr(args, "mode", "cycle")
    if mode == "cycle":
        resolved = player.cycle_repeat()
    else:
        resolved = player.set_repeat_mode(mode)
    console.print(f"[bold bright_magenta]\\[REPEAT: {resolved.upper()}][/bold bright_magenta]")
    return 0


def _cmd_queue(_args: argparse.Namespace) -> int:
    player = MPVPlayer()
    status = player.get_status()
    queue: list[Track] = status.get("queue") or player.load_queue()
    if not queue:
        console.print("[bold bright_yellow]Queue is empty.[/bold bright_yellow]")
        return 0
    active_idx = status.get("playlist_pos")
    if not isinstance(active_idx, int) or active_idx < 0:
        active_idx = None
    _render_queue_table(queue, title="Current Queue", active_index=active_idx)
    return 0


def _cmd_vol(args: argparse.Namespace) -> int:
    player = MPVPlayer()
    new_vol = player.set_volume(args.value)
    console.print(
        f"[bold bright_cyan]Volume:[/bold bright_cyan] "
        f"[bold bright_yellow]{int(round(new_vol))}%[/bold bright_yellow]"
    )
    return 0


def _cmd_stop(_args: argparse.Namespace) -> int:
    player = MPVPlayer()
    player.stop()
    console.print("[bold bright_yellow]\\[STOPPED][/bold bright_yellow] Background player terminated.")
    return 0


def _cmd_tui(args: argparse.Namespace) -> int:
    run_tui(focus=bool(getattr(args, "focus", False)))
    return 0


def _cmd_like(_args: argparse.Namespace) -> int:
    player = MPVPlayer()
    status = player.get_status()
    track: Track | None = status.get("current_track")
    if not status.get("running") or track is None:
        console.print("[bold bright_yellow]No active track is currently playing.[/bold bright_yellow]")
        return 1

    store = LikedStore()
    liked = store.toggle(track)
    player.sync_track_like(track.video_id, liked)

    client = YTMClient(liked_store=store)
    synced_remote = client.rate_track(track.video_id, liked) if client.authenticated else False
    remote_suffix = " [bright_green](synced to YouTube Music)[/bright_green]" if synced_remote else ""

    if liked:
        console.print(
            f"[bold bright_magenta]\\[LIKED][/bold bright_magenta] "
            f"[bold bright_white]{escape(track.title)}[/bold bright_white] "
            f"[bright_cyan]by {escape(track.artist)}[/bright_cyan]{remote_suffix}"
        )
    else:
        console.print(
            f"[bold bright_yellow]\\[UNLIKED][/bold bright_yellow] "
            f"[bold bright_white]{escape(track.title)}[/bold bright_white] "
            f"[bright_cyan]by {escape(track.artist)}[/bright_cyan]{remote_suffix}"
        )
    return 0


def _cmd_download(args: argparse.Namespace) -> int:
    store = LikedStore()
    player = MPVPlayer(liked_store=store)
    downloader = TrackDownloader()
    query = " ".join(args.query).strip()

    target: Track | None = None
    if query:
        client = YTMClient(liked_store=store)
        with console.status(f"[bright_cyan]Searching YouTube Music for {query!r}...[/bright_cyan]"):
            results = client.search_tracks(query, limit=5)
        if not results:
            console.print(f"[bold bright_yellow]No tracks found for {query!r}.[/bold bright_yellow]")
            return 1
        target = results[0]
    else:
        status = player.get_status()
        target = status.get("current_track")
        if not status.get("running") or target is None:
            console.print(
                "[bold bright_yellow]No active track playing. Provide a query: ytm download \"<song>\"[/bold bright_yellow]"
            )
            return 1

    with console.status(f"[bright_cyan]Downloading {target.title!r}...[/bright_cyan]"):
        saved_path = downloader.download(target)

    store.update_local_path(target.video_id, str(saved_path))
    player.sync_track_local_path(target.video_id, str(saved_path))
    console.print(
        f"[bold bright_green]\\[CACHED][/bold bright_green] "
        f"[bold bright_white]{escape(target.title)}[/bold bright_white] -> "
        f"[bright_cyan]{escape(str(saved_path))}[/bright_cyan]"
    )
    return 0


def _cmd_auth(args: argparse.Namespace) -> int:
    store = LikedStore()
    if args.logout:
        removed = remove_auth()
        if removed:
            console.print(
                f"[bold bright_yellow]\\[LOGGED OUT][/bold bright_yellow] Removed [bright_cyan]{AUTH_HEADERS_FILE}[/bright_cyan]."
            )
        else:
            console.print("[bold bright_yellow]No saved YouTube Music credentials found.[/bold bright_yellow]")
        return 0

    if args.status:
        liked_count = len(store.list_tracks())
        if AUTH_HEADERS_FILE.is_file():
            console.print(
                f"[bold bright_green]\\[SYNCED][/bold bright_green] Authenticated via "
                f"[bright_cyan]{AUTH_HEADERS_FILE}[/bright_cyan] "
                f"([bold bright_white]{liked_count}[/bold bright_white] local liked tracks)."
            )
        else:
            console.print(
                f"[bold bright_cyan]\\[GUEST MODE][/bold bright_cyan] Not linked to a YouTube Music account "
                f"([bold bright_white]{liked_count}[/bold bright_white] local liked tracks). "
                "Run [bold bright_white]ytm auth[/bold bright_white] to link."
            )
        return 0

    if args.sync:
        client = YTMClient(liked_store=store)
        if not client.authenticated:
            console.print(
                "[bold bright_yellow]Not authenticated.[/bold bright_yellow] "
                "Run [bold bright_white]ytm auth[/bold bright_white] first."
            )
            return 1
        with console.status("[bright_cyan]Syncing Liked Music from YouTube Music...[/bright_cyan]"):
            remote = client.fetch_liked_songs(limit=200)
            added = store.merge_tracks(remote)
        console.print(
            f"[bold bright_green]\\[SYNCED][/bold bright_green] "
            f"Fetched [bold bright_white]{len(remote)}[/bold bright_white] liked tracks "
            f"([bold bright_cyan]{added}[/bold bright_cyan] newly added)."
        )
        return 0

    return run_interactive_auth(console)


def _cmd_cache_liked(args: argparse.Namespace) -> int:
    store = LikedStore()
    playlists = PlaylistStore()
    settings_store = SettingsStore()
    player = MPVPlayer(liked_store=store, settings_store=settings_store)
    downloader = TrackDownloader(settings_store=settings_store)

    pending = [
        t
        for t in store.list_tracks()
        if t.video_id and downloader.find_existing(t.video_id) is None
    ]
    if args.limit is not None and args.limit > 0:
        pending = pending[: args.limit]

    if not pending:
        console.print(
            f"[bold bright_green]\\[CACHED][/bold bright_green] "
            f"All liked tracks are already cached in [bright_cyan]{escape(str(downloader.music_dir))}[/bright_cyan]."
        )
        return 0

    total = len(pending)
    cached_count = 0
    for idx, track in enumerate(pending, 1):
        console.print(
            f"[bright_cyan]\\[{idx}/{total}][/bright_cyan] "
            f"Caching [bold bright_white]{escape(track.title)}[/bold bright_white] "
            f"[bright_cyan]by {escape(track.artist)}[/bright_cyan]..."
        )
        try:
            saved = downloader.download(track)
        except KeyboardInterrupt:
            console.print(
                f"\n[bold bright_yellow]Interrupted after caching {cached_count}/{total} tracks.[/bold bright_yellow]"
            )
            return 130
        except Exception as exc:
            console.print(
                f"[bold bright_yellow]Skipped:[/bold bright_yellow] {escape(clean_error_text(exc))}"
            )
        else:
            local_str = str(saved)
            store.update_local_path(track.video_id, local_str)
            playlists.update_local_path(track.video_id, local_str)
            player.sync_track_local_path(track.video_id, local_str)
            cached_count += 1
            console.print(
                f"[bold bright_green]\\[SAVED][/bold bright_green] {escape(local_str)}"
            )

        if idx < total:
            try:
                time.sleep(1.0)
            except KeyboardInterrupt:
                console.print(
                    f"\n[bold bright_yellow]Interrupted after caching {cached_count}/{total} tracks.[/bold bright_yellow]"
                )
                return 130

    console.print(
        f"[bold bright_green]\\[DONE][/bold bright_green] "
        f"Cached [bold bright_white]{cached_count}[/bold bright_white]/{total} liked tracks."
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    """Build the top-level CLI argument parser."""
    parser = argparse.ArgumentParser(
        prog="ytm",
        description="Terminal YouTube Music player powered by ytmusicapi, yt-dlp, and mpv.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"ytm {__version__}",
    )
    parser.add_argument(
        "--focus",
        action="store_true",
        help="Launch directly into minimalist Focus Mode",
    )

    sub = parser.add_subparsers(dest="command")

    p_play = sub.add_parser("play", help="Search and play the top match with auto-radio queue")
    p_play.add_argument("query", nargs="+", help="Song title or artist query")
    p_play.set_defaults(func=_cmd_play)

    p_search = sub.add_parser("search", help="Search YouTube Music and pick a track to play")
    p_search.add_argument("query", nargs="+", help="Search query")
    p_search.add_argument("--limit", type=int, default=15, help="Max results (default: 15)")
    p_search.add_argument(
        "--no-interactive",
        action="store_true",
        help="Print results without prompting to select a track",
    )
    p_search.add_argument(
        "--pick",
        type=int,
        default=None,
        help="Automatically play result number N (1-based)",
    )
    p_search.set_defaults(func=_cmd_search)

    p_status = sub.add_parser("status", help="Show current playback status and Up Next queue")
    p_status.set_defaults(func=_cmd_status)

    p_toggle = sub.add_parser(
        "toggle",
        aliases=["pause", "resume"],
        help="Toggle play/pause on the active session",
    )
    p_toggle.set_defaults(func=_cmd_toggle)

    p_next = sub.add_parser("next", help="Skip to the next track in the queue")
    p_next.set_defaults(func=_cmd_next)

    p_prev = sub.add_parser("prev", help="Return to the previous track in the queue")
    p_prev.set_defaults(func=_cmd_prev)

    p_replay = sub.add_parser("replay", help="Restart the current track from 00:00")
    p_replay.set_defaults(func=_cmd_replay)

    p_shuffle = sub.add_parser("shuffle", help="Toggle or set shuffle mode")
    p_shuffle.add_argument(
        "mode",
        nargs="?",
        choices=["on", "off", "toggle"],
        default="toggle",
        help="Shuffle state (default: toggle)",
    )
    p_shuffle.set_defaults(func=_cmd_shuffle)

    p_repeat = sub.add_parser("repeat", help="Cycle or set repeat mode (off, all, one)")
    p_repeat.add_argument(
        "mode",
        nargs="?",
        choices=["off", "all", "one", "cycle"],
        default="cycle",
        help="Repeat mode (default: cycle)",
    )
    p_repeat.set_defaults(func=_cmd_repeat)

    p_queue = sub.add_parser("queue", help="Display the full playback queue")
    p_queue.set_defaults(func=_cmd_queue)

    p_vol = sub.add_parser("vol", help="Set or adjust volume (e.g. 80, +10, -10)")
    p_vol.add_argument("value", help="Target volume (0-150) or relative delta (+N/-N)")
    p_vol.set_defaults(func=_cmd_vol)

    p_stop = sub.add_parser("stop", help="Stop playback and quit the background mpv daemon")
    p_stop.set_defaults(func=_cmd_stop)

    p_tui = sub.add_parser("tui", help="Launch the interactive multi-pane terminal UI")
    p_tui.add_argument(
        "--focus",
        action="store_true",
        help="Launch directly into minimalist Focus Mode",
    )
    p_tui.set_defaults(func=_cmd_tui)

    p_like = sub.add_parser("like", help="Toggle like on the currently playing track")
    p_like.set_defaults(func=_cmd_like)

    p_download = sub.add_parser(
        "download",
        help="Cache audio locally into ~/Music/ytm (current track or search query)",
    )
    p_download.add_argument("query", nargs="*", help="Optional song query to search and cache")
    p_download.set_defaults(func=_cmd_download)

    p_auth = sub.add_parser("auth", help="Manage YouTube Music account authentication and sync")
    auth_grp = p_auth.add_mutually_exclusive_group()
    auth_grp.add_argument("--status", action="store_true", help="Show account link status")
    auth_grp.add_argument("--sync", action="store_true", help="Sync Liked Music from YouTube Music")
    auth_grp.add_argument("--logout", action="store_true", help="Remove saved auth credentials")
    p_auth.set_defaults(func=_cmd_auth)

    p_cache_liked = sub.add_parser(
        "cache-liked",
        help="Politely download uncached Liked Music tracks into ~/Music/ytm",
    )
    p_cache_liked.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Optional maximum number of tracks to cache in this run",
    )
    p_cache_liked.set_defaults(func=_cmd_cache_liked)

    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint for ytm."""
    parser = build_parser()
    args = parser.parse_args(argv)
    handler = getattr(args, "func", None)
    if handler is None:
        if sys.stdout.isatty() and sys.stdin.isatty():
            run_tui(focus=bool(getattr(args, "focus", False)))
            return 0
        return _cmd_status(args)
    try:
        return int(handler(args))
    except (MPVError, ValueError) as exc:
        console.print(f"[bold bright_red]Error:[/bold bright_red] {exc}")
        return 1
    except KeyboardInterrupt:
        console.print()
        return 130


if __name__ == "__main__":
    raise SystemExit(main())

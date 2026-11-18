"""Stage 2 verification tests for LikedStore, MPVPlayer extensions, and Textual TUI."""

from __future__ import annotations

import asyncio
import math
import os
import struct
import wave
from pathlib import Path

from textual.widgets import DataTable, Input, Static

from ytm.cli import build_parser
from ytm.liked import LikedStore
from ytm.models import Track
from ytm.player import MPVPlayer
from ytm.tui import YTMApp


class _FakeYTMClient:
    """Offline test double for YTMClient."""

    authenticated = False

    def __init__(self, liked_store: LikedStore) -> None:
        self.liked_store = liked_store

    def search_tracks(self, query: str, limit: int = 15) -> list[Track]:
        tracks = [
            Track(
                video_id="vid_search_1",
                title=f"Result for {query} [Remix]",
                artist="Test Artist",
                album="Test Album",
                duration="03:45",
                duration_seconds=225,
            )
        ]
        return self.liked_store.annotate(tracks[:limit])

    def get_radio_queue(self, video_id: str, limit: int = 20) -> list[Track]:
        return []


def _write_test_wav(path: Path, duration_sec: float = 8.0, freq: float = 440.0) -> Path:
    sample_rate = 16000
    n_samples = int(sample_rate * duration_sec)
    with wave.open(str(path), "w") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        frames = bytearray()
        for i in range(n_samples):
            val = int(400 * math.sin(2.0 * math.pi * freq * i / sample_rate))
            frames.extend(struct.pack("<h", val))
        wf.writeframes(bytes(frames))
    return path


def test_liked_store_persistence_and_annotate(tmp_path: Path) -> None:
    liked_file = tmp_path / "liked_tracks.json"
    store = LikedStore(liked_file=liked_file)

    t1 = Track(video_id="v1", title="First Song", artist="Artist A", duration="03:10")
    t2 = Track(video_id="v2", title="Second Song", artist="Artist B", duration="04:20")

    assert store.list_tracks() == []
    assert store.is_liked("v1") is False

    assert store.toggle(t1) is True
    assert t1.is_liked is True
    assert store.is_liked("v1") is True

    assert store.toggle(t2) is True
    # Most recently liked track should appear first
    listed = store.list_tracks()
    assert [t.video_id for t in listed] == ["v2", "v1"]
    assert all(t.is_liked for t in listed)

    # New instance reads persisted file
    store_reloaded = LikedStore(liked_file=liked_file)
    fresh = [
        Track(video_id="v1", title="First Song", artist="Artist A"),
        Track(video_id="v3", title="Third Song", artist="Artist C"),
    ]
    store_reloaded.annotate(fresh)
    assert fresh[0].is_liked is True
    assert fresh[1].is_liked is False

    # Unlike t1
    assert store_reloaded.toggle(t1) is False
    assert store_reloaded.is_liked("v1") is False
    assert [t.video_id for t in store_reloaded.list_tracks()] == ["v2"]


def test_mpv_play_index_and_seek_relative(tmp_path: Path) -> None:
    sock_path = Path(f"/tmp/ytm-test-s2-{os.getpid()}.sock")
    state_file = tmp_path / "state.json"
    liked_store = LikedStore(liked_file=tmp_path / "liked.json")
    wav1 = _write_test_wav(tmp_path / "a.wav", duration_sec=8.0, freq=220.0)
    wav2 = _write_test_wav(tmp_path / "b.wav", duration_sec=8.0, freq=330.0)

    tracks = [
        Track(
            video_id="idx0",
            title="Track Zero",
            artist="Tester",
            duration="00:08",
            duration_seconds=8,
            local_path=str(wav1),
        ),
        Track(
            video_id="idx1",
            title="Track One",
            artist="Tester",
            duration="00:08",
            duration_seconds=8,
            local_path=str(wav2),
        ),
    ]

    player = MPVPlayer(socket_path=sock_path, state_file=state_file, liked_store=liked_store)
    try:
        player.play_queue(tracks, start_index=0)
        jumped = player.play_index(1)
        assert jumped is not None
        assert jumped.video_id == "idx1"

        player.seek_relative(2.0)
        status = player.get_status()
        assert status["running"] is True
        assert status["playlist_pos"] == 1
    finally:
        player.stop()


def test_tui_headless_navigation_search_and_like(tmp_path: Path) -> None:
    sock_path = Path(f"/tmp/ytm-test-tui-{os.getpid()}.sock")
    state_file = tmp_path / "state.json"
    liked_store = LikedStore(liked_file=tmp_path / "liked.json")
    music_dir = tmp_path / "music"
    music_dir.mkdir()

    player = MPVPlayer(socket_path=sock_path, state_file=state_file, liked_store=liked_store)
    seed_tracks = [
        Track(
            video_id="q1",
            title="Veridis Quo [Live]",
            artist="Daft Punk",
            album="Discovery",
            duration="05:46",
            duration_seconds=346,
        ),
        Track(
            video_id="q2",
            title="Midnight City",
            artist="M83",
            album="Hurry Up",
            duration="04:03",
            duration_seconds=243,
        ),
    ]
    player.save_queue(seed_tracks)
    client = _FakeYTMClient(liked_store=liked_store)

    async def _run() -> None:
        app = YTMApp(
            player=player,
            client=client,  # type: ignore[arg-type]
            liked_store=liked_store,
            music_dir=music_dir,
        )
        async with app.run_test(size=(120, 36)) as pilot:
            await pilot.pause()

            # Verify core widgets mounted
            assert app.query_one("#top-bar") is not None
            assert app.query_one("#search-input", Input) is not None
            assert app.query_one("#sidebar") is not None
            table = app.query_one("#track-table", DataTable)
            assert app.query_one("#now-playing-box", Static) is not None
            assert app.query_one("#visualizer-box", Static) is not None
            assert table.row_count == 2

            # Switch views with 2, 3, 4, 1
            await pilot.press("2")
            await pilot.pause()
            assert app.active_view == "search"

            await pilot.press("3")
            await pilot.pause()
            assert app.active_view == "liked"
            assert table.row_count == 0

            await pilot.press("4")
            await pilot.pause()
            assert app.active_view == "local"

            await pilot.press("1")
            await pilot.pause()
            assert app.active_view == "queue"
            assert table.row_count == 2

            # Move down with j and like selected row with L
            await pilot.press("j")
            await pilot.pause()
            assert table.cursor_row == 1

            await pilot.press("L")
            await pilot.pause()
            assert liked_store.is_liked("q2") is True

            # Switch to Liked Music view and confirm q2 appears
            await pilot.press("3")
            await pilot.pause()
            assert app.active_view == "liked"
            assert table.row_count == 1

            # Focus search with /, type query containing binding letters, submit, Esc back
            await pilot.press("slash")
            await pilot.pause()
            search_input = app.query_one("#search-input", Input)
            assert app.focused is search_input
            await pilot.press("j", "a", "z", "z", "enter")
            await pilot.pause(0.2)
            assert search_input.value == "jazz"
            assert app.active_view == "search"
            assert table.row_count == 1

            await pilot.press("slash")
            await pilot.pause()
            assert app.focused is search_input
            await pilot.press("escape")
            await pilot.pause()
            assert app.focused is table

            # Detach with q
            await pilot.press("q")

    asyncio.run(_run())


def test_cli_stage2_subcommands() -> None:
    parser = build_parser()
    assert parser.parse_args(["tui"]).command == "tui"
    assert parser.parse_args(["like"]).command == "like"

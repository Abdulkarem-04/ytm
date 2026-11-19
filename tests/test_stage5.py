"""Stage 5 verification tests for safe like/undo, playlists, collection-only playback, and cover art."""

from __future__ import annotations

import asyncio
import math
import os
import struct
import subprocess
import wave
from pathlib import Path

from textual.widgets import DataTable, Input, Static

from ytm.art import CoverArtRenderer
from ytm.liked import LikedStore
from ytm.models import Track
from ytm.player import MPVPlayer
from ytm.playlists import PlaylistStore
from ytm.tui import YTMApp


def _write_test_wav(path: Path, duration_sec: float = 4.0, freq: float = 330.0) -> Path:
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


def test_liked_store_add_only_unlike_and_undo(tmp_path: Path) -> None:
    store = LikedStore(liked_file=tmp_path / "liked.json")
    t1 = Track(video_id="safe1", title="Safe Song One", artist="Artist One")
    t2 = Track(video_id="safe2", title="Safe Song Two", artist="Artist Two")

    assert store.like(t1) is True
    # Calling like again must remain idempotent and never remove the track
    assert store.like(t1) is False
    assert store.is_liked("safe1") is True

    assert store.like(t2) is True
    assert [t.video_id for t in store.list_tracks()] == ["safe2", "safe1"]

    # Explicit unlike moves track to undo history
    assert store.unlike(t1) is True
    assert store.is_liked("safe1") is False
    assert [t.video_id for t in store.list_tracks()] == ["safe2"]

    # Popping undo restores it
    restored = store.pop_undo()
    assert restored is not None
    assert restored.video_id == "safe1"
    assert store.is_liked("safe1") is True
    assert store.pop_undo() is None


def test_playlist_store_crud_and_cycling(tmp_path: Path) -> None:
    pl_file = tmp_path / "playlists.json"
    store = PlaylistStore(playlists_file=pl_file)
    assert store.list_names() == []
    assert store.get_active_name() is None

    # Adding before creating falls back to 'Favorites'
    t1 = Track(video_id="p1", title="Chill One", artist="A")
    t2 = Track(video_id="p2", title="Chill Two", artist="B")
    name, added = store.add_track(t1)
    assert name == "Favorites"
    assert added is True
    _, added_dup = store.add_track(t1)
    assert added_dup is False

    created = store.create("Night Drive")
    assert created == "Night Drive"
    assert store.get_active_name() == "Night Drive"
    store.add_track(t2)
    assert [t.video_id for t in store.get_tracks("Night Drive")] == ["p2"]

    # Cycle active playlist backward and forward
    assert store.cycle_active(-1) == "Favorites"
    assert store.cycle_active(1) == "Night Drive"

    # Persistence check
    reloaded = PlaylistStore(playlists_file=pl_file)
    assert reloaded.list_names() == ["Favorites", "Night Drive"]
    assert reloaded.remove_track("p2", "Night Drive") is True
    assert reloaded.get_tracks("Night Drive") == []
    assert reloaded.delete("Night Drive") is True
    assert reloaded.list_names() == ["Favorites"]


def test_cover_art_renderer_placeholder_and_ffmpeg_render(tmp_path: Path) -> None:
    renderer = CoverArtRenderer(covers_dir=tmp_path / "covers")
    placeholder = renderer.placeholder_markup()
    assert len(placeholder.splitlines()) == 7

    # Generate a 16x16 JPEG via ffmpeg and verify Truecolor half-block rendering
    sample_jpg = tmp_path / "sample.jpg"
    subprocess.check_call(
        [
            "ffmpeg",
            "-y",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=cyan:s=28x28",
            "-vframes",
            "1",
            str(sample_jpg),
        ]
    )
    rendered = renderer.render_file(sample_jpg, width=14, height_cells=7)
    lines = rendered.splitlines()
    assert len(lines) == 7
    assert all("▀" in line for line in lines)


class _DummyYTMClient:
    authenticated = False

    def search_tracks(self, query: str, limit: int = 15) -> list[Track]:
        return []

    def get_radio_queue(self, video_id: str, limit: int = 20) -> list[Track]:
        raise AssertionError("Radio queue should not be fetched during collection-only playback")


def test_tui_safe_like_undo_custom_playlists_and_collection_playback(tmp_path: Path) -> None:
    sock_path = Path(f"/tmp/ytm-test-s5-{os.getpid()}.sock")
    state_file = tmp_path / "state.json"
    liked_store = LikedStore(liked_file=tmp_path / "liked.json")
    playlist_store = PlaylistStore(playlists_file=tmp_path / "playlists.json")
    art_renderer = CoverArtRenderer(covers_dir=tmp_path / "covers")
    music_dir = tmp_path / "music"
    music_dir.mkdir()

    wav1 = _write_test_wav(tmp_path / "s1.wav", freq=260.0)
    wav2 = _write_test_wav(tmp_path / "s2.wav", freq=390.0)

    t1 = Track(
        video_id="liked_1",
        title="Only In Liked",
        artist="Artist One",
        album="Album One",
        duration="00:04",
        duration_seconds=4,
        local_path=str(wav1),
    )
    t2 = Track(
        video_id="liked_2",
        title="Second Liked",
        artist="Artist Two",
        album="Album Two",
        duration="00:04",
        duration_seconds=4,
        local_path=str(wav2),
    )
    liked_store.like(t2)
    liked_store.like(t1)

    player = MPVPlayer(socket_path=sock_path, state_file=state_file, liked_store=liked_store)
    client = _DummyYTMClient()

    try:
        async def _run() -> None:
            app = YTMApp(
                player=player,
                client=client,  # type: ignore[arg-type]
                liked_store=liked_store,
                playlist_store=playlist_store,
                art_renderer=art_renderer,
                music_dir=music_dir,
            )
            async with app.run_test(size=(120, 40)) as pilot:
                await pilot.pause()
                assert app.query_one("#cover-box", Static) is not None

                # Switch to [3] Liked Music
                await pilot.press("3")
                await pilot.pause()
                assert app.active_view == "liked"
                table = app.query_one("#track-table", DataTable)
                assert table.row_count == 2

                # Press L twice on an already-liked track: must NOT remove it
                await pilot.press("L")
                await pilot.pause()
                await pilot.press("L")
                await pilot.pause()
                assert liked_store.is_liked("liked_1") is True
                assert table.row_count == 2

                # Press x to remove liked_1, then u to undo and restore it
                await pilot.press("x")
                await pilot.pause()
                assert liked_store.is_liked("liked_1") is False
                assert table.row_count == 1

                await pilot.press("u")
                await pilot.pause()
                assert liked_store.is_liked("liked_1") is True
                assert table.row_count == 2

                # Press c to create a custom playlist named 'Chill'
                await pilot.press("c")
                await pilot.pause()
                inp = app.query_one("#search-input", Input)
                assert app.focused is inp
                await pilot.press("C", "h", "i", "l", "l", "enter")
                await pilot.pause()
                assert app.active_view == "playlists"
                assert playlist_store.get_active_name() == "Chill"
                folders_table = app.query_one("#playlist-folders-table", DataTable)
                assert app.focused is folders_table

                # Switch back to Liked Music, add both tracks with m
                await pilot.press("3")
                await pilot.pause()
                await pilot.press("m")
                await pilot.pause()
                await pilot.press("j")
                await pilot.pause()
                await pilot.press("m")
                await pilot.pause()

                # Switch to [5] Playlists folder list, open Chill, and play inside playlist view
                await pilot.press("5")
                await pilot.pause()
                assert app.active_view == "playlists"
                assert folders_table.row_count == 1
                await pilot.press("enter")
                await pilot.pause()
                assert table.display is True
                assert table.row_count == 2

                await pilot.press("enter")
                await pilot.pause(0.4)

                # Must stay in view 5 and play ONLY the playlist with radio_autoplay == False
                assert app.active_view == "playlists"
                assert app.radio_autoplay is False
                status = player.get_status()
                assert status["running"] is True
                assert status["playlist_count"] == 2
                assert [t.video_id for t in status["queue"]] == ["liked_1", "liked_2"]

                await pilot.press("q")

        asyncio.run(_run())
    finally:
        player.stop()

"""Stage 7 verification tests for Phase 1: Shuffle/Repeat, folder playlists, picker modal, and \\ filter."""

from __future__ import annotations

import asyncio
import math
import os
import random
import struct
import time
import wave
from pathlib import Path

import pytest
from textual.widgets import DataTable, Input

from ytm.art import CoverArtRenderer
from ytm.cli import build_parser
from ytm.liked import LikedStore
from ytm.models import Track
from ytm.player import MPVPlayer
from ytm.playlists import PlaylistStore
from ytm.tui import PlaylistPickerModal, YTMApp


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


def test_playlist_store_rename_and_summaries(tmp_path: Path) -> None:
    pl_file = tmp_path / "playlists.json"
    store = PlaylistStore(playlists_file=pl_file)
    local_file = _write_test_wav(tmp_path / "local.wav", duration_sec=1.0)

    store.create("Chill")
    store.create("Drive")
    store.set_active_name("Chill")

    store.add_track(Track(video_id="v1", title="T1", artist="A1", local_path=str(local_file)), name="Chill")
    store.add_track(Track(video_id="v2", title="T2", artist="A2"), name="Chill")
    store.add_track(Track(video_id="v3", title="T3", artist="A3"), name="Drive")

    store.set_active_name("Chill")
    renamed = store.rename("Chill", "Evening")
    assert renamed == "Evening"
    assert store.get_active_name() == "Evening"
    assert store.list_names() == ["Evening", "Drive"]

    with pytest.raises(ValueError):
        store.rename("Evening", "   ")
    with pytest.raises(ValueError):
        store.rename("Missing", "NewName")
    with pytest.raises(ValueError):
        store.rename("Evening", "Drive")

    summaries = store.list_summaries()
    assert summaries == [
        {"name": "Evening", "count": 2, "local": 1, "active": True},
        {"name": "Drive", "count": 1, "local": 0, "active": False},
    ]


def test_mpv_shuffle_zero_interruption_and_restore_order(tmp_path: Path) -> None:
    sock_path = Path(f"/tmp/ytm-test-s7-shuf-{os.getpid()}.sock")
    state_file = tmp_path / "state.json"
    liked_store = LikedStore(liked_file=tmp_path / "liked.json")
    player = MPVPlayer(socket_path=sock_path, state_file=state_file, liked_store=liked_store)

    tracks = [
        Track(
            video_id=f"s{i}",
            title=f"Track {i}",
            artist="Artist",
            duration="00:05",
            duration_seconds=5,
            local_path=str(_write_test_wav(tmp_path / f"s{i}.wav", duration_sec=5.0, freq=220.0 + i * 40)),
        )
        for i in range(5)
    ]

    try:
        player._rng = random.Random(7)
        player.play_queue(tracks, start_index=2, radio_autoplay=False)
        time.sleep(0.25)

        before = player.get_status()
        assert before["running"] is True
        assert before["current_track"].video_id == "s2"
        assert before["playlist_pos"] == 2

        # Turn Shuffle ON: currently playing track remains s2 without stopping, moves cleanly to index 0
        assert player.set_shuffle(True) is True
        after_on = player.get_status()
        assert after_on["shuffle"] is True
        assert after_on["current_track"].video_id == "s2"
        assert after_on["playlist_pos"] == 0
        assert after_on["playlist_count"] == 5
        assert {t.video_id for t in after_on["queue"]} == {"s0", "s1", "s2", "s3", "s4"}

        # Turn Shuffle OFF: original order s0..s4 is restored with s2 at index 2
        assert player.set_shuffle(False) is False
        after_off = player.get_status()
        assert after_off["shuffle"] is False
        assert [t.video_id for t in after_off["queue"]] == ["s0", "s1", "s2", "s3", "s4"]
        assert after_off["playlist_pos"] == 2
        assert after_off["playlist_count"] == 5
        assert after_off["current_track"].video_id == "s2"
    finally:
        player.stop()


def test_mpv_repeat_modes_and_guarded_replace_upcoming(tmp_path: Path) -> None:
    sock_path = Path(f"/tmp/ytm-test-s7-rep-{os.getpid()}.sock")
    state_file = tmp_path / "state.json"
    liked_store = LikedStore(liked_file=tmp_path / "liked.json")
    player = MPVPlayer(socket_path=sock_path, state_file=state_file, liked_store=liked_store)

    w0 = _write_test_wav(tmp_path / "r0.wav", duration_sec=4.0, freq=260.0)
    w1 = _write_test_wav(tmp_path / "r1.wav", duration_sec=4.0, freq=330.0)
    w1_new = _write_test_wav(tmp_path / "r1_new.wav", duration_sec=4.0, freq=440.0)
    t0 = Track(video_id="r0", title="Zero", artist="A", local_path=str(w0))
    t1 = Track(video_id="r1", title="One", artist="A", local_path=str(w1))

    try:
        player.play_queue([t0, t1], start_index=0, radio_autoplay=False)
        assert player.load_repeat_mode() == "off"

        assert player.cycle_repeat() == "all"
        assert player._get_property("loop-playlist") == "inf"
        assert player._get_property("loop-file") in (False, "no")

        assert player.cycle_repeat() == "one"
        assert player._get_property("loop-file") == "inf"
        assert player._get_property("loop-playlist") in (False, "no")

        assert player.cycle_repeat() == "off"
        assert player._get_property("loop-file") in (False, "no")
        assert player._get_property("loop-playlist") in (False, "no")

        # Stale expected_video_id prevents accidental swap after shuffle reorder
        assert player.replace_upcoming_entry(1, str(w1_new), expected_video_id="wrong_id") is False
        assert player.replace_upcoming_entry(1, str(w1_new), expected_video_id="r1") is True
    finally:
        player.stop()


class _OfflineDummyClient:
    authenticated = False

    def search_tracks(self, query: str, limit: int = 15) -> list[Track]:
        return []

    def get_radio_queue(self, video_id: str, limit: int = 20) -> list[Track]:
        return []


def test_tui_phase1_filter_folders_picker_shuffle_repeat(tmp_path: Path) -> None:
    sock_path = Path(f"/tmp/ytm-test-s7-tui-{os.getpid()}.sock")
    state_file = tmp_path / "state.json"
    liked_store = LikedStore(liked_file=tmp_path / "liked.json")
    playlist_store = PlaylistStore(playlists_file=tmp_path / "playlists.json")
    art_renderer = CoverArtRenderer(covers_dir=tmp_path / "covers")
    music_dir = tmp_path / "music"
    music_dir.mkdir()

    l0 = Track(
        video_id="l0",
        title="Alpha Sun",
        artist="Artist X",
        album="Day",
        duration="00:04",
        duration_seconds=4,
        local_path=str(_write_test_wav(tmp_path / "l0.wav")),
    )
    l1 = Track(
        video_id="l1",
        title="Midnight Rain",
        artist="Halsey",
        album="Night",
        duration="00:04",
        duration_seconds=4,
        local_path=str(_write_test_wav(tmp_path / "l1.wav")),
    )
    l2 = Track(
        video_id="l2",
        title="Ocean Breeze",
        artist="Artist Z",
        album="Coast",
        duration="00:04",
        duration_seconds=4,
        local_path=str(_write_test_wav(tmp_path / "l2.wav")),
    )
    # liked_store.like inserts at index 0, so l0, l1, l2 appear in [l0, l1, l2] order when liked reverse
    liked_store.like(l2)
    liked_store.like(l1)
    liked_store.like(l0)

    player = MPVPlayer(socket_path=sock_path, state_file=state_file, liked_store=liked_store)

    try:
        async def _run() -> None:
            app = YTMApp(
                player=player,
                client=_OfflineDummyClient(),  # type: ignore[arg-type]
                liked_store=liked_store,
                playlist_store=playlist_store,
                art_renderer=art_renderer,
                music_dir=music_dir,
            )
            async with app.run_test(size=(120, 40)) as pilot:
                await pilot.pause()

                track_table = app.query_one("#track-table", DataTable)
                folders_table = app.query_one("#playlist-folders-table", DataTable)
                inp = app.query_one("#search-input", Input)

                # 1. Instant offline filter (\) in [3] Liked Music
                await pilot.press("3")
                await pilot.pause()
                assert track_table.row_count == 3

                await pilot.press("backslash")
                await pilot.pause()
                assert app.focused is inp
                await pilot.press("h", "a", "l", "s", "e", "y")
                await pilot.pause()
                assert track_table.row_count == 1
                selected = app._selected_track()
                assert selected is not None
                assert selected[0] == 1  # Real index in liked_tracks
                assert selected[1].video_id == "l1"

                # Enter keeps filter while focusing table; Esc clears it
                await pilot.press("enter")
                await pilot.pause()
                assert app.focused is track_table
                assert app._filter_text == "halsey"
                assert track_table.row_count == 1

                await pilot.press("escape")
                await pilot.pause()
                assert app._filter_text == ""
                assert track_table.row_count == 3

                # 2. Folder-style [5] Playlists: create 2 playlists, rename, open/back
                await pilot.press("c")
                await pilot.pause()
                await pilot.press(*"Chill", "enter")
                await pilot.pause()

                await pilot.press("c")
                await pilot.pause()
                await pilot.press(*"Focus", "enter")
                await pilot.pause()

                assert app.active_view == "playlists"
                assert folders_table.display is True
                assert folders_table.row_count == 2

                # Rename highlighted playlist ("Focus") -> "DeepWork"
                await pilot.press("e")
                await pilot.pause()
                assert app.focused is inp
                await pilot.press("backspace", "backspace", "backspace", "backspace", "backspace")
                await pilot.press(*"DeepWork", "enter")
                await pilot.pause()
                assert playlist_store.list_names() == ["Chill", "DeepWork"]

                # Open folder with Enter, go back up with h
                await pilot.press("enter")
                await pilot.pause()
                assert app._playlist_browse_mode == "tracks"
                assert track_table.display is True
                assert folders_table.display is False

                await pilot.press("h")
                await pilot.pause()
                assert app._playlist_browse_mode == "folders"
                assert folders_table.display is True

                # 3. Modal playlist picker (m) when 2+ playlists exist
                await pilot.press("3")
                await pilot.pause()
                await pilot.press("m")
                await pilot.pause()
                assert isinstance(app.screen, PlaylistPickerModal)
                await pilot.press("2")  # quick pick #2 -> DeepWork
                await pilot.pause()
                assert not isinstance(app.screen, PlaylistPickerModal)
                assert [t.video_id for t in playlist_store.get_tracks("DeepWork")] == ["l0"]

                # 4. Safe two-press delete (x) on a playlist folder
                await pilot.press("5")
                await pilot.pause()
                assert folders_table.row_count == 2
                await pilot.press("x")
                await pilot.pause(0.45)
                assert folders_table.row_count == 2
                await pilot.press("x")
                await pilot.pause()
                assert folders_table.row_count == 1

                # 5. Shuffle (S) and Repeat (w) toggles
                assert app.shuffle_on is False
                assert app.repeat_mode == "off"
                await pilot.press("S")
                await pilot.pause()
                assert app.shuffle_on is True
                await pilot.press("w")
                await pilot.pause()
                assert app.repeat_mode == "all"
                await pilot.press("w")
                await pilot.pause()
                assert app.repeat_mode == "one"

                await pilot.press("q")

        asyncio.run(_run())
    finally:
        player.stop()


def test_cli_shuffle_and_repeat_parsers() -> None:
    parser = build_parser()
    s_default = parser.parse_args(["shuffle"])
    assert s_default.command == "shuffle"
    assert s_default.mode == "toggle"

    s_on = parser.parse_args(["shuffle", "on"])
    assert s_on.mode == "on"

    r_default = parser.parse_args(["repeat"])
    assert r_default.command == "repeat"
    assert r_default.mode == "cycle"

    r_one = parser.parse_args(["repeat", "one"])
    assert r_one.mode == "one"

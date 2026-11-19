"""Stage 8 verification tests for Phase 2: Global OS Media Keys via MPRIS2 (D-Bus)."""

from __future__ import annotations

import asyncio
import math
import os
import struct
import time
import wave
from pathlib import Path

from dbus_fast.aio import MessageBus

from ytm.art import CoverArtRenderer
from ytm.liked import LikedStore
from ytm.models import Track
from ytm.mpris import (
    NO_TRACK_PATH,
    MPRISController,
    MPRISPlayerInterface,
    MPRISRootInterface,
    build_mpris_metadata,
    ensure_mpris_daemon,
    is_mpris_running,
    loop_status_from_repeat,
    playback_status_from_state,
    repeat_from_loop_status,
    stop_mpris_daemon,
    track_object_path,
)
from ytm.player import MPVPlayer


def _write_test_wav(path: Path, duration_sec: float = 6.0, freq: float = 330.0) -> Path:
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


def test_mpris_helpers_and_metadata_mapping(tmp_path: Path) -> None:
    assert loop_status_from_repeat("off") == "None"
    assert loop_status_from_repeat("all") == "Playlist"
    assert loop_status_from_repeat("one") == "Track"
    assert repeat_from_loop_status("None") == "off"
    assert repeat_from_loop_status("Playlist") == "all"
    assert repeat_from_loop_status("Track") == "one"

    assert playback_status_from_state(False, False, True) == "Stopped"
    assert playback_status_from_state(True, False, False) == "Stopped"
    assert playback_status_from_state(True, True, True) == "Paused"
    assert playback_status_from_state(True, False, True) == "Playing"

    assert track_object_path(None) == NO_TRACK_PATH
    assert track_object_path("abc-123", 2) == "/org/ytm/MediaPlayer2/Track/abc_123_2"

    art = tmp_path / "cover.jpg"
    art.write_bytes(b"\xff\xd8\xff\xd9")
    track = Track(
        video_id="vid-42",
        title="Midnight City",
        artist="M83",
        album="Hurry Up, We're Dreaming",
        duration="04:03",
        duration_seconds=243,
    )
    meta = build_mpris_metadata(track, index=1, duration_secs=243.5, art_path=art)
    assert meta["mpris:trackid"].value == "/org/ytm/MediaPlayer2/Track/vid_42_1"
    assert meta["xesam:title"].value == "Midnight City"
    assert meta["xesam:artist"].value == ["M83"]
    assert meta["xesam:album"].value == "Hurry Up, We're Dreaming"
    assert meta["mpris:length"].value == 243_500_000
    assert meta["mpris:artUrl"].value == art.resolve().as_uri()


def test_mpris_interfaces_and_controller_against_mpv(tmp_path: Path) -> None:
    sock_path = Path(f"/tmp/ytm-test-s8-{os.getpid()}.sock")
    state_file = tmp_path / "state.json"
    liked_store = LikedStore(liked_file=tmp_path / "liked.json")
    art_renderer = CoverArtRenderer(covers_dir=tmp_path / "covers")
    player = MPVPlayer(socket_path=sock_path, state_file=state_file, liked_store=liked_store)

    w1 = _write_test_wav(tmp_path / "m1.wav", duration_sec=8.0, freq=260.0)
    w2 = _write_test_wav(tmp_path / "m2.wav", duration_sec=8.0, freq=390.0)
    t1 = Track(video_id="mvid1", title="First Song", artist="Artist A", album="Alb 1", local_path=str(w1))
    t2 = Track(video_id="mvid2", title="Second Song", artist="Artist B", album="Alb 2", local_path=str(w2))

    ctrl = MPRISController(player=player, art_renderer=art_renderer)
    root_iface = MPRISRootInterface(ctrl)
    player_iface = MPRISPlayerInterface(ctrl)

    try:
        player.play_queue([t1, t2], start_index=0, radio_autoplay=False)
        time.sleep(0.25)

        assert root_iface.Identity == "ytm"
        assert root_iface.DesktopEntry == "ytm"
        assert root_iface.CanQuit is True
        assert player_iface.PlaybackStatus == "Playing"
        assert player_iface.Metadata["xesam:title"].value == "First Song"

        player_iface.PlayPause()
        assert player_iface.PlaybackStatus == "Paused"
        player_iface.PlayPause()
        assert player_iface.PlaybackStatus == "Playing"

        player_iface.Next()
        time.sleep(0.2)
        assert player_iface.Metadata["xesam:title"].value == "Second Song"

        player_iface.Previous()
        time.sleep(0.2)
        assert player_iface.Metadata["xesam:title"].value == "First Song"

        player_iface.LoopStatus = "Playlist"
        assert player.load_repeat_mode() == "all"
        player_iface.LoopStatus = "Track"
        assert player.load_repeat_mode() == "one"
        player_iface.LoopStatus = "None"
        assert player.load_repeat_mode() == "off"

        player_iface.Shuffle = True
        assert player.load_shuffle() is True
        player_iface.Shuffle = False
        assert player.load_shuffle() is False

        player_iface.Volume = 0.8
        assert 0.78 <= player_iface.Volume <= 0.82

        player_iface.Seek(2_000_000)
        assert player_iface.Position >= 1_500_000

        track_id = player_iface.Metadata["mpris:trackid"].value
        player_iface.SetPosition(track_id, 500_000)
        assert player_iface.Position >= 0
    finally:
        player.stop()


def test_mpris_daemon_live_session_bus_if_available(tmp_path: Path) -> None:
    if not os.environ.get("DBUS_SESSION_BUS_ADDRESS"):
        return

    pid = os.getpid()
    sock_path = Path(f"/tmp/ytm-test-s8-bus-{pid}.sock")
    state_file = tmp_path / "state.json"
    pid_file = tmp_path / "mpris.pid"
    bus_name = f"org.mpris.MediaPlayer2.ytm_test_{pid}"

    liked_store = LikedStore(liked_file=tmp_path / "liked.json")
    player = MPVPlayer(socket_path=sock_path, state_file=state_file, liked_store=liked_store)
    w1 = _write_test_wav(tmp_path / "b1.wav", duration_sec=6.0, freq=300.0)
    t1 = Track(video_id="busvid1", title="Bus Track", artist="Bus Artist", local_path=str(w1))

    try:
        player.play_queue([t1], start_index=0, radio_autoplay=False)
        started = ensure_mpris_daemon(
            socket_path=sock_path,
            state_file=state_file,
            pid_file=pid_file,
            bus_name=bus_name,
        )
        assert started is True
        assert is_mpris_running(pid_file) is True

        async def _query_bus() -> None:
            bus = await MessageBus().connect()
            try:
                intro = await bus.introspect(bus_name, "/org/mpris/MediaPlayer2")
                proxy = bus.get_proxy_object(bus_name, "/org/mpris/MediaPlayer2", intro)
                root_proxy = proxy.get_interface("org.mpris.MediaPlayer2")
                player_proxy = proxy.get_interface("org.mpris.MediaPlayer2.Player")

                assert await root_proxy.get_identity() == "ytm"
                assert await player_proxy.get_playback_status() in ("Playing", "Paused")
                meta = await player_proxy.get_metadata()
                assert meta["xesam:title"].value == "Bus Track"

                await player_proxy.call_play_pause()
                await asyncio.sleep(0.15)
                assert await player_proxy.get_playback_status() == "Paused"
            finally:
                bus.disconnect()

        asyncio.run(_query_bus())
        assert stop_mpris_daemon(pid_file) is True
        assert is_mpris_running(pid_file) is False
    finally:
        stop_mpris_daemon(pid_file)
        player.stop()

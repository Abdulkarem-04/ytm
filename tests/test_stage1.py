"""Stage 1 verification tests for ytm."""

from __future__ import annotations

import math
import os
import socket
import struct
import wave
from pathlib import Path
from typing import Any

import requests

from ytm.api import YTMClient
from ytm.cli import build_parser
from ytm.liked import LikedStore
from ytm.models import Track, format_duration, parse_duration_seconds
from ytm.player import MPVPlayer


def _write_test_wav(path: Path, duration_sec: float = 2.0, freq: float = 440.0) -> Path:
    """Create a short silent/low-amplitude WAV file for local mpv IPC testing."""
    sample_rate = 16000
    n_samples = int(sample_rate * duration_sec)
    with wave.open(str(path), "w") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        frames = bytearray()
        for i in range(n_samples):
            val = int(500 * math.sin(2.0 * math.pi * freq * i / sample_rate))
            frames.extend(struct.pack("<h", val))
        wf.writeframes(bytes(frames))
    return path


def test_track_serialization_and_url(tmp_path: Path) -> None:
    remote = Track(
        video_id="dQw4w9WgXcQ",
        title="Test Song",
        artist="Test Artist",
        album="Test Album",
        duration="03:32",
        duration_seconds=212,
        is_liked=True,
    )
    payload = remote.to_dict()
    restored = Track.from_dict(payload)
    assert restored == remote
    assert restored.url == "https://www.youtube.com/watch?v=dQw4w9WgXcQ"

    local_file = tmp_path / "cached.wav"
    local_file.write_bytes(b"RIFF")
    cached = Track(
        video_id="abc123",
        title="Cached Song",
        artist="Local Artist",
        local_path=str(local_file),
    )
    assert cached.url == str(local_file)

    assert parse_duration_seconds("3:42") == 222
    assert parse_duration_seconds("1:02:03") == 3723
    assert format_duration(222) == "03:42"
    assert format_duration(3723) == "1:02:03"


def test_ytm_client_search_and_radio(tmp_path: Path, monkeypatch: Any) -> None:
    client = YTMClient(liked_store=LikedStore(liked_file=tmp_path / "liked.json"))

    online = True
    try:
        socket.getaddrinfo("music.youtube.com", 443)
    except OSError:
        online = False

    def _install_offline_mocks() -> None:
        def _fake_search(query: str, filter: str = "songs", limit: int = 15) -> list[dict[str, Any]]:
            return [
                {
                    "videoId": "fA3aB8X09L4",
                    "title": "Veridis Quo",
                    "artists": [{"name": "Daft Punk", "id": "UC123"}],
                    "album": {"name": "Discovery", "id": "MPREb123"},
                    "duration": "5:46",
                    "duration_seconds": 346,
                }
            ]

        def _fake_watch(videoId: str, limit: int = 20) -> dict[str, Any]:
            return {
                "tracks": [
                    {
                        "videoId": videoId,
                        "title": "Veridis Quo",
                        "artists": [{"name": "Daft Punk"}],
                        "album": {"name": "Discovery"},
                        "length": "5:46",
                    },
                    {
                        "videoId": "FGBhQbmPwH8",
                        "title": "One More Time",
                        "artists": [{"name": "Daft Punk"}],
                        "album": {"name": "Discovery"},
                        "length": "5:20",
                    },
                ]
            }

        monkeypatch.setattr(client.yt, "search", _fake_search)
        monkeypatch.setattr(client.yt, "get_watch_playlist", _fake_watch)

    if not online:
        _install_offline_mocks()

    try:
        results = client.search_tracks("Daft Punk Veridis Quo", limit=5)
        assert len(results) >= 1
        first = results[0]
        radio = client.get_radio_queue(first.video_id, limit=10)
    except requests.exceptions.RequestException:
        _install_offline_mocks()
        results = client.search_tracks("Daft Punk Veridis Quo", limit=5)
        assert len(results) >= 1
        first = results[0]
        radio = client.get_radio_queue(first.video_id, limit=10)

    assert first.video_id
    assert first.title
    assert first.artist
    assert len(radio) >= 1
    assert all(t.video_id and t.title for t in radio)


def test_mpv_daemon_lifecycle_and_controls(tmp_path: Path) -> None:
    sock_path = Path(f"/tmp/ytm-test-{os.getpid()}.sock")
    state_file = tmp_path / "state.json"
    wav1 = _write_test_wav(tmp_path / "track1.wav", duration_sec=3.0, freq=220.0)
    wav2 = _write_test_wav(tmp_path / "track2.wav", duration_sec=3.0, freq=330.0)

    tracks = [
        Track(
            video_id="test1",
            title="Tone One",
            artist="Tester",
            duration="00:03",
            duration_seconds=3,
            local_path=str(wav1),
        ),
        Track(
            video_id="test2",
            title="Tone Two",
            artist="Tester",
            duration="00:03",
            duration_seconds=3,
            local_path=str(wav2),
        ),
    ]

    player = MPVPlayer(socket_path=sock_path, state_file=state_file)
    try:
        player.ensure_daemon()
        assert player.is_running() is True

        player.play_queue(tracks, start_index=0)
        status = player.get_status()
        assert status["running"] is True
        assert status["current_track"] is not None
        assert status["current_track"].video_id == "test1"
        assert status["playlist_count"] == 2

        paused = player.toggle_pause()
        assert paused is True
        paused_again = player.toggle_pause()
        assert paused_again is False

        vol_80 = player.set_volume("80")
        assert round(vol_80) == 80
        vol_up = player.set_volume("+10")
        assert round(vol_up) == 90
        vol_down = player.set_volume("-15")
        assert round(vol_down) == 75

        next_t = player.next_track()
        assert next_t is not None
        assert next_t.video_id == "test2"

        prev_t = player.prev_track()
        assert prev_t is not None
        assert prev_t.video_id == "test1"

        player.replay()
        replayed_status = player.get_status()
        assert replayed_status["paused"] is False
    finally:
        player.stop()
        assert player.is_running() is False
        assert not sock_path.exists()


def test_cli_argparse_vol_negative_and_positive() -> None:
    parser = build_parser()
    args_pos = parser.parse_args(["vol", "+10"])
    assert args_pos.value == "+10"
    args_neg = parser.parse_args(["vol", "-10"])
    assert args_neg.value == "-10"

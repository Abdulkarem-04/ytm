"""Stage 6 verification tests for SettingsStore, rolling preload, mpv hot-swap/restart, and auto-cache."""

from __future__ import annotations

import asyncio
import math
import os
import struct
import time
import wave
from pathlib import Path
from typing import Any

import yt_dlp
from textual.widgets import DataTable

from ytm.art import CoverArtRenderer
from ytm.cache_worker import BackgroundCacheManager
from ytm.cli import build_parser
from ytm.downloader import TrackDownloader, dir_stats
from ytm.liked import LikedStore
from ytm.models import Track
from ytm.player import MPVPlayer
from ytm.playlists import PlaylistStore
from ytm.settings import AppSettings, SettingsStore
from ytm.tui import YTMApp


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


def test_settings_store_defaults_cycling_and_persistence(tmp_path: Path) -> None:
    cfg_path = tmp_path / "config.json"
    store = SettingsStore(settings_file=cfg_path)

    defaults = store.load()
    assert defaults.auto_cache_liked is False
    assert defaults.preload_next is True
    assert defaults.preload_limit == 10
    assert defaults.audio_quality == "best"
    assert defaults.stream_buffer_secs == 600
    assert defaults.mpris_enabled is True
    assert defaults.spectrum_mode == "fft"
    assert defaults.ytdlp_format == "bestaudio/best"

    s1 = store.cycle("auto_cache_liked")
    assert s1.auto_cache_liked is True
    s2 = store.cycle("preload_next")
    assert s2.preload_next is False
    s3 = store.cycle("preload_limit")
    assert s3.preload_limit == 20
    assert store.cycle("preload_limit").preload_limit == 5
    s4 = store.cycle("audio_quality")
    assert s4.audio_quality == "saver"
    assert s4.ytdlp_format == "bestaudio[abr<=96]/bestaudio/best"
    s5 = store.cycle("stream_buffer_secs")
    assert s5.stream_buffer_secs == 60
    s6 = store.cycle("mpris_enabled")
    assert s6.mpris_enabled is False
    assert store.cycle("spectrum_mode").spectrum_mode == "simulated"

    reloaded = SettingsStore(settings_file=cfg_path).load()
    assert reloaded.auto_cache_liked is True
    assert reloaded.preload_next is False
    assert reloaded.preload_limit == 5
    assert reloaded.audio_quality == "saver"
    assert reloaded.stream_buffer_secs == 60
    assert reloaded.mpris_enabled is False
    assert reloaded.spectrum_mode == "simulated"

    clamped = AppSettings.from_dict(
        {
            "preload_limit": 999,
            "audio_quality": "invalid",
            "stream_buffer_secs": -1,
            "spectrum_mode": "invalid",
        }
    )
    assert clamped.preload_limit == 10
    assert clamped.audio_quality == "best"
    assert clamped.stream_buffer_secs == 600
    assert clamped.spectrum_mode == "fft"


def test_downloader_preload_prune_clear_and_promote(tmp_path: Path, monkeypatch: Any) -> None:
    music_dir = tmp_path / "music"
    preload_dir = tmp_path / "preload"
    settings_store = SettingsStore(settings_file=tmp_path / "config.json")
    dl = TrackDownloader(music_dir=music_dir, preload_dir=preload_dir, settings_store=settings_store)

    class _FakeYDL:
        calls = 0

        def __init__(self, opts: dict[str, Any]) -> None:
            self.opts = opts

        def __enter__(self) -> _FakeYDL:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def download(self, urls: list[str]) -> None:
            _FakeYDL.calls += 1
            out = str(self.opts["outtmpl"]).replace("%(ext)s", "opus")
            Path(out).write_bytes(b"OggS-audio-data")

    monkeypatch.setattr(yt_dlp, "YoutubeDL", _FakeYDL)

    t1 = Track(video_id="vid1", title="One", artist="A")
    t2 = Track(video_id="vid2", title="Two", artist="B")
    t3 = Track(video_id="vid3", title="Three", artist="C")

    p1 = dl.preload(t1, max_files=2)
    time.sleep(0.02)
    p2 = dl.preload(t2, max_files=2)
    time.sleep(0.02)
    p3 = dl.preload(t3, max_files=2)

    assert p3.is_file()
    assert p2.is_file()
    # Oldest file vid1 pruned beyond max_files=2
    assert not p1.exists()
    count, total_bytes = dir_stats(preload_dir)
    assert count == 2
    assert total_bytes == len(b"OggS-audio-data") * 2

    # Calling download on already-preloaded vid2 promotes it to music_dir without re-downloading
    calls_before = _FakeYDL.calls
    promoted = dl.download(t2)
    assert _FakeYDL.calls == calls_before
    assert promoted.parent == music_dir
    assert promoted.is_file()
    assert t2.local_path == str(promoted)
    assert dl.find_preloaded("vid2") is None

    cleared = dl.clear_preload()
    assert cleared == 1
    assert dir_stats(preload_dir) == (0, 0)


def test_mpv_replace_upcoming_entry_and_restart_daemon(tmp_path: Path) -> None:
    sock_path = Path(f"/tmp/ytm-test-s6-{os.getpid()}.sock")
    state_file = tmp_path / "state.json"
    liked_store = LikedStore(liked_file=tmp_path / "liked.json")
    settings_store = SettingsStore(settings_file=tmp_path / "config.json")

    w1 = _write_test_wav(tmp_path / "w1.wav", freq=220.0)
    w2 = _write_test_wav(tmp_path / "w2.wav", freq=330.0)
    w2_new = _write_test_wav(tmp_path / "w2_new.wav", freq=440.0)
    w3 = _write_test_wav(tmp_path / "w3.wav", freq=550.0)

    tracks = [
        Track(video_id="t1", title="One", artist="A", duration="00:06", duration_seconds=6, local_path=str(w1)),
        Track(video_id="t2", title="Two", artist="B", duration="00:06", duration_seconds=6, local_path=str(w2)),
        Track(video_id="t3", title="Three", artist="C", duration="00:06", duration_seconds=6, local_path=str(w3)),
    ]

    player = MPVPlayer(
        socket_path=sock_path,
        state_file=state_file,
        liked_store=liked_store,
        settings_store=settings_store,
    )
    try:
        player.play_queue(tracks, start_index=0, radio_autoplay=False)
        time.sleep(0.25)

        # Cannot replace active track 0
        assert player.replace_upcoming_entry(0, str(w2_new)) is False

        # Hot-swap upcoming track 1 without interrupting track 0
        assert player.replace_upcoming_entry(1, str(w2_new)) is True
        pl = player._get_property("playlist")
        assert isinstance(pl, list) and len(pl) == 3
        assert pl[1].get("filename") == str(w2_new)
        assert player._get_property("playlist-pos") == 0

        # Restart daemon preserves queue and active position
        old_pid = player._get_property("pid")
        assert player.restart_daemon() is True
        new_pid = player._get_property("pid")
        assert isinstance(new_pid, int) and new_pid != old_pid
        status = player.get_status()
        assert status["running"] is True
        assert status["playlist_count"] == 3
        assert status["playlist_pos"] == 0
    finally:
        player.stop()


def test_background_cache_manager_preload_and_auto_cache(tmp_path: Path, monkeypatch: Any) -> None:
    sock_path = Path(f"/tmp/ytm-test-s6-bg-{os.getpid()}.sock")
    music_dir = tmp_path / "music"
    preload_dir = tmp_path / "preload"
    settings_store = SettingsStore(settings_file=tmp_path / "config.json")
    liked_store = LikedStore(liked_file=tmp_path / "liked.json")
    playlist_store = PlaylistStore(playlists_file=tmp_path / "playlists.json")
    player = MPVPlayer(
        socket_path=sock_path,
        state_file=tmp_path / "state.json",
        liked_store=liked_store,
        settings_store=settings_store,
    )
    dl = TrackDownloader(music_dir=music_dir, preload_dir=preload_dir, settings_store=settings_store)

    w0 = _write_test_wav(tmp_path / "q0.wav", freq=220.0)
    w1_src = _write_test_wav(tmp_path / "q1_src.wav", freq=330.0)

    def _fake_download_to_dir(self: TrackDownloader, track: Track, target_dir: Path, audio_format: str | None = None) -> Path:
        target_dir.mkdir(parents=True, exist_ok=True)
        out = target_dir / f"{track.video_id}.wav"
        out.write_bytes(w1_src.read_bytes())
        return out

    monkeypatch.setattr(TrackDownloader, "_download_to_dir", _fake_download_to_dir)

    liked_track = Track(video_id="liked_bg", title="Liked BG", artist="Artist")
    liked_store.like(liked_track)

    q0 = Track(video_id="q0", title="Playing Now", artist="A", local_path=str(w0))
    q1 = Track(video_id="q1_remote", title="Upcoming Stream", artist="B")

    cached_events: list[str] = []
    manager = BackgroundCacheManager(
        downloader=dl,
        player=player,
        liked_store=liked_store,
        playlist_store=playlist_store,
        settings_store=settings_store,
        on_track_cached=lambda t, p: cached_events.append(t.video_id),
        inter_track_delay=0.05,
    )

    try:
        player.play_queue([q0, q1], start_index=0, radio_autoplay=False)
        manager.start()

        # Priority 1: preload q1 while auto_cache_liked stays OFF
        manager.request_preload(q1, 1)
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            if dl.find_preloaded("q1_remote") is not None:
                break
            time.sleep(0.05)

        assert dl.find_preloaded("q1_remote") is not None
        # Confirm auto_cache_liked=False did NOT download liked_bg
        assert dl.find_existing("liked_bg") is None

        # Priority 2: enable auto_cache_liked=True and wake coordinator
        settings_store.update(auto_cache_liked=True)
        manager.wake()
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            if dl.find_existing("liked_bg") is not None and "liked_bg" in cached_events:
                break
            time.sleep(0.05)

        assert dl.find_existing("liked_bg") is not None
        assert liked_store.list_tracks()[0].local_path == dl.find_existing("liked_bg")
        prog = manager.get_progress()
        assert prog["cached_liked"] == 1
        assert prog["total_liked"] == 1
    finally:
        manager.stop()
        player.stop()


class _OfflineClient:
    authenticated = False

    def search_tracks(self, query: str, limit: int = 15) -> list[Track]:
        return []

    def get_radio_queue(self, video_id: str, limit: int = 20) -> list[Track]:
        return []


def test_tui_settings_page_toggles_and_actions(tmp_path: Path) -> None:
    sock_path = Path(f"/tmp/ytm-test-s6-tui-{os.getpid()}.sock")
    music_dir = tmp_path / "music"
    preload_dir = tmp_path / "preload"
    preload_dir.mkdir(parents=True)
    (preload_dir / "temp_song.opus").write_bytes(b"OggS")

    settings_store = SettingsStore(settings_file=tmp_path / "config.json")
    liked_store = LikedStore(liked_file=tmp_path / "liked.json")
    playlist_store = PlaylistStore(playlists_file=tmp_path / "playlists.json")
    player = MPVPlayer(
        socket_path=sock_path,
        state_file=tmp_path / "state.json",
        liked_store=liked_store,
        settings_store=settings_store,
    )
    downloader = TrackDownloader(
        music_dir=music_dir,
        preload_dir=preload_dir,
        settings_store=settings_store,
    )
    art_renderer = CoverArtRenderer(covers_dir=tmp_path / "covers")

    async def _run() -> None:
        app = YTMApp(
            player=player,
            client=_OfflineClient(),  # type: ignore[arg-type]
            liked_store=liked_store,
            playlist_store=playlist_store,
            downloader=downloader,
            art_renderer=art_renderer,
            settings_store=settings_store,
            music_dir=music_dir,
        )
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()

            track_table = app.query_one("#track-table", DataTable)
            settings_table = app.query_one("#settings-table", DataTable)
            assert track_table.display is True
            assert settings_table.display is False

            # Press 6 to open [6] Settings
            await pilot.press("6")
            await pilot.pause()
            assert app.active_view == "settings"
            assert settings_table.display is True
            assert track_table.display is False
            assert settings_table.row_count == 10

            # Row 0: Toggle Auto-Cache Liked Music OFF -> ON -> OFF
            await pilot.press("enter")
            await pilot.pause()
            assert settings_store.load().auto_cache_liked is True
            await pilot.press("enter")
            await pilot.pause()
            assert settings_store.load().auto_cache_liked is False

            # Row 1: Toggle Gapless Next-Track Preload ON -> OFF
            await pilot.press("j", "enter")
            await pilot.pause()
            assert settings_store.load().preload_next is False

            # Row 2: Cycle Preload Temp Cache Cap 10 -> 20 -> 5
            await pilot.press("j", "enter")
            await pilot.pause()
            assert settings_store.load().preload_limit == 20
            await pilot.press("enter")
            await pilot.pause()
            assert settings_store.load().preload_limit == 5

            # Row 3: Cycle Audio Quality best -> saver
            await pilot.press("j", "enter")
            await pilot.pause()
            assert settings_store.load().audio_quality == "saver"

            # Row 7: Clear Temp Preload (j past stream_buffer_secs, mpris_enabled, spectrum_mode)
            await pilot.press("j", "j", "j", "j", "enter")
            await pilot.pause()
            assert not (preload_dir / "temp_song.opus").exists()

            # Press 1 to switch back to Active Queue
            await pilot.press("1")
            await pilot.pause()
            assert app.active_view == "queue"
            assert track_table.display is True
            assert settings_table.display is False

            await pilot.press("q")

    asyncio.run(_run())


def test_cli_cache_liked_parser() -> None:
    parser = build_parser()
    args_all = parser.parse_args(["cache-liked"])
    assert args_all.command == "cache-liked"
    assert args_all.limit is None

    args_lim = parser.parse_args(["cache-liked", "--limit", "15"])
    assert args_lim.limit == 15

"""Stage 3 & 4 verification tests for auth, downloader, queue refill, and TUI polish."""

from __future__ import annotations

import asyncio
import json
import math
import os
import struct
import wave
from pathlib import Path
from typing import Any

import yt_dlp
from textual.widgets import DataTable, Static

from ytm.auth import remove_auth, sanitize_auth_file, save_headers_auth
from ytm.cli import build_parser
from ytm.downloader import TrackDownloader
from ytm.liked import LikedStore
from ytm.models import Track
from ytm.player import MPVPlayer
from ytm.tui import YTMApp


def _write_test_wav(path: Path, duration_sec: float = 6.0, freq: float = 440.0) -> Path:
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


def test_liked_store_merge_and_update_local_path(tmp_path: Path) -> None:
    store = LikedStore(liked_file=tmp_path / "liked.json")
    t1 = Track(video_id="v1", title="One", artist="Artist 1")
    store.toggle(t1)

    remote = [
        Track(video_id="v1", title="One", artist="Artist 1", local_path="/tmp/v1.opus"),
        Track(video_id="v2", title="Two", artist="Artist 2"),
        Track(video_id="v3", title="Three", artist="Artist 3"),
    ]
    added = store.merge_tracks(remote)
    assert added == 2

    tracks = {t.video_id: t for t in store.list_tracks()}
    assert set(tracks) == {"v1", "v2", "v3"}
    assert tracks["v1"].local_path == "/tmp/v1.opus"
    assert all(t.is_liked for t in tracks.values())

    store.update_local_path("v2", "/tmp/v2.opus")
    refreshed = {t.video_id: t for t in store.list_tracks()}
    assert refreshed["v2"].local_path == "/tmp/v2.opus"


def test_mpv_append_tracks_and_sync_local_path(tmp_path: Path) -> None:
    sock_path = Path(f"/tmp/ytm-test-s34-{os.getpid()}.sock")
    state_file = tmp_path / "state.json"
    liked_store = LikedStore(liked_file=tmp_path / "liked.json")
    wav1 = _write_test_wav(tmp_path / "w1.wav", freq=220.0)
    wav2 = _write_test_wav(tmp_path / "w2.wav", freq=330.0)

    t1 = Track(video_id="vid1", title="First", artist="A", local_path=str(wav1))
    t2_dup = Track(video_id="vid1", title="First Dup", artist="A", local_path=str(wav1))
    t2 = Track(video_id="vid2", title="Second", artist="B", local_path=str(wav2))

    player = MPVPlayer(socket_path=sock_path, state_file=state_file, liked_store=liked_store)
    try:
        player.play_queue([t1], start_index=0)
        added = player.append_tracks([t2_dup, t2])
        assert added == 1

        status = player.get_status()
        assert status["playlist_count"] == 2
        assert [t.video_id for t in status["queue"]] == ["vid1", "vid2"]

        player.sync_track_local_path("vid2", "/tmp/custom_v2.opus")
        reloaded = player.load_queue()
        assert reloaded[1].local_path == "/tmp/custom_v2.opus"
    finally:
        player.stop()


def test_track_downloader_find_existing_and_download(tmp_path: Path, monkeypatch: Any) -> None:
    music_dir = tmp_path / "music"
    downloader = TrackDownloader(music_dir=music_dir)
    assert downloader.find_existing("abc123") is None

    # Stub yt_dlp.YoutubeDL to create an .opus file
    class _FakeYDL:
        def __init__(self, opts: dict[str, Any]) -> None:
            self.opts = opts

        def __enter__(self) -> _FakeYDL:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def download(self, urls: list[str]) -> None:
            assert len(urls) == 1
            out = str(self.opts["outtmpl"]).replace("%(ext)s", "opus")
            Path(out).write_bytes(b"OggS-test")

    monkeypatch.setattr(yt_dlp, "YoutubeDL", _FakeYDL)

    track = Track(video_id="abc123", title="Cached Beat", artist="Producer")
    saved = downloader.download(track)
    assert saved.is_file()
    assert saved.name == "abc123.opus"
    assert track.local_path == str(saved)
    assert downloader.find_existing("abc123") == str(saved)

    # Calling download again reuses existing file without invoking YoutubeDL
    def _fail_ydl(_opts: dict[str, Any]) -> Any:
        raise AssertionError("YoutubeDL should not be invoked when already cached")

    monkeypatch.setattr(yt_dlp, "YoutubeDL", _fail_ydl)
    again = downloader.download(track)
    assert again == saved


def test_auth_save_and_remove(tmp_path: Path, monkeypatch: Any) -> None:
    import ytmusicapi

    auth_file = tmp_path / "headers_auth.json"
    assert remove_auth(auth_file) is False

    def _fake_setup(filepath: str | None = None, headers_raw: str | None = None) -> str:
        assert filepath == str(auth_file)
        assert headers_raw and "Cookie:" in headers_raw
        Path(filepath).write_text('{"Cookie": "test=1"}', encoding="utf-8")
        return "{}"

    monkeypatch.setattr(ytmusicapi, "setup", _fake_setup)
    saved = save_headers_auth("Cookie: test=1\nUser-Agent: test", auth_file=auth_file)
    assert saved.is_file()
    assert remove_auth(auth_file) is True
    assert not auth_file.exists()


def test_sanitize_chromium_pseudo_headers_and_curl(tmp_path: Path) -> None:
    # 1. Multiline Chromium pseudo-headers + content-encoding
    chromium_paste = "\n".join(
        [
            ":authority",
            "music.youtube.com",
            ":method",
            "POST",
            ":path",
            "/youtubei/v1/account/account_menu?prettyPrint=false",
            ":scheme",
            "https",
            "accept",
            "*/*",
            "content-encoding",
            "gzip",
            "cookie",
            "SID=abc; __Secure-3PAPISID=xyz",
            "x-goog-authuser",
            "0",
        ]
    )
    auth_file = tmp_path / "chromium_auth.json"
    save_headers_auth(chromium_paste, auth_file=auth_file)
    parsed = json.loads(auth_file.read_text(encoding="utf-8"))
    assert parsed["cookie"] == "SID=abc; __Secure-3PAPISID=xyz"
    assert parsed["x-goog-authuser"] == "0"
    assert "music.youtube.com" not in parsed
    assert "content-encoding" not in parsed
    assert all("/" not in k and ":" not in k for k in parsed)

    # 2. cURL command with -H and -b cookie flag
    curl_paste = (
        "curl 'https://music.youtube.com/youtubei/v1/browse' \\\n"
        "  -H 'accept: */*' \\\n"
        "  -H 'x-goog-authuser: 0' \\\n"
        "  -b 'SID=curl_sid; __Secure-3PAPISID=curl_sap'"
    )
    curl_file = tmp_path / "curl_auth.json"
    save_headers_auth(curl_paste, auth_file=curl_file)
    curl_parsed = json.loads(curl_file.read_text(encoding="utf-8"))
    assert curl_parsed["cookie"] == "SID=curl_sid; __Secure-3PAPISID=curl_sap"
    assert curl_parsed["x-goog-authuser"] == "0"

    # 3. Sanitizing an already-saved dirty JSON file in place
    dirty_file = tmp_path / "dirty.json"
    dirty_file.write_text(
        json.dumps(
            {
                "/youtubei/v1/browse": "https",
                "music.youtube.com": "POST",
                "content-encoding": "gzip",
                "cookie": "SID=clean",
                "x-goog-authuser": "0",
            }
        ),
        encoding="utf-8",
    )
    assert sanitize_auth_file(dirty_file) is True
    assert sanitize_auth_file(dirty_file) is False
    cleaned = json.loads(dirty_file.read_text(encoding="utf-8"))
    assert set(cleaned) == {"cookie", "x-goog-authuser"}


class _FakeRadioClient:
    authenticated = False

    def __init__(self) -> None:
        self.radio_calls: list[str] = []

    def search_tracks(self, query: str, limit: int = 15) -> list[Track]:
        return []

    def get_radio_queue(self, video_id: str, limit: int = 20) -> list[Track]:
        self.radio_calls.append(video_id)
        return [
            Track(
                video_id="radio_next_1",
                title="Auto Radio Next",
                artist="Radio Artist",
                album="Radio Album",
                duration="03:30",
                duration_seconds=210,
            )
        ]


def test_tui_brackets_download_and_auto_radio_refill(tmp_path: Path) -> None:
    sock_path = Path(f"/tmp/ytm-test-tui-s34-{os.getpid()}.sock")
    state_file = tmp_path / "state.json"
    liked_store = LikedStore(liked_file=tmp_path / "liked.json")
    music_dir = tmp_path / "music"
    music_dir.mkdir()

    # 1. Verify escaped Rich markup brackets in stopped state
    player_stopped = MPVPlayer(
        socket_path=sock_path,
        state_file=state_file,
        liked_store=liked_store,
    )
    seed = Track(
        video_id="dl_vid_1",
        title="Very Long Track Title That Should Not Break Duration Layout In Table",
        artist="Artist Name Very Long Indeed",
        album="Album Name Very Long Indeed",
        duration="04:00",
        duration_seconds=240,
    )
    player_stopped.save_queue([seed])

    class _StubDownloader(TrackDownloader):
        def download(self, track: Track) -> Path:
            self.music_dir.mkdir(parents=True, exist_ok=True)
            out = self.music_dir / f"{track.video_id}.opus"
            out.write_bytes(b"OggS")
            track.local_path = str(out)
            return out

    fake_client = _FakeRadioClient()
    stub_dl = _StubDownloader(music_dir=music_dir)

    async def _run_stopped_and_download() -> None:
        app = YTMApp(
            player=player_stopped,
            client=fake_client,  # type: ignore[arg-type]
            liked_store=liked_store,
            downloader=stub_dl,
            music_dir=music_dir,
        )
        async with app.run_test(size=(120, 36)) as pilot:
            await pilot.pause()

            badge_plain = str(app.query_one("#daemon-badge", Static).render())
            now_plain = str(app.query_one("#now-playing-box", Static).render())
            assert "[DAEMON: STOPPED]" in badge_plain
            assert "[GUEST MODE]" in badge_plain
            assert "[STOPPED]" in now_plain

            # Press D to cache the selected row into music_dir
            await pilot.press("D")
            await pilot.pause(0.2)

            assert (music_dir / "dl_vid_1.opus").is_file()
            await pilot.press("4")
            await pilot.pause()
            assert app.active_view == "local"
            table = app.query_one("#track-table", DataTable)
            assert table.row_count == 1
            await pilot.press("q")

    asyncio.run(_run_stopped_and_download())

    # 2. Verify continuous auto-radio refill when playing last track in queue
    wav_last = _write_test_wav(tmp_path / "last.wav", duration_sec=6.0, freq=260.0)
    wav_radio = _write_test_wav(tmp_path / "radio_next_1.wav", duration_sec=6.0, freq=390.0)
    # Pre-place radio_next_1.wav in music_dir? Or test with FakeClient returning local_path!
    last_track = Track(
        video_id="last_seed",
        title="Last Seed",
        artist="Seed Artist",
        duration="00:06",
        duration_seconds=6,
        local_path=str(wav_last),
    )

    class _LocalRadioClient(_FakeRadioClient):
        def get_radio_queue(self, video_id: str, limit: int = 20) -> list[Track]:
            self.radio_calls.append(video_id)
            return [
                Track(
                    video_id="radio_next_1",
                    title="Auto Radio Next",
                    artist="Radio Artist",
                    duration="00:06",
                    duration_seconds=6,
                    local_path=str(wav_radio),
                )
            ]

    live_player = MPVPlayer(
        socket_path=sock_path,
        state_file=tmp_path / "live_state.json",
        liked_store=liked_store,
    )
    radio_client = _LocalRadioClient()
    try:
        live_player.play_queue([last_track], start_index=0)

        async def _run_refill() -> None:
            app = YTMApp(
                player=live_player,
                client=radio_client,  # type: ignore[arg-type]
                liked_store=liked_store,
                downloader=stub_dl,
                music_dir=music_dir,
            )
            async with app.run_test(size=(120, 36)) as pilot:
                await pilot.pause(0.4)
                assert "last_seed" in radio_client.radio_calls
                status = live_player.get_status()
                assert status["playlist_count"] == 2
                assert [t.video_id for t in status["queue"]] == ["last_seed", "radio_next_1"]
                now_plain = str(app.query_one("#now-playing-box", Static).render())
                assert "[PLAYING]" in now_plain or "[PAUSED]" in now_plain
                await pilot.press("q")

        asyncio.run(_run_refill())
    finally:
        live_player.stop()


def test_cli_stage3_4_subcommands() -> None:
    parser = build_parser()
    auth_status = parser.parse_args(["auth", "--status"])
    assert auth_status.command == "auth"
    assert auth_status.status is True

    auth_sync = parser.parse_args(["auth", "--sync"])
    assert auth_sync.sync is True

    auth_logout = parser.parse_args(["auth", "--logout"])
    assert auth_logout.logout is True

    dl_current = parser.parse_args(["download"])
    assert dl_current.command == "download"
    assert dl_current.query == []

    dl_query = parser.parse_args(["download", "Daft", "Punk"])
    assert dl_query.query == ["Daft", "Punk"]

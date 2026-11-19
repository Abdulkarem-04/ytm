"""Stage 10 verification tests for Phase 4: Minimalist Focus Mode and Synced Lyrics."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from textual.widgets import Static

from ytm.api import YTMClient
from ytm.art import CoverArtRenderer
from ytm.cli import build_parser
from ytm.liked import LikedStore
from ytm.lyrics import LyricLine, LyricsData, LyricsProvider, active_line_index, parse_lrc
from ytm.models import Track
from ytm.player import MPVPlayer
from ytm.playlists import PlaylistStore
from ytm.settings import SettingsStore
from ytm.tui import YTMApp


def test_parse_lrc_and_active_line_index() -> None:
    sample_lrc = """
[ar:Neon Artist]
[ti:Midnight Drive]
[00:10.00][00:30.500]Chorus line repeats
[00:02.50]<00:02.80>First verse opens
[00:20.00]Second verse here
[00:45]Outro fade
"""
    lines = parse_lrc(sample_lrc)
    assert len(lines) == 5
    assert lines[0].start_sec == 2.5
    assert lines[0].text == "First verse opens"
    assert lines[0].end_sec == 10.0
    assert lines[1].start_sec == 10.0
    assert lines[1].text == "Chorus line repeats"
    assert lines[2].start_sec == 20.0
    assert lines[3].start_sec == 30.5
    assert lines[3].text == "Chorus line repeats"
    assert lines[4].start_sec == 45.0
    assert lines[4].end_sec is None

    assert active_line_index([], 5.0) == -1
    assert active_line_index(lines, 0.5) == 0
    assert active_line_index(lines, 9.9) == 1
    assert active_line_index(lines, 21.0) == 2
    assert active_line_index(lines, 31.0) == 3
    assert active_line_index(lines, 99.0) == 4


def test_ytm_client_fetch_lyrics_synced_and_plain(tmp_path: Path) -> None:
    liked_store = LikedStore(liked_file=tmp_path / "liked.json")
    client = YTMClient(liked_store=liked_store)

    class _FakeYTTimed:
        def get_watch_playlist(self, videoId: str, limit: int = 1) -> dict[str, Any]:
            return {"lyrics": "MPDT123"}

        def get_lyrics(self, browseId: str, timestamps: bool = False) -> dict[str, Any]:
            if timestamps:
                return {
                    "hasTimestamps": True,
                    "source": "LyricFind",
                    "lyrics": [
                        SimpleNamespace(text="Line one", start_time=1500, end_time=4500),
                        SimpleNamespace(text="Line two", start_time=5000, end_time=9000),
                    ],
                }
            return {"hasTimestamps": False, "source": "LyricFind", "lyrics": "Line one\nLine two"}

    client.yt = _FakeYTTimed()
    synced_res = client.fetch_lyrics("vidSync1")
    assert synced_res is not None
    assert synced_res["synced"] is True
    assert synced_res["source"] == "LyricFind"
    assert len(synced_res["lines"]) == 2
    assert synced_res["lines"][0]["start_sec"] == 1.5
    assert synced_res["lines"][1]["end_sec"] == 9.0

    class _FakeYTPlain:
        def get_watch_playlist(self, videoId: str, limit: int = 1) -> dict[str, Any]:
            return {"lyrics": "MPDP123"}

        def get_lyrics(self, browseId: str, timestamps: bool = False) -> dict[str, Any]:
            return {"hasTimestamps": False, "source": "Musixmatch", "lyrics": "Plain line A\nPlain line B"}

    client.yt = _FakeYTPlain()
    plain_res = client.fetch_lyrics("vidPlain1")
    assert plain_res is not None
    assert plain_res["synced"] is False
    assert plain_res["source"] == "Musixmatch"
    assert "Plain line A" in plain_res["plain_text"]


def test_lyrics_provider_cache_and_lrclib_fallback(tmp_path: Path, monkeypatch: Any) -> None:
    lyrics_dir = tmp_path / "lyrics"

    class _PlainYTMClient:
        def fetch_lyrics(self, video_id: str) -> dict[str, Any] | None:
            if video_id == "vidFallback":
                return {
                    "synced": False,
                    "source": "YouTube Music",
                    "lines": [],
                    "plain_text": "Plain fallback only",
                }
            return None

    provider = LyricsProvider(client=_PlainYTMClient(), lyrics_dir=lyrics_dir)  # type: ignore[arg-type]

    def _fake_http(url: str) -> Any:
        if "lrclib.net/api/get?" in url and "FallbackSong" in url:
            return {
                "syncedLyrics": "[00:05.00]Synced via LRCLIB\n[00:12.00]Second synced line",
                "plainLyrics": "Synced via LRCLIB\nSecond synced line",
            }
        if "lrclib.net/api/get?" in url and "SearchOnly" in url:
            return None
        if "lrclib.net/api/search?" in url and "SearchOnly" in url:
            return [
                {
                    "syncedLyrics": "[00:03.00]Found on search\n[00:09.00]Keep going",
                }
            ]
        return None

    monkeypatch.setattr(provider, "_http_get_json", _fake_http)

    track_a = Track(
        video_id="vidFallback",
        title="FallbackSong",
        artist="Test Artist",
        duration="03:00",
        duration_seconds=180,
    )
    res_a = provider.fetch(track_a)
    assert res_a is not None
    assert res_a.synced is True
    assert res_a.source == "LRCLIB"
    assert len(res_a.lines) == 2
    assert (lyrics_dir / "vidFallback.json").is_file()

    # Offline reload from disk cache without client or HTTP calls
    offline_provider = LyricsProvider(client=None, lyrics_dir=lyrics_dir)
    monkeypatch.setattr(offline_provider, "_http_get_json", lambda _url: None)
    cached_a = offline_provider.fetch(track_a)
    assert cached_a is not None
    assert cached_a.synced is True
    assert cached_a.lines[1].text == "Second synced line"

    # LRCLIB /api/search fallback when /api/get returns None
    track_b = Track(video_id="vidSearch", title="SearchOnly", artist="Artist B", duration_seconds=200)
    res_b = provider.fetch(track_b)
    assert res_b is not None
    assert res_b.synced is True
    assert res_b.lines[0].text == "Found on search"

    # Unsynced plain fallback returned and cached if LRCLIB has nothing
    class _PlainOnlyProvider(LyricsProvider):
        def _http_get_json(self, url: str) -> Any:
            return None

    plain_provider = _PlainOnlyProvider(client=_PlainYTMClient(), lyrics_dir=tmp_path / "lyrics2")  # type: ignore[arg-type]
    plain_res = plain_provider.fetch(track_a)
    assert plain_res is not None
    assert plain_res.synced is False
    assert plain_res.plain_text == "Plain fallback only"


class _DummyPlayer(MPVPlayer):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.mock_time_pos = 1.0
        self.mock_track = Track(
            video_id="focusVid01",
            title="Synthwave Anthem",
            artist="Neon Pulse",
            album="Night Grid",
            duration="03:20",
            duration_seconds=200,
            local_path="/tmp/fake.opus",
        )

    def is_running(self) -> bool:
        return True

    def get_status(self) -> dict[str, Any]:
        return {
            "running": True,
            "paused": False,
            "core_idle": False,
            "time_pos": self.mock_time_pos,
            "duration": 200.0,
            "volume": 85.0,
            "playlist_pos": 0,
            "playlist_count": 3,
            "current_track": self.mock_track,
            "queue": [self.mock_track],
            "upcoming": [],
            "radio_autoplay": False,
            "shuffle": True,
            "repeat_mode": "all",
        }


class _DummyClient:
    authenticated = False

    def search_tracks(self, query: str, limit: int = 15) -> list[Track]:
        return []

    def get_radio_queue(self, video_id: str, limit: int = 20) -> list[Track]:
        return []

    def fetch_lyrics(self, video_id: str) -> dict[str, Any] | None:
        return None


def test_tui_focus_mode_and_synced_lyrics(tmp_path: Path) -> None:
    sock_path = Path(f"/tmp/ytm-test-s10-{os.getpid()}.sock")
    settings_store = SettingsStore(settings_file=tmp_path / "config.json")
    liked_store = LikedStore(liked_file=tmp_path / "liked.json")
    playlist_store = PlaylistStore(playlists_file=tmp_path / "playlists.json")
    player = _DummyPlayer(
        socket_path=sock_path,
        state_file=tmp_path / "state.json",
        liked_store=liked_store,
        settings_store=settings_store,
    )

    lyrics_dir = tmp_path / "lyrics"
    lyrics_dir.mkdir(parents=True, exist_ok=True)
    synced_payload = LyricsData(
        video_id="focusVid01",
        synced=True,
        source="LRCLIB",
        lines=[
            LyricLine(start_sec=0.0, text="Opening intro neon glow", end_sec=10.0),
            LyricLine(start_sec=10.0, text="Cruising down midnight highway", end_sec=20.0),
            LyricLine(start_sec=20.0, text="Final chorus lights align", end_sec=35.0),
        ],
        plain_text="Opening intro neon glow\nCruising down midnight highway\nFinal chorus lights align",
    )
    (lyrics_dir / "focusVid01.json").write_text(
        json.dumps(synced_payload.to_dict()),
        encoding="utf-8",
    )

    plain_lines = [f"Plain lyric verse line {i:02d}" for i in range(1, 25)]
    plain_payload = LyricsData(
        video_id="plainVid02",
        synced=False,
        source="YouTube Music",
        lines=[],
        plain_text="\n".join(plain_lines),
    )
    (lyrics_dir / "plainVid02.json").write_text(
        json.dumps(plain_payload.to_dict()),
        encoding="utf-8",
    )

    lyrics_provider = LyricsProvider(client=_DummyClient(), lyrics_dir=lyrics_dir)  # type: ignore[arg-type]

    async def _run() -> None:
        app = YTMApp(
            player=player,
            client=_DummyClient(),  # type: ignore[arg-type]
            liked_store=liked_store,
            playlist_store=playlist_store,
            art_renderer=CoverArtRenderer(covers_dir=tmp_path / "covers"),
            settings_store=settings_store,
            lyrics_provider=lyrics_provider,
            initial_focus=True,
            music_dir=tmp_path / "music",
        )
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            assert app.focus_mode is True
            assert app.query_one("#top-bar").display is False
            assert app.query_one("#workspace").display is False
            assert app.query_one("#bottom-dock").display is False
            assert app.query_one("#focus-stage").display is True
            assert app.query_one("#focus-lyrics-box").display is False

            focus_spec = app.query_one("#focus-spectrum-box", Static)
            assert int(focus_spec.content_size.width) > 80

            # Toggle Lyrics open with y
            await pilot.press("y")
            await pilot.pause()
            assert app.lyrics_visible is True
            assert app.query_one("#focus-lyrics-box").display is True

            lyrics_box = app.query_one("#focus-lyrics-box", Static)
            rendered_early = str(lyrics_box.render())
            assert "|> Opening intro neon glow" in rendered_early

            # Advance playback position -> active marker advances to line 3
            player.mock_time_pos = 22.0
            app._refresh_status_tick()
            await pilot.pause()
            rendered_late = str(lyrics_box.render())
            assert "|> Final chorus lights align" in rendered_late

            # Switch to a track with plain unsynced lyrics and scroll via j/k
            player.mock_track = Track(
                video_id="plainVid02",
                title="Plain Ballad",
                artist="Acoustic Echo",
                duration="02:40",
                duration_seconds=160,
            )
            app._refresh_status_tick()
            await pilot.pause()
            assert app._current_lyrics is not None
            assert app._current_lyrics.synced is False
            assert "Plain lyric verse line 01" in str(lyrics_box.render())

            await pilot.press("j", "j", "j")
            await pilot.pause()
            assert app._lyrics_scroll_offset == 3
            assert "Plain lyric verse line 04" in str(lyrics_box.render())

            # First Esc closes lyrics panel while staying in Focus Mode
            await pilot.press("escape")
            await pilot.pause()
            assert app.focus_mode is True
            assert app.lyrics_visible is False
            assert app.query_one("#focus-lyrics-box").display is False

            # Second Esc exits Focus Mode back to full workspace
            await pilot.press("escape")
            await pilot.pause()
            assert app.focus_mode is False
            assert app.query_one("#focus-stage").display is False
            assert app.query_one("#workspace").display is True

            # Pressing z re-enters Focus Mode
            await pilot.press("z")
            await pilot.pause()
            assert app.focus_mode is True
            assert app.query_one("#focus-stage").display is True

            await pilot.press("q")

    asyncio.run(_run())


def test_cli_focus_flag_parsing() -> None:
    parser = build_parser()
    default_args = parser.parse_args([])
    assert default_args.focus is False

    top_focus = parser.parse_args(["--focus"])
    assert top_focus.focus is True
    assert top_focus.command is None

    sub_focus = parser.parse_args(["tui", "--focus"])
    assert sub_focus.focus is True
    assert sub_focus.command == "tui"

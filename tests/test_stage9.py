"""Stage 9 verification tests for Phase 3: Square Sixel cover art and PipeWire FFT spectrum DSP."""

from __future__ import annotations

import asyncio
import math
import os
import time
from pathlib import Path

import numpy as np
from PIL import Image
from textual.widgets import DataTable, Static

from ytm.art import CoverArtRenderer, trim_and_crop_square
from ytm.liked import LikedStore
from ytm.models import Track
from ytm.player import MPVPlayer
from ytm.playlists import PlaylistStore
from ytm.settings import SettingsStore
from ytm.spectrum import PipeWireSpectrumAnalyzer, SpectrumDSP, parse_pactl_sink_inputs
from ytm.tui import YTMApp


def test_cover_art_letterbox_trim_square_crop_and_dynamic_halfblock(tmp_path: Path) -> None:
    # Build a synthetic 480x360 letterboxed thumbnail: 45px black bars top/bottom, bright cyan center
    arr = np.zeros((360, 480, 3), dtype=np.uint8)
    arr[45:315, :, :] = [0, 230, 255]
    raw_jpg = tmp_path / "letterbox.jpg"
    Image.fromarray(arr, mode="RGB").save(raw_jpg, format="JPEG", quality=95)

    sq_png = trim_and_crop_square(raw_jpg, out_path=tmp_path / "letterbox_sq.png", size=360)
    assert sq_png is not None and sq_png.is_file()

    with Image.open(sq_png) as squared:
        assert squared.size == (360, 360)
        sq_arr = np.asarray(squared.convert("RGB"), dtype=np.uint8)
        # Top edge must be bright cyan rather than trimmed black letterbox bar
        assert float(sq_arr[5, 180, 1]) > 180.0
        assert float(sq_arr[5, 180, 2]) > 200.0
        assert float(sq_arr[354, 180, 1]) > 180.0

    renderer = CoverArtRenderer(covers_dir=tmp_path / "covers")
    small_markup = renderer.render_file(sq_png, width=14, height_cells=7)
    large_markup = renderer.render_file(sq_png, width=28, height_cells=14)
    assert len(small_markup.splitlines()) == 7
    assert len(large_markup.splitlines()) == 14
    assert all("▀" in line for line in large_markup.splitlines())


def test_parse_pactl_sink_inputs() -> None:
    sample = """
Sink Input #112
\tCorked: yes
\tProperties:
\t\tapplication.name = "Firefox"
\t\tnode.name = "Firefox"

Sink Input #396
\tCorked: no
\tProperties:
\t\tnode.name = "ytm"
\t\tapplication.name = "ytm"
\t\tmedia.name = "song.webm - mpv"

Sink Input #404
\tCorked: no
\tProperties:
\t\tapplication.name = "spotify"
"""
    assert parse_pactl_sink_inputs(sample, app_name="ytm") == 396
    assert parse_pactl_sink_inputs(sample, app_name="spotify") == 404
    assert parse_pactl_sink_inputs(sample, app_name="nonexistent") is None


def test_spectrum_dsp_frequency_separation_and_agc_volume_independence() -> None:
    sr = 44100
    chunk = 1024
    t_axis = np.arange(chunk, dtype=np.float64) / sr

    # 1. Pure 100 Hz bass signal should concentrate energy in lower bands (0..5)
    bass_dsp = SpectrumDSP(sample_rate=sr, fft_size=2048)
    bass_buf = np.zeros(2048, dtype=np.float32)
    for step in range(35):
        phase = step * chunk / sr
        samples = (0.5 * np.sin(2.0 * math.pi * 100.0 * (t_axis + phase))).astype(np.float32)
        bass_buf = np.concatenate([bass_buf[chunk:], samples])
        bass_bands = bass_dsp.process_samples(bass_buf, band_count=24)
    assert max(bass_bands[:6]) > max(bass_bands[16:]) + 0.35

    # 2. Pure 5000 Hz treble signal should concentrate energy in upper bands (16..23)
    treble_dsp = SpectrumDSP(sample_rate=sr, fft_size=2048)
    treble_buf = np.zeros(2048, dtype=np.float32)
    for step in range(35):
        phase = step * chunk / sr
        samples = (0.5 * np.sin(2.0 * math.pi * 5000.0 * (t_axis + phase))).astype(np.float32)
        treble_buf = np.concatenate([treble_buf[chunk:], samples])
        treble_bands = treble_dsp.process_samples(treble_buf, band_count=24)
    assert max(treble_bands[16:]) > max(treble_bands[:6]) + 0.35

    # 3. Volume independence: 0.80 amplitude vs 0.08 amplitude (-20 dB) stabilize near full height
    loud_dsp = SpectrumDSP(sample_rate=sr, fft_size=2048)
    quiet_dsp = SpectrumDSP(sample_rate=sr, fft_size=2048)
    loud_buf = np.zeros(2048, dtype=np.float32)
    quiet_buf = np.zeros(2048, dtype=np.float32)
    for step in range(40):
        phase = step * chunk / sr
        base = np.sin(2.0 * math.pi * 440.0 * (t_axis + phase))
        loud_s = (0.80 * base).astype(np.float32)
        quiet_s = (0.08 * base).astype(np.float32)
        loud_buf = np.concatenate([loud_buf[chunk:], loud_s])
        quiet_buf = np.concatenate([quiet_buf[chunk:], quiet_s])
        loud_bands = loud_dsp.process_samples(loud_buf, band_count=24)
        quiet_bands = quiet_dsp.process_samples(quiet_buf, band_count=24)

    assert max(loud_bands) > 0.70
    assert max(quiet_bands) > 0.70
    assert abs(max(loud_bands) - max(quiet_bands)) <= 0.12

    # 4. Digital silence decays smoothly toward resting floor
    silence = np.zeros(2048, dtype=np.float32)
    for _ in range(20):
        sil_bands = loud_dsp.process_samples(silence, band_count=24)
    assert max(sil_bands) <= 0.08


class _OfflineDummyClient:
    authenticated = False

    def search_tracks(self, query: str, limit: int = 15) -> list[Track]:
        return []

    def get_radio_queue(self, video_id: str, limit: int = 20) -> list[Track]:
        return []


def test_tui_phase3_spectrum_setting_cycle(tmp_path: Path) -> None:
    sock_path = Path(f"/tmp/ytm-test-s9-tui-{os.getpid()}.sock")
    settings_store = SettingsStore(settings_file=tmp_path / "config.json")
    liked_store = LikedStore(liked_file=tmp_path / "liked.json")
    playlist_store = PlaylistStore(playlists_file=tmp_path / "playlists.json")
    player = MPVPlayer(
        socket_path=sock_path,
        state_file=tmp_path / "state.json",
        liked_store=liked_store,
        settings_store=settings_store,
    )

    async def _run() -> None:
        app = YTMApp(
            player=player,
            client=_OfflineDummyClient(),  # type: ignore[arg-type]
            liked_store=liked_store,
            playlist_store=playlist_store,
            art_renderer=CoverArtRenderer(covers_dir=tmp_path / "covers"),
            settings_store=settings_store,
            music_dir=tmp_path / "music",
        )
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await pilot.press("6")
            await pilot.pause()

            settings_table = app.query_one("#settings-table", DataTable)
            assert settings_table.row_count == 10

            # Navigate to row 6 (Spectrum Mode) and cycle fft -> simulated -> off
            settings_table.move_cursor(row=6)
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            assert settings_store.load().spectrum_mode == "simulated"
            await pilot.press("enter")
            await pilot.pause()
            assert settings_store.load().spectrum_mode == "off"

            await pilot.press("q")

    asyncio.run(_run())


def test_dynamic_spectrum_width_fills_visualizer_box(tmp_path: Path) -> None:
    analyzer = PipeWireSpectrumAnalyzer(band_count=24)
    analyzer._bands = [0.2 + 0.6 * (i / 23.0) for i in range(24)]
    analyzer._capturing = True
    analyzer._last_frame_ts = time.monotonic()

    b42 = analyzer.get_bands(42)
    b18 = analyzer.get_bands(18)
    assert len(b42) == 42
    assert len(b18) == 18

    sock_path = Path(f"/tmp/ytm-test-s9-w-{os.getpid()}.sock")
    settings_store = SettingsStore(settings_file=tmp_path / "config.json")
    liked_store = LikedStore(liked_file=tmp_path / "liked.json")
    playlist_store = PlaylistStore(playlists_file=tmp_path / "playlists.json")
    player = MPVPlayer(
        socket_path=sock_path,
        state_file=tmp_path / "state.json",
        liked_store=liked_store,
        settings_store=settings_store,
    )

    async def _run() -> None:
        app = YTMApp(
            player=player,
            client=_OfflineDummyClient(),  # type: ignore[arg-type]
            liked_store=liked_store,
            playlist_store=playlist_store,
            art_renderer=CoverArtRenderer(covers_dir=tmp_path / "covers"),
            settings_store=settings_store,
            music_dir=tmp_path / "music",
        )

        async with app.run_test(size=(150, 40)) as pilot:
            await pilot.pause()
            app._tick_visualizer()
            vis_box = app.query_one("#visualizer-box", Static)
            expected_w = int(vis_box.content_size.width)
            assert expected_w > 24
            assert len(app._bands) == expected_w
            await pilot.press("q")

    asyncio.run(_run())

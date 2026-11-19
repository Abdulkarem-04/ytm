"""Live beat-reactive PipeWire FFT spectrum analyzer with Automatic Gain Control."""

from __future__ import annotations

import math
import os
import re
import select
import shutil
import subprocess
import threading
import time

import numpy as np

_SINK_INPUT_RE = re.compile(r"^Sink Input #(\d+)\s*$")


def parse_pactl_sink_inputs(output: str, app_name: str = "ytm") -> int | None:
    """Return the active PipeWire/PulseAudio sink-input ID matching app_name."""
    if not output:
        return None

    blocks: list[tuple[int, bool, bool]] = []
    cur_id: int | None = None
    corked = False
    matched = False

    def _flush() -> None:
        if cur_id is not None and matched:
            blocks.append((cur_id, corked, matched))

    target_app = f'application.name = "{app_name}"'
    target_node = f'node.name = "{app_name}"'
    for raw_line in output.splitlines():
        line = raw_line.strip()
        m = _SINK_INPUT_RE.match(line)
        if m:
            _flush()
            cur_id = int(m.group(1))
            corked = False
            matched = False
            continue
        if cur_id is None:
            continue
        if line.startswith("Corked:"):
            corked = line.split(":", 1)[1].strip().lower() == "yes"
        elif line == target_app or line == target_node:
            matched = True

    _flush()
    if not blocks:
        return None
    for sink_id, is_corked, _ in blocks:
        if not is_corked:
            return sink_id
    return blocks[0][0]


def find_ytm_sink_input(app_name: str = "ytm") -> int | None:
    """Query `pactl list sink-inputs` and return ytm's mpv sink-input index."""
    if shutil.which("pactl") is None:
        return None
    try:
        output = subprocess.check_output(
            ["pactl", "list", "sink-inputs"],
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=1.5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return parse_pactl_sink_inputs(output, app_name=app_name)


class SpectrumDSP:
    """Windowed RFFT frequency-band analyzer with volume-independent AGC."""

    def __init__(self, sample_rate: int = 44100, fft_size: int = 2048) -> None:
        self.sample_rate = max(8000, int(sample_rate))
        self.fft_size = max(256, int(fft_size))
        self._window = np.hanning(self.fft_size).astype(np.float32)
        self._freqs = np.fft.rfftfreq(self.fft_size, d=1.0 / self.sample_rate)
        self._agc_peak: float = 0.02
        self._smoothed: list[float] = []
        self._band_cache: dict[int, tuple[list[np.ndarray], np.ndarray]] = {}

    def reset(self) -> None:
        """Reset smoothing envelopes and AGC tracking state."""
        self._agc_peak = 0.02
        self._smoothed = []

    def _ensure_smoothed(self, band_count: int) -> None:
        if len(self._smoothed) != band_count:
            self._smoothed = [0.03] * band_count

    def _get_band_plan(self, band_count: int) -> tuple[list[np.ndarray], np.ndarray]:
        cached = self._band_cache.get(band_count)
        if cached is not None:
            return cached

        low_hz = 40.0
        high_hz = min(14000.0, float(self.sample_rate) * 0.45)
        edges = np.geomspace(low_hz, high_hz, band_count + 1)
        bins_per_band: list[np.ndarray] = []
        tilts = np.empty(band_count, dtype=np.float64)

        for i in range(band_count):
            lo = float(edges[i])
            hi = float(edges[i + 1])
            idx = np.where((self._freqs >= lo) & (self._freqs < hi))[0]
            if idx.size == 0:
                mid = 0.5 * (lo + hi)
                idx = np.array([int(np.argmin(np.abs(self._freqs - mid)))], dtype=np.int64)
            bins_per_band.append(idx)
            center = math.sqrt(lo * hi)
            tilts[i] = min(3.2, max(0.75, (center / 220.0) ** 0.34))

        self._band_cache[band_count] = (bins_per_band, tilts)
        return bins_per_band, tilts

    def decay(self, band_count: int = 24, factor: float = 0.72) -> list[float]:
        """Smoothly decay all bars toward the resting floor when paused or silent."""
        self._ensure_smoothed(band_count)
        for i in range(band_count):
            self._smoothed[i] = max(0.03, self._smoothed[i] * factor)
        return list(self._smoothed)

    def process_samples(self, samples: np.ndarray, band_count: int = 24) -> list[float]:
        """Compute normalized logarithmic spectrum band heights (0.03..1.0) from PCM float32 audio."""
        self._ensure_smoothed(band_count)
        arr = np.asarray(samples, dtype=np.float32).reshape(-1)
        if arr.size < 64:
            return self.decay(band_count)

        if arr.size >= self.fft_size:
            frame = arr[-self.fft_size :]
        else:
            frame = np.zeros(self.fft_size, dtype=np.float32)
            frame[-arr.size :] = arr

        centered = frame - float(np.mean(frame))
        rms = float(np.sqrt(np.mean(centered * centered)))
        if rms < 1e-5:
            return self.decay(band_count)

        spec = np.abs(np.fft.rfft(centered * self._window)) / float(self.fft_size)
        bins_per_band, tilts = self._get_band_plan(band_count)

        raw = np.empty(band_count, dtype=np.float64)
        for i, idx in enumerate(bins_per_band):
            vals = spec[idx]
            mag = 0.65 * float(np.max(vals)) + 0.35 * float(np.mean(vals))
            raw[i] = mag * tilts[i]

        frame_ref = max(
            float(np.percentile(raw, 92)),
            float(np.max(raw)) * 0.75,
            1e-6,
        )
        if frame_ref > self._agc_peak:
            self._agc_peak = 0.30 * self._agc_peak + 0.70 * frame_ref
        else:
            self._agc_peak = max(1e-5, 0.955 * self._agc_peak + 0.045 * frame_ref)

        scale = max(self._agc_peak * 1.05, 1e-6)
        for i in range(band_count):
            norm = min(1.0, max(0.04, (float(raw[i]) / scale) ** 0.85))
            if norm >= self._smoothed[i]:
                self._smoothed[i] = 0.25 * self._smoothed[i] + 0.75 * norm
            else:
                self._smoothed[i] = max(0.03, 0.64 * self._smoothed[i] + 0.36 * norm)

        return list(self._smoothed)


class PipeWireSpectrumAnalyzer:
    """Monitors only ytm's mpv PipeWire stream in a background thread to drive live FFT bars."""

    def __init__(
        self,
        sample_rate: int = 44100,
        fft_size: int = 2048,
        band_count: int = 24,
        app_name: str = "ytm",
    ) -> None:
        self.sample_rate = sample_rate
        self.fft_size = fft_size
        self.band_count = band_count
        self.app_name = app_name
        self.dsp = SpectrumDSP(sample_rate=sample_rate, fft_size=fft_size)

        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._proc: subprocess.Popen[bytes] | None = None

        self._active: bool = False
        self._capturing: bool = False
        self._bands: list[float] = [0.03] * band_count
        self._last_frame_ts: float = 0.0

    def set_active(self, active: bool) -> None:
        """Inform the monitor thread whether playback is actively unpaused."""
        clean = bool(active)
        terminate = False
        with self._lock:
            if self._active and not clean:
                self._capturing = False
                terminate = True
            self._active = clean
        if terminate:
            self._terminate_proc()

    def set_band_count(self, band_count: int) -> None:
        """Adjust target frequency band count (e.g. when entering Focus Mode)."""
        clean = max(8, int(band_count))
        with self._lock:
            self.band_count = clean

    def is_capturing(self) -> bool:
        """Return True when live audio samples from PipeWire arrived recently."""
        with self._lock:
            return self._capturing and (time.monotonic() - self._last_frame_ts) < 0.8

    def get_bands(self, band_count: int = 24) -> list[float]:
        """Return latest normalized FFT band heights (or decayed idle bars if stalled/paused)."""
        self.set_band_count(band_count)
        with self._lock:
            recent = self._capturing and (time.monotonic() - self._last_frame_ts) < 0.25
            if recent and self._bands:
                if len(self._bands) == band_count:
                    return list(self._bands)
                src_x = np.linspace(0.0, 1.0, len(self._bands))
                dst_x = np.linspace(0.0, 1.0, band_count)
                return [float(v) for v in np.interp(dst_x, src_x, self._bands)]
            self._bands = self.dsp.decay(band_count=band_count, factor=0.72)
            return list(self._bands)

    def start(self) -> None:
        """Start the background PipeWire capture thread if not already running."""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop_event.clear()
            self._thread = threading.Thread(
                target=self._worker_loop,
                name="ytm-pipewire-spectrum",
                daemon=True,
            )
            self._thread.start()

    def stop(self, timeout: float = 1.5) -> None:
        """Stop PipeWire capture cleanly and terminate any running parec child process."""
        self._stop_event.set()
        with self._lock:
            self._active = False
            self._capturing = False
            thread = self._thread
        self._terminate_proc()
        if thread is not None and thread.is_alive():
            thread.join(timeout=timeout)
        with self._lock:
            self._thread = None

    def _terminate_proc(self) -> None:
        with self._lock:
            proc = self._proc
            self._proc = None
        if proc is None:
            return
        try:
            proc.terminate()
            proc.wait(timeout=0.3)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

    def _worker_loop(self) -> None:
        ring = np.zeros(self.fft_size, dtype=np.float32)
        chunk_bytes = 1024 * 4  # 1024 float32 samples (~23 ms at 44.1 kHz)

        while not self._stop_event.is_set():
            with self._lock:
                is_active = self._active

            if not is_active or shutil.which("parec") is None:
                self._terminate_proc()
                with self._lock:
                    self._capturing = False
                self._stop_event.wait(0.15)
                continue

            sink_id = find_ytm_sink_input(self.app_name)
            if sink_id is None:
                with self._lock:
                    self._capturing = False
                self._stop_event.wait(0.4)
                continue

            cmd = [
                "parec",
                "--raw",
                f"--monitor-stream={sink_id}",
                "--client-name=ytm-spectrum",
                "--format=float32le",
                f"--rate={self.sample_rate}",
                "--channels=1",
                "--latency-msec=25",
                "--process-time-msec=25",
            ]
            try:
                proc = subprocess.Popen(
                    cmd,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                )
            except OSError:
                self._stop_event.wait(0.5)
                continue

            with self._lock:
                self._proc = proc
            ring.fill(0.0)
            remainder = b""
            last_data = time.monotonic()

            try:
                stdout = proc.stdout
                if stdout is None:
                    continue
                fd = stdout.fileno()
                while not self._stop_event.is_set():
                    with self._lock:
                        still_active = self._active
                    if not still_active:
                        break

                    ready, _, _ = select.select([fd], [], [], 0.2)
                    if not ready:
                        if time.monotonic() - last_data > 1.5:
                            new_sink = find_ytm_sink_input(self.app_name)
                            if new_sink is not None and new_sink != sink_id:
                                break
                        continue

                    try:
                        chunk = os.read(fd, chunk_bytes)
                    except OSError:
                        break
                    if not chunk:
                        break

                    buf = remainder + chunk
                    usable = (len(buf) // 4) * 4
                    remainder = buf[usable:]
                    if usable == 0:
                        continue

                    samples = np.frombuffer(buf[:usable], dtype=np.float32)
                    if samples.size >= self.fft_size:
                        ring[:] = samples[-self.fft_size :]
                    else:
                        n = samples.size
                        ring[:-n] = ring[n:]
                        ring[-n:] = samples

                    with self._lock:
                        target_bands = self.band_count
                        bands = self.dsp.process_samples(ring, band_count=target_bands)
                        self._bands = bands
                        self._capturing = True
                        self._last_frame_ts = time.monotonic()
                    last_data = time.monotonic()
            finally:
                with self._lock:
                    self._capturing = False
                self._terminate_proc()

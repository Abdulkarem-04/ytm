"""MPRIS2 D-Bus bridge exposing ytm playback controls to OS media keys and desktop widgets."""

import argparse
import asyncio
import json
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable, Annotated

from dbus_fast import Variant
from dbus_fast.aio import MessageBus
from dbus_fast.annotations import (
    DBusBool,
    DBusDict,
    DBusDouble,
    DBusInt64,
    DBusObjectPath,
    DBusSignature,
    DBusStr,
)
from dbus_fast.service import PropertyAccess, ServiceInterface, dbus_method, dbus_property, dbus_signal

from ytm.art import CoverArtRenderer
from ytm.config import COVERS_DIR, MPRIS_PID_FILE, MPV_SOCKET_PATH, STATE_FILE, ensure_dirs
from ytm.models import Track
from ytm.player import MPVError, MPVPlayer

DBusStrArr = Annotated[list[str], DBusSignature("as")]

MPRIS_BUS_NAME = "org.mpris.MediaPlayer2.ytm"
MPRIS_OBJECT_PATH = "/org/mpris/MediaPlayer2"
NO_TRACK_PATH = "/org/mpris/MediaPlayer2/TrackList/NoTrack"

_OBSERVED_MPV_PROPS = (
    "pause",
    "playlist-pos",
    "duration",
    "volume",
    "loop-file",
    "loop-playlist",
    "core-idle",
)


def loop_status_from_repeat(repeat_mode: str) -> str:
    """Convert ytm repeat_mode ('off', 'all', 'one') to MPRIS LoopStatus."""
    if repeat_mode == "one":
        return "Track"
    if repeat_mode == "all":
        return "Playlist"
    return "None"


def repeat_from_loop_status(loop_status: str) -> str:
    """Convert MPRIS LoopStatus ('None', 'Track', 'Playlist') to ytm repeat_mode."""
    mapping = {
        "None": "off",
        "Track": "one",
        "Playlist": "all",
    }
    if loop_status not in mapping:
        raise ValueError(f"Unsupported MPRIS LoopStatus: {loop_status!r}")
    return mapping[loop_status]


def playback_status_from_state(running: bool, paused: bool, has_track: bool) -> str:
    """Convert daemon state to MPRIS PlaybackStatus ('Playing', 'Paused', 'Stopped')."""
    if not running or not has_track:
        return "Stopped"
    if paused:
        return "Paused"
    return "Playing"


def track_object_path(video_id: str | None, index: int = 0) -> str:
    """Build a valid D-Bus object path for a track (or NO_TRACK_PATH if unset)."""
    if not video_id:
        return NO_TRACK_PATH
    safe_id = re.sub(r"[^A-Za-z0-9_]", "_", video_id)
    if not safe_id:
        safe_id = "track"
    return f"/org/ytm/MediaPlayer2/Track/{safe_id}_{max(0, int(index))}"


def build_mpris_metadata(
    track: Track | None,
    index: int = 0,
    duration_secs: float = 0.0,
    art_path: Path | None = None,
) -> dict[str, Variant]:
    """Build the MPRIS2 Metadata a{sv} dictionary for a track."""
    track_id = track_object_path(track.video_id if track else None, index)
    meta: dict[str, Variant] = {
        "mpris:trackid": Variant("o", track_id),
    }
    if track is None:
        return meta

    effective_duration = float(duration_secs or track.duration_seconds or 0.0)
    length_us = int(round(max(0.0, effective_duration) * 1_000_000))
    artist = track.artist.strip() if track.artist else "Unknown Artist"

    meta["xesam:title"] = Variant("s", track.title or "Unknown Title")
    meta["xesam:artist"] = Variant("as", [artist or "Unknown Artist"])
    meta["xesam:album"] = Variant("s", track.album or "YouTube Music")
    meta["xesam:url"] = Variant("s", track.url)
    meta["mpris:length"] = Variant("x", length_us)
    if art_path is not None and art_path.is_file():
        meta["mpris:artUrl"] = Variant("s", art_path.resolve().as_uri())
    return meta


class MPRISController:
    """Bridges MPRIS actions and property snapshots to MPVPlayer and CoverArtRenderer."""

    def __init__(
        self,
        player: MPVPlayer | None = None,
        art_renderer: CoverArtRenderer | None = None,
    ) -> None:
        self.player = player or MPVPlayer()
        self.art_renderer = art_renderer or CoverArtRenderer(covers_dir=COVERS_DIR)

    def get_art_path(self, video_id: str | None) -> Path | None:
        """Return cached thumbnail Path for video_id if present on disk."""
        if not video_id:
            return None
        for suffix in (".jpg", "_sq.png", ".png"):
            candidate = self.art_renderer.covers_dir / f"{video_id}{suffix}"
            if candidate.is_file() and candidate.stat().st_size > 0:
                return candidate
        return None

    def get_snapshot(self) -> dict[str, Any]:
        """Query MPVPlayer and build an MPRIS property snapshot."""
        status = self.player.get_status()
        running = bool(status.get("running"))
        paused = bool(status.get("paused", False))
        track: Track | None = status.get("current_track") if running else None
        pos_idx = int(status.get("playlist_pos", 0)) if running else 0
        duration = float(status.get("duration", 0.0) or 0.0) if running else 0.0
        time_pos = float(status.get("time_pos", 0.0) or 0.0) if running else 0.0
        volume_pct = float(status.get("volume", 100.0) or 100.0) if running else 100.0
        repeat_mode = str(status.get("repeat_mode", self.player.load_repeat_mode()))
        shuffle = bool(status.get("shuffle", self.player.load_shuffle()))

        has_track = track is not None
        art_path = self.get_art_path(track.video_id) if track is not None else None
        metadata = build_mpris_metadata(
            track,
            index=max(0, pos_idx),
            duration_secs=duration,
            art_path=art_path,
        )

        return {
            "running": running,
            "has_track": has_track,
            "playback_status": playback_status_from_state(running, paused, has_track),
            "loop_status": loop_status_from_repeat(repeat_mode),
            "shuffle": shuffle,
            "volume": round(max(0.0, min(1.5, volume_pct / 100.0)), 4),
            "position_us": int(round(max(0.0, time_pos) * 1_000_000)),
            "metadata": metadata,
            "track_id": track_object_path(track.video_id if track else None, max(0, pos_idx)),
            "video_id": track.video_id if track else None,
            "art_url": metadata["mpris:artUrl"].value if "mpris:artUrl" in metadata else "",
        }

    def get_position_us(self) -> int:
        """Return live playback position in microseconds."""
        if not self.player.is_running():
            return 0
        pos = self.player._get_property("time-pos", 0.0)
        try:
            return int(round(max(0.0, float(pos or 0.0)) * 1_000_000))
        except (TypeError, ValueError):
            return 0

    def play(self) -> None:
        """Resume playback if daemon is running."""
        if not self.player.is_running():
            return
        try:
            self.player._set_property("pause", False)
        except MPVError:
            pass

    def pause(self) -> None:
        """Pause playback if daemon is running."""
        if not self.player.is_running():
            return
        try:
            self.player._set_property("pause", True)
        except MPVError:
            pass

    def play_pause(self) -> None:
        """Toggle pause state if daemon is running."""
        if not self.player.is_running():
            return
        try:
            self.player.toggle_pause()
        except MPVError:
            pass

    def next_track(self) -> None:
        """Skip to the next track cleanly."""
        if not self.player.is_running():
            return
        try:
            self.player.next_track()
        except MPVError:
            pass

    def prev_track(self) -> None:
        """Return to the previous track cleanly."""
        if not self.player.is_running():
            return
        try:
            self.player.prev_track()
        except MPVError:
            pass

    def stop(self) -> None:
        """Stop playback and terminate mpv."""
        try:
            self.player.stop()
        except MPVError:
            pass

    def seek_relative_us(self, offset_us: int) -> int:
        """Seek relative to current position (in microseconds) and return new position."""
        if not self.player.is_running():
            return 0
        try:
            self.player.seek_relative(float(offset_us) / 1_000_000.0)
        except MPVError:
            pass
        return self.get_position_us()

    def set_position_us(self, track_id: str, position_us: int) -> int | None:
        """Seek to an absolute position (in microseconds) if track_id matches active track."""
        if not self.player.is_running():
            return None
        current_track_id = self.get_snapshot()["track_id"]
        if track_id != current_track_id:
            return None
        target = max(0.0, float(position_us) / 1_000_000.0)
        try:
            resp = self.player._ipc(["seek", target, "absolute"])
            if resp.get("error") != "success":
                time.sleep(0.05)
                self.player._ipc(["seek", target, "absolute"])
        except MPVError:
            return None
        return self.get_position_us()

    def set_loop_status(self, loop_status: str) -> None:
        """Update ytm repeat mode from an MPRIS LoopStatus string."""
        try:
            mode = repeat_from_loop_status(loop_status)
            self.player.set_repeat_mode(mode)
        except (ValueError, MPVError):
            pass

    def set_shuffle(self, enabled: bool) -> None:
        """Enable or disable shuffle via MPRIS."""
        try:
            self.player.set_shuffle(bool(enabled))
        except MPVError:
            pass

    def set_volume(self, volume: float) -> None:
        """Set volume from MPRIS 0.0..1.5 normalized scalar."""
        if not self.player.is_running():
            return
        pct = max(0.0, min(150.0, float(volume) * 100.0))
        try:
            self.player.set_volume(f"{pct:.1f}")
        except (MPVError, ValueError):
            pass


class MPRISRootInterface(ServiceInterface):
    """Implements org.mpris.MediaPlayer2."""

    def __init__(
        self,
        controller: MPRISController,
        on_quit: Callable[[], None] | None = None,
    ) -> None:
        super().__init__("org.mpris.MediaPlayer2")
        self.controller = controller
        self._on_quit = on_quit

    @dbus_method()
    def Raise(self) -> None:
        return None

    @dbus_method()
    def Quit(self) -> None:
        self.controller.stop()
        if self._on_quit is not None:
            self._on_quit()

    @dbus_property(access=PropertyAccess.READ)
    def CanQuit(self) -> DBusBool:
        return True

    @dbus_property(access=PropertyAccess.READ)
    def CanRaise(self) -> DBusBool:
        return False

    @dbus_property(access=PropertyAccess.READ)
    def HasTrackList(self) -> DBusBool:
        return False

    @dbus_property(access=PropertyAccess.READ)
    def Identity(self) -> DBusStr:
        return "ytm"

    @dbus_property(access=PropertyAccess.READ)
    def DesktopEntry(self) -> DBusStr:
        return "ytm"

    @dbus_property(access=PropertyAccess.READ)
    def SupportedUriSchemes(self) -> DBusStrArr:
        return ["https", "http", "file"]

    @dbus_property(access=PropertyAccess.READ)
    def SupportedMimeTypes(self) -> DBusStrArr:
        return [
            "audio/ogg",
            "audio/opus",
            "audio/mpeg",
            "audio/mp4",
            "audio/webm",
            "audio/wav",
        ]


class MPRISPlayerInterface(ServiceInterface):
    """Implements org.mpris.MediaPlayer2.Player."""

    def __init__(
        self,
        controller: MPRISController,
        on_change: Callable[[], None] | None = None,
    ) -> None:
        super().__init__("org.mpris.MediaPlayer2.Player")
        self.controller = controller
        self._on_change = on_change

    def _notify_change(self) -> None:
        if self._on_change is not None:
            self._on_change()

    @dbus_method()
    def Next(self) -> None:
        self.controller.next_track()
        self._notify_change()

    @dbus_method()
    def Previous(self) -> None:
        self.controller.prev_track()
        self._notify_change()

    @dbus_method()
    def Pause(self) -> None:
        self.controller.pause()
        self._notify_change()

    @dbus_method()
    def PlayPause(self) -> None:
        self.controller.play_pause()
        self._notify_change()

    @dbus_method()
    def Stop(self) -> None:
        self.controller.stop()
        self._notify_change()

    @dbus_method()
    def Play(self) -> None:
        self.controller.play()
        self._notify_change()

    @dbus_method()
    def Seek(self, Offset: DBusInt64) -> None:
        new_pos = self.controller.seek_relative_us(int(Offset))
        self.Seeked(new_pos)
        self._notify_change()

    @dbus_method()
    def SetPosition(self, TrackId: DBusObjectPath, Position: DBusInt64) -> None:
        new_pos = self.controller.set_position_us(str(TrackId), int(Position))
        if new_pos is not None:
            self.Seeked(new_pos)
        self._notify_change()

    @dbus_method()
    def OpenUri(self, Uri: DBusStr) -> None:
        return None

    @dbus_signal()
    def Seeked(self, Position: DBusInt64) -> DBusInt64:
        return int(Position)

    @dbus_property(access=PropertyAccess.READ)
    def PlaybackStatus(self) -> DBusStr:
        return str(self.controller.get_snapshot()["playback_status"])

    @dbus_property(access=PropertyAccess.READWRITE)
    def LoopStatus(self) -> DBusStr:
        return str(self.controller.get_snapshot()["loop_status"])

    @LoopStatus.setter
    def LoopStatus(self, value: DBusStr) -> None:
        self.controller.set_loop_status(str(value))
        self._notify_change()

    @dbus_property(access=PropertyAccess.READWRITE)
    def Rate(self) -> DBusDouble:
        return 1.0

    @Rate.setter
    def Rate(self, value: DBusDouble) -> None:
        return None

    @dbus_property(access=PropertyAccess.READWRITE)
    def Shuffle(self) -> DBusBool:
        return bool(self.controller.get_snapshot()["shuffle"])

    @Shuffle.setter
    def Shuffle(self, value: DBusBool) -> None:
        self.controller.set_shuffle(bool(value))
        self._notify_change()

    @dbus_property(access=PropertyAccess.READ)
    def Metadata(self) -> DBusDict:
        return dict(self.controller.get_snapshot()["metadata"])

    @dbus_property(access=PropertyAccess.READWRITE)
    def Volume(self) -> DBusDouble:
        return float(self.controller.get_snapshot()["volume"])

    @Volume.setter
    def Volume(self, value: DBusDouble) -> None:
        self.controller.set_volume(float(value))
        self._notify_change()

    @dbus_property(access=PropertyAccess.READ)
    def Position(self) -> DBusInt64:
        return self.controller.get_position_us()

    @dbus_property(access=PropertyAccess.READ)
    def MinimumRate(self) -> DBusDouble:
        return 1.0

    @dbus_property(access=PropertyAccess.READ)
    def MaximumRate(self) -> DBusDouble:
        return 1.0

    @dbus_property(access=PropertyAccess.READ)
    def CanGoNext(self) -> DBusBool:
        return bool(self.controller.get_snapshot()["has_track"])

    @dbus_property(access=PropertyAccess.READ)
    def CanGoPrevious(self) -> DBusBool:
        return bool(self.controller.get_snapshot()["has_track"])

    @dbus_property(access=PropertyAccess.READ)
    def CanPlay(self) -> DBusBool:
        return bool(self.controller.get_snapshot()["has_track"])

    @dbus_property(access=PropertyAccess.READ)
    def CanPause(self) -> DBusBool:
        return bool(self.controller.get_snapshot()["has_track"])

    @dbus_property(access=PropertyAccess.READ)
    def CanSeek(self) -> DBusBool:
        return bool(self.controller.get_snapshot()["has_track"])

    @dbus_property(access=PropertyAccess.READ)
    def CanControl(self) -> DBusBool:
        return True


class MPRISDaemon:
    """Asyncio event-driven MPRIS daemon tied to the lifetime of mpv IPC socket."""

    def __init__(
        self,
        socket_path: Path = MPV_SOCKET_PATH,
        state_file: Path = STATE_FILE,
        pid_file: Path = MPRIS_PID_FILE,
        bus_name: str = MPRIS_BUS_NAME,
    ) -> None:
        self.socket_path = socket_path
        self.state_file = state_file
        self.pid_file = pid_file
        self.bus_name = bus_name
        self.player = MPVPlayer(socket_path=socket_path, state_file=state_file)
        self.art_renderer = CoverArtRenderer(covers_dir=COVERS_DIR)
        self.controller = MPRISController(player=self.player, art_renderer=self.art_renderer)
        self._stop_event = asyncio.Event()
        self._sync_event = asyncio.Event()
        self._last_signature: tuple[Any, ...] | None = None
        self._fetching_cover_ids: set[str] = set()
        self.root_iface: MPRISRootInterface | None = None
        self.player_iface: MPRISPlayerInterface | None = None

    def _schedule_sync(self) -> None:
        self._sync_event.set()

    def _request_stop(self) -> None:
        self._stop_event.set()

    def _sync_properties(self, loop: asyncio.AbstractEventLoop) -> None:
        if self.player_iface is None:
            return
        snap = self.controller.get_snapshot()
        video_id = snap["video_id"]
        art_url = snap["art_url"]
        if video_id and not art_url and video_id not in self._fetching_cover_ids:
            self._fetching_cover_ids.add(video_id)

            async def _fetch_cover(vid: str) -> None:
                try:
                    path = await loop.run_in_executor(
                        None, self.art_renderer._download_thumbnail, vid
                    )
                    if path is not None:
                        self._schedule_sync()
                finally:
                    self._fetching_cover_ids.discard(vid)

            asyncio.create_task(_fetch_cover(video_id))

        meta = snap["metadata"]
        meta_sig = (
            snap["track_id"],
            meta["xesam:title"].value if "xesam:title" in meta else "",
            tuple(meta["xesam:artist"].value) if "xesam:artist" in meta else (),
            meta["xesam:album"].value if "xesam:album" in meta else "",
            meta["mpris:length"].value if "mpris:length" in meta else 0,
            art_url,
        )
        sig = (
            snap["playback_status"],
            snap["loop_status"],
            snap["shuffle"],
            snap["volume"],
            snap["has_track"],
            meta_sig,
        )
        if sig == self._last_signature:
            return

        prev = self._last_signature
        self._last_signature = sig
        if prev is None:
            return

        changed: dict[str, Any] = {}
        if sig[0] != prev[0]:
            changed["PlaybackStatus"] = snap["playback_status"]
        if sig[1] != prev[1]:
            changed["LoopStatus"] = snap["loop_status"]
        if sig[2] != prev[2]:
            changed["Shuffle"] = snap["shuffle"]
        if sig[3] != prev[3]:
            changed["Volume"] = snap["volume"]
        if sig[4] != prev[4]:
            has_t = snap["has_track"]
            changed["CanGoNext"] = has_t
            changed["CanGoPrevious"] = has_t
            changed["CanPlay"] = has_t
            changed["CanPause"] = has_t
            changed["CanSeek"] = has_t
        if sig[5] != prev[5]:
            changed["Metadata"] = meta

        if changed:
            self.player_iface.emit_properties_changed(changed)

    async def _observe_mpv_loop(self) -> None:
        if not self.socket_path.exists():
            self._request_stop()
            return
        try:
            reader, writer = await asyncio.open_unix_connection(str(self.socket_path))
        except OSError:
            self._request_stop()
            return

        try:
            for req_id, prop in enumerate(_OBSERVED_MPV_PROPS, start=1):
                cmd = json.dumps({"command": ["observe_property", req_id, prop]}) + "\n"
                writer.write(cmd.encode("utf-8"))
            await writer.drain()

            while not self._stop_event.is_set():
                line = await reader.readline()
                if not line:
                    break
                try:
                    msg = json.loads(line.decode("utf-8", errors="replace"))
                except json.JSONDecodeError:
                    continue
                if isinstance(msg, dict) and msg.get("event") in (
                    "property-change",
                    "seek",
                    "playback-restart",
                    "start-file",
                    "end-file",
                ):
                    if msg.get("event") == "seek" and self.player_iface is not None:
                        self.player_iface.Seeked(self.controller.get_position_us())
                    self._schedule_sync()
        except (OSError, asyncio.CancelledError):
            pass
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except OSError:
                pass
            self._request_stop()

    async def _sync_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._sync_properties(loop)
        while not self._stop_event.is_set():
            try:
                await asyncio.wait_for(self._sync_event.wait(), timeout=1.0)
            except asyncio.TimeoutError:
                pass
            self._sync_event.clear()
            if not self.socket_path.exists():
                self._request_stop()
                break
            self._sync_properties(loop)

    def _write_pid_file(self) -> None:
        if self.pid_file == MPRIS_PID_FILE:
            ensure_dirs()
        else:
            self.pid_file.parent.mkdir(parents=True, exist_ok=True)
        self.pid_file.write_text(str(os.getpid()), encoding="utf-8")

    def _remove_pid_file(self) -> None:
        try:
            if self.pid_file.is_file() and self.pid_file.read_text(encoding="utf-8").strip() == str(os.getpid()):
                self.pid_file.unlink()
        except OSError:
            pass

    async def run(self) -> None:
        """Connect to the session D-Bus and serve MPRIS until mpv exits or stopped."""
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, self._request_stop)
            except NotImplementedError:
                pass

        bus = await MessageBus().connect()
        self.root_iface = MPRISRootInterface(self.controller, on_quit=self._request_stop)
        self.player_iface = MPRISPlayerInterface(self.controller, on_change=self._schedule_sync)
        bus.export(MPRIS_OBJECT_PATH, self.root_iface)
        bus.export(MPRIS_OBJECT_PATH, self.player_iface)

        try:
            await bus.request_name(self.bus_name)
            self._write_pid_file()

            observe_task = asyncio.create_task(self._observe_mpv_loop())
            sync_task = asyncio.create_task(self._sync_loop(loop))
            await self._stop_event.wait()

            for task in (observe_task, sync_task):
                task.cancel()
            await asyncio.gather(observe_task, sync_task, return_exceptions=True)
        finally:
            self._remove_pid_file()
            bus.disconnect()


def _read_live_pid(pid_file: Path) -> int | None:
    if not pid_file.is_file():
        return None
    try:
        pid = int(pid_file.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None
    if pid <= 0:
        return None
    try:
        os.kill(pid, 0)
    except OSError:
        return None
    cmdline_file = Path(f"/proc/{pid}/cmdline")
    if cmdline_file.is_file():
        try:
            cmdline = cmdline_file.read_bytes().decode("utf-8", errors="replace")
            if "mpris" not in cmdline:
                return None
        except OSError:
            return None
    return pid


def is_mpris_running(pid_file: Path = MPRIS_PID_FILE) -> bool:
    """Return True if the detached ytm MPRIS D-Bus bridge process is alive."""
    return _read_live_pid(pid_file) is not None


def ensure_mpris_daemon(
    socket_path: Path = MPV_SOCKET_PATH,
    state_file: Path = STATE_FILE,
    pid_file: Path = MPRIS_PID_FILE,
    bus_name: str = MPRIS_BUS_NAME,
) -> bool:
    """Start the detached MPRIS D-Bus bridge if session bus is available and mpv is running."""
    if is_mpris_running(pid_file):
        return True
    if not os.environ.get("DBUS_SESSION_BUS_ADDRESS"):
        return False
    if not socket_path.exists():
        return False

    cmd = [
        sys.executable,
        "-m",
        "ytm.mpris",
        "--socket",
        str(socket_path),
        "--state-file",
        str(state_file),
        "--pid-file",
        str(pid_file),
        "--bus-name",
        bus_name,
    ]
    try:
        proc = subprocess.Popen(
            cmd,
            start_new_session=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError:
        return False

    deadline = time.monotonic() + 1.5
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            return False
        if is_mpris_running(pid_file):
            return True
        time.sleep(0.05)
    return is_mpris_running(pid_file)


def stop_mpris_daemon(pid_file: Path = MPRIS_PID_FILE) -> bool:
    """Terminate any running detached ytm MPRIS daemon and remove its PID file."""
    pid = _read_live_pid(pid_file)
    stopped = False
    if pid is not None:
        try:
            os.kill(pid, signal.SIGTERM)
            deadline = time.monotonic() + 1.2
            while time.monotonic() < deadline:
                if _read_live_pid(pid_file) is None:
                    break
                time.sleep(0.05)
            stopped = True
        except OSError:
            pass
    try:
        if pid_file.is_file():
            pid_file.unlink()
    except OSError:
        pass
    return stopped


def _build_cli_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="ytm MPRIS2 D-Bus bridge")
    parser.add_argument("--socket", type=Path, default=MPV_SOCKET_PATH)
    parser.add_argument("--state-file", type=Path, default=STATE_FILE)
    parser.add_argument("--pid-file", type=Path, default=MPRIS_PID_FILE)
    parser.add_argument("--bus-name", default=MPRIS_BUS_NAME)
    return parser


def main(argv: list[str] | None = None) -> int:
    """Entrypoint for running the detached ytm MPRIS D-Bus daemon."""
    args = _build_cli_parser().parse_args(argv)
    daemon = MPRISDaemon(
        socket_path=args.socket,
        state_file=args.state_file,
        pid_file=args.pid_file,
        bus_name=args.bus_name,
    )
    try:
        asyncio.run(daemon.run())
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

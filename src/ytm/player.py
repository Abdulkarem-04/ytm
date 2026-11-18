"""Persistent background mpv JSON-IPC player controller."""

from __future__ import annotations

import json
import socket
import subprocess
import time
from typing import Any

from ytm.config import MPV_SOCKET_PATH, STATE_FILE, ensure_dirs, get_ytdlp_path
from ytm.liked import LikedStore
from ytm.models import Track
from ytm.resolver import StreamResolver


class MPVError(RuntimeError):
    """Raised when communicating with the background mpv daemon fails."""


class MPVPlayer:
    """Controls a persistent background mpv process over a Unix JSON-IPC socket."""

    def __init__(
        self,
        socket_path=MPV_SOCKET_PATH,
        state_file=STATE_FILE,
        liked_store: LikedStore | None = None,
    ) -> None:
        self.socket_path = socket_path
        self.state_file = state_file
        self.liked_store = liked_store or LikedStore()
        self.resolver = StreamResolver()

    def _ipc(self, command: list[Any], timeout: float = 2.0) -> dict[str, Any]:
        """Send a single JSON-IPC command to mpv and return its response object."""
        if not self.socket_path.exists():
            raise MPVError("mpv IPC socket does not exist.")

        payload = (json.dumps({"command": command}) + "\n").encode("utf-8")
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
                sock.settimeout(timeout)
                sock.connect(str(self.socket_path))
                sock.sendall(payload)
                reader = sock.makefile("r", encoding="utf-8", errors="replace")
                for line in reader:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        msg = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(msg, dict) and "error" in msg:
                        return msg
        except OSError as exc:
            raise MPVError(f"Failed to communicate with mpv socket: {exc}") from exc

        raise MPVError("mpv closed IPC connection before responding.")

    def _get_property(self, prop: str, default: Any = None) -> Any:
        """Query an mpv property, returning default if unavailable or unset."""
        try:
            resp = self._ipc(["get_property", prop])
        except MPVError:
            return default
        if resp.get("error") == "success":
            return resp.get("data", default)
        return default

    def _set_property(self, prop: str, value: Any) -> None:
        """Set an mpv property and raise MPVError on failure."""
        resp = self._ipc(["set_property", prop, value])
        err = resp.get("error")
        if err != "success":
            raise MPVError(f"mpv set_property {prop!r} failed: {err}")

    def is_running(self) -> bool:
        """Return True if the background mpv daemon is alive and answering IPC."""
        if not self.socket_path.exists():
            return False
        try:
            resp = self._ipc(["get_property", "pid"], timeout=0.5)
            return resp.get("error") == "success" and isinstance(resp.get("data"), int)
        except MPVError:
            return False

    def ensure_daemon(self) -> None:
        """Start the background mpv daemon if it is not already running."""
        if self.is_running():
            return

        ensure_dirs()
        if self.socket_path.exists():
            try:
                self.socket_path.unlink()
            except OSError:
                pass

        cmd = [
            "mpv",
            "--no-video",
            "--idle=yes",
            "--force-window=no",
            f"--input-ipc-server={self.socket_path}",
            "--audio-client-name=ytm",
            "--ytdl-format=bestaudio/best",
            "--ytdl-raw-options=no-playlist=",
            f"--script-opts=ytdl_hook-ytdl_path={get_ytdlp_path()}",
        ]
        try:
            proc = subprocess.Popen(
                cmd,
                start_new_session=True,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except OSError as exc:
            raise MPVError(f"Failed to launch mpv daemon: {exc}") from exc

        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                raise MPVError(f"mpv daemon exited prematurely with code {proc.returncode}.")
            if self.is_running():
                return
            time.sleep(0.05)

        raise MPVError("Timed out waiting for mpv IPC socket to become ready.")

    def load_radio_autoplay(self) -> bool:
        """Read radio_autoplay preference from state.json (False if absent, True if unset in existing file)."""
        if not self.state_file.is_file():
            return False
        try:
            data = json.loads(self.state_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False
        if not isinstance(data, dict):
            return False
        return bool(data.get("radio_autoplay", True))

    def save_queue(self, tracks: list[Track], radio_autoplay: bool | None = None) -> None:
        """Persist the current track queue metadata and radio_autoplay state to disk."""
        ensure_dirs()
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        if radio_autoplay is None:
            resolved_radio = self.load_radio_autoplay() if self.state_file.is_file() else True
        else:
            resolved_radio = bool(radio_autoplay)
        payload = {
            "queue": [t.to_dict() for t in tracks],
            "radio_autoplay": resolved_radio,
            "updated_at": time.time(),
        }
        self.state_file.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def load_queue(self) -> list[Track]:
        """Load the persisted track queue metadata from disk."""
        if not self.state_file.is_file():
            return []
        try:
            data = json.loads(self.state_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        raw_queue = data.get("queue") if isinstance(data, dict) else None
        if not isinstance(raw_queue, list):
            return []
        tracks = [Track.from_dict(item) for item in raw_queue if isinstance(item, dict)]
        return self.liked_store.annotate(tracks)

    def _update_queue_tracks(self, video_id: str, attr: str, value: Any) -> None:
        if not self.state_file.is_file() or not video_id:
            return
        queue = self.load_queue()
        changed = False
        for track in queue:
            if track.video_id == video_id and getattr(track, attr) != value:
                setattr(track, attr, value)
                changed = True
        if changed:
            self.save_queue(queue)

    def sync_track_like(self, video_id: str, is_liked: bool) -> None:
        """Update is_liked flag in the persisted queue for matching video_id."""
        self._update_queue_tracks(video_id, "is_liked", is_liked)

    def sync_track_local_path(self, video_id: str, local_path: str) -> None:
        """Update local_path in the persisted queue for matching video_id."""
        self._update_queue_tracks(video_id, "local_path", local_path)

    def append_tracks(self, new_tracks: list[Track]) -> int:
        """Append unique tracks to the active mpv playlist and state.json without interrupting playback."""
        if not new_tracks or not self.is_running():
            return 0
        queue = self.load_queue()
        existing_ids = {t.video_id for t in queue if t.video_id}
        unique_tracks: list[Track] = []
        for track in new_tracks:
            if track.video_id and track.video_id not in existing_ids:
                existing_ids.add(track.video_id)
                unique_tracks.append(track)
        if not unique_tracks:
            return 0

        appended: list[Track] = []
        for track in unique_tracks:
            resp = self._ipc(["loadfile", track.url, "append"])
            if resp.get("error") == "success":
                appended.append(track)

        if appended:
            self.save_queue(queue + appended)
        return len(appended)

    def play_queue(
        self,
        tracks: list[Track],
        start_index: int = 0,
        radio_autoplay: bool = True,
    ) -> None:
        """Replace the active playback queue and begin playing at start_index."""
        if not tracks:
            raise ValueError("Cannot play an empty track queue.")
        if start_index < 0 or start_index >= len(tracks):
            start_index = 0

        self.ensure_daemon()
        self.save_queue(tracks, radio_autoplay=radio_autoplay)

        self._ipc(["playlist-clear"])
        for idx, track in enumerate(tracks):
            mode = "replace" if idx == 0 else "append"
            resp = self._ipc(["loadfile", track.url, mode])
            if resp.get("error") != "success":
                raise MPVError(f"Failed to load track {track.title!r}: {resp.get('error')}")

        self._set_property("pause", False)
        if start_index > 0:
            self.play_index(start_index)

        upcoming_ids = [
            t.video_id for t in tracks[start_index + 1 : start_index + 4] if not t.local_path
        ]
        if upcoming_ids:
            self.resolver.prefetch_async(upcoming_ids)

    def get_status(self) -> dict[str, Any]:
        """Return playback state, timing, volume, active track, and queue details."""
        if not self.is_running():
            return {"running": False}

        paused = bool(self._get_property("pause", False))
        time_pos = float(self._get_property("time-pos", 0.0) or 0.0)
        duration = float(self._get_property("duration", 0.0) or 0.0)
        volume = float(self._get_property("volume", 100.0) or 100.0)
        playlist_pos = self._get_property("playlist-pos", -1)
        playlist_count = int(self._get_property("playlist-count", 0) or 0)
        core_idle = bool(self._get_property("core-idle", True))

        queue = self.load_queue()
        idx = int(playlist_pos) if isinstance(playlist_pos, int) else -1
        current_track = queue[idx] if 0 <= idx < len(queue) else None
        upcoming = queue[idx + 1 :] if 0 <= idx < len(queue) else []

        if current_track and not duration and current_track.duration_seconds:
            duration = float(current_track.duration_seconds)

        return {
            "running": True,
            "paused": paused,
            "core_idle": core_idle,
            "time_pos": time_pos,
            "duration": duration,
            "volume": volume,
            "playlist_pos": idx,
            "playlist_count": playlist_count,
            "current_track": current_track,
            "upcoming": upcoming,
            "queue": queue,
            "radio_autoplay": self.load_radio_autoplay(),
        }

    def toggle_pause(self) -> bool:
        """Toggle pause/resume on the active daemon and return the new pause state."""
        if not self.is_running():
            raise MPVError("No active ytm playback session.")
        resp = self._ipc(["cycle", "pause"])
        if resp.get("error") != "success":
            raise MPVError(f"Failed to toggle pause: {resp.get('error')}")
        return bool(self._get_property("pause", False))

    def next_track(self) -> Track | None:
        """Advance to the next track in the queue and return it."""
        if not self.is_running():
            raise MPVError("No active ytm playback session.")
        resp = self._ipc(["playlist-next", "weak"])
        if resp.get("error") != "success":
            raise MPVError(f"Already at the end of the queue ({resp.get('error')}).")
        time.sleep(0.1)
        return self.get_status().get("current_track")

    def prev_track(self) -> Track | None:
        """Go back to the previous track in the queue and return it."""
        if not self.is_running():
            raise MPVError("No active ytm playback session.")
        resp = self._ipc(["playlist-prev", "weak"])
        if resp.get("error") != "success":
            raise MPVError(f"Already at the start of the queue ({resp.get('error')}).")
        time.sleep(0.1)
        return self.get_status().get("current_track")

    def replay(self) -> None:
        """Restart the current track from 0:00 and ensure playback is unpaused."""
        if not self.is_running():
            raise MPVError("No active ytm playback session.")
        resp = self._ipc(["seek", 0, "absolute"])
        if resp.get("error") != "success":
            raise MPVError(f"Failed to seek to start: {resp.get('error')}")
        self._set_property("pause", False)

    def play_index(self, index: int) -> Track | None:
        """Jump directly to a specific 0-based index in the current playlist."""
        if not self.is_running():
            raise MPVError("No active ytm playback session.")
        resp = self._ipc(["playlist-play-index", index])
        if resp.get("error") != "success":
            raise MPVError(f"Failed to jump to playlist index {index}: {resp.get('error')}")
        self._set_property("pause", False)
        deadline = time.monotonic() + 1.2
        while time.monotonic() < deadline:
            pos = self._get_property("playlist-pos", -1)
            time_pos = self._get_property("time-pos")
            if pos == index and time_pos is not None:
                break
            if pos != index:
                self._ipc(["playlist-play-index", index])
                self._set_property("pause", False)
            time.sleep(0.05)
        return self.get_status().get("current_track")

    def seek_relative(self, seconds: float) -> None:
        """Seek forward or backward by relative seconds."""
        if not self.is_running():
            raise MPVError("No active ytm playback session.")
        resp = self._ipc(["seek", seconds, "relative"])
        if resp.get("error") != "success":
            time.sleep(0.08)
            resp = self._ipc(["seek", seconds, "relative"])
        if resp.get("error") != "success":
            raise MPVError(f"Failed to seek: {resp.get('error')}")

    def set_volume(self, vol_spec: str) -> float:
        """Set or adjust volume (e.g. '80', '+10', '-10'), clamped to 0..150."""
        if not self.is_running():
            raise MPVError("No active ytm playback session.")
        raw = vol_spec.strip()
        try:
            if raw.startswith(("+", "-")):
                delta = float(raw)
                current = float(self._get_property("volume", 100.0) or 100.0)
                target = max(0.0, min(150.0, current + delta))
            else:
                target = max(0.0, min(150.0, float(raw)))
        except ValueError as exc:
            raise ValueError(f"Invalid volume value: {vol_spec!r}") from exc

        self._set_property("volume", target)
        return float(self._get_property("volume", target) or target)

    def stop(self) -> None:
        """Terminate the background mpv daemon and clean up its IPC socket."""
        if self.is_running():
            try:
                self._ipc(["quit"], timeout=1.0)
            except MPVError:
                pass
            deadline = time.monotonic() + 1.5
            while time.monotonic() < deadline and self.socket_path.exists():
                if not self.is_running():
                    break
                time.sleep(0.05)

        if self.socket_path.exists():
            try:
                self.socket_path.unlink()
            except OSError:
                pass

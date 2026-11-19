"""Persistent background mpv JSON-IPC player controller."""

from __future__ import annotations

import json
import os
import random
import socket
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

from ytm.config import MPV_SOCKET_PATH, STATE_FILE, ensure_dirs, get_ytdlp_path
from ytm.liked import LikedStore
from ytm.models import Track
from ytm.resolver import StreamResolver
from ytm.settings import SettingsStore

REPEAT_MODES = ("off", "all", "one")


class MPVError(RuntimeError):
    """Raised when communicating with the background mpv daemon fails."""


class MPVPlayer:
    """Controls a persistent background mpv process over a Unix JSON-IPC socket."""

    def __init__(
        self,
        socket_path=MPV_SOCKET_PATH,
        state_file=STATE_FILE,
        liked_store: LikedStore | None = None,
        settings_store: SettingsStore | None = None,
    ) -> None:
        self.socket_path = socket_path
        self.state_file = state_file
        self.liked_store = liked_store or LikedStore()
        self.settings_store = settings_store or SettingsStore()
        self.resolver = StreamResolver()
        self._playlist_lock = threading.RLock()
        self._rng = random.Random()

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

    def _maybe_start_mpris(self) -> bool:
        """Launch the detached MPRIS bridge for the live production daemon when enabled."""
        if self.socket_path != MPV_SOCKET_PATH:
            return False
        if not self.settings_store.load().mpris_enabled:
            return False
        from ytm.mpris import ensure_mpris_daemon

        return ensure_mpris_daemon(socket_path=self.socket_path, state_file=self.state_file)

    def _maybe_stop_mpris(self) -> bool:
        """Stop the detached MPRIS bridge for the live production daemon."""
        if self.socket_path != MPV_SOCKET_PATH:
            return False
        from ytm.mpris import stop_mpris_daemon

        return stop_mpris_daemon()

    def ensure_daemon(self) -> None:
        """Start the background mpv daemon if it is not already running."""
        if self.is_running():
            self._maybe_start_mpris()
            return

        ensure_dirs()
        if self.socket_path.exists():
            try:
                self.socket_path.unlink()
            except OSError:
                pass

        settings = self.settings_store.load()
        cmd = [
            "mpv",
            "--no-video",
            "--idle=yes",
            "--force-window=no",
            f"--input-ipc-server={self.socket_path}",
            "--audio-client-name=ytm",
            f"--ytdl-format={settings.ytdlp_format}",
            "--ytdl-raw-options=no-playlist=",
            f"--script-opts=ytdl_hook-ytdl_path={get_ytdlp_path()}",
            "--cache=yes",
            f"--demuxer-readahead-secs={settings.stream_buffer_secs}",
            "--demuxer-max-bytes=64MiB",
            "--prefetch-playlist=yes",
            "--gapless-audio=yes",
            "--network-timeout=30",
            "--stream-lavf-o=reconnect=1,reconnect_streamed=1,reconnect_delay_max=5",
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
                self._maybe_start_mpris()
                return
            time.sleep(0.05)

        raise MPVError("Timed out waiting for mpv IPC socket to become ready.")

    def _load_state(self) -> dict[str, Any]:
        """Return the raw state.json payload, or an empty dict if missing or invalid."""
        if not self.state_file.is_file():
            return {}
        try:
            data = json.loads(self.state_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    def _write_state(self, data: dict[str, Any]) -> None:
        """Atomically persist the raw state.json payload (tmp file + os.replace)."""
        if self.state_file == STATE_FILE:
            ensure_dirs()
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        data["updated_at"] = time.time()
        tmp = self.state_file.with_name(
            f"{self.state_file.name}.tmp-{os.getpid()}-{threading.get_ident()}"
        )
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        os.replace(tmp, self.state_file)

    def load_radio_autoplay(self) -> bool:
        """Read radio_autoplay preference from state.json (False if absent, True if unset in existing file)."""
        if not self.state_file.is_file():
            return False
        data = self._load_state()
        if not data:
            return False
        return bool(data.get("radio_autoplay", True))

    def load_shuffle(self) -> bool:
        """Return True if shuffle mode is enabled in state.json."""
        return bool(self._load_state().get("shuffle", False))

    def load_repeat_mode(self) -> str:
        """Return the persisted repeat mode ('off', 'all', or 'one')."""
        mode = self._load_state().get("repeat_mode", "off")
        return mode if mode in REPEAT_MODES else "off"

    def save_queue(self, tracks: list[Track], radio_autoplay: bool | None = None) -> None:
        """Persist the current track queue metadata and radio_autoplay state to disk."""
        data = self._load_state()
        if radio_autoplay is None:
            resolved_radio = bool(data.get("radio_autoplay", True)) if self.state_file.is_file() else True
        else:
            resolved_radio = bool(radio_autoplay)
        data["queue"] = [t.to_dict() for t in tracks]
        data["radio_autoplay"] = resolved_radio
        self._write_state(data)

    def load_queue(self) -> list[Track]:
        """Load the persisted track queue metadata from disk."""
        raw_queue = self._load_state().get("queue")
        if not isinstance(raw_queue, list):
            return []
        tracks = [Track.from_dict(item) for item in raw_queue if isinstance(item, dict)]
        return self.liked_store.annotate(tracks)

    def _load_original_queue(self) -> list[Track]:
        raw = self._load_state().get("original_queue")
        if not isinstance(raw, list):
            return []
        return [Track.from_dict(item) for item in raw if isinstance(item, dict)]

    def _update_queue_tracks(self, video_id: str, attr: str, value: Any) -> None:
        if not self.state_file.is_file() or not video_id:
            return
        with self._playlist_lock:
            data = self._load_state()
            changed = False
            for key in ("queue", "original_queue"):
                items = data.get(key)
                if not isinstance(items, list):
                    continue
                for item in items:
                    if isinstance(item, dict) and item.get("video_id") == video_id and item.get(attr) != value:
                        item[attr] = value
                        changed = True
            if changed:
                self._write_state(data)

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
        with self._playlist_lock:
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
                data = self._load_state()
                if data.get("shuffle") and isinstance(data.get("original_queue"), list):
                    data["original_queue"].extend(t.to_dict() for t in appended)
                    self._write_state(data)
            return len(appended)

    # ----- Repeat -----------------------------------------------------------

    def _apply_loop_props(self, mode: str) -> None:
        """Mirror the repeat mode onto mpv loop-file / loop-playlist properties."""
        if not self.is_running():
            return
        for prop, value in (
            ("loop-file", "inf" if mode == "one" else "no"),
            ("loop-playlist", "inf" if mode == "all" else "no"),
        ):
            try:
                self._set_property(prop, value)
            except MPVError:
                pass

    def set_repeat_mode(self, mode: str) -> str:
        """Set repeat mode ('off', 'all', 'one'), persist it, and apply to mpv."""
        clean = str(mode).strip().lower()
        if clean not in REPEAT_MODES:
            raise ValueError(f"Invalid repeat mode: {mode!r} (use off, all, or one).")
        with self._playlist_lock:
            data = self._load_state()
            data["repeat_mode"] = clean
            self._write_state(data)
        self._apply_loop_props(clean)
        return clean

    def cycle_repeat(self) -> str:
        """Cycle repeat mode off -> all -> one -> off and return the new mode."""
        current = self.load_repeat_mode()
        nxt = REPEAT_MODES[(REPEAT_MODES.index(current) + 1) % len(REPEAT_MODES)]
        return self.set_repeat_mode(nxt)

    # ----- Shuffle ----------------------------------------------------------

    def _prefetch_upcoming(self, tracks: list[Track]) -> None:
        upcoming_ids = [t.video_id for t in tracks[:3] if t.video_id and not t.local_path]
        if upcoming_ids:
            self.resolver.prefetch_async(upcoming_ids)

    def set_shuffle(self, enabled: bool) -> bool:
        """Enable or disable shuffle without interrupting the currently playing track."""
        enabled = bool(enabled)
        with self._playlist_lock:
            data = self._load_state()
            if bool(data.get("shuffle", False)) == enabled:
                return enabled

            queue = self.load_queue()
            pos = self._get_property("playlist-pos", -1) if self.is_running() else -1
            if not queue or not isinstance(pos, int) or not (0 <= pos < len(queue)):
                data["shuffle"] = enabled
                if not enabled:
                    data.pop("original_queue", None)
                self._write_state(data)
                return enabled

            current = queue[pos]
            if enabled:
                rest = queue[:pos] + queue[pos + 1 :]
                self._rng.shuffle(rest)
                self._ipc(["playlist-clear"])
                loaded: list[Track] = [current]
                for track in rest:
                    if self._ipc(["loadfile", track.url, "append"]).get("error") == "success":
                        loaded.append(track)
                data = self._load_state()
                data["queue"] = [t.to_dict() for t in loaded]
                data["original_queue"] = [t.to_dict() for t in queue]
                data["shuffle"] = True
                self._write_state(data)
                self._prefetch_upcoming(loaded[1:])
                return True

            original = self._load_original_queue() or list(queue)
            original = self.liked_store.annotate(original)
            k = next(
                (i for i, t in enumerate(original) if t.video_id == current.video_id),
                -1,
            )
            before = original[:k] if k >= 0 else []
            after = original[k + 1 :] if k >= 0 else [t for t in original if t.video_id != current.video_id]

            self._ipc(["playlist-clear"])
            restored_before: list[Track] = []
            for track in before:
                if self._ipc(["loadfile", track.url, "append"]).get("error") != "success":
                    continue
                self._ipc(["playlist-move", len(restored_before) + 1, len(restored_before)])
                restored_before.append(track)
            restored_after: list[Track] = []
            for track in after:
                if self._ipc(["loadfile", track.url, "append"]).get("error") == "success":
                    restored_after.append(track)

            restored = restored_before + [current] + restored_after
            data = self._load_state()
            data["queue"] = [t.to_dict() for t in restored]
            data.pop("original_queue", None)
            data["shuffle"] = False
            self._write_state(data)
            self._prefetch_upcoming(restored_after)
            return False

    def toggle_shuffle(self) -> bool:
        """Toggle shuffle mode and return the new state."""
        return self.set_shuffle(not self.load_shuffle())

    def play_queue(
        self,
        tracks: list[Track],
        start_index: int = 0,
        radio_autoplay: bool = True,
        preserve_order: bool = False,
    ) -> None:
        """Replace the active playback queue and begin playing at start_index.

        preserve_order=True reloads tracks exactly as given and keeps any saved shuffle
        original_queue (used when restarting the daemon).
        """
        if not tracks:
            raise ValueError("Cannot play an empty track queue.")
        if start_index < 0 or start_index >= len(tracks):
            start_index = 0

        self.ensure_daemon()
        with self._playlist_lock:
            original_order: list[Track] | None = None
            if not preserve_order and self.load_shuffle() and len(tracks) > 1:
                original_order = list(tracks)
                chosen = tracks[start_index]
                rest = tracks[:start_index] + tracks[start_index + 1 :]
                self._rng.shuffle(rest)
                tracks = [chosen] + rest
                start_index = 0

            self.save_queue(tracks, radio_autoplay=radio_autoplay)
            if not preserve_order:
                data = self._load_state()
                if original_order is not None:
                    data["original_queue"] = [t.to_dict() for t in original_order]
                else:
                    data.pop("original_queue", None)
                self._write_state(data)

            self._ipc(["playlist-clear"])
            for idx, track in enumerate(tracks):
                mode = "replace" if idx == 0 else "append"
                resp = self._ipc(["loadfile", track.url, mode])
                if resp.get("error") != "success":
                    raise MPVError(f"Failed to load track {track.title!r}: {resp.get('error')}")

        self._set_property("pause", False)
        if start_index > 0:
            self.play_index(start_index)
        self._apply_loop_props(self.load_repeat_mode())

        self._prefetch_upcoming(tracks[start_index + 1 : start_index + 4])

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
        state = self._load_state()
        repeat_mode = state.get("repeat_mode", "off")
        if repeat_mode not in REPEAT_MODES:
            repeat_mode = "off"
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
            "shuffle": bool(state.get("shuffle", False)),
            "repeat_mode": repeat_mode,
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
        if self.socket_path == MPV_SOCKET_PATH:
            self._maybe_stop_mpris()
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

    def replace_upcoming_entry(
        self,
        index: int,
        new_path: str,
        expected_video_id: str | None = None,
    ) -> bool:
        """Hot-swap an upcoming playlist entry with a local/preloaded file without interrupting playback.

        When expected_video_id is given, the swap only happens if the persisted queue still has
        that track at index (guards against stale preloads after shuffle reorders the playlist).
        """
        if not new_path or not Path(new_path).is_file() or not self.is_running():
            return False
        with self._playlist_lock:
            if expected_video_id is not None:
                queue = self.load_queue()
                if not (0 <= index < len(queue)) or queue[index].video_id != expected_video_id:
                    return False
            current_pos = self._get_property("playlist-pos", -1)
            count = self._get_property("playlist-count", 0)
            if not isinstance(current_pos, int) or not isinstance(count, int):
                return False
            if index <= current_pos or index >= count:
                return False
            try:
                r_add = self._ipc(["loadfile", str(new_path), "append"])
                if r_add.get("error") != "success":
                    return False
                r_mv = self._ipc(["playlist-move", count, index])
                if r_mv.get("error") != "success":
                    return False
                r_rm = self._ipc(["playlist-remove", index + 1])
                return r_rm.get("error") == "success"
            except MPVError:
                return False

    def restart_daemon(self) -> bool:
        """Restart the mpv daemon cleanly with updated settings, restoring queue and position."""
        status = self.get_status()
        self.stop()
        self.ensure_daemon()

        queue: list[Track] = status.get("queue") or []
        pos_idx = int(status.get("playlist_pos", -1)) if status.get("running") else -1
        if status.get("running") and queue and 0 <= pos_idx < len(queue):
            self.play_queue(
                queue,
                start_index=pos_idx,
                radio_autoplay=bool(status.get("radio_autoplay", False)),
                preserve_order=True,
            )
            volume = status.get("volume")
            if isinstance(volume, (int, float)):
                try:
                    self._set_property("volume", float(volume))
                except MPVError:
                    pass
            time_pos = float(status.get("time_pos", 0.0) or 0.0)
            if time_pos > 1.0:
                deadline = time.monotonic() + 1.0
                while time.monotonic() < deadline:
                    if self._get_property("time-pos") is not None:
                        break
                    time.sleep(0.05)
                try:
                    resp = self._ipc(["seek", time_pos, "absolute"])
                    if resp.get("error") != "success":
                        time.sleep(0.08)
                        self._ipc(["seek", time_pos, "absolute"])
                except MPVError:
                    pass
            try:
                self._set_property("pause", bool(status.get("paused", False)))
            except MPVError:
                pass
        return True

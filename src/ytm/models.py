"""Core data models and duration formatting helpers for ytm."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


def parse_duration_seconds(duration: str | None) -> int:
    """Parse a MM:SS or HH:MM:SS duration string into integer seconds."""
    if not duration or not isinstance(duration, str):
        return 0
    parts = duration.strip().split(":")
    try:
        nums = [int(p) for p in parts]
    except ValueError:
        return 0
    if len(nums) == 2:
        return nums[0] * 60 + nums[1]
    if len(nums) == 3:
        return nums[0] * 3600 + nums[1] * 60 + nums[2]
    if len(nums) == 1:
        return nums[0]
    return 0


def format_duration(seconds: float | int | None) -> str:
    """Format seconds as MM:SS or HH:MM:SS."""
    if seconds is None or seconds < 0:
        return "0:00"
    total = int(seconds)
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours > 0:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


@dataclass
class Track:
    """Represents a playable YouTube Music or locally cached track."""

    video_id: str
    title: str
    artist: str
    album: str = ""
    duration: str = "0:00"
    duration_seconds: int = 0
    is_liked: bool = False
    local_path: str | None = None

    @property
    def url(self) -> str:
        """Return local file path if cached on disk, otherwise YouTube watch URL."""
        if self.local_path and Path(self.local_path).is_file():
            return self.local_path
        return f"https://www.youtube.com/watch?v={self.video_id}"

    def to_dict(self) -> dict[str, Any]:
        """Serialize Track to a JSON-compatible dictionary."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Track:
        """Deserialize a Track from a dictionary."""
        return cls(
            video_id=str(data.get("video_id", "")),
            title=str(data.get("title", "Unknown Title")),
            artist=str(data.get("artist", "Unknown Artist")),
            album=str(data.get("album", "")),
            duration=str(data.get("duration", "0:00")),
            duration_seconds=int(data.get("duration_seconds", 0) or 0),
            is_liked=bool(data.get("is_liked", False)),
            local_path=data.get("local_path"),
        )

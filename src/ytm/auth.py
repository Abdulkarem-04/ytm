"""YouTube Music account authentication helpers using browser request headers."""

from __future__ import annotations

import json
import re
import shlex
from pathlib import Path
from typing import Any

import ytmusicapi
from rich.console import Console
from rich.panel import Panel

from ytm.config import AUTH_HEADERS_FILE, ensure_dirs

_VALID_HEADER_NAME_RE = re.compile(r"^[a-zA-Z0-9-]+$")
_PSEUDO_HEADERS = {":authority", ":method", ":path", ":scheme"}
_EXCLUDED_HEADERS = {"content-encoding"}


def sanitize_auth_headers_dict(headers: dict[str, Any]) -> dict[str, str]:
    """Keep only valid HTTP header keys and drop invalid Chromium pseudo-header artifacts."""
    cleaned: dict[str, str] = {}
    for raw_key, raw_val in headers.items():
        if not isinstance(raw_key, str):
            continue
        key = raw_key.strip()
        if not _VALID_HEADER_NAME_RE.match(key):
            continue
        if key.lower() in _EXCLUDED_HEADERS:
            continue
        cleaned[key] = str(raw_val)
    return cleaned


def sanitize_auth_file(auth_file: Path = AUTH_HEADERS_FILE) -> bool:
    """Sanitize an existing headers_auth.json in place. Returns True if modified."""
    if not auth_file.is_file():
        return False
    try:
        data = json.loads(auth_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    if not isinstance(data, dict):
        return False

    cleaned = sanitize_auth_headers_dict(data)
    if cleaned == data:
        return False

    auth_file.write_text(json.dumps(cleaned, indent=4, sort_keys=True), encoding="utf-8")
    return True


def _extract_headers_from_curl(raw_text: str) -> str:
    """Extract 'Key: Value' header lines from a copied cURL command."""
    normalized = raw_text.replace("\\\r\n", " ").replace("\\\n", " ")
    try:
        tokens = shlex.split(normalized, posix=True)
    except ValueError:
        tokens = []

    extracted: list[str] = []
    if tokens:
        i = 0
        while i < len(tokens):
            tok = tokens[i]
            if tok in ("-H", "--header") and i + 1 < len(tokens):
                extracted.append(tokens[i + 1])
                i += 2
                continue
            if tok.startswith("--header="):
                extracted.append(tok.split("=", 1)[1])
            elif tok in ("-b", "--cookie") and i + 1 < len(tokens):
                extracted.append(f"cookie: {tokens[i + 1]}")
                i += 2
                continue
            elif tok.startswith("--cookie="):
                extracted.append("cookie: " + tok.split("=", 1)[1])
            i += 1

    if not extracted:
        for match in re.finditer(r"(?:-H|--header)\s+(['\"])(.*?)\1", normalized):
            extracted.append(match.group(2))
        cookie_match = re.search(r"(?:-b|--cookie)\s+(['\"])(.*?)\1", normalized)
        if cookie_match:
            extracted.append(f"cookie: {cookie_match.group(2)}")

    return "\n".join(extracted)


def _strip_pseudo_headers(raw_text: str) -> str:
    """Remove HTTP/2 pseudo-headers (:authority, :method, :path, :scheme) including multiline Chromium blocks."""
    lines = raw_text.splitlines()
    out: list[str] = []
    skip_next_value = False

    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        if skip_next_value:
            skip_next_value = False
            continue

        if stripped.startswith(":"):
            # Handle ':authority' or ':authority:' (value on next line) vs ':authority: music.youtube.com'
            colon_idx = stripped.find(":", 1)
            if colon_idx == -1:
                if stripped.lower() in _PSEUDO_HEADERS:
                    skip_next_value = True
                continue
            name = stripped[:colon_idx].lower()
            rest = stripped[colon_idx + 1 :].strip()
            if name in _PSEUDO_HEADERS and not rest:
                skip_next_value = True
            continue

        out.append(line)

    return "\n".join(out)


def _clean_raw_headers(raw_headers: str) -> str:
    """Normalize pasted request headers or cURL commands before passing to ytmusicapi.setup."""
    text = raw_headers.strip()
    if text.lower().startswith("curl ") and ("-h " in text.lower() or "--header" in text.lower()):
        extracted = _extract_headers_from_curl(text)
        if extracted:
            text = extracted
    return _strip_pseudo_headers(text)


def save_headers_auth(raw_headers: str, auth_file: Path = AUTH_HEADERS_FILE) -> Path:
    """Parse raw browser request headers or cURL via ytmusicapi.setup and save sanitized JSON."""
    if not raw_headers.strip():
        raise ValueError("No request headers provided.")
    cleaned_raw = _clean_raw_headers(raw_headers)
    if not cleaned_raw.strip():
        raise ValueError("No valid HTTP headers found in input.")

    if auth_file == AUTH_HEADERS_FILE:
        ensure_dirs()
    else:
        auth_file.parent.mkdir(parents=True, exist_ok=True)

    ytmusicapi.setup(filepath=str(auth_file), headers_raw=cleaned_raw)
    sanitize_auth_file(auth_file)
    return auth_file


def remove_auth(auth_file: Path = AUTH_HEADERS_FILE) -> bool:
    """Remove saved YouTube Music authentication headers if present."""
    if auth_file.is_file():
        auth_file.unlink()
        return True
    return False


def run_interactive_auth(console: Console, auth_file: Path = AUTH_HEADERS_FILE) -> int:
    """Guide the user through pasting browser request headers or cURL and syncing Liked Music."""
    from ytm.api import YTMClient
    from ytm.liked import LikedStore

    instructions = (
        "[bold bright_white]Link your YouTube Music account via Browser Request Headers or cURL[/bold bright_white]\n\n"
        "[bold bright_cyan]1.[/] Open [bold bright_white]https://music.youtube.com[/] in your browser (logged in).\n"
        "[bold bright_cyan]2.[/] Open DevTools ([bold bright_yellow]F12[/]) -> [bold bright_yellow]Network[/] tab -> click Library/Home.\n"
        "[bold bright_cyan]3.[/] Click any [bold bright_white]browse[/], [bold bright_white]account_menu[/], or [bold bright_white]get[/] request.\n"
        "[bold bright_cyan]4.[/] Copy [bold bright_yellow]Request Headers[/] (or right-click -> [bold bright_yellow]Copy as cURL (bash)[/]).\n"
        "[bold bright_cyan]5.[/] Paste below, then type [bold bright_green]END[/] on a new line (or press [bold bright_yellow]Ctrl-D[/])."
    )
    console.print(
        Panel(
            instructions,
            title="[bold bright_cyan]ytm auth setup[/bold bright_cyan]",
            border_style="bright_cyan",
        )
    )

    lines: list[str] = []
    console.print("[bold bright_yellow]Paste request headers or cURL now:[/bold bright_yellow]")
    while True:
        try:
            line = input()
        except EOFError:
            break
        except KeyboardInterrupt:
            console.print("\n[bold bright_yellow]Authentication cancelled.[/bold bright_yellow]")
            return 130
        if line.strip() == "END":
            break
        lines.append(line)

    raw_headers = "\n".join(lines).strip()
    if not raw_headers:
        console.print("[bold bright_red]Error:[/bold bright_red] No headers received.")
        return 1

    try:
        saved_path = save_headers_auth(raw_headers, auth_file=auth_file)
    except Exception as exc:
        console.print(f"[bold bright_red]Failed to parse headers:[/bold bright_red] {exc}")
        return 1

    console.print(f"[bold bright_green]\\[SAVED][/bold bright_green] Auth headers written to [bright_cyan]{saved_path}[/bright_cyan]")

    try:
        store = LikedStore()
        client = YTMClient(liked_store=store)
        with console.status("[bright_cyan]Verifying account and syncing Liked Music...[/bright_cyan]"):
            remote_tracks = client.fetch_liked_songs(limit=200)
            added = store.merge_tracks(remote_tracks)
    except Exception as exc:
        console.print(
            f"[bold bright_yellow]Warning:[/bold bright_yellow] Credentials saved, "
            f"but Liked Music verification failed: {exc}"
        )
        return 1

    console.print(
        f"[bold bright_green]\\[SYNCED][/bold bright_green] Connected! "
        f"Fetched [bold bright_white]{len(remote_tracks)}[/bold bright_white] liked tracks "
        f"([bold bright_cyan]{added}[/bold bright_cyan] newly added to local store)."
    )
    return 0

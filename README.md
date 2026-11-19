# ytm

A high-contrast terminal music player built around YouTube Music (`ytmusicapi`), `yt-dlp`, `Textual`, and a persistent background `mpv` JSON-IPC daemon.

## Features

- Full-screen `btop`/`lazygit`-style interactive TUI (`ytm` or `ytm tui`) with Truecolor half-block album cover art (`#cover-box`) and a 7-row Unicode spectrum visualizer
- `[6] Settings` view (`~/.config/ytm/config.json`) to control connectivity, audio quality, buffering, and background caching without cluttering the sidebar
- Resilient streaming for slow connections: full-song RAM buffering (`--demuxer-readahead-secs=600`), FFmpeg auto-reconnect, rolling next-track preloading (`~/.cache/ytm/preload/`), optional Data Saver (`<=96k`) Opus quality, and polite sequential auto-caching of Liked Music (off by default)
- Collection-only playback inside `[3] Liked Music`, `[4] Local Cache`, and `[5] Playlists` without injecting unrelated tracks, plus explicit `R` Radio Mix mode
- Custom named playlists (`[5] Playlists`) persisted in `~/.config/ytm/playlists.json`
- Safe like workflow: `l`/`L` are add-only, `x` removes (debounced), and `u` restores the last unliked track from undo history
- Optional YouTube Music account link (`ytm auth`) to sync your Liked Music library and rate songs directly on your account
- Hybrid local audio caching (`ytm download`, `ytm cache-liked`, or `d`/`D` in the TUI) storing audio in `~/Music/ytm` for instant offline playback
- Persistent `mpv` background daemon over `/tmp/ytm-mpv.sock` so audio keeps playing after you detach or close your terminal

## TUI Keybindings

| Key | Action |
| --- | --- |
| `1` - `6` / `Tab` | Switch views (Active Queue, Search Results, Liked Music, Local Cache, Playlists, Settings) |
| `/` | Focus YouTube Music search input |
| `Esc` | Cancel input and return focus to the active table |
| `j` / `k` | Move cursor down / up |
| `g` / `G` | Jump to top / bottom of table |
| `Enter` / `o` | Play selected track (collection-only in `[3]`/`[4]`/`[5]`; Radio Mix in `[2]`) or toggle/run row in `[6] Settings` |
| `a` | Play all tracks in the active view starting at the highlighted row (collection-only) |
| `R` | Start a YouTube Music Radio mix seeded from the selected track |
| `Space` | Toggle Play / Pause |
| `n` / `p` | Next / Previous track |
| `r` | Replay current track from `00:00` |
| `l` / `L` | Add currently playing (`l`) or highlighted (`L`) track to Liked Music (add-only) |
| `x` | Remove highlighted track from active playlist (`[5]`) or unlike from Liked Music (debounced) |
| `u` | Undo the most recent unlike and restore track to Liked Music |
| `c` | Create a new custom playlist |
| `m` / `M` | Add highlighted (`m`) or currently playing (`M`) track to active playlist |
| `,` / `.` | Switch to previous / next custom playlist |
| `d` / `D` | Download/cache currently playing (`d`) or highlighted (`D`) track to `~/Music/ytm` |
| `s` | Sync Liked Music from linked YouTube Music account |
| `[` / `]` | Seek `-5s` / `+5s` |
| `-` / `+` | Adjust volume `-5%` / `+5%` |
| `q` | Detach TUI (keep background playback running) |
| `Q` | Stop background daemon and quit |

## Account Sync (`ytm auth`)

1. Run `ytm auth` in your terminal.
2. Open `https://music.youtube.com` in your browser, open DevTools (`F12`) -> `Network`, and click Library or Home.
3. Click any `browse`, `account_menu`, or `get` request and copy its **Request Headers** (or right-click -> **Copy as cURL (bash)**).
4. Paste into the prompt and end with `END` (or `Ctrl-D`).

Other auth commands:
- `ytm auth --status` - Check connection status
- `ytm auth --sync` - Pull remote Liked Music into local store
- `ytm auth --logout` - Remove saved credentials

## Quick CLI Usage

```bash
ytm
ytm play "Daft Punk Veridis Quo"
ytm status
ytm like
ytm download
ytm download "M83 Midnight City"
ytm cache-liked
ytm cache-liked --limit 25
ytm auth --status
ytm queue
ytm next
ytm prev
ytm replay
ytm toggle
ytm vol 80
ytm stop
```

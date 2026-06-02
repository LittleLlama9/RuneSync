# RuneSync

A lightweight champion-select companion for League of Legends. Auto-imports
runes for your locked-in champion and shows your matchup win rate vs the
opposing laner.

## What it does

- Detects when you lock in a champion in champ select via the League Client API (LCU)
- Pulls a rune page for your champion + role and pushes it into your client
- Shows your matchup win rate vs the opposing laner during the game
- Lives in the system tray; optionally starts with Windows

## Install / run

**Easy path (recommended):**
1. Download `RuneSync.exe` from the [Releases](../../releases) page
2. Launch League of Legends, then run `RuneSync.exe`

Optional: in Settings, toggle **"Start with Windows"** and RuneSync will live
silently in your tray, popping up automatically when League opens.

**From source:**
```
py -m pip install -r requirements.txt
py main.py
```

Requires Python 3.11+ on Windows. League must be running.

## Building the exe yourself

```
build.bat
```

Produces `dist/RuneSync.exe` via PyInstaller.

## Data sources

Rune recommendations and matchup win rates are aggregated with attribution from:

- [lolalytics.com](https://lolalytics.com)
- [u.gg](https://u.gg)

## License

GPL-3.0 — see [LICENSE](LICENSE).

## Disclaimer

RuneSync isn't endorsed by Riot Games and doesn't reflect the views or
opinions of Riot Games or anyone officially involved in producing or managing
League of Legends. League of Legends and Riot Games are trademarks or
registered trademarks of Riot Games, Inc. League of Legends © Riot Games, Inc.

# RuneSync

A lightweight League of Legends companion. Auto-imports runes during champion
select, shows matchup data, and keeps a local post-game performance history.

## What it does

- Detects when you lock in a champion in champ select via the League Client API (LCU)
- Pulls a rune page for your champion + role and pushes it into your client
- Shows your matchup win rate vs the opposing laner during the game
- Imports the latest available Summoner's Rift match history from the League client
- Calculates a transparent 0-100 DAEMON Score and ranks all 10 players
- Shows overall, recent-20, champion, and role win rates
- Opens a local post-game report with score components and factual observations
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
py app.py
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

Match history and post-game statistics come from the locally running League
Client API. They are not uploaded anywhere and never leave your machine.

## Post-game history and privacy

RuneSync stores normalized match history in:

```
%APPDATA%\RuneSync\history.db
```

The League client currently exposes the latest 100 matches through its local
history endpoint. RuneSync imports those once, then retains every supported game
it sees afterward, so long-term "all time" statistics mean the initial backfill
plus games captured after installation.

Version 1 scores Summoner's Rift normal draft, ranked solo/duo, and ranked flex.
Other queues and remakes are not scored.

## DAEMON Score

DAEMON Score is RuneSync's own transparent 0-100 performance summary. It is not
an imported U.GG/OP.GG score and is not an MMR, ELO, LP, or Riot rank estimate.

The versioned formula compares all 10 players using role-aware components:

- combat
- economy
- objectives
- vision
- survival/teamplay

The match rank is the score order from 1 through 10. A small win bonus is used,
but individual performance can outweigh the result. Stored reports keep their
score model version so future formula changes do not silently rewrite history.

## License

GPL-3.0 — see [LICENSE](LICENSE).

## Disclaimer

RuneSync isn't endorsed by Riot Games and doesn't reflect the views or
opinions of Riot Games or anyone officially involved in producing or managing
League of Legends. League of Legends and Riot Games are trademarks or
registered trademarks of Riot Games, Inc. League of Legends © Riot Games, Inc.

# RuneSync Mock LCU Server — Instructions

## Prerequisites
- Python installed (run as `py`)
- RuneSync source folder at `C:\Users\Matth\RuneSync`

---

## Setup (do this every time)

### Step 1 — Start the mock server
```powershell
cd C:\Users\Matth\RuneSync
py mock_lcu.py
```
The mock writes its lockfile to `%APPDATA%\RuneSync\lockfile` automatically and prints confirmation.

### Step 2 — Open RuneSync
Open `RuneSync.exe` normally, or in a second terminal:
```powershell
cd C:\Users\Matth\RuneSync
py main.py
```
No env var needed — RuneSync finds the mock lockfile automatically.

> **Note:** RuneSync waits ~15 seconds before its first connection attempt. Watch the Debug Log tab — wait for "✓ Connected to League Client" before sending mock commands.

---

## CLI Commands

### Draft control
| Command | Effect |
|---|---|
| `draft blue top` | Full reset: you are blue side, top lane (cell 0). Also enters ChampSelect. |
| `draft red mid` | Full reset: you are red side, mid lane (cell 4). |
| `start [role]` | Enter ChampSelect, reset draft. Default role: top. |
| `end` | Reset phase to None, clear draft state. |
| `phase <phase>` | Set gameflow phase directly. Values: `None`, `Lobby`, `ChampSelect`, `InProgress`, `EndOfGame` |

### Picking
| Command | Effect |
|---|---|
| `epick <pos> <champion>` | Enemy locks champion at position. Positions: `top`, `jng`, `mid`, `bot`, `sup` |
| `mypick <champion>` | You hover a champion (fires rune import if trigger = Hover) |
| `mylock <champion>` | You lock in a champion (fires rune import if trigger = Lock) |
| `turn <cell>` | Set `isInProgress=True` for a cell (marks it as your pick turn) |
| `pick <cell> <champion>` | Hover by raw cell ID (0–9). Cells 0–4 = your team, 5–9 = enemy team. |
| `lock <cell> <champion>` | Lock by raw cell ID. |
| `endturn <cell>` | Set `isInProgress=False` for a cell. |
| `myrole <role>` | Change your assigned position. |

### Inspection
| Command | Effect |
|---|---|
| `pages` | Print all rune pages RuneSync has created during this session. |
| `spells` | Print the last summoner spells RuneSync set. |
| `status` | Print full draft state (phase, teams, picks, pages, spells). |

### Scenarios (automated flows)
| Command | Effect |
|---|---|
| `scene toplaner_hovering` | Enemy top locks Garen, you hover Darius. Expect rune import on hover trigger. |
| `scene enemy_picks_first` | Enemy top locks Darius first, then it's your pick turn. Tests counterpick flow. |
| `scene full_draft` | All 10 picks filled in draft order with delays. |
| `scene game_start` | Enters ChampSelect then switches to InProgress after 3s. Tests window management. |
| `scenes` | List all available scenarios. |

### Misc
| Command | Effect |
|---|---|
| `addchamp <id> <name>` | Add a champion to the ID map at runtime. Example: `addchamp 777 Yone` |
| `help` | Print command list. |
| `q` / `quit` | Stop mock server and exit. |

---

## Typical Test Workflows

### 1. Rune import (basic)
```
mock> draft blue top
mock> epick top Garen
mock> turn 0
mock> mypick Darius
```
RuneSync should detect Garen as enemy top laner, display matchup tips, and import runes for Darius. Then check:
```
mock> pages     ← shows the rune page RuneSync created (name, tree IDs, perk IDs)
mock> spells    ← shows summoner spell IDs RuneSync set
```

### 2. Lock-in trigger
```
mock> draft blue top
mock> epick top Garen
mock> mylock Darius
```
Same as above but fires on lock-in instead of hover. Useful if trigger setting is set to "Lock".

### 3. Counterpick flow (enemy picks first)
```
mock> draft blue top
mock> epick top Darius
mock> turn 0
mock> mypick Garen
mock> mylock Garen
```
RuneSync should show Darius as the confirmed enemy laner before you pick, then import runes when you hover/lock Garen.

### 4. Window move test (requires second monitor)
```
mock> draft blue top
mock> phase InProgress
```
RuneSync window should move to the second monitor and go to game size. Check Debug Log for:
- "Game started — window moved to second monitor" ✓
- "No second monitor — window stays in place" (if no second monitor detected)

Then restore:
```
mock> end
```

### 5. Full automated scenario
```
mock> scene toplaner_hovering
```
Runs a scripted sequence automatically. Watch the RuneSync Debug Log in real time.

---

## Known Behavior

**u.gg errors are expected.** The u.gg scraping server (`localhost:8000`) is a separate process not running during mock tests. These messages in the Debug Log are normal:
- `⚠ Counters lookup failed: ...`
- `✗ Could not fetch u.gg build: ...`
- `⚠ Matchup lookup failed: ...`

Matchup tips from the **local `matchups.json` cache** still work — these require no external server.

**Champion not found?** The mock has ~160 champions built in. If a champion name isn't recognized, use:
```
mock> addchamp <riot_id> <exact_name>
```

**Reset between tests** with `end` or `draft blue top` to clear state before starting a new scenario.

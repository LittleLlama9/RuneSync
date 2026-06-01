## RuneSync v1.0.0 — first public release

Auto-imports runes for your locked-in champion and shows matchup tips during
champ select. Works in the background, lives in the system tray.

### Install

1. Download both **`RuneSync.exe`** and **`matchups.json`** from this release
2. Put them in the **same folder**
3. Launch League of Legends
4. Run `RuneSync.exe`

Optional: in Settings, toggle **"Start with Windows"** and RuneSync will live
silently in your tray, popping up automatically when League opens.

### What it does

- Detects when you lock in a champion in champ select via the League Client API
- Pulls a rune page for your champion + role from a daily-refreshed data bundle
  and applies it to your client
- Shows a small overlay with tips for the matchup against the opposing laner
- Per-champion overrides if you have your own preferred rune setup

### Architecture notes (for the curious)

- **No installer.** Single .exe, no admin required, no background services.
- **No scraping on your machine.** The u.gg / lolalytics scrape runs once daily
  on GitHub Actions and publishes a static JSON bundle. Clients just download
  it. No Brave, no Chromium, no FastAPI server needed on the user side.
- **Single process.** Tray icon, window, and League watcher are all one .exe.
- **Open source.** GPL-3.0. Data attribution to lolalytics, u.gg, op.gg,
  counterstats, and mobalytics in the README.

### Disclaimer

RuneSync isn't endorsed by Riot Games and doesn't reflect the views or
opinions of Riot Games. League of Legends © Riot Games, Inc.

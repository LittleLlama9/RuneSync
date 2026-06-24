"""bridge.py — Python<->JS glue for the DAEMON webview UI.

Api  : exposed to JS as window.pywebview.api.* (JS -> Python, returns JSON).
Pusher: window.evaluate_js marshaller for live events (Python -> JS), safe to
        call from the monitor's daemon threads.

All League logic stays in monitor/lcu/ugg/overrides; this is the presentation
glue that used to live in main.py's RuneSyncApp.
"""
import time, threading, datetime

import webview
import ugg_api
import item_data
import perks
from lcu import LCUClient, LCUConnectionError
from ugg_api import UGGClient
from overrides import OverrideManager
from monitor import ChampSelectMonitor
from tray import is_autostart_enabled, set_autostart

SUMMONER_SPELLS = {
    "Flash": 4, "Ignite": 14, "Exhaust": 3, "Barrier": 21, "Heal": 7,
    "Ghost": 6, "Teleport": 12, "Cleanse": 1, "Smite": 11, "Clarity": 13,
}
_SPELL_ID_TO_NAME = {v: k for k, v in SUMMONER_SPELLS.items()}
_LOG_CLS = {"success": "ok", "warn": "warn", "error": "error", "champ": "champ", "info": ""}


def _spell_label(s1, s2) -> str:
    if not s1 and not s2:
        return "u.gg default"
    n1 = _SPELL_ID_TO_NAME.get(s1, "?") if s1 else "—"
    n2 = _SPELL_ID_TO_NAME.get(s2, "?") if s2 else "—"
    return f"{n1} / {n2}".upper()


class Pusher:
    """Thread-safe Python -> JS event queue (PULL model).

    The edgechromium backend rejects window.evaluate_js() from non-UI threads,
    so instead of pushing we queue events here and let the JS drain them via
    Api.poll_events() — a normal JS->Python->return call, which is the supported
    direction and safe from any thread.
    """
    def __init__(self):
        self._q: list = []
        self._lock = threading.Lock()

    def push(self, event: str, payload: dict | None = None):
        with self._lock:
            self._q.append({"event": event, "payload": payload or {}})
            if len(self._q) > 1000:  # backstop if JS ever stops polling
                self._q = self._q[-500:]

    def drain(self) -> list:
        with self._lock:
            out, self._q = self._q, []
            return out


class Api:
    def __init__(self, pusher: Pusher):
        self.pusher = pusher
        self.lcu = LCUClient()
        self.overrides = OverrideManager()
        ugg_api.SERVER_URL = self.overrides.settings.get("server_url", ugg_api.SERVER_URL)
        self.ugg = UGGClient()
        self.monitor = None
        self.running = False
        self.status = "booting"
        self._quitting = False
        self._connect_lock = threading.Lock()
        self._connecting = False
        self.tray = None
        self.poller = None
        # snapshot of the live panels so a reload (get_state) rehydrates them
        self.snap = self._idle_snapshot()
        self.log_buf: list[dict] = []

    # ── snapshot helpers ──────────────────────────────────────────────────────
    @staticmethod
    def _idle_snapshot() -> dict:
        return {
            "champ": "", "champMeta": "[ awaiting champ select ]", "imported": False,
            "enemy": "", "wr": None, "wrLabel": "", "wrTag": "info", "sample": "",
            "runes": {"keystone": "", "primary": "", "secondary": "",
                      "primaryMinor": "", "secondaryMinor": "", "summoners": ""},
            "buildSrc": "idle", "build": [], "inGame": False,
        }

    def _settings(self) -> dict:
        s = self.overrides.settings
        return {
            "rank": s.get("rank", "Platinum+"), "region": s.get("region", "World"),
            "auto_role": s.get("auto_role", True), "trigger": s.get("trigger", "hover"),
            "phosphor": s.get("phosphor", "amber"), "autostart": is_autostart_enabled(),
        }

    # ── lifecycle ─────────────────────────────────────────────────────────────
    @staticmethod
    def _win():
        """The live window via the global registry — never stored on this object,
        so pywebview can't try to serialize the .NET window into the JS bridge."""
        try:
            return webview.windows[0] if webview.windows else None
        except Exception:
            return None

    def poll_events(self) -> list:
        """JS drains queued Python->JS events here (called on a timer)."""
        return self.pusher.drain()

    def boot(self):
        perks.warm()
        item_data.init()
        threading.Thread(target=self._load_bundle, daemon=True).start()
        threading.Thread(target=self._try_connect, daemon=True).start()

    def _load_bundle(self):
        try:
            ok = ugg_api.init_bundle()
        except Exception:
            ok = False
        if not ok:
            self._emit("Couldn't load build data — using fallback; some builds may be missing.", "warn")

    # ════════════════ JS -> Python API ════════════════
    def get_state(self) -> dict:
        st = {"status": self.status, "running": self.running,
              "theme": self.overrides.settings.get("phosphor", "amber"),
              "settings": self._settings(), "builds": self.get_builds(),
              "log": self.log_buf[-80:]}
        st.update(self.snap)
        return st

    def get_settings(self) -> dict:
        return self._settings()

    def get_builds(self) -> list:
        out = []
        for champ, d in self.overrides.all().items():
            out.append({
                "champ": champ, "role": d.get("role", "auto"),
                "path": f"{d.get('primary_tree', '—')} × {d.get('secondary_tree', '—')}",
                "summoners": _spell_label(d.get("spell1", 0), d.get("spell2", 0)),
            })
        return out

    def start_monitoring(self) -> dict:
        if not self.lcu.connected:
            return {"ok": False, "error": "League not connected."}
        self._start()
        return {"ok": True}

    def stop_monitoring(self) -> dict:
        self._stop()
        return {"ok": True}

    def reimport(self) -> dict:
        if not self.monitor or not self.running:
            self._emit("Start monitoring first.", "warn"); return {"ok": False}
        champ = self.monitor._my_champ
        if not champ:
            self._emit("No champion detected yet.", "warn"); return {"ok": False}
        session = self.lcu.get_champ_select_session()
        if not session:
            self._emit("Not in champion select.", "warn"); return {"ok": False}
        self._emit(f"Reimporting build for {champ}...", "info")
        threading.Thread(target=self.monitor._import_runes, args=(champ, session), daemon=True).start()
        return {"ok": True}

    def set_matchup_override(self, enemy: str) -> dict:
        enemy = (enemy or "").strip()
        if not enemy:
            return {"ok": False}
        if not self.monitor:
            self._emit("Start monitoring first to use matchup override.", "warn"); return {"ok": False}
        self.monitor.set_matchup_override(enemy)
        return {"ok": True}

    def set_theme(self, name: str) -> dict:
        s = dict(self.overrides.settings)   # full dict — save_settings replaces wholesale
        s["phosphor"] = name
        self.overrides.save_settings(s)
        return {"ok": True}

    def hide_to_tray(self) -> dict:
        try:
            w = self._win()
            if w: w.hide()
            if self.tray and self.tray.available():
                self.tray.notify("DAEMON", "Still running in the tray. Right-click the icon to quit.")
        except Exception:
            pass
        return {"ok": True}

    def quit_app(self) -> dict:
        self._quitting = True
        try:
            if self.poller: self.poller.stop()
        except Exception: pass
        try:
            if self.tray: self.tray.stop()
        except Exception: pass
        try:
            w = self._win()
            if w: w.destroy()
        except Exception: pass
        return {"ok": True}

    # ════════════════ connect / monitor ════════════════
    def _set_status(self, kind: str):
        self.status = kind
        self.pusher.push("status", {"kind": kind})

    def _try_connect(self):
        with self._connect_lock:
            if self.lcu.connected or self._connecting:
                return
            self._connecting = True
        try:
            self._emit("Connecting to League client...", "info")
            self._set_status("connecting")
            delay, attempts = 2, 8
            for attempt in range(1, attempts + 1):
                try:
                    self.lcu.connect()
                    self._emit("✓ Connected to League Client", "success")
                    self._set_status("connected")
                    self._start()
                    return
                except LCUConnectionError:
                    if attempt < attempts:
                        self._set_status("waiting")
                        time.sleep(delay); delay = min(delay * 2, 10)
                    else:
                        self._emit("League not detected — DAEMON will auto-connect when you open League.", "warn")
                        self._set_status("waiting")
        finally:
            with self._connect_lock:
                self._connecting = False

    def on_league_open(self):
        try:
            w = self._win()
            if w: w.show()
        except Exception: pass
        if not self.lcu.connected:
            threading.Thread(target=self._try_connect, daemon=True).start()

    def on_league_close(self):
        if self.running:
            self._stop()
        self.lcu.connected = False
        self._set_status("waiting")

    def _start(self):
        if not self.lcu.connected or self.running:
            return
        self.running = True
        self.pusher.push("running", {"on": True})
        self._set_status("monitoring")
        self._emit("──── Monitoring started ────", "warn")
        s = self.overrides.settings
        self.monitor = ChampSelectMonitor(
            lcu=self.lcu, ugg=self.ugg, overrides=self.overrides,
            on_log=self._emit, trigger=s.get("trigger", "hover"),
            rank=s.get("rank", "Platinum+"), region=s.get("region", "World"),
            auto_role=s.get("auto_role", True),
            on_game_start=lambda: self._on_game(True),
            on_game_end=lambda: self._on_game(False),
            on_league_closed=self._on_league_closed,
            on_matchup_winrate=self._on_matchup,
            on_import=self._on_import,
            on_runes_imported=self._on_runes,
            on_champ_detected=self._on_champ,
            on_build_detail=self._on_build,
        )
        threading.Thread(target=self.monitor.run, daemon=True).start()

    def _stop(self):
        self.running = False
        if self.monitor:
            self.monitor.stop()
        self.pusher.push("running", {"on": False})
        self._set_status("connected" if self.lcu.connected else "waiting")
        self._emit("──── Monitoring stopped ────", "warn")

    def _on_league_closed(self):
        if self.snap["inGame"]:
            self._on_game(False)
        if self.monitor:
            self.monitor._in_game = False
        self._stop()
        self.lcu.connected = False
        self._set_status("waiting")
        self._emit("League client closed — waiting for it to reopen...", "warn")

    # ════════════════ monitor callbacks -> pushes ════════════════
    def _emit(self, msg: str, tag: str = "info"):
        rec = {"ts": datetime.datetime.now().strftime("%H:%M:%S"),
               "msg": msg, "cls": _LOG_CLS.get(tag, "")}
        self.log_buf.append(rec)
        if len(self.log_buf) > 400:
            self.log_buf = self.log_buf[-300:]
        self.pusher.push("log", rec)

    def _on_champ(self, champ, role):
        lane = f"{role} lane" if role and role not in ("auto", "") else "lane"
        self.snap["champ"] = champ
        self.snap["champMeta"] = f"[ locked · {lane} ]"
        self.snap["imported"] = False
        self.pusher.push("champ", {"champ": champ, "meta": self.snap["champMeta"]})

    def _on_matchup(self, champ, enemy, role, wr, label, tag):
        clean = label.replace("✓", "").replace("✗", "").strip().upper()
        s = self.overrides.settings
        sample = f"{s.get('rank', 'Platinum+')} · {s.get('region', 'World')}".upper()
        self.snap.update({"champ": champ, "enemy": enemy, "wr": wr,
                          "wrLabel": clean, "wrTag": tag, "sample": sample})
        self.pusher.push("matchup", {"champ": champ, "enemy": enemy, "wr": wr,
                                     "label": clean, "tag": tag, "sample": sample})

    def _on_import(self, champ):
        self.snap["imported"] = True
        self.pusher.push("import_ok", {"champ": champ})

    def _on_runes(self, info):
        exp = perks.expand_rune_page(info.get("perk_ids", []))
        runes = {
            "keystone": (info.get("keystone") or exp["keystone"] or "—").upper(),
            "primary": info.get("primary") or "—",
            "secondary": info.get("secondary") or "—",
            "primaryMinor": exp["primaryMinor"],
            "secondaryMinor": exp["secondaryMinor"],
            "summoners": _spell_label(info.get("spell1", 0), info.get("spell2", 0)),
        }
        self.snap["runes"] = runes
        self.pusher.push("rune_page", runes)

    def _on_build(self, build, is_custom):
        item_data.wait_ready(4.0)   # resolve real names even if catalog just loaded
        items = []
        n = 0
        for iid in (build.get("items_start_ids") or []):
            n += 1
            items.append({"i": n, "name": item_data.name_for(iid), "tag": "start"})
        for j, iid in enumerate(build.get("items_core_ids") or []):
            n += 1
            items.append({"i": n, "name": item_data.name_for(iid),
                          "tag": "core ←" if j == 0 else "core", "core": j == 0})
        src = "custom" if is_custom else "u.gg"
        self.snap["buildSrc"] = src
        self.snap["build"] = items
        self.pusher.push("build", {"src": src, "items": items})

    def _on_game(self, in_game):
        self.snap["inGame"] = in_game
        self.pusher.push("game", {"in_game": in_game})

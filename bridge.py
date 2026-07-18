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
import champion_data
import perks
from lcu import LCUClient, LCUConnectionError
from ugg_api import UGGClient
from overrides import OverrideManager
from monitor import ACTIVE_GAME_PHASES, TERMINAL_GAME_PHASES, ChampSelectMonitor
from history_store import default_history_path
from match_history import MatchHistoryService
from live_client import LiveCaptureManager
from performance_score import ScoreRoutingError, load_score_v2_artifacts
from tray import is_autostart_enabled, set_autostart as _reg_set_autostart

SUMMONER_SPELLS = {
    "Flash": 4, "Ignite": 14, "Exhaust": 3, "Barrier": 21, "Heal": 7,
    "Ghost": 6, "Teleport": 12, "Cleanse": 1, "Smite": 11, "Clarity": 13,
}
_SPELL_ID_TO_NAME = {v: k for k, v in SUMMONER_SPELLS.items()}
_LOG_CLS = {"success": "ok", "warn": "warn", "error": "error", "champ": "champ", "info": ""}


def _load_configured_score_v2_artifacts(settings, directory):
    # DAEMON Score v2 is the default scorer whenever valid local artifacts are
    # installed; it is only skipped when the user has explicitly turned it off.
    # The shipped build ships no artifacts, so a fresh install loads nothing and
    # v1 stays active until a user installs artifacts locally.
    if settings.get("score_v2_beta") is False:
        return {}
    return load_score_v2_artifacts(
        directory,
        require_production_ready=False,
    )


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
        self._monitor_lock = threading.Lock()   # makes _start's check-and-set atomic
        self._pending_postgame_recovery = False
        self.tray = None
        self.poller = None
        self.log_queue = None   # set by app.py; drained to the debug console
        # snapshot of the live panels so a reload (get_state) rehydrates them
        self.snap = self._idle_snapshot()
        self.log_buf: list[dict] = []
        self.history = None
        self._history_error = ""
        self._score_v2_beta_sources: tuple[str, ...] = ()
        self._score_v2_beta_error = ""
        score_v2_beta = self.overrides.settings.get("score_v2_beta") is not False
        score_v2_artifacts = {}
        try:
            score_v2_artifacts = _load_configured_score_v2_artifacts(
                self.overrides.settings,
                default_history_path().parent / "score-v2-artifacts",
            )
        except ScoreRoutingError as e:
            self._score_v2_beta_error = str(e)
            self._emit(f"Score v2 artifacts unavailable: {e}", "warn")
        self._score_v2_beta_sources = tuple(score_v2_artifacts)
        try:
            self.history = MatchHistoryService(
                self.lcu,
                on_log=self._emit,
                on_updated=self._on_history_updated,
                on_postgame=self._on_postgame_ready,
                score_v2_artifacts=score_v2_artifacts,
                allow_development_score_v2=score_v2_beta,
            )
        except Exception as e:
            self._history_error = f"Local history unavailable: {e}"
            self._emit(self._history_error, "warn")
        # Supplemental/fallback Live Client Data capture (live_client.py).
        # Only meaningful alongside a working history store; if that failed
        # to construct there is nowhere to persist capture sessions.
        self.live_capture = (
            LiveCaptureManager(self.history.store, on_log=self._emit)
            if self.history else None
        )

    # ── snapshot helpers ──────────────────────────────────────────────────────
    @staticmethod
    def _idle_snapshot() -> dict:
        return {
            "champ": "", "champMeta": "[ awaiting champ select ]", "imported": False,
            "selecting": False,
            "enemy": "", "wr": None, "wrLabel": "", "wrTag": "info", "sample": "",
            "runes": {"keystone": "", "primary": "", "secondary": "",
                      "primaryMinor": "", "secondaryMinor": "", "summoners": ""},
            "buildSrc": "idle", "build": [], "inGame": False,
            "duo": None, "hud": None, "draft": None, "itemRecs": None,
        }

    def _settings(self) -> dict:
        s = self.overrides.settings
        interface_style = s.get("interface_style", "standard")
        if interface_style not in {"standard", "classic"}:
            interface_style = "standard"
        return {
            "rank": s.get("rank", "Platinum+"), "region": s.get("region", "World"),
            "auto_role": s.get("auto_role", True), "trigger": s.get("trigger", "hover"),
            "phosphor": s.get("phosphor", "amber"), "interface_style": interface_style,
            "score_v2_beta": s.get("score_v2_beta") is not False,
            "score_v2_beta_sources": list(
                getattr(self, "_score_v2_beta_sources", ()),
            ),
            "score_v2_beta_error": getattr(self, "_score_v2_beta_error", ""),
            "autostart": is_autostart_enabled(),
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
        champion_data.init()   # Data Dragon champion classes for the recommenders
        if self.log_queue is not None:
            threading.Thread(target=self._drain_log, daemon=True).start()
        threading.Thread(target=self._load_bundle, daemon=True).start()
        threading.Thread(target=self._try_connect, daemon=True).start()

    def _drain_log(self):
        """Pump the logging queue into the debug console (logrec events)."""
        q = self.log_queue
        while True:
            try:
                rec = q.get(timeout=1.0)
            except Exception:
                continue
            try:
                ts = datetime.datetime.fromtimestamp(rec.created).strftime("%H:%M:%S")
                self.pusher.push("logrec", {
                    "ts": ts,
                    "tag": getattr(rec, "rs_tag", "[unknown]"),
                    "sev": getattr(rec, "rs_severity", "debug"),
                    "msg": rec.getMessage(),
                })
            except Exception:
                pass

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
              "log": self.log_buf[-80:], "historyError": self._history_error}
        st.update(self.snap)
        return st

    def get_builds(self) -> list:
        out = []
        for champ, d in self.overrides.all().items():
            spell1 = d.get("spell1", 0)
            spell2 = d.get("spell2", 0)
            out.append({
                "champ": champ, "role": d.get("role", "auto"),
                "path": f"{d.get('primary_tree', '—')} × {d.get('secondary_tree', '—')}",
                "summoners": _spell_label(spell1, spell2),
                "spell1": spell1, "spell2": spell2,
            })
        return out

    def items_ready(self) -> bool:
        return item_data.is_ready()

    def search_items(self, query: str) -> list:
        item_data.wait_ready(2.0)
        return [{"id": i["id"], "name": i["name"], "icon": item_data.icon_url(i["id"])}
                for i in item_data.search(query or "", 14)]

    def get_override(self, champ: str) -> dict:
        d = self.overrides.get(champ) or {}
        return {
            "champ": champ,
            "role": d.get("role", "auto"),
            "primary_tree": d.get("primary_tree", "Precision"),
            "keystone": d.get("keystone", ""),
            "secondary_tree": d.get("secondary_tree", "Domination"),
            "rune_ids": d.get("rune_ids", []),
            "note": d.get("note", ""),
            "page_name": d.get("page_name", ""),
            "spell1": d.get("spell1", 0),
            "spell2": d.get("spell2", 0),
            "items_build": d.get("items_build", {}),   # preserved; edited in P5
        }

    def save_override(self, champ: str, data: dict) -> dict:
        champ = (champ or "").strip()
        if not champ:
            return {"ok": False, "error": "Enter a champion name."}
        # rune_ids may arrive as a comma string (from the input) or a list.
        rids = []
        raw = data.get("rune_ids")
        if isinstance(raw, str):
            raw = raw.strip()
            if raw:
                try:
                    rids = [int(x.strip()) for x in raw.split(",") if x.strip()]
                except ValueError:
                    return {"ok": False, "error": "Rune IDs must be integers."}
        elif isinstance(raw, list):
            try:
                rids = [int(x) for x in raw]
            except (TypeError, ValueError):
                rids = []
        existing = self.overrides.get(champ) or {}
        self.overrides.set(champ, {
            "role": data.get("role", "auto"),
            "primary_tree": data.get("primary_tree", "Precision"),
            "keystone": data.get("keystone", ""),
            "secondary_tree": data.get("secondary_tree", "Domination"),
            "rune_ids": rids,
            "note": (data.get("note") or "").strip(),
            "page_name": data.get("page_name", existing.get("page_name", "")),
            "spell1": int(data.get("spell1", 0) or 0),
            "spell2": int(data.get("spell2", 0) or 0),
            "items_build": data.get("items_build", existing.get("items_build", {})),
        })
        return {"ok": True}

    def remove_override(self, champ: str) -> dict:
        self.overrides.remove(champ)
        return {"ok": True}

    def import_rune_page_from_client(self) -> dict:
        from lcu import LCUClient, RUNE_TREE_IDS, KEYSTONE_IDS
        try:
            lcu = self.lcu
            if not lcu.connected:
                lcu = LCUClient(); lcu.connect()
            page = lcu.get_current_rune_page()
            if not page:
                return {"ok": False, "error": "No rune page found."}
            id_to_tree = {v: k for k, v in RUNE_TREE_IDS.items()}
            id_to_ks = {v: k for k, v in KEYSTONE_IDS.items()}
            perk_ids = page.get("selectedPerkIds", [])
            return {
                "ok": True,
                "primary_tree": id_to_tree.get(page.get("primaryStyleId", 0), ""),
                "secondary_tree": id_to_tree.get(page.get("subStyleId", 0), ""),
                "keystone": id_to_ks.get(perk_ids[0], "") if perk_ids else "",
                "rune_ids": perk_ids,
                "page_name": page.get("name", ""),
            }
        except LCUConnectionError as ex:
            return {"ok": False, "error": str(ex)}
        except Exception:
            return {"ok": False, "error": "Couldn't read your rune page — is League open?"}

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

    def set_interface(self, name: str) -> dict:
        if name not in {"standard", "classic"}:
            return {"ok": False, "error": "invalid interface style"}
        s = dict(self.overrides.settings)
        s["interface_style"] = name
        self.overrides.save_settings(s)
        return {"ok": True, "interface_style": name}

    def save_settings(self, data: dict) -> dict:
        # Start from the existing dict so unknown keys (server_url, phosphor) survive
        # — OverrideManager.save_settings replaces the whole dict. autostart is NOT
        # a settings key (it lives in the registry) so it is deliberately ignored.
        s = dict(self.overrides.settings)
        for k in ("rank", "region", "auto_role", "trigger", "phosphor"):
            if k in data:
                s[k] = data[k]
        if "score_v2_beta" in data:
            s["score_v2_beta"] = data["score_v2_beta"] is True
        interface_style = data.get("interface_style")
        if interface_style in {"standard", "classic"}:
            s["interface_style"] = interface_style
        elif s.get("interface_style") not in {"standard", "classic"}:
            s["interface_style"] = "standard"
        self.overrides.save_settings(s)
        # live-apply to a running monitor (plain attributes)
        if self.monitor:
            self.monitor.rank = s.get("rank", "Platinum+")
            self.monitor.region = s.get("region", "World")
            self.monitor.auto_role = s.get("auto_role", True)
            self.monitor.trigger = s.get("trigger", "hover")
        return {"ok": True}

    def set_autostart(self, enabled: bool) -> dict:
        ok = bool(_reg_set_autostart(bool(enabled)))
        return {"ok": ok, "enabled": is_autostart_enabled()}

    def get_history_summary(self) -> dict:
        if not self.history:
            return {
                "overall": {}, "recent20": {}, "champions": [], "roles": [],
                "error": self._history_error,
            }
        return self.history.summary()

    def get_match_history(self, offset: int = 0, limit: int = 25) -> list:
        if not self.history:
            return []
        return self.history.list_history(offset, limit)

    def get_match_report(self, game_id: int) -> dict:
        if not self.history:
            return {}
        try:
            report = self.history.report(int(game_id))
        except (TypeError, ValueError):
            report = None
        return report or {}

    def refresh_match_history(self) -> dict:
        if not self.history:
            return {"ok": False, "error": self._history_error}
        if not self.lcu.connected:
            return {"ok": False, "error": "League not connected."}
        threading.Thread(target=self._sync_history, daemon=True).start()
        return {"ok": True}

    def minimize(self) -> dict:
        w = self._win()
        if w:
            try: w.minimize()
            except Exception: pass
        return {"ok": True}

    def toggle_fullscreen(self) -> dict:
        w = self._win()
        if w:
            try: w.toggle_fullscreen()
            except Exception: pass
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
                    if self.live_capture:
                        threading.Thread(
                            target=self._recover_stale_live_capture, daemon=True,
                        ).start()
                    if self.history:
                        threading.Thread(
                            target=(
                                self._recover_postgame_after_reconnect
                                if self._pending_postgame_recovery
                                else self._sync_history
                            ),
                            daemon=True,
                        ).start()
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
        if self.snap["inGame"]:
            self._pending_postgame_recovery = True
            self._on_game(False, import_postgame=False)
        if self.running:
            self._stop()
        self.lcu.connected = False
        self._set_status("waiting")

    def _start(self):
        # Both the connect thread (_try_connect) and the JS worker thread
        # (start_monitoring) can call this; the lock makes the running guard
        # atomic so we never spawn two monitor threads against one LCU.
        with self._monitor_lock:
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
            on_champ_select_enter=self._on_champ_select_enter,
            on_duo_recommendations=self._on_duo,
            on_hud=self._on_hud,
            on_draft=self._on_draft,
            on_item_recs=self._on_item_recs,
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
            self._pending_postgame_recovery = True
            self._on_game(False, import_postgame=False)
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
        # Also persist to runesync.log (+ the debug console) so the import path
        # is diagnosable from the file, not just the in-window dispatch log.
        try:
            import logging
            lvl = {"warn": logging.WARNING, "error": logging.ERROR}.get(tag, logging.INFO)
            logging.getLogger().log(lvl, msg, extra={
                "rs_tag": "[monitor]",
                "rs_severity": {"warn": "warn", "error": "error"}.get(tag, "info")})
        except Exception:
            pass

    def _on_champ_select_enter(self):
        # Fresh champ select: wipe last game's panels back to a "selecting" state
        # so the UI doesn't show the previous champ/matchup until new data lands.
        fresh = self._idle_snapshot()
        fresh["selecting"] = True
        fresh["champMeta"] = "[ in champ select · selecting… ]"
        fresh["inGame"] = self.snap.get("inGame", False)
        self.snap.update(fresh)
        self.pusher.push("champ_select", {"active": True})

    def _on_champ(self, champ, role):
        lane = f"{role} lane" if role and role not in ("auto", "") else "lane"
        self.snap["champ"] = champ
        self.snap["champMeta"] = f"[ locked · {lane} ]"
        self.snap["imported"] = False
        self.snap["selecting"] = False
        self.pusher.push("champ", {"champ": champ, "meta": self.snap["champMeta"]})

    def _on_matchup(self, champ, enemy, role, wr, label, tag):
        clean = label.replace("✓", "").replace("✗", "").strip().upper()
        s = self.overrides.settings
        sample = f"{s.get('rank', 'Platinum+')} · {s.get('region', 'World')}".upper()
        self.snap.update({"champ": champ, "enemy": enemy, "wr": wr, "selecting": False,
                          "wrLabel": clean, "wrTag": tag, "sample": sample})
        self.pusher.push("matchup", {"champ": champ, "enemy": enemy, "wr": wr,
                                     "label": clean, "tag": tag, "sample": sample})

    def _on_duo(self, partner_champ, partner_role, my_role, recs):
        # Best champs to pick alongside a locked botlane partner. Empty recs =>
        # no duo data in the bundle; push an inactive payload so the panel hides.
        s = self.overrides.settings
        sample = f"{s.get('rank', 'Platinum+')} · {s.get('region', 'World')}".upper()
        payload = {
            "partner": partner_champ,
            "partnerRole": partner_role,
            "myRole": my_role,
            "recs": recs or [],
            "sample": sample,
            "active": bool(recs),
        }
        self.snap["duo"] = payload if recs else None
        self.pusher.push("duo_recs", payload)

    def _on_hud(self, hud):
        # Live in-game HUD snapshot (CS/min, lane deltas, gold estimate,
        # objective timers). Additive/read-only; stored on the snapshot so a
        # late-hydrating UI can pull it, and pushed for the live panel.
        self.snap["hud"] = hud
        self.pusher.push("hud", hud or {})

    def _on_draft(self, recs):
        # Champ-select draft/composition analysis (damage balance, engage, CC of
        # CHAMPIONS only). Additive/read-only. None => nothing to analyse yet.
        self.snap["draft"] = recs
        self.pusher.push("draft", recs or {})

    def _on_item_recs(self, recs):
        # In-game defensive item suggestions from the enemy damage profile.
        # About the user's own itemisation vs the enemy comp; additive/read-only.
        self.snap["itemRecs"] = recs
        self.pusher.push("item_recs", recs or {})

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
            items.append({"i": n, "name": item_data.name_for(iid), "tag": "start",
                          "icon": item_data.icon_url(iid)})
        for j, iid in enumerate(build.get("items_core_ids") or []):
            n += 1
            items.append({"i": n, "name": item_data.name_for(iid),
                          "tag": "core ←" if j == 0 else "core", "core": j == 0,
                          "icon": item_data.icon_url(iid)})
        src = "custom" if is_custom else "u.gg"
        self.snap["buildSrc"] = src
        self.snap["build"] = items
        self.pusher.push("build", {"src": src, "items": items})

    def _sync_history(self):
        if not self.history:
            return
        self.pusher.push("history_sync", {"active": True})
        try:
            imported = self.history.sync_recent(100)
            self._emit(
                f"History sync complete: {imported} new scored game"
                f"{'' if imported == 1 else 's'}.",
                "success" if imported else "info",
            )
        except Exception as e:
            self._emit(f"History sync failed: {e}", "warn")
            self.pusher.push("history_error", {"message": str(e)})
        finally:
            self.pusher.push("history_sync", {"active": False})

    def _ingest_postgame(self, live_capture_session_id=None):
        if not self.history:
            return
        self.pusher.push("history_sync", {"active": True})
        try:
            game_id = self.history.ingest_after_game()
            if game_id is None:
                self._emit("Post-game report skipped for this queue or remake.", "info")
            elif self.live_capture and live_capture_session_id:
                try:
                    self.live_capture.reconcile(game_id, live_capture_session_id)
                    self.history.refresh_score_v2(game_id)
                except Exception as e:
                    self._emit(f"Live client capture reconciliation failed: {e}", "warn")
        except Exception as e:
            self._emit(str(e), "warn")
            self.pusher.push("history_error", {"message": str(e)})
        finally:
            self.pusher.push("history_sync", {"active": False})

    def _on_history_updated(self):
        self.pusher.push("history_updated")

    def _recover_stale_live_capture(self):
        """Sweep any capture session left 'active' by a crashed prior
        process. Skipped while a game is currently active/reconnecting --
        that path is handled by _on_game(True)'s resume logic instead, which
        needs to know the authoritative game ID before deciding what to keep."""
        if not self.live_capture:
            return
        try:
            phase = self.lcu.get_game_flow_phase()
        except Exception:
            return
        if phase not in ACTIVE_GAME_PHASES:
            try:
                self.live_capture.recover_stale_sessions()
            except Exception as e:
                self._emit(f"Live client capture recovery failed: {e}", "warn")

    def _recover_postgame_after_reconnect(self):
        try:
            phase = self.lcu.get_game_flow_phase()
            if phase in TERMINAL_GAME_PHASES:
                # No live_capture_session_id: this process never started a
                # capture session for this game (it wasn't running while the
                # game happened), so there is nothing to reconcile against --
                # reconciliation only ever targets a session this process
                # itself stopped, never a guess at "the latest" one.
                self._ingest_postgame()
            elif phase in ACTIVE_GAME_PHASES:
                self._emit("Game still active after reconnect; post-game recovery deferred.", "info")
                if self.live_capture and self.history:
                    try:
                        self.history.capture_active_game()
                        self.live_capture.start(self.history.active_game_id)
                    except Exception as e:
                        self._emit(f"Live client capture could not resume: {e}", "warn")
        finally:
            self._pending_postgame_recovery = False
        self._sync_history()

    def _on_postgame_ready(self, game_id: int):
        self.pusher.push("postgame_ready", {"game_id": game_id})
        try:
            window = self._win()
            if window:
                window.show()
        except Exception:
            pass

    def _on_game(self, in_game, import_postgame=True):
        self.snap["inGame"] = in_game
        self.pusher.push("game", {"in_game": in_game})
        if in_game:
            if self.history:
                try:
                    self.history.capture_active_game()
                except Exception as e:
                    self._emit(f"Could not capture active game metadata: {e}", "warn")
            if self.live_capture:
                try:
                    self.live_capture.start(
                        self.history.active_game_id if self.history else None,
                    )
                except Exception as e:
                    self._emit(f"Live client capture could not start: {e}", "warn")
        else:
            stopped_session_id = None
            if self.live_capture:
                try:
                    stopped_session_id, _ = self.live_capture.stop(
                        status=None if import_postgame else "partial_client_closed",
                    )
                except Exception as e:
                    self._emit(f"Live client capture stop failed: {e}", "warn")
            if import_postgame and self.history:
                threading.Thread(
                    target=self._ingest_postgame,
                    args=(stopped_session_id,), daemon=True,
                ).start()

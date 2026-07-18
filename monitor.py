"""
ChampSelectMonitor — polls the LCU for champ select events and imports runes.
"""

import time, threading
from typing import Callable, Optional
from lcu import LCUClient, LCUConnectionError, RUNE_TREE_IDS, KEYSTONE_IDS
from ugg_api import UGGClient
from overrides import OverrideManager
from champion_roles import infer_roles, infer_full_assignment
from live_client import LiveClientDataClient, LiveClientDataError
import live_hud
import item_recs
import draft_recs

# Standard lane second-summoner (paired with Flash) used to replace a jungle
# Smite when an off-role fallback build is imported for a laner. Smite is
# useless outside the jungle, so a top Sejuani should never have it set.
_LANE_SECOND_SPELL = {"top": 12, "mid": 14, "bot": 7, "support": 3}  # TP/Ignite/Heal/Exhaust
_SMITE_SPELL_ID = 11
_LCU_POSITION_TO_ROLE = {
    "top": "top", "jungle": "jungle", "middle": "mid",
    "bottom": "bot", "utility": "support",
}
ACTIVE_GAME_PHASES = {"InProgress", "Reconnect"}
TERMINAL_GAME_PHASES = {"WaitingForStats", "PreEndOfGame", "EndOfGame", "Lobby"}


class ChampSelectMonitor:
    POLL_INTERVAL = 1.0

    def __init__(self, lcu: LCUClient, ugg: UGGClient, overrides: OverrideManager,
                 on_log: Callable, trigger: str = "hover", rank: str = "Platinum+",
                 region: str = "World", auto_role: bool = True,
                 on_game_start=None, on_game_end=None, on_league_closed=None,
                 on_matchup_winrate=None, on_item_build=None, on_import=None,
                 on_runes_imported=None, on_champ_detected=None, on_build_detail=None,
                 on_champ_select_enter=None, on_duo_recommendations=None,
                 on_hud=None, on_draft=None, on_item_recs=None):
        self.lcu = lcu
        self.ugg = ugg
        self.overrides = overrides
        self.log = on_log
        self.trigger = trigger
        self.rank = rank
        self.region = region
        self.auto_role = auto_role
        self._on_game_start = on_game_start
        self._on_game_end = on_game_end
        self._on_league_closed = on_league_closed
        self._on_matchup_winrate = on_matchup_winrate
        self._on_item_build = on_item_build
        self._on_import = on_import   # (champ_name) -> shown as a success banner
        # (info dict) -> populates the RUNE PAGE panel with what was just pushed.
        # Read-only: fired after a successful import, never drives import logic.
        self._on_runes_imported = on_runes_imported
        # (champ, role) when a champion is detected; (build_dict, is_custom) with
        # the full build (section ID lists) so the UI can show start/core tags.
        # Both read-only/additive — the legacy on_item_build(list) still fires.
        self._on_champ_detected = on_champ_detected
        self._on_build_detail = on_build_detail
        # () fired once each time we freshly enter champ select, so the UI can
        # clear last game's champ/matchup panels back to a "selecting" state.
        self._on_champ_select_enter = on_champ_select_enter
        # (partner_champ, partner_role, my_role, recs[]) fired once when your
        # botlane lane partner has LOCKED their champ and you have not locked
        # yet — recs is the best champions for you to pair with it. Read-only /
        # additive; ships inert (empty recs, no callback noise) when the data
        # bundle has no `duos` section yet.
        self._on_duo_recommendations = on_duo_recommendations
        # (hud_dict) fired ~once per second while a game is in progress with the
        # live in-game HUD snapshot (CS/min, lane deltas, gold estimate,
        # objective timers) derived from the local Live Client Data API. Purely
        # read-only/additive; ships inert when the Live Client endpoint is not
        # reachable (e.g. pre-loading screen, spectator gaps).
        self._on_hud = on_hud
        # Lazily-created Live Client Data client + light throttle so the HUD poll
        # only hits :2999 every few ticks, and only while actually in a game.
        self._live_client: Optional[LiveClientDataClient] = None
        self._hud_unavailable_logged = False
        # (recs_dict | None) fired during champ select whenever the set of picked
        # champions on either team changes, with a neutral draft/composition
        # analysis (damage balance, engage, CC) of CHAMPIONS only — never a
        # judgment of players. Read-only/additive; degrades to safe defaults for
        # champions missing from the curated attribute catalog.
        self._on_draft = on_draft
        # (recs_dict | None) fired ~once per second in-game with defensive item
        # suggestions derived from the enemy team's damage profile (from the same
        # Live Client poll as the HUD). About the user's own itemisation vs the
        # enemy comp — never a rating of players. Ships inert when the Live Client
        # endpoint is unreachable or the attribute catalog is missing.
        self._on_item_recs = on_item_recs
        self._draft_done_for: frozenset = frozenset()  # champ set we last analysed
        self._stop_event = threading.Event()
        # Serializes rune/item-set imports. The Reimport button (main.py) pushes
        # _import_runes on its own thread while the poll loop also calls it on
        # lane-swap / champ-detect; two threads driving the same LCUClient can
        # interleave a half-applied item set or duplicate rune page. RLock (not
        # Lock) so a same-thread re-entry can never self-deadlock.
        self._import_lock = threading.RLock()
        self._last_champ_id: Optional[int] = None
        self._last_phase: Optional[str] = None
        self._champ_name_map: dict = {}
        # Champ select state
        self._my_role: str = "auto"
        self._my_champ: str = ""
        # Matchup / counterpick state
        self._enemy_laner: str = ""          # confirmed enemy laner name
        self._enemy_laner_is_guess: bool = False  # True if set from guess, not confident assignment
        self._enemy_role_confirmed: bool = False  # True when Riot supplied the assigned position
        self._enemy_names_evaluated: frozenset = frozenset()  # picks that produced current assignment
        self._counters_done_for: str = ""    # enemy we already ran counters for
        self._matchup_done_for: str = ""     # "mychamp|enemy" we already ran matchup for
        self._waiting_logged: bool = False   # whether we've logged "waiting for laner"
        self._pick_turn_fired: bool = False  # whether we've fired counterpick for this turn
        self._matchup_override: Optional[str] = None
        self._all_picks_finalized: bool = False
        self._duo_done_for: str = ""         # partner champ we already ran duo recs for
        # Game state
        self._in_game: bool = False
        self._lcu_fail_count: int = 0
        self._role_refresh_done: bool = False

    def stop(self):
        self._stop_event.set()

    def set_matchup_override(self, enemy_name: str):
        """Called from the UI when the user manually specifies their lane opponent."""
        self._matchup_override = enemy_name.strip() if enemy_name.strip() else None
        if not self._matchup_override:
            return
        self._enemy_laner = self._matchup_override
        self._enemy_role_confirmed = True
        self.log(f"  → Matchup override: {self._matchup_override}", "info")
        if self._my_champ:
            # We already have our champ — run matchup directly
            mk = f"{self._my_champ}|{self._matchup_override}"
            if self._matchup_done_for != mk:
                self._matchup_done_for = mk
                threading.Thread(
                    target=self._run_matchup_lookup,
                    args=(self._my_champ, self._matchup_override, self._my_role),
                    daemon=True,
                ).start()
        else:
            # No champ yet — suggest counters
            if self._counters_done_for != self._matchup_override:
                self._counters_done_for = self._matchup_override
                threading.Thread(
                    target=self._run_counters_lookup,
                    args=(self._matchup_override, self._my_role),
                    daemon=True,
                ).start()

    def run(self):
        self.log("Waiting for champion select...", "info")
        try:
            self._champ_name_map = self.lcu.get_champion_name_map()
            self.log(f"Loaded {len(self._champ_name_map)} champion names.", "info")
        except Exception as e:
            self.log(f"Warning: could not load champ names ({e})", "warn")

        while not self._stop_event.is_set():
            try:
                self._tick()
            except LCUConnectionError as e:
                self._lcu_fail_count += 1
                if self._lcu_fail_count == 1:
                    self.log(f"LCU connection lost: {e}", "error")
                    self.log("Reconnecting...", "warn")
                time.sleep(5)
                try:
                    self.lcu.connect()
                    self.log("Reconnected.", "success")
                    self._lcu_fail_count = 0
                except Exception:
                    if self._lcu_fail_count >= 2:
                        self.log("League client closed — shutting down RuneSync.", "warn")
                        if self._on_league_closed:
                            self._on_league_closed()
                        return
            except Exception as e:
                self.log(f"Unexpected error: {e}", "error")
            time.sleep(self.POLL_INTERVAL)
        self.log("Monitor stopped.", "info")

    def _poll_hud(self):
        """Fetch the live HUD snapshot from the Live Client Data API and emit it.

        Best-effort and fully contained: the :2999 endpoint is unreachable for
        the first several seconds of loading and during brief spectator gaps, so
        any failure just skips this tick without disturbing the rest of the
        monitor loop. Never raises to the caller.
        """
        try:
            if self._live_client is None:
                self._live_client = LiveClientDataClient()
            data = self._live_client.get_all_game_data()
        except LiveClientDataError:
            # Expected while the game is still loading; stay quiet after once.
            if not self._hud_unavailable_logged:
                self._hud_unavailable_logged = True
            return
        except Exception:
            return

        if self._on_hud:
            try:
                hud = live_hud.build_hud(data, fallback_role=self._my_role)
                if hud:
                    self._on_hud(hud)
            except Exception:
                pass

        if self._on_item_recs:
            try:
                recs = item_recs.build_item_recs(data)
                if recs:
                    self._on_item_recs(recs)
            except Exception:
                pass

    def _tick(self):
        phase = self.lcu.get_game_flow_phase()

        # ── InProgress transition detection ───────────────────────────────────
        if phase in ACTIVE_GAME_PHASES and not self._in_game:
            self._in_game = True
            self.log("Game started!", "success")
            if self._on_game_start:
                self._on_game_start()
        elif phase in TERMINAL_GAME_PHASES and self._in_game:
            self._in_game = False
            self.log("Game ended.", "info")
            self._hud_unavailable_logged = False
            if self._on_game_end:
                self._on_game_end()

        # ── Live in-game HUD + item recommender (only while in a game) ────────
        if self._in_game and (self._on_hud or self._on_item_recs):
            self._poll_hud()

        if phase != "ChampSelect":
            if self._last_phase == "ChampSelect":
                self.log("Left champion select.", "info")
                self._last_champ_id = None
                self._enemy_laner = ""
                self._enemy_laner_is_guess = False
                self._enemy_role_confirmed = False
                self._enemy_names_evaluated = frozenset()
                self._counters_done_for = ""
                self._matchup_done_for = ""
                self._waiting_logged = False
                self._pick_turn_fired = False
                self._matchup_override = None
                self._all_picks_finalized = False
                self._duo_done_for = ""
                self._my_champ = ""
                self._draft_done_for = frozenset()
                self._clear_draft()
            self._last_phase = phase
            return
        if self._last_phase != "ChampSelect":
            self.log("Entered champion select!", "success")
            self._last_champ_id = None
            self._enemy_laner = ""
            self._enemy_laner_is_guess = False
            self._enemy_role_confirmed = False
            self._enemy_names_evaluated = frozenset()
            self._counters_done_for = ""
            self._matchup_done_for = ""
            self._waiting_logged = False
            self._pick_turn_fired = False
            self._matchup_override = None
            self._all_picks_finalized = False
            self._duo_done_for = ""
            self._draft_done_for = frozenset()
            self._clear_draft()
            if self._on_champ_select_enter:
                self._on_champ_select_enter()
        self._last_phase = phase

        session = self.lcu.get_champ_select_session()
        if not session:
            return

        # ── Role detection (runs every tick, even before champ is picked) ───
        detected_role = self._detect_role(session)
        if detected_role != "auto":
            if self._my_role == "auto":
                self._my_role = detected_role  # initial lock-in
                # A champ picked before assignedPosition was available imported
                # under the placeholder "auto" role, which falls back to the
                # champ's highest-pickrate role (e.g. Sejuani -> jungle). Now the
                # real lane is known, re-import so a top laner doesn't keep a
                # jungle rune page / Smite.
                if self._my_champ:
                    self.log(f"  → Role resolved: {detected_role} — re-importing", "info")
                    if self._on_champ_detected:
                        self._on_champ_detected(self._my_champ, detected_role)
                    self._import_runes(self._my_champ, session)
            elif detected_role != self._my_role:
                # assignedPosition changed — teammate lane swap in the draft UI
                old_role = self._my_role
                self._my_role = detected_role
                self.log(f"  → Lane swap: {old_role} → {detected_role}", "warn")
                # Reset matchup state so it re-runs for the new lane
                self._enemy_laner = ""
                self._enemy_laner_is_guess = False
                self._enemy_role_confirmed = False
                self._enemy_names_evaluated = frozenset()
                self._counters_done_for = ""
                self._matchup_done_for = ""
                self._waiting_logged = False
                if self._my_champ:
                    if self._on_champ_detected:
                        self._on_champ_detected(self._my_champ, detected_role)
                    self._import_runes(self._my_champ, session)

        # ── My champion detection ──────────────────────────────────────────
        champ_id = self._get_my_champ_id(session)
        if champ_id and champ_id != self._last_champ_id:
            if self.trigger == "lock" and not self._is_locked(session, champ_id):
                pass
            else:
                self._last_champ_id = champ_id
                self._my_champ = self._champ_name_map.get(champ_id, f"Champion#{champ_id}")
                self._my_role = self._detect_role(session)
                self.log(f"Champion detected: {self._my_champ}", "champ")
                if self._on_champ_detected:
                    self._on_champ_detected(self._my_champ, self._my_role)
                self._import_runes(self._my_champ, session)
                # If we already know the enemy laner, run matchup now
                if self._enemy_laner:
                    mk = f"{self._my_champ}|{self._enemy_laner}"
                    if self._matchup_done_for != mk:
                        self._matchup_done_for = mk
                        threading.Thread(
                            target=self._run_matchup_lookup,
                            args=(self._my_champ, self._enemy_laner, self._my_role),
                            daemon=True,
                        ).start()

        # ── Enemy laner state machine (runs every tick, always) ────────────
        # When all 5 enemy picks are locked in, force a fresh assignment regardless
        # of cache — ensures the final pick always overrides any earlier guess.
        if not self._all_picks_finalized and self._all_enemy_picks_complete(session):
            self._all_picks_finalized = True
            self._enemy_names_evaluated = frozenset()
        self._update_enemy_laner(session)

        # ── Draft/composition analysis (inline; fires on any pick change) ─────
        self._maybe_emit_draft(session)

        # ── Botlane duo recommendation ─────────────────────────────────────
        # When your botlane lane partner has locked their champ and you have not
        # locked yet, recommend the best champs for YOU to pair with it. Fires
        # once per locked partner champ. Silent/inert until the bundle carries
        # duo data (get_best_partners returns []).
        if self._my_role in ("bot", "support") and not self._my_pick_completed(session):
            partner_champ, partner_role = self._get_locked_botlane_partner(session)
            if partner_champ and self._duo_done_for != partner_champ:
                self._duo_done_for = partner_champ
                threading.Thread(
                    target=self._run_duo_lookup,
                    args=(partner_champ, partner_role, self._my_role),
                    daemon=True,
                ).start()

        # ── It's your pick turn and you haven't locked in yet ──────────────
        # Fire once per turn: show counterpick suggestions if enemy laner is
        # known, or log that we're still waiting if they haven't picked yet.
        if not self._my_champ and self._is_my_pick_turn(session):
            if not self._pick_turn_fired:
                self._pick_turn_fired = True
                role_label = self._my_role.title() if self._my_role not in ("auto", "") else "lane"
                if self._enemy_laner:
                    self.log(f"── It's your pick turn ──", "warn")
                    self.log(f"  ⚔  Enemy {role_label} laner: {self._enemy_laner}", "champ")
                    if self._counters_done_for != self._enemy_laner:
                        self._counters_done_for = self._enemy_laner
                        threading.Thread(
                            target=self._run_counters_lookup,
                            args=(self._enemy_laner, self._my_role),
                            daemon=True,
                        ).start()
                else:
                    self.log(f"── It's your pick turn ──", "warn")
                    self.log(f"  → Enemy {role_label} laner not picked yet — no counterpick data.", "info")
        elif not self._is_my_pick_turn(session):
            # Turn ended (they picked or it moved on) — reset so next turn fires fresh
            self._pick_turn_fired = False

    def _update_enemy_laner(self, session: dict):
        """
        State machine that runs every tick during champ select.

        States:
          1. Enemy laner already confirmed → nothing left to do here.
          2. Manual override set → already handled in set_matchup_override.
          3. Check current enemy picks via infer_full_assignment:
             a. Our role IS assigned → confirmed laner found.
                - No my_champ yet → suggest counterpicks (counters lookup).
                - Have my_champ → run matchup lookup.
             b. Our role is NOT assigned yet (e.g. only Lux/Jinx/Zed picked,
                we're top) → log "Waiting for enemy top laner..." once.
        """
        # State 2: a manual override is active — the laner the user typed wins
        # until they clear it or champ select resets (both null _matchup_override
        # at lines 61/144/159). Without this guard the next enemy pick change
        # re-runs inference and silently clobbers the override, defeating the
        # "type a different name to override" feature.
        if self._matchup_override:
            return

        enemy_names = self._get_enemy_champ_names(session)
        if not enemy_names:
            return

        # If we don't know our role yet, we can't assign a specific laner
        if self._my_role in ("auto", ""):
            return

        current_names = frozenset(enemy_names)
        assigned_laner = None
        if (not self._enemy_role_confirmed
                or current_names != self._enemy_names_evaluated):
            assigned_laner = self._get_assigned_enemy_laner(session, current_names)

        # Don't re-run unchanged statistical inference. Assigned-position lookup
        # is attempted first because gameflow can fill in after the picks do.
        if current_names == self._enemy_names_evaluated and not assigned_laner:
            return

        detected = assigned_laner
        is_guess = False

        if not detected:
            assignment, guesses = infer_full_assignment(enemy_names)
            detected = assignment.get(self._my_role)
            if not detected:
                # Use guesses immediately rather than waiting for all 5 picks.
                # Guesses are shown with a warning already; no info is worse than
                # an uncertain guess (e.g. Brand 18% mid when only Briar + Brand visible).
                detected = guesses.get(self._my_role)
                is_guess = detected is not None

        if not detected:
            # No enemy laner assigned to our role yet
            self._enemy_names_evaluated = current_names
            if not self._waiting_logged:
                self._waiting_logged = True
                role_label = self._my_role.title() if self._my_role != "auto" else "lane"
                self.log(f"  → Waiting for enemy {role_label} laner...", "info")
            return

        # Laner found — record it (guess or confident)
        self._enemy_laner = detected
        self._enemy_laner_is_guess = is_guess
        self._enemy_role_confirmed = assigned_laner is not None
        self._enemy_names_evaluated = current_names
        self._waiting_logged = False
        role_label = self._my_role.title() if self._my_role != "auto" else "lane"
        if is_guess:
            self.log(f"  ⚠  Best guess — {detected} may be enemy {role_label} laner (flex pick, could be wrong)", "warn")
        else:
            self.log(f"  ⚔  Enemy {self._my_role} laner: {detected}", "champ")

        if self._my_champ:
            # We have our champ → matchup lookup
            mk = f"{self._my_champ}|{detected}"
            if self._matchup_done_for != mk:
                self._matchup_done_for = mk
                threading.Thread(
                    target=self._run_matchup_lookup,
                    args=(self._my_champ, detected, self._my_role),
                    daemon=True,
                ).start()
        else:
            # No champ yet → suggest counters
            if self._counters_done_for != detected:
                self._counters_done_for = detected
                threading.Thread(
                    target=self._run_counters_lookup,
                    args=(detected, self._my_role),
                    daemon=True,
                ).start()

    def _get_assigned_enemy_laner(
            self, session: dict, enemy_names: frozenset[str]) -> Optional[str]:
        """Prefer Riot's assigned position over statistical role inference."""
        for player in session.get("theirTeam", []):
            position = (player.get("assignedPosition") or "").lower()
            if _LCU_POSITION_TO_ROLE.get(position) != self._my_role:
                continue
            champion_id = player.get("championId", 0)
            name = self._champ_name_map.get(champion_id, "")
            if name in enemy_names:
                return name

        try:
            champion_id = self.lcu.get_enemy_champion_id_for_role(self._my_role)
        except Exception:
            champion_id = None
        name = self._champ_name_map.get(champion_id, "")
        return name if name in enemy_names else None

    def _get_ally_champ_names(self, session: dict) -> list[str]:
        """Return names of all ally champions locked in (completed picks).

        Mirrors _get_enemy_champ_names for the myTeam cells so the draft
        recommender can analyse both compositions as they fill in.
        """
        names = []
        my_team_cells = {p.get("cellId") for p in session.get("myTeam", [])}
        for action_group in session.get("actions", []):
            for action in action_group:
                if action.get("type") != "pick":
                    continue
                if not action.get("completed", False):
                    continue
                cid = action.get("championId", 0)
                if cid <= 0:
                    continue
                cell = action.get("actorCellId", -1)
                if cell in my_team_cells:
                    name = self._champ_name_map.get(cid, "")
                    if name and name not in names:
                        names.append(name)
        return names

    def _clear_draft(self):
        """Push an empty draft so the UI hides last selection's analysis.

        Fired on champ-select enter and leave (e.g. after a dodge) so stale
        observations never linger into the next lobby or the loading screen.
        """
        if not self._on_draft:
            return
        try:
            self._on_draft(None)
        except Exception:
            pass

    def _maybe_emit_draft(self, session: dict):
        """Compute + emit a draft/composition analysis when the picks change.

        Cheap pure logic (no network), so it runs inline each champ-select tick
        but only fires the callback when the combined champ set actually changes.
        """
        if not self._on_draft:
            return
        ally = self._get_ally_champ_names(session)
        # Include our own hovered/picked champ even before it's a completed pick.
        if self._my_champ and self._my_champ not in ally:
            ally = ally + [self._my_champ]
        enemy = self._get_enemy_champ_names(session)
        key = frozenset(ally) | frozenset("~" + e for e in enemy)
        if key == self._draft_done_for:
            return
        self._draft_done_for = key
        try:
            recs = draft_recs.build_draft_recs(ally, enemy)
        except Exception:
            return
        try:
            self._on_draft(recs)
        except Exception:
            pass

    def _get_enemy_champ_names(self, session: dict) -> list[str]:
        """Return names of all enemy champions that have been locked in (completed=True)."""
        names = []
        my_team_cells = {p.get("cellId") for p in session.get("myTeam", [])}
        for action_group in session.get("actions", []):
            for action in action_group:
                if action.get("type") != "pick":
                    continue
                if not action.get("completed", False):
                    continue
                cid = action.get("championId", 0)
                if cid <= 0:
                    continue
                cell = action.get("actorCellId", -1)
                if cell not in my_team_cells:
                    name = self._champ_name_map.get(cid, "")
                    if name and name not in names:
                        names.append(name)
        return names

    def _all_enemy_picks_complete(self, session: dict) -> bool:
        """Return True when all 5 enemy picks are locked in (completed=True)."""
        my_team_cells = {p.get("cellId") for p in session.get("myTeam", [])}
        completed = 0
        for action_group in session.get("actions", []):
            for action in action_group:
                if action.get("type") != "pick":
                    continue
                cell = action.get("actorCellId", -1)
                if cell not in my_team_cells and action.get("completed", False):
                    completed += 1
        return completed >= 5

    def _run_counters_lookup(self, enemy_champ: str, role: str):
        """Background thread: scrape u.gg counters for enemy_champ and emit suggestions."""
        self.log(f"  → Fetching counterpick suggestions vs {enemy_champ}...", "info")
        try:
            counters = self.ugg.get_counters(enemy_champ, role=role, top_n=5)
            if counters is None or len(counters) == 0:
                self.log(f"  ⚠  No counter data for {enemy_champ} — data bundle may be incomplete", "warn")
                return
            self.log(f"  ✓  Top counters vs {enemy_champ}:", "success")
            for i, c in enumerate(counters, 1):
                self.log(f"     {i}. {c['champion']}  ({c['win_rate']:.1f}% WR)", "success")
        except Exception as e:
            self.log(f"  ⚠  Counters lookup failed: {e}", "warn")

    def _my_pick_completed(self, session: dict) -> bool:
        """True once our own pick action is locked in (completed)."""
        my_cell = session.get("localPlayerCellId", -1)
        for action_group in session.get("actions", []):
            for action in action_group:
                if (action.get("actorCellId") == my_cell
                        and action.get("type") == "pick"
                        and action.get("completed", False)):
                    return True
        return False

    def _get_locked_botlane_partner(self, session: dict):
        """Return (partner_champ_name, partner_role) if our botlane lane partner
        has locked their champion, else (None, None).

        Partner = the teammate assigned to the complementary botlane role
        (bot<->support). "Locked" means their pick action is completed; a mere
        hover (myTeam.championId set with no completed action) does not count.
        """
        if self._my_role not in ("bot", "support"):
            return None, None
        partner_role = "support" if self._my_role == "bot" else "bot"
        partner_cell = None
        for p in session.get("myTeam", []):
            pos = (p.get("assignedPosition") or "").lower()
            if _LCU_POSITION_TO_ROLE.get(pos) == partner_role:
                partner_cell = p.get("cellId")
                break
        if partner_cell is None:
            return None, None
        for action_group in session.get("actions", []):
            for action in action_group:
                if (action.get("actorCellId") == partner_cell
                        and action.get("type") == "pick"
                        and action.get("completed", False)
                        and action.get("championId", 0) > 0):
                    name = self._champ_name_map.get(action.get("championId"), "")
                    if name:
                        return name, partner_role
        return None, None

    def _run_duo_lookup(self, partner_champ: str, partner_role: str, my_role: str):
        """Background thread: find the best champs to pair with a locked partner."""
        try:
            recs = self.ugg.get_best_partners(partner_champ, partner_role, top_n=5)
        except Exception as e:
            self.log(f"  ⚠ Duo lookup failed: {e}", "warn")
            return
        if not recs:
            # Bundle has no duo data yet — stay inert (no log spam), but still
            # notify the UI so any stale panel clears.
            if self._on_duo_recommendations:
                self._on_duo_recommendations(partner_champ, partner_role, my_role, [])
            return
        role_label = my_role.title()
        self.log(f"  ✓  Best {role_label} pairs with locked {partner_champ}:", "success")
        for i, r in enumerate(recs, 1):
            self.log(f"     {i}. {r['champion']}  ({r['win_rate']:.1f}% WR — {r['tier_label']})",
                     "success")
        if self._on_duo_recommendations:
            self._on_duo_recommendations(partner_champ, partner_role, my_role, recs)

    def _run_matchup_lookup(self, my_champ: str, enemy_champ: str, role: str):
        """Background thread: scrape u.gg matchup data and emit to log."""
        self.log(f"  → Looking up matchup vs {enemy_champ}...", "info")
        self.log(f"     (Type a different name in the matchup box to override)", "info")
        try:
            result = self.ugg.get_matchup_winrate(my_champ, enemy_champ, role)
            if result is None:
                self.log(f"     No win-rate data for this matchup.", "info")
                if self._on_matchup_winrate:
                    self._on_matchup_winrate(
                        my_champ, enemy_champ, role, None,
                        "Win rate unavailable", "info",
                    )
            else:
                wr = result["win_rate"]
                if wr >= 52:
                    label = "Favored ✓"
                    tag = "success"
                elif wr >= 50:
                    label = "Slightly Favored"
                    tag = "success"
                elif wr >= 48:
                    label = "Even"
                    tag = "info"
                elif wr >= 46:
                    label = "Slightly Unfavored"
                    tag = "warn"
                else:
                    label = "Unfavored ✗"
                    tag = "error"
                self.log(f"  ⚔  vs {enemy_champ}: {wr:.1f}% WR — {label}", tag)
                if self._on_matchup_winrate:
                    self._on_matchup_winrate(my_champ, enemy_champ, role, wr, label, tag)
        except Exception as e:
            self.log(f"  ⚠ Matchup lookup failed: {e}", "warn")

    def _get_my_champ_id(self, session: dict) -> Optional[int]:
        my_cell = session.get("localPlayerCellId", -1)
        for action_group in session.get("actions", []):
            for action in action_group:
                if action.get("actorCellId") == my_cell and action.get("type") == "pick":
                    cid = action.get("championId", 0)
                    return cid if cid > 0 else None
        return None

    def _is_my_pick_turn(self, session: dict) -> bool:
        """Return True if it is currently our turn to pick (action in progress, not completed)."""
        my_cell = session.get("localPlayerCellId", -1)
        for action_group in session.get("actions", []):
            for action in action_group:
                if (action.get("actorCellId") == my_cell
                        and action.get("type") == "pick"
                        and action.get("isInProgress", False)
                        and not action.get("completed", False)):
                    return True
        return False

    def _is_locked(self, session: dict, champ_id: int) -> bool:
        my_cell = session.get("localPlayerCellId", -1)
        for action_group in session.get("actions", []):
            for action in action_group:
                if (action.get("actorCellId") == my_cell and
                        action.get("type") == "pick" and
                        action.get("championId") == champ_id and
                        action.get("completed", False)):
                    return True
        return False

    def _fix_offrole_summoners(self, summoners: list, role: str) -> list:
        """Strip a jungle Smite from an off-role fallback build's summoners.

        When the bundle has no data for the player's lane and falls back to a
        jungle build, the jungle Smite is nonsense in lane. Swap it for the
        lane's standard second summoner (Flash + TP/Ignite/Heal/Exhaust),
        leaving Flash and any non-Smite spell untouched.
        """
        if role in ("", "auto", "jungle") or _SMITE_SPELL_ID not in summoners:
            return summoners
        repl = _LANE_SECOND_SPELL.get(role, 4)
        fixed = [repl if s == _SMITE_SPELL_ID else s for s in summoners]
        self.log(f"  → Dropped jungle Smite for {role} (set spell {repl})", "info")
        return fixed

    def _detect_role(self, session: dict) -> str:
        my_cell = session.get("localPlayerCellId", -1)
        for p in session.get("myTeam", []):
            if p.get("cellId") == my_cell:
                pos = p.get("assignedPosition", "").lower()
                return {"top": "top", "jungle": "jungle", "middle": "mid",
                        "bottom": "bot", "utility": "support"}.get(pos, "auto")
        return "auto"

    def _import_runes(self, champ_name: str, session: dict):
        # Serialize: if another thread (Reimport button vs poll loop) is already
        # mid-import, wait for it rather than racing two rune/item-set pushes
        # through the same LCUClient. Non-blocking probe first so we can log the
        # contention; the real acquire below still blocks until it's our turn.
        if not self._import_lock.acquire(blocking=False):
            self.log(f"  → Import already in progress — queuing {champ_name}...", "info")
            self._import_lock.acquire()
        try:
            override = self.overrides.get(champ_name)
            if override:
                self.log(f"  → Using YOUR custom build for {champ_name}", "success")
                self._apply_override(champ_name, override)
            else:
                self.log(f"  → Fetching u.gg top build for {champ_name}...", "info")
                self._apply_ugg(champ_name, session)
        finally:
            self._import_lock.release()

    def _report_rune_page(self, primary_id, secondary_id, perk_ids, spell1=0, spell2=0):
        """Fire on_runes_imported with display-ready rune-page info (names, not IDs).
        Best-effort and read-only — a failure here must never break an import."""
        if not self._on_runes_imported:
            return
        try:
            id_to_tree = {v: k for k, v in RUNE_TREE_IDS.items()}
            id_to_ks = {v: k for k, v in KEYSTONE_IDS.items()}
            self._on_runes_imported({
                "keystone":  id_to_ks.get(perk_ids[0], "") if perk_ids else "",
                "primary":   id_to_tree.get(primary_id, ""),
                "secondary": id_to_tree.get(secondary_id, ""),
                "spell1": spell1 or 0, "spell2": spell2 or 0,
                "perk_ids": list(perk_ids or []),
            })
        except Exception:
            pass

    def _apply_ugg(self, champ_name: str, session: dict):
        # Prefer the resolved lane (self._my_role) over a fresh detect: the role
        # state machine in run() accounts for assignedPosition arriving late, so
        # re-detecting here could momentarily read "auto" and pull the champ's
        # off-role highest-pickrate build.
        if self.auto_role:
            role = self._my_role if self._my_role not in ("", "auto") \
                else self._detect_role(session)
        else:
            role = "auto"
        if role != "auto":
            self.log(f"  → Detected role: {role}", "info")
        try:
            build = self.ugg.get_top_build(champ_name, role=role,
                                           rank=self.rank, region=self.region)
        except Exception as e:
            self.log(f"  ✗ Could not fetch u.gg build: {e}", "error")
            return
        # The bundle may have no data for the requested lane (e.g. off-meta
        # Sejuani top) and fall back to another role's build. Flag it so the user
        # knows, and strip a jungle Smite that's nonsense in a lane.
        build_role = build.get("role", role)
        off_role = role not in ("", "auto") and build_role != role
        if off_role:
            self.log(f"  ⚠ No {role} data for {champ_name} — using {build_role} "
                     f"build as a fallback", "warn")
        summoners = list(build.get("summoners", []) or [])
        if off_role:
            summoners = self._fix_offrole_summoners(summoners, role)
        self.log(f"  → {build['role']} | perks: {build['selected_perk_ids'][:4]}...", "info")
        if build.get("items_core"):
            self.log(f"  → Core items: {build['items_core']}", "info")
            if self._on_item_build:
                self._on_item_build(build["items_core"], False)
            if self._on_build_detail:
                self._on_build_detail(build, False)
        ok = self.lcu.import_rune_page(champ_name, build["primary_style_id"],
                                       build["sub_style_id"], build["selected_perk_ids"])
        if ok:
            self.log(f"  ✓ Runes imported for {champ_name} ({build['role']})", "success")
            if self._on_import:
                self._on_import(champ_name)
            self._report_rune_page(build["primary_style_id"], build["sub_style_id"],
                                   build["selected_perk_ids"],
                                   summoners[0] if len(summoners) >= 1 else 0,
                                   summoners[1] if len(summoners) >= 2 else 0)
            if build.get("items_core_ids"):
                champ_id = next((k for k, v in self._champ_name_map.items() if v == champ_name), 0)
                set_ok = self.lcu.import_item_set(champ_name, champ_id, role,
                                                  build.get("items_start_ids", []),
                                                  build["items_core_ids"],
                                                  build.get("items_fourth_ids"),
                                                  build.get("items_fifth_ids"),
                                                  build.get("items_sixth_ids"))
                if set_ok:
                    self.log(f"  ✓ Item set imported for {champ_name}", "success")
            if len(summoners) >= 2:
                spell_ok = self.lcu.set_summoner_spells(summoners[0], summoners[1])
                if spell_ok:
                    self.log(f"  ✓ Summoner spells set ({summoners[0]}, {summoners[1]})", "success")
                else:
                    self.log(f"  ✗ Failed to set summoner spells", "warn")
            else:
                self.log(f"  ⚠ No summoner spells found on u.gg page", "warn")
        else:
            self.log(f"  ✗ Failed to push rune page to client", "error")
        # Brave is running now — check if role weights need a patch update
        if not self._role_refresh_done:
            self._role_refresh_done = True
            threading.Thread(target=self._maybe_refresh_roles, daemon=True).start()

    def _maybe_refresh_roles(self):
        """Background thread: refresh role weights if patch changed (Brave must be running)."""
        try:
            from champion_roles import needs_role_refresh, reload_weights
            from role_updater import refresh_roles_now
            if needs_role_refresh():
                self.log("  → New patch detected — updating role weights...", "info")
                ok = refresh_roles_now()
                if ok:
                    # Pull the freshly-written cache into the in-memory weights;
                    # otherwise inference keeps using the import-time table and
                    # the success message below would be a lie.
                    reload_weights()
                    self.log("  ✓ Role weights updated for new patch", "success")
                else:
                    self.log("  ⚠ Role weight update failed (server unreachable)", "warn")
        except Exception as e:
            self.log(f"  ⚠ Role weight check failed: {e}", "warn")

    def _apply_override(self, champ_name: str, override: dict):
        primary_tree = override.get("primary_tree", "Precision")
        secondary_tree = override.get("secondary_tree", "Domination")
        primary_id = RUNE_TREE_IDS.get(primary_tree, 8000)
        secondary_id = RUNE_TREE_IDS.get(secondary_tree, 8100)
        rune_ids = override.get("rune_ids", [])

        if len(rune_ids) >= 9:
            perk_ids = rune_ids[:9]
        else:
            self.log(f"  → Fetching u.gg base then applying your keystone...", "info")
            try:
                build = self.ugg.get_top_build(champ_name,
                    role=override.get("role", "auto"), rank="Platinum+", region="World")
                perk_ids = list(build["selected_perk_ids"])
                primary_id = RUNE_TREE_IDS.get(primary_tree, build["primary_style_id"])
                secondary_id = RUNE_TREE_IDS.get(secondary_tree, build["sub_style_id"])
                keystone_name = override.get("keystone", "")
                if keystone_name and keystone_name in KEYSTONE_IDS:
                    perk_ids[0] = KEYSTONE_IDS[keystone_name]
                    self.log(f"  → Keystone overridden: {keystone_name}", "info")
            except Exception as e:
                self.log(f"  ✗ Could not fetch base build: {e}", "error")
                return

        ok = self.lcu.import_rune_page(override.get("page_name") or f"{champ_name} (custom)",
                                       primary_id, secondary_id, perk_ids)
        if ok:
            self.log(f"  ✓ Custom runes imported for {champ_name}", "success")
            if self._on_import:
                self._on_import(champ_name)
            self._report_rune_page(primary_id, secondary_id, perk_ids,
                                   override.get("spell1", 0), override.get("spell2", 0))
            if override.get("note"):
                self.log(f"     Note: {override['note']}", "info")
            spell1 = override.get("spell1", 0)
            spell2 = override.get("spell2", 0)
            if spell1 and spell2:
                # Both spells explicitly set — use them directly, no u.gg fetch needed
                spell_ok = self.lcu.set_summoner_spells(spell1, spell2)
                if spell_ok:
                    self.log(f"  ✓ Summoner spells set (custom: {spell1}, {spell2})", "success")
                else:
                    self.log(f"  ✗ Failed to set summoner spells", "warn")
            else:
                # Fall back to u.gg for spells
                try:
                    spell_build = self.ugg.get_top_build(champ_name,
                        role=override.get("role", "auto"), rank="Platinum+", region="World")
                    summoners = spell_build.get("summoners", [])
                    # If one spell is set, override just that slot
                    if spell1 and len(summoners) >= 2:
                        summoners[0] = spell1
                    if spell2 and len(summoners) >= 2:
                        summoners[1] = spell2
                    if len(summoners) >= 2:
                        spell_ok = self.lcu.set_summoner_spells(summoners[0], summoners[1])
                        if spell_ok:
                            self.log(f"  ✓ Summoner spells set ({summoners[0]}, {summoners[1]})", "success")
                        else:
                            self.log(f"  ✗ Failed to set summoner spells", "warn")
                    else:
                        self.log(f"  ⚠ No summoner spells found on u.gg page", "warn")
                except Exception as e:
                    self.log(f"  ⚠ Could not fetch summoner spells: {e}", "warn")
        else:
            self.log(f"  ✗ Failed to push rune page", "error")

        # ── Item build ─────────────────────────────────────────────────────────
        custom_build = override.get("items_build", {})
        has_custom = bool(custom_build)

        if has_custom and isinstance(custom_build, list):
            # Legacy flat-list format (display only)
            if self._on_item_build:
                self._on_item_build(custom_build, True)

        elif has_custom and isinstance(custom_build, dict):
            # Structured format from item_builder
            starter = custom_build.get("starter", [])
            core    = custom_build.get("core",    [])
            fourth  = custom_build.get("fourth",  [])
            fifth   = custom_build.get("fifth",   [])
            sixth   = custom_build.get("sixth",   [])
            display = [i["name"] for i in core] if core else [i["name"] for i in starter]
            if display and self._on_item_build:
                self._on_item_build(display, True)
            if core:
                item_role = override.get("role", "auto")
                champ_id  = next((k for k, v in self._champ_name_map.items() if v == champ_name), 0)
                try:
                    set_ok = self.lcu.import_item_set(
                        champ_name, champ_id, item_role,
                        [i["id"] for i in starter],
                        [i["id"] for i in core],
                        [i["id"] for i in fourth] or None,
                        [i["id"] for i in fifth]  or None,
                        [i["id"] for i in sixth]  or None,
                    )
                    if set_ok:
                        self.log(f"  ✓ Custom item set imported for {champ_name}", "success")
                except Exception:
                    pass

        else:
            try:
                item_build = self.ugg.get_top_build(
                    champ_name, role=override.get("role", "auto"),
                    rank="Platinum+", region="World")
                if item_build and item_build.get("items_core"):
                    if self._on_item_build:
                        self._on_item_build(item_build["items_core"], False)
                    if item_build.get("items_core_ids"):
                        item_role = override.get("role", "auto")
                        champ_id = next((k for k, v in self._champ_name_map.items() if v == champ_name), 0)
                        set_ok = self.lcu.import_item_set(champ_name, champ_id, item_role,
                                                          item_build.get("items_start_ids", []),
                                                          item_build["items_core_ids"],
                                                          item_build.get("items_fourth_ids"),
                                                          item_build.get("items_fifth_ids"),
                                                          item_build.get("items_sixth_ids"))
                        if set_ok:
                            self.log(f"  ✓ Item set imported for {champ_name}", "success")
            except Exception:
                pass

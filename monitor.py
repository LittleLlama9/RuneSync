"""
ChampSelectMonitor — polls the LCU for champ select events and imports runes.
"""

import time, threading
from typing import Callable, Optional
from lcu import LCUClient, LCUConnectionError, RUNE_TREE_IDS, KEYSTONE_IDS
from ugg_api import UGGClient
from overrides import OverrideManager
from champion_roles import infer_roles, infer_full_assignment


class ChampSelectMonitor:
    POLL_INTERVAL = 1.0

    def __init__(self, lcu: LCUClient, ugg: UGGClient, overrides: OverrideManager,
                 on_log: Callable, trigger: str = "hover", rank: str = "Platinum+",
                 region: str = "World", auto_role: bool = True,
                 on_game_start=None, on_game_end=None, on_league_closed=None,
                 on_matchup_winrate=None, on_item_build=None):
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
        self._stop_event = threading.Event()
        self._last_champ_id: Optional[int] = None
        self._last_phase: Optional[str] = None
        self._champ_name_map: dict = {}
        # Champ select state
        self._my_role: str = "auto"
        self._my_champ: str = ""
        # Matchup / counterpick state
        self._enemy_laner: str = ""          # confirmed enemy laner name
        self._enemy_laner_is_guess: bool = False  # True if set from guess, not confident assignment
        self._enemy_names_evaluated: frozenset = frozenset()  # picks that produced current assignment
        self._counters_done_for: str = ""    # enemy we already ran counters for
        self._matchup_done_for: str = ""     # "mychamp|enemy" we already ran matchup for
        self._waiting_logged: bool = False   # whether we've logged "waiting for laner"
        self._pick_turn_fired: bool = False  # whether we've fired counterpick for this turn
        self._matchup_override: Optional[str] = None
        self._all_picks_finalized: bool = False
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

    def _tick(self):
        phase = self.lcu.get_game_flow_phase()

        # ── InProgress transition detection ───────────────────────────────────
        if phase == "InProgress" and not self._in_game:
            self._in_game = True
            self.log("Game started!", "success")
            if self._on_game_start:
                self._on_game_start()
        elif phase != "InProgress" and self._in_game:
            self._in_game = False
            self.log("Game ended.", "info")
            if self._on_game_end:
                self._on_game_end()

        if phase != "ChampSelect":
            if self._last_phase == "ChampSelect":
                self.log("Left champion select.", "info")
                self._last_champ_id = None
                self._enemy_laner = ""
                self._enemy_laner_is_guess = False
                self._enemy_names_evaluated = frozenset()
                self._counters_done_for = ""
                self._matchup_done_for = ""
                self._waiting_logged = False
                self._pick_turn_fired = False
                self._matchup_override = None
                self._all_picks_finalized = False
                self._my_champ = ""
            self._last_phase = phase
            return
        if self._last_phase != "ChampSelect":
            self.log("Entered champion select!", "success")
            self._last_champ_id = None
            self._enemy_laner = ""
            self._enemy_laner_is_guess = False
            self._enemy_names_evaluated = frozenset()
            self._counters_done_for = ""
            self._matchup_done_for = ""
            self._waiting_logged = False
            self._pick_turn_fired = False
            self._matchup_override = None
            self._all_picks_finalized = False
        self._last_phase = phase

        session = self.lcu.get_champ_select_session()
        if not session:
            return

        # ── Role detection (runs every tick, even before champ is picked) ───
        detected_role = self._detect_role(session)
        if detected_role != "auto":
            if self._my_role == "auto":
                self._my_role = detected_role  # initial lock-in
            elif detected_role != self._my_role:
                # assignedPosition changed — teammate lane swap in the draft UI
                old_role = self._my_role
                self._my_role = detected_role
                self.log(f"  → Lane swap: {old_role} → {detected_role}", "warn")
                # Reset matchup state so it re-runs for the new lane
                self._enemy_laner = ""
                self._enemy_laner_is_guess = False
                self._enemy_names_evaluated = frozenset()
                self._counters_done_for = ""
                self._matchup_done_for = ""
                self._waiting_logged = False
                if self._my_champ:
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

        # Don't re-run if the enemy pick set hasn't changed since last evaluation
        current_names = frozenset(enemy_names)
        if current_names == self._enemy_names_evaluated:
            return

        assignment, guesses = infer_full_assignment(enemy_names)
        detected = assignment.get(self._my_role)
        is_guess = False

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

    def _run_matchup_lookup(self, my_champ: str, enemy_champ: str, role: str):
        """Background thread: scrape u.gg matchup data and emit to log."""
        self.log(f"  → Looking up matchup vs {enemy_champ}...", "info")
        self.log(f"     (Type a different name in the matchup box to override)", "info")
        try:
            result = self.ugg.get_matchup_winrate(my_champ, enemy_champ, role)
            if result is None:
                # Common case in bundle mode — the bundle only carries WRs for
                # the top counters per (champ, role), not every pair. Stay quiet.
                self.log(f"     No win-rate data for this matchup.", "info")
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

    def _detect_role(self, session: dict) -> str:
        my_cell = session.get("localPlayerCellId", -1)
        for p in session.get("myTeam", []):
            if p.get("cellId") == my_cell:
                pos = p.get("assignedPosition", "").lower()
                return {"top": "top", "jungle": "jungle", "middle": "mid",
                        "bottom": "bot", "utility": "support"}.get(pos, "auto")
        return "auto"

    def _import_runes(self, champ_name: str, session: dict):
        override = self.overrides.get(champ_name)
        if override:
            self.log(f"  → Using YOUR custom build for {champ_name}", "success")
            self._apply_override(champ_name, override)
        else:
            self.log(f"  → Fetching u.gg top build for {champ_name}...", "info")
            self._apply_ugg(champ_name, session)

    def _apply_ugg(self, champ_name: str, session: dict):
        role = self._detect_role(session) if self.auto_role else "auto"
        if role != "auto":
            self.log(f"  → Detected role: {role}", "info")
        try:
            build = self.ugg.get_top_build(champ_name, role=role,
                                           rank=self.rank, region=self.region)
        except Exception as e:
            self.log(f"  ✗ Could not fetch u.gg build: {e}", "error")
            return
        self.log(f"  → {build['role']} | perks: {build['selected_perk_ids'][:4]}...", "info")
        if build.get("items_core"):
            self.log(f"  → Core items: {build['items_core']}", "info")
            if self._on_item_build:
                self._on_item_build(build["items_core"], False)
        ok = self.lcu.import_rune_page(champ_name, build["primary_style_id"],
                                       build["sub_style_id"], build["selected_perk_ids"])
        if ok:
            self.log(f"  ✓ Runes imported for {champ_name} ({build['role']})", "success")
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
            summoners = build.get("summoners", [])
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

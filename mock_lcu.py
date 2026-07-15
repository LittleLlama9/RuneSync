"""
mock_lcu.py — Fake League Client Update API server for testing RuneSync.

Usage:
  Terminal 1:  python mock_lcu.py
  Terminal 2:  set RUNESYNC_LOCKFILE=<path printed above> && python main.py

Then drive the draft with CLI commands. Type 'help' for the full list.
"""

import copy
import json
import os
import socket
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from socketserver import ThreadingMixIn

# ---------------------------------------------------------------------------
# Champion ID map — real Riot IDs for common champions
# ---------------------------------------------------------------------------

MOCK_CHAMPION_MAP = {
    266: "Aatrox",
    84: "Akali",
    12: "Alistar",
    32: "Amumu",
    103: "Ahri",
    22: "Ashe",
    53: "Blitzcrank",
    51: "Caitlyn",
    164: "Camille",
    69: "Cassiopeia",
    122: "Darius",
    131: "Diana",
    60: "Elise",
    81: "Ezreal",
    3: "Galio",
    41: "Gangplank",
    86: "Garen",
    150: "Gnar",
    79: "Gragas",
    104: "Graves",
    120: "Hecarim",
    39: "Irelia",
    59: "Jarvan IV",
    24: "Jax",
    202: "Jhin",
    222: "Jinx",
    40: "Janna",
    121: "Kha'Zix",
    141: "Kayn",
    55: "Katarina",
    10: "Kayle",
    85: "Kennen",
    7: "LeBlanc",
    64: "Lee Sin",
    89: "Leona",
    236: "Lucian",
    99: "Lux",
    54: "Malphite",
    11: "Master Yi",
    21: "Miss Fortune",
    75: "Nasus",
    56: "Nocturne",
    516: "Ornn",
    80: "Pantheon",
    78: "Poppy",
    58: "Renekton",
    107: "Rengar",
    92: "Riven",
    68: "Rumble",
    13: "Ryze",
    113: "Sejuani",
    875: "Sett",
    102: "Shyvana",
    27: "Singed",
    14: "Sion",
    15: "Sivir",
    72: "Skarner",
    37: "Sona",
    16: "Soraka",
    412: "Thresh",
    23: "Tryndamere",
    4: "Twisted Fate",
    77: "Udyr",
    254: "Vi",
    106: "Volibear",
    498: "Xayah",
    5: "Xin Zhao",
    157: "Yasuo",
    83: "Yorick",
    777: "Yone",
    154: "Zac",
    238: "Zed",
    115: "Ziggs",
    26: "Zilean",
    143: "Zyra",
    57: "Maokai",
    2: "Olaf",
    98: "Shen",
    91: "Talon",
}

def _build_champion_map() -> dict[int, str]:
    """Start with hardcoded IDs, then fill in any champions from champion_roles.py
    that aren't already present, using synthetic IDs starting at 10000."""
    result = dict(MOCK_CHAMPION_MAP)
    known_names = {v.lower() for v in result.values()}
    try:
        from champion_roles import ROLE_WEIGHTS
        synthetic_id = 10000
        for name in ROLE_WEIGHTS:
            if name.lower() not in known_names:
                result[synthetic_id] = name
                known_names.add(name.lower())
                synthetic_id += 1
    except Exception:
        pass
    return result


# Reverse map for name -> ID lookups (built at startup)
_NAME_TO_ID: dict[str, int] = {}

# Draft order: blue 1, red 1, red 2, blue 2, blue 3, red 3, red 4, blue 4, blue 5, red 5
_DRAFT_ORDER = [0, 5, 6, 1, 2, 7, 8, 3, 4, 9]

_POSITION_LABELS = ["top", "jungle", "middle", "bottom", "utility"]
_POSITION_ALIASES = {
    "jng": "jungle", "jg": "jungle",
    "mid": "middle",
    "bot": "bottom", "adc": "bottom",
    "sup": "utility", "support": "utility",
}


def _normalize_position(pos: str) -> str:
    p = pos.lower()
    return _POSITION_ALIASES.get(p, p)


def _cell_for_position(position: str, enemy: bool) -> int:
    pos = _normalize_position(position)
    idx = _POSITION_LABELS.index(pos) if pos in _POSITION_LABELS else 0
    return idx + (5 if enemy else 0)


def _name_to_id(name: str) -> int:
    key = name.lower().strip()
    if key in _NAME_TO_ID:
        return _NAME_TO_ID[key]
    print(f"\n[mock] WARNING: Unknown champion '{name}' — use 'addchamp <id> <name>' to add it. Using ID 0.")
    return 0


# ---------------------------------------------------------------------------
# Draft state
# ---------------------------------------------------------------------------

class DraftState:
    def __init__(self):
        self.lock = threading.Lock()
        self.phase = "None"
        self.local_player_cell_id = 0
        self.my_team = []
        self.their_team = []
        self.actions = []
        self.rune_pages: list[dict] = []
        self._next_page_id = 1000
        self.spell1_id = 4
        self.spell2_id = 12
        self.champion_map = _build_champion_map()
        self.game_id = 9990001
        self.reset_draft()

    def reset_draft(self, local_cell: int = 0, local_position: str = "top"):
        """Reset the draft session state for a new champion select."""
        self.local_player_cell_id = local_cell
        # Build teams — cells 0-4 blue, 5-9 red
        self.my_team = [
            {"cellId": i, "assignedPosition": _POSITION_LABELS[i], "championId": 0, "summonerId": i + 1}
            for i in range(5)
        ]
        self.their_team = [
            {"cellId": i + 5, "assignedPosition": _POSITION_LABELS[i], "championId": 0, "summonerId": i + 6}
            for i in range(5)
        ]
        # Override local player's position
        self.my_team[local_cell]["assignedPosition"] = _normalize_position(local_position)
        # Build actions in draft order
        self.actions = [
            [{"actorCellId": cell, "type": "pick", "championId": 0,
              "isInProgress": False, "completed": False, "id": idx}]
            for idx, cell in enumerate(_DRAFT_ORDER)
        ]

    def session_dict(self) -> dict:
        return {
            "localPlayerCellId": self.local_player_cell_id,
            "myTeam": copy.deepcopy(self.my_team),
            "theirTeam": copy.deepcopy(self.their_team),
            "actions": copy.deepcopy(self.actions),
        }

    def gameflow_dict(self) -> dict:
        position_map = {
            "top": "TOP", "jungle": "JUNGLE", "middle": "MIDDLE",
            "bottom": "BOTTOM", "utility": "UTILITY",
        }

        def convert(player):
            cell = player["cellId"]
            return {
                "championId": player.get("championId", 0),
                "puuid": "mock-puuid-0000" if cell == self.local_player_cell_id
                         else f"mock-puuid-{cell:04d}",
                "summonerId": cell + 1,
                "selectedPosition": position_map.get(
                    player.get("assignedPosition", ""), "",
                ),
            }

        return {
            "phase": self.phase,
            "gameData": {
                "gameId": self.game_id,
                "teamOne": [convert(p) for p in self.my_team],
                "teamTwo": [convert(p) for p in self.their_team],
            },
        }

    def match_dict(self) -> dict:
        defaults = [122, 64, 103, 51, 89, 86, 59, 3, 202, 40]
        position_lane = {
            "top": ("TOP", "SOLO"), "jungle": ("JUNGLE", "NONE"),
            "middle": ("MIDDLE", "SOLO"), "bottom": ("BOTTOM", "CARRY"),
            "utility": ("BOTTOM", "SUPPORT"),
        }
        participants = []
        identities = []
        for cell, player in enumerate(self.my_team + self.their_team):
            participant_id = cell + 1
            champion_id = player.get("championId") or defaults[cell]
            lane, role = position_lane.get(
                player.get("assignedPosition", ""), ("NONE", "NONE"),
            )
            team_id = 100 if cell < 5 else 200
            participants.append({
                "participantId": participant_id,
                "championId": champion_id,
                "teamId": team_id,
                "timeline": {"lane": lane, "role": role},
                "stats": {
                    "win": team_id == 100,
                    "kills": 4 + cell, "deaths": 3 + (cell % 3), "assists": 6,
                    "goldEarned": 10000 + cell * 350,
                    "totalMinionsKilled": 150 if role != "SUPPORT" else 25,
                    "neutralMinionsKilled": 50 if lane == "JUNGLE" else 0,
                    "champLevel": 16,
                    "totalDamageDealtToChampions": 15000 + cell * 1800,
                    "damageDealtToObjectives": 4000 + cell * 500,
                    "damageDealtToTurrets": 1500 + cell * 250,
                    "totalDamageTaken": 14000 + cell * 600,
                    "damageSelfMitigated": 7000 + cell * 500,
                    "totalHeal": 1000 + cell * 100,
                    "visionScore": 18 + cell * 3,
                    "wardsPlaced": 7 + cell, "wardsKilled": cell % 4,
                    "item0": 1054, "item1": 3006, "item2": 3071,
                },
            })
            identities.append({
                "participantId": participant_id,
                "player": {
                    "puuid": "mock-puuid-0000"
                             if player["cellId"] == self.local_player_cell_id
                             else f"mock-puuid-{player['cellId']:04d}",
                    "gameName": f"MockPlayer{participant_id}",
                    "tagLine": "TEST",
                },
            })
        return {
            "gameId": self.game_id, "queueId": 420, "mapId": 11,
            "gameMode": "CLASSIC", "gameDuration": 1800,
            "gameCreation": 1784000000000,
            "gameCreationDate": "2026-07-14T20:00:00Z",
            "gameVersion": "16.13.1",
            "participants": participants,
            "participantIdentities": identities,
            "teams": [],
        }

    def set_champion(self, cell_id: int, champ_id: int, completed: bool, in_progress: bool = None):
        """Update champion and pick state for a cell across actions and team lists."""
        for action_group in self.actions:
            for action in action_group:
                if action["actorCellId"] == cell_id and action["type"] == "pick":
                    action["championId"] = champ_id
                    action["completed"] = completed
                    action["isInProgress"] = (not completed) if in_progress is None else in_progress
        for player in self.my_team + self.their_team:
            if player["cellId"] == cell_id:
                player["championId"] = champ_id

    def set_in_progress(self, cell_id: int, value: bool):
        for action_group in self.actions:
            for action in action_group:
                if action["actorCellId"] == cell_id and action["type"] == "pick":
                    action["isInProgress"] = value


# ---------------------------------------------------------------------------
# HTTP server
# ---------------------------------------------------------------------------

class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True


class MockLCUHandler(BaseHTTPRequestHandler):
    state: DraftState  # set as class attribute before server starts

    def log_message(self, format, *args):
        pass  # suppress per-request noise

    def _send_json(self, data, status: int = 200):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_empty(self, status: int = 204):
        self.send_response(status)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(length)) if length else {}

    def do_GET(self):
        path = self.path.split("?")[0]
        s = self.state

        if path == "/lol-summoner/v1/current-summoner":
            self._send_json({"displayName": "MockSummoner", "puuid": "mock-puuid-0000", "summonerId": 1})

        elif path == "/lol-gameflow/v1/gameflow-phase":
            with s.lock:
                self._send_json(s.phase)

        elif path == "/lol-gameflow/v1/session":
            with s.lock:
                self._send_json(s.gameflow_dict())

        elif path == "/lol-champ-select/v1/session":
            with s.lock:
                if s.phase != "ChampSelect":
                    self._send_empty(404)
                    return
                data = s.session_dict()
            self._send_json(data)

        elif path == "/lol-game-data/assets/v1/champion-summary.json":
            with s.lock:
                data = [{"id": k, "name": v} for k, v in s.champion_map.items()]
            self._send_json(data)

        elif path == "/lol-perks/v1/pages":
            with s.lock:
                self._send_json(list(s.rune_pages))

        elif path.startswith("/lol-match-history/v1/products/lol/") \
                and path.endswith("/matches"):
            with s.lock:
                game = s.match_dict()
            self._send_json({"games": {
                "gameCount": 1, "gameIndexBegin": 0, "gameIndexEnd": 0,
                "games": [game],
            }})

        elif path.startswith("/lol-match-history/v1/games/"):
            try:
                game_id = int(path.rsplit("/", 1)[1])
            except ValueError:
                self._send_empty(400)
                return
            with s.lock:
                if game_id != s.game_id:
                    self._send_empty(404)
                    return
                game = s.match_dict()
            self._send_json(game)

        elif path == "/lol-end-of-game/v1/eog-stats-block":
            with s.lock:
                if s.phase != "EndOfGame":
                    self._send_empty(404)
                    return
                game = s.match_dict()
            self._send_json(game)

        else:
            self._send_empty(404)

    def do_POST(self):
        path = self.path.split("?")[0]
        s = self.state

        if path == "/lol-perks/v1/pages":
            body = self._read_body()
            with s.lock:
                page = dict(body)
                page["id"] = s._next_page_id
                page["isDeletable"] = True
                page["isEditable"] = True
                page["isActive"] = True
                page["current"] = True
                for p in s.rune_pages:
                    p["isActive"] = False
                    p["current"] = False
                s.rune_pages.append(page)
                s._next_page_id += 1
            perks = page.get("selectedPerkIds", [])
            print(f"\n[mock] Rune page CREATED: '{page.get('name')}' "
                  f"| primary={page.get('primaryStyleId')} secondary={page.get('subStyleId')} "
                  f"| perks={perks}")
            self._send_json(page, 200)
        else:
            self._send_empty(404)

    def do_DELETE(self):
        parts = self.path.split("?")[0].split("/")
        # /lol-perks/v1/pages/{id}
        if len(parts) >= 5 and parts[3] == "pages":
            try:
                page_id = int(parts[4])
            except (ValueError, IndexError):
                self._send_empty(400)
                return
            with self.state.lock:
                before = len(self.state.rune_pages)
                self.state.rune_pages = [p for p in self.state.rune_pages if p["id"] != page_id]
                removed = before - len(self.state.rune_pages)
            if removed:
                print(f"\n[mock] Rune page DELETED: id={page_id}")
            self._send_empty(204)
        else:
            self._send_empty(404)

    def do_PATCH(self):
        path = self.path.split("?")[0]
        if path == "/lol-champ-select/v1/session/my-selection":
            body = self._read_body()
            with self.state.lock:
                self.state.spell1_id = body.get("spell1Id", self.state.spell1_id)
                self.state.spell2_id = body.get("spell2Id", self.state.spell2_id)
                s1, s2 = self.state.spell1_id, self.state.spell2_id
            print(f"\n[mock] Summoner spells SET: spell1={s1} spell2={s2}")
            self._send_json({})
        else:
            self._send_empty(404)


# ---------------------------------------------------------------------------
# Server startup
# ---------------------------------------------------------------------------

def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def start_server(state: DraftState) -> tuple[ThreadedHTTPServer, int, Path]:
    port = _find_free_port()
    password = "mockpassword123"
    appdata = os.environ.get("APPDATA", tempfile.gettempdir())
    lockfile_dir = Path(appdata) / "RuneSync"
    lockfile_dir.mkdir(parents=True, exist_ok=True)
    lockfile = lockfile_dir / "lockfile"
    # Format: name:pid:port:password:protocol:region (6 parts, index 2=port, 3=password)
    lockfile.write_text(f"LeagueClientUx.exe:0:{port}:{password}:https:na1")

    MockLCUHandler.state = state
    server = ThreadedHTTPServer(("127.0.0.1", port), MockLCUHandler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    return server, port, lockfile


# ---------------------------------------------------------------------------
# Preset scenarios
# ---------------------------------------------------------------------------

def _scene_toplaner_hovering(state: DraftState):
    """Enemy top hovered, then you hover — expect rune import on hover trigger."""
    print("[scene] toplaner_hovering: enemy top picks Garen, you hover Darius")
    with state.lock:
        state.phase = "ChampSelect"
        state.reset_draft(local_cell=0, local_position="top")
    time.sleep(1.5)
    with state.lock:
        state.set_champion(5, _name_to_id("Garen"), completed=True, in_progress=False)
    print("[scene] Enemy top locked Garen")
    time.sleep(1.5)
    with state.lock:
        state.set_in_progress(0, True)
    print("[scene] Your pick turn started")
    time.sleep(1.0)
    with state.lock:
        state.set_champion(0, _name_to_id("Darius"), completed=False, in_progress=True)
    print("[scene] You hovered Darius — RuneSync should import runes now (if trigger=hover)")


def _scene_enemy_picks_first(state: DraftState):
    """Enemy top confirmed before your turn — counterpick flow."""
    print("[scene] enemy_picks_first: enemy top confirms Darius, you counter-pick")
    with state.lock:
        state.phase = "ChampSelect"
        state.reset_draft(local_cell=1, local_position="top")
    time.sleep(1.5)
    with state.lock:
        state.set_champion(5, _name_to_id("Darius"), completed=True, in_progress=False)
    print("[scene] Enemy top locked Darius")
    time.sleep(2.0)
    with state.lock:
        state.set_in_progress(1, True)
    print("[scene] Your pick turn — RuneSync should suggest counters")
    time.sleep(3.0)
    with state.lock:
        state.set_champion(1, _name_to_id("Garen"), completed=True, in_progress=False)
    print("[scene] You locked Garen — rune import should fire")


def _scene_game_start(state: DraftState):
    """Champion select → InProgress — tests window management."""
    print("[scene] game_start: entering ChampSelect, then transitioning to InProgress")
    with state.lock:
        state.phase = "ChampSelect"
        state.reset_draft(local_cell=0, local_position="top")
        state.set_champion(0, _name_to_id("Darius"), completed=True, in_progress=False)
        state.set_champion(5, _name_to_id("Garen"), completed=True, in_progress=False)
    print("[scene] In ChampSelect with picks set. Switching to InProgress in 3s...")
    time.sleep(3)
    with state.lock:
        state.phase = "InProgress"
    print("[scene] Phase = InProgress — RuneSync should detect game started")


def _scene_full_draft(state: DraftState):
    """All 10 picks in order with delays."""
    champions = [
        (0, "Darius"), (5, "Garen"), (6, "Lee Sin"), (1, "Irelia"), (2, "Ahri"),
        (7, "Zed"), (8, "Thresh"), (3, "Jinx"), (4, "Janna"), (9, "Caitlyn"),
    ]
    print("[scene] full_draft: walking through all 10 picks")
    with state.lock:
        state.phase = "ChampSelect"
        state.reset_draft(local_cell=0, local_position="top")
    time.sleep(1)
    for cell, champ in champions:
        with state.lock:
            state.set_in_progress(cell, True)
        time.sleep(1)
        cid = _name_to_id(champ)
        with state.lock:
            state.set_champion(cell, cid, completed=True, in_progress=False)
        print(f"[scene] Cell {cell} locked {champ}")
        time.sleep(1.5)
    print("[scene] All picks complete")


SCENARIOS = {
    "toplaner_hovering": _scene_toplaner_hovering,
    "enemy_picks_first": _scene_enemy_picks_first,
    "game_start": _scene_game_start,
    "full_draft": _scene_full_draft,
}


# ---------------------------------------------------------------------------
# CLI helpers
# ---------------------------------------------------------------------------

def _cmd_help():
    print("""
Commands:
  start [role]             Enter ChampSelect, reset draft (default role: top)
  draft <side> <role>      Full reset: side=blue|red, role=top|jng|mid|bot|sup
  phase <phase>            Set phase: None | Lobby | ChampSelect | InProgress | EndOfGame
  end                      Reset phase to None
  epick <pos> <champion>   Enemy picks champion at position (top/jng/mid/bot/sup)
  mypick <champion>        You hover champion (triggers rune import if trigger=hover)
  mylock <champion>        You lock champion (triggers rune import if trigger=lock)
  pick <cell> <champion>   Hover by raw cell ID (0-9)
  lock <cell> <champion>   Lock by raw cell ID
  turn <cell>              Set isInProgress=True for cell (your pick turn)
  endturn <cell>           Set isInProgress=False for cell
  myrole <role>            Change your assignedPosition
  pages                    Print current rune pages
  spells                   Print last summoner spells RuneSync set
  status                   Print full state summary
  scene <name>             Run preset scenario (see list below)
  scenes                   List available scenarios
  addchamp <id> <name>     Add champion to ID map
  q / quit                 Exit

Scenarios: """ + ", ".join(SCENARIOS.keys()))


def _print_status(state: DraftState):
    with state.lock:
        print(f"\n  Phase: {state.phase}")
        print(f"  Local cell: {state.local_player_cell_id}")
        print("  My team:")
        for p in state.my_team:
            cname = state.champion_map.get(p["championId"], f"id={p['championId']}")
            print(f"    cell {p['cellId']} {p['assignedPosition']:8s} => {cname}")
        print("  Their team:")
        for p in state.their_team:
            cname = state.champion_map.get(p["championId"], f"id={p['championId']}")
            print(f"    cell {p['cellId']} {p['assignedPosition']:8s} => {cname}")
        print(f"  Rune pages: {len(state.rune_pages)}")
        print(f"  Spells: {state.spell1_id} / {state.spell2_id}")


def run_cli(state: DraftState):
    _build_reverse_map(state)
    print("\n[mock] Interactive CLI ready. Type 'help' for commands.")
    print("[mock] Quick start: draft blue top  ->  epick top Garen  ->  mypick Darius\n")

    while True:
        try:
            line = input("mock> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n[mock] Shutting down.")
            break
        if not line:
            continue
        parts = line.split()
        cmd = parts[0].lower()

        try:
            if cmd in ("q", "quit", "exit"):
                break

            elif cmd == "help":
                _cmd_help()

            elif cmd == "scenes":
                print("  " + "\n  ".join(SCENARIOS.keys()))

            elif cmd == "start":
                role = parts[1] if len(parts) > 1 else "top"
                with state.lock:
                    state.reset_draft(local_cell=0, local_position=role)
                    state.phase = "ChampSelect"
                print(f"[mock] ChampSelect started | you=cell0 role={_normalize_position(role)}")

            elif cmd == "draft":
                side = parts[1].lower() if len(parts) > 1 else "blue"
                role = parts[2] if len(parts) > 2 else "top"
                local_cell = 0 if side == "blue" else 4
                with state.lock:
                    state.reset_draft(local_cell=local_cell, local_position=role)
                    state.phase = "ChampSelect"
                print(f"[mock] ChampSelect started | you=cell{local_cell} ({side}) role={_normalize_position(role)}")

            elif cmd == "phase":
                if len(parts) < 2:
                    print("[mock] Usage: phase <phase>")
                    continue
                with state.lock:
                    state.phase = parts[1]
                print(f"[mock] Phase = {parts[1]}")

            elif cmd == "end":
                with state.lock:
                    state.phase = "None"
                    state.my_team = []
                    state.their_team = []
                    state.actions = []
                print("[mock] Phase reset to None, draft cleared")

            elif cmd == "epick":
                if len(parts) < 3:
                    print("[mock] Usage: epick <position> <champion>")
                    continue
                pos = parts[1]
                champ = " ".join(parts[2:])
                cell = _cell_for_position(pos, enemy=True)
                cid = _name_to_id(champ)
                with state.lock:
                    state.set_champion(cell, cid, completed=True, in_progress=False)
                cname = state.champion_map.get(cid, champ)
                print(f"[mock] Enemy {_normalize_position(pos)} (cell {cell}) locked {cname}")

            elif cmd == "mypick":
                if len(parts) < 2:
                    print("[mock] Usage: mypick <champion>")
                    continue
                champ = " ".join(parts[1:])
                cid = _name_to_id(champ)
                with state.lock:
                    cell = state.local_player_cell_id
                    state.set_champion(cell, cid, completed=False, in_progress=True)
                cname = state.champion_map.get(cid, champ)
                print(f"[mock] You hovered {cname} (cell {cell})")

            elif cmd == "mylock":
                if len(parts) < 2:
                    print("[mock] Usage: mylock <champion>")
                    continue
                champ = " ".join(parts[1:])
                cid = _name_to_id(champ)
                with state.lock:
                    cell = state.local_player_cell_id
                    state.set_champion(cell, cid, completed=True, in_progress=False)
                cname = state.champion_map.get(cid, champ)
                print(f"[mock] You locked {cname} (cell {cell})")

            elif cmd == "pick":
                if len(parts) < 3:
                    print("[mock] Usage: pick <cell> <champion>")
                    continue
                cell = int(parts[1])
                champ = " ".join(parts[2:])
                cid = _name_to_id(champ)
                with state.lock:
                    state.set_champion(cell, cid, completed=False, in_progress=True)
                cname = state.champion_map.get(cid, champ)
                print(f"[mock] Cell {cell} hovered {cname}")

            elif cmd == "lock":
                if len(parts) < 3:
                    print("[mock] Usage: lock <cell> <champion>")
                    continue
                cell = int(parts[1])
                champ = " ".join(parts[2:])
                cid = _name_to_id(champ)
                with state.lock:
                    state.set_champion(cell, cid, completed=True, in_progress=False)
                cname = state.champion_map.get(cid, champ)
                print(f"[mock] Cell {cell} locked {cname}")

            elif cmd == "turn":
                if len(parts) < 2:
                    print("[mock] Usage: turn <cell>")
                    continue
                cell = int(parts[1])
                with state.lock:
                    state.set_in_progress(cell, True)
                print(f"[mock] Cell {cell} isInProgress=True")

            elif cmd == "endturn":
                if len(parts) < 2:
                    print("[mock] Usage: endturn <cell>")
                    continue
                cell = int(parts[1])
                with state.lock:
                    state.set_in_progress(cell, False)
                print(f"[mock] Cell {cell} isInProgress=False")

            elif cmd == "myrole":
                if len(parts) < 2:
                    print("[mock] Usage: myrole <role>")
                    continue
                role = _normalize_position(parts[1])
                with state.lock:
                    cell = state.local_player_cell_id
                    for p in state.my_team:
                        if p["cellId"] == cell:
                            p["assignedPosition"] = role
                print(f"[mock] Your role set to {role}")

            elif cmd == "pages":
                with state.lock:
                    pages = list(state.rune_pages)
                if not pages:
                    print("[mock] No rune pages.")
                for p in pages:
                    print(f"  id={p['id']} name='{p.get('name')}' "
                          f"primary={p.get('primaryStyleId')} secondary={p.get('subStyleId')} "
                          f"active={p.get('isActive')}")

            elif cmd == "spells":
                with state.lock:
                    s1, s2 = state.spell1_id, state.spell2_id
                print(f"[mock] spell1={s1}  spell2={s2}")

            elif cmd == "status":
                _print_status(state)

            elif cmd == "scene":
                name = parts[1] if len(parts) > 1 else ""
                if name not in SCENARIOS:
                    print(f"[mock] Unknown scene '{name}'. Available: {', '.join(SCENARIOS.keys())}")
                    continue
                t = threading.Thread(target=SCENARIOS[name], args=(state,), daemon=True)
                t.start()

            elif cmd == "addchamp":
                if len(parts) < 3:
                    print("[mock] Usage: addchamp <id> <name>")
                    continue
                cid = int(parts[1])
                cname = " ".join(parts[2:])
                with state.lock:
                    state.champion_map[cid] = cname
                _build_reverse_map(state)
                print(f"[mock] Added champion: {cid} = {cname}")

            else:
                print(f"[mock] Unknown command '{cmd}'. Type 'help'.")

        except (ValueError, IndexError) as e:
            print(f"[mock] Error: {e}")


def _build_reverse_map(state: DraftState):
    global _NAME_TO_ID
    with state.lock:
        _NAME_TO_ID = {v.lower(): k for k, v in state.champion_map.items()}


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    state = DraftState()
    server, port, lockfile = start_server(state)

    print("=" * 60)
    print("  RuneSync Mock LCU Server")
    print("=" * 60)
    print(f"  HTTP server: http://127.0.0.1:{port}")
    print(f"  Lockfile:    {lockfile}")
    print()
    print("  Now open RuneSync normally:")
    print("    RuneSync.exe  — or —  py main.py")
    print("=" * 60)

    try:
        run_cli(state)
    finally:
        server.shutdown()
        try:
            lockfile.unlink(missing_ok=True)
        except Exception:
            pass
        print("[mock] Server stopped.")


if __name__ == "__main__":
    main()

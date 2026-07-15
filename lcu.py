"""
LCU (League Client Update) API client.
Connects to the League client's internal HTTPS server via the lockfile.
"""

import os, ssl, json, base64, subprocess, sys, urllib.request, urllib.error
from pathlib import Path
from typing import Optional


class LCUConnectionError(Exception):
    pass


RUNE_TREE_IDS = {
    "Precision": 8000, "Domination": 8100,
    "Sorcery": 8200, "Resolve": 8400, "Inspiration": 8300,
}

KEYSTONE_IDS = {
    "Press the Attack": 8005, "Lethal Tempo": 8008,
    "Fleet Footwork": 8021, "Conqueror": 8010,
    "Electrocute": 8112, "Predator": 8124,
    "Dark Harvest": 8128, "Hail of Blades": 9923,
    "Summon Aery": 8214, "Arcane Comet": 8229, "Phase Rush": 8230,
    "Grasp of the Undying": 8437, "Aftershock": 8439, "Guardian": 8465,
    "Glacial Augment": 8351, "First Strike": 8360, "Unsealed Spellbook": 8369,
}


class LCUClient:
    def __init__(self):
        self.connected = False
        self._port: Optional[int] = None
        self._password: Optional[str] = None
        self._summoner_id: Optional[int] = None
        self._puuid: Optional[str] = None
        self._perk_meta: dict = {}  # {perk_id: (tree_id, row_index)}
        self._ssl_ctx = ssl.create_default_context()
        self._ssl_ctx.check_hostname = False
        self._ssl_ctx.verify_mode = ssl.CERT_NONE

    def connect(self):
        lockfile_path = self._find_lockfile()
        if not lockfile_path:
            raise LCUConnectionError(
                "Could not find the League lockfile. Is the League client open?"
            )
        self._parse_lockfile(lockfile_path)
        try:
            summoner = self._get("/lol-summoner/v1/current-summoner")
            self._summoner_id = summoner.get("summonerId") or summoner.get("accountId")
            self._puuid = summoner.get("puuid")
            self.connected = True
        except Exception as e:
            raise LCUConnectionError(f"LCU reachable but request failed: {e}")
        self._load_perk_metadata()

    def _load_perk_metadata(self):
        """Fetch perk tree/row mapping from the LCU so we can sort selectedPerkIds."""
        try:
            styles = self._get("/lol-perks/v1/styles")
            meta = {}
            for style in styles:
                tree_id = style.get("id", 0)
                for row_idx, slot in enumerate(style.get("slots", [])):
                    for perk in slot.get("perks", []):
                        meta[perk] = (tree_id, row_idx)
            self._perk_meta = meta
        except Exception as e:
            print(f"[lcu] perk metadata load failed (non-fatal): {e}", file=sys.stderr)

    def _sort_perk_ids(self, perk_ids: list, primary_id: int, secondary_id: int) -> list:
        """Sort selectedPerkIds into correct LCU positional order.

        Order: [keystone, row1, row2, row3, secondary1, secondary2, shard1, shard2, shard3]
        """
        if not self._perk_meta or len(perk_ids) < 9:
            return perk_ids

        rune_perks = perk_ids[:6]
        shard_perks = perk_ids[6:9]

        primary = []
        secondary = []
        for pid in rune_perks:
            tree_id, row = self._perk_meta.get(pid, (0, 99))
            if tree_id == primary_id:
                primary.append((row, pid))
            elif tree_id == secondary_id:
                secondary.append((row, pid))
            else:
                if len(primary) < 4:
                    primary.append((row, pid))
                else:
                    secondary.append((row, pid))

        primary.sort()
        secondary.sort()
        return [pid for _, pid in primary] + [pid for _, pid in secondary] + shard_perks

    @staticmethod
    def _lockfile_port_open(path: Path) -> bool:
        """True if the port named in a lockfile (name:pid:port:pw:proto) is
        accepting connections — used to ignore a stale mock lockfile."""
        try:
            port = int(path.read_text().strip().split(":")[2])
        except Exception:
            return False
        import socket
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.3):
                return True
        except Exception:
            return False

    def _find_lockfile(self) -> Optional[Path]:
        env_path = os.environ.get("RUNESYNC_LOCKFILE")
        if env_path:
            p = Path(env_path)
            if p.exists():
                return p
        # Mock lockfile in APPDATA/RuneSync (written by mock_lcu.py) — only honour
        # it if the mock server is actually listening. A stale mock lockfile left
        # by a force-killed test run must NOT hijack a real League client; if the
        # port is dead, delete it and fall through to the real install.
        appdata = os.environ.get("APPDATA", "")
        if appdata:
            mock_lock = Path(appdata) / "RuneSync" / "lockfile"
            if mock_lock.exists():
                if self._lockfile_port_open(mock_lock):
                    return mock_lock
                try:
                    mock_lock.unlink()
                except Exception:
                    pass
        # Common install locations — checked in order
        candidates = [
            Path("C:/Riot Games/League of Legends/lockfile"),
            Path("D:/Riot Games/League of Legends/lockfile"),
            Path("C:/Program Files/Riot Games/League of Legends/lockfile"),
            Path("C:/Program Files (x86)/Riot Games/League of Legends/lockfile"),
            Path(os.path.expanduser("~/Riot Games/League of Legends/lockfile")),
        ]
        for p in candidates:
            if p.exists():
                return p

        # Scan all drives for lockfile
        import string
        for drive in string.ascii_uppercase:
            p = Path(f"{drive}:/Riot Games/League of Legends/lockfile")
            if p.exists():
                return p

        # Read install path directly from the running LeagueClientUx.exe via psutil
        try:
            import psutil
            for proc in psutil.process_iter(["name", "exe"]):
                try:
                    name = proc.info["name"]
                    if name and name.lower() == "leagueclientux.exe":
                        exe = proc.info["exe"]
                        if exe:
                            candidate = Path(exe).parent / "lockfile"
                            if candidate.exists():
                                return candidate
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue
        except Exception:
            pass

        return None

    def _parse_lockfile(self, path: Path):
        try:
            content = path.read_text(encoding="utf-8", errors="replace").strip()
        except OSError as e:
            raise LCUConnectionError(f"Could not read lockfile: {e}")
        parts = content.split(":")
        if len(parts) < 5:
            raise LCUConnectionError("Lockfile format unexpected.")
        try:
            self._port = int(parts[2])
        except ValueError:
            # Partially-written lockfile during client launch: surface as a
            # connection error (connect() is outside any try here) so the caller
            # retries instead of a raw ValueError crashing the thread.
            raise LCUConnectionError("Lockfile port not yet written.")
        self._password = parts[3]

    @property
    def _auth_header(self) -> str:
        token = base64.b64encode(f"riot:{self._password}".encode()).decode()
        return f"Basic {token}"

    @property
    def _base_url(self) -> str:
        appdata = os.environ.get("APPDATA", "")
        mock_lock = Path(appdata) / "RuneSync" / "lockfile" if appdata else None
        is_mock = bool(os.environ.get("RUNESYNC_LOCKFILE")) or (mock_lock and mock_lock.exists())
        scheme = "http" if is_mock else "https"
        return f"{scheme}://127.0.0.1:{self._port}"

    def _get(self, path: str) -> dict:
        url = self._base_url + path
        req = urllib.request.Request(url, headers={"Authorization": self._auth_header})
        with urllib.request.urlopen(req, context=self._ssl_ctx, timeout=5) as resp:
            return json.loads(resp.read())

    def _post(self, path: str, body: dict) -> dict:
        url = self._base_url + path
        data = json.dumps(body).encode()
        req = urllib.request.Request(url, data=data, method="POST", headers={
            "Authorization": self._auth_header, "Content-Type": "application/json",
        })
        try:
            with urllib.request.urlopen(req, context=self._ssl_ctx, timeout=5) as resp:
                return json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as e:
            raise LCUConnectionError(f"POST {path} failed {e.code}: {e.read().decode(errors='replace')}")

    def _patch(self, path: str, body: dict) -> dict:
        url = self._base_url + path
        data = json.dumps(body).encode()
        req = urllib.request.Request(url, data=data, method="PATCH", headers={
            "Authorization": self._auth_header, "Content-Type": "application/json",
        })
        try:
            with urllib.request.urlopen(req, context=self._ssl_ctx, timeout=5) as resp:
                return json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as e:
            raise LCUConnectionError(f"PATCH {path} failed {e.code}: {e.read().decode(errors='replace')}")

    def _put(self, path: str, body: dict) -> dict:
        url = self._base_url + path
        data = json.dumps(body).encode()
        req = urllib.request.Request(url, data=data, method="PUT", headers={
            "Authorization": self._auth_header, "Content-Type": "application/json",
        })
        try:
            with urllib.request.urlopen(req, context=self._ssl_ctx, timeout=5) as resp:
                return json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as e:
            raise LCUConnectionError(f"PUT {path} failed {e.code}: {e.read().decode(errors='replace')}")

    def _delete(self, path: str):
        url = self._base_url + path
        req = urllib.request.Request(url, method="DELETE",
                                     headers={"Authorization": self._auth_header})
        try:
            urllib.request.urlopen(req, context=self._ssl_ctx, timeout=5)
        except urllib.error.HTTPError as e:
            # urlopen only enters this block for status >= 400; a 404 means the
            # page is already gone, which is success for an idempotent delete.
            if e.code != 404:
                raise LCUConnectionError(f"DELETE {path} failed {e.code}")

    def get_champ_select_session(self) -> Optional[dict]:
        try:
            return self._get("/lol-champ-select/v1/session")
        except Exception:
            return None

    def get_game_flow_phase(self) -> str:
        try:
            return self._get("/lol-gameflow/v1/gameflow-phase")
        except urllib.error.URLError as e:
            # Network-level failure means the client process is gone
            raise LCUConnectionError(f"League client not reachable: {e}")
        except Exception:
            return "None"

    def get_enemy_champion_id_for_role(self, role: str) -> Optional[int]:
        """Return the enemy champion assigned to ``role`` when gameflow exposes it."""
        target = {
            "top": "TOP", "jungle": "JUNGLE", "mid": "MIDDLE",
            "bot": "BOTTOM", "support": "UTILITY",
        }.get(role)
        if not target or (not self._summoner_id and not self._puuid):
            return None

        try:
            game_data = self._get("/lol-gameflow/v1/session").get("gameData") or {}
            teams = [game_data.get("teamOne") or [], game_data.get("teamTwo") or []]
            own_index = next(
                (
                    i for i, team in enumerate(teams)
                    if any(
                        (self._summoner_id and p.get("summonerId") == self._summoner_id)
                        or (self._puuid and p.get("puuid") == self._puuid)
                        for p in team
                    )
                ),
                None,
            )
            if own_index is None:
                return None

            for player in teams[1 - own_index]:
                position = (player.get("selectedPosition")
                            or player.get("assignedPosition") or "").upper()
                champion_id = player.get("championId", 0)
                if position == target and isinstance(champion_id, int) and champion_id > 0:
                    return champion_id
        except Exception:
            return None
        return None

    def get_champion_name_map(self) -> dict:
        try:
            data = self._get("/lol-game-data/assets/v1/champion-summary.json")
            return {c["id"]: c["name"] for c in data if c["id"] > 0}
        except Exception:
            return {}

    def set_summoner_spells(self, spell1_id: int, spell2_id: int) -> bool:
        import sys
        # Flash (4) must always be on F key (spell2). Swap if needed.
        FLASH_ID = 4
        if spell1_id == FLASH_ID and spell2_id != FLASH_ID:
            spell1_id, spell2_id = spell2_id, spell1_id
        # Guard: only valid during ChampSelect
        try:
            phase = self.get_game_flow_phase()
        except Exception as e:
            # Couldn't confirm the phase (client unreachable). Don't PATCH blind —
            # falling through here is exactly what the guard exists to prevent.
            print(f"[lcu] set_summoner_spells skipped — phase check failed: {e}", file=sys.stderr)
            return False
        if phase != "ChampSelect":
            print(f"[lcu] set_summoner_spells skipped — phase is '{phase}'", file=sys.stderr)
            return False
        try:
            self._patch("/lol-champ-select/v1/session/my-selection", {
                "spell1Id": spell1_id,
                "spell2Id": spell2_id,
            })
            return True
        except LCUConnectionError as e:
            print(f"[lcu] set_summoner_spells failed: {e}", file=sys.stderr)
            return False

    def get_current_rune_page(self):
        """Return the currently active rune page from the League client, or None."""
        try:
            pages = self._get("/lol-perks/v1/pages")
            for page in pages:
                if page.get("current", False) or page.get("isActive", False):
                    return page
            return pages[0] if pages else None
        except Exception:
            return None

    def import_rune_page(self, name: str, primary_id: int,
                         secondary_id: int, perk_ids: list) -> bool:
        import sys
        # Delete the oldest non-default page to stay under the 20-page limit
        try:
            pages = self._get("/lol-perks/v1/pages")
            # Find a deletable page (isDeletable=True), prefer one named "RuneSync" or oldest
            deletable = [p for p in pages if p.get("isDeletable", False) or p.get("isEditable", False)]
            if deletable:
                # Prefer a previously imported RuneSync page, otherwise take the first deletable
                runesync_pages = [p for p in deletable if "RuneSync" in p.get("name", "") or p.get("name", "").startswith(name.split()[0])]
                to_delete = runesync_pages[0] if runesync_pages else deletable[0]
                self._delete(f"/lol-perks/v1/pages/{to_delete['id']}")
                print(f"[lcu] deleted page '{to_delete.get('name')}' (id={to_delete['id']})", file=sys.stderr)
        except Exception as e:
            print(f"[lcu] page cleanup error: {e}", file=sys.stderr)
        sorted_perks = self._sort_perk_ids(perk_ids, primary_id, secondary_id)
        body = {
            "name": name,
            "primaryStyleId": primary_id,
            "subStyleId": secondary_id,
            "selectedPerkIds": sorted_perks,
            "current": True,
        }
        try:
            self._post("/lol-perks/v1/pages", body)
            return True
        except LCUConnectionError as e:
            print(f"[lcu] import_rune_page failed: {e}", file=sys.stderr)
            print(f"[lcu] body was: {body}", file=sys.stderr)
            return False

    def import_item_set(self, champion_name: str, champion_id: int, role: str,
                        starter_ids: list, core_ids: list,
                        fourth_ids: list = None, fifth_ids: list = None, sixth_ids: list = None) -> bool:
        import sys, time
        if not core_ids:
            return False
        slug = champion_name.lower().replace(" ", "-").replace("'", "").replace(".", "")
        uid = f"runesync-{slug}-{role.lower()}"
        blocks = []
        if starter_ids:
            blocks.append({
                "items": [{"count": 1, "id": str(i)} for i in starter_ids],
                "showIfSummonerSpell": "", "hideIfSummonerSpell": "",
                "type": "Starter Items",
            })
        blocks.append({
            "items": [{"count": 1, "id": str(i)} for i in core_ids],
            "showIfSummonerSpell": "", "hideIfSummonerSpell": "",
            "type": "Core Build",
        })
        for label, ids in [("4th Item Options", fourth_ids), ("5th Item Options", fifth_ids), ("6th Item Options", sixth_ids)]:
            if ids:
                blocks.append({
                    "items": [{"count": 1, "id": str(i)} for i in ids],
                    "showIfSummonerSpell": "", "hideIfSummonerSpell": "",
                    "type": label,
                })
        new_set = {
            "associatedChampions": [champion_id] if champion_id else [],
            "associatedMaps": [11, 12],
            "blocks": blocks,
            "map": "any",
            "mode": "any",
            "preferredItemSlots": [],
            "sortrank": 0,
            "startedFrom": "blank",
            "title": f"RuneSync \u2014 {champion_name} {role.title()}",
            "type": "custom",
            "uid": uid,
        }
        try:
            sid = self._summoner_id
            path = f"/lol-item-sets/v1/item-sets/{sid}/sets" if sid else "/lol-item-sets/v1/item-sets/sets"
            existing = self._get(path)
            item_sets = existing.get("itemSets", [])
            # Replace any existing RuneSync set for this champion/role
            item_sets = [s for s in item_sets if s.get("uid") != uid]
            item_sets.insert(0, new_set)
            # Trim oldest sets if payload exceeds ~80KB (LCU 413 limit)
            payload = {"itemSets": item_sets, "timestamp": int(time.time() * 1000)}
            while len(json.dumps(payload).encode()) > 30000 and len(payload["itemSets"]) > 1:
                payload["itemSets"].pop()
            self._put(path, payload)
            return True
        except Exception as e:
            print(f"[lcu] import_item_set failed: {e}", file=sys.stderr)
            return False

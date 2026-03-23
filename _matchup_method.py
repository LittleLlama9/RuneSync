
    def get_matchup_winrate(self, my_champ: str, enemy_champ: str,
                            role: str = "auto") -> Optional[dict]:
        """
        Scrape u.gg matchup win rate for my_champ vs enemy_champ in the given role.
        Returns {"win_rate": float, "enemy": str} or None on failure.
        """
        self._ensure_debug_port()

        role_slug = ROLE_MAP.get(role.lower(), "")
        url = f"https://u.gg/lol/champions/{my_champ.lower().replace(' ', '-').replace(\"'\", '')}/matchups"
        if role_slug:
            url += f"?role={role_slug}"

        import urllib.parse
        encoded = urllib.parse.quote(url, safe="")
        new_tab = self._cdp_put(f"/json/new?{encoded}")
        tab_id  = new_tab["id"]
        ws_url  = new_tab["webSocketDebuggerUrl"]

        time.sleep(1.5)

        # Build JS with enemy name substituted in
        js = MATCHUP_JS_TEMPLATE.replace("'ENEMY_NAME'", f"'{enemy_champ}'")

        try:
            raw = self._ws_eval(ws_url, js, timeout=22)
            wr = raw.get("winRate") if raw else None
            if wr is None:
                return None
            return {"win_rate": wr, "enemy": enemy_champ}
        except Exception as e:
            import sys
            print(f"[ugg] matchup scrape failed ({my_champ} vs {enemy_champ}): {e}", file=sys.stderr)
            return None
        finally:
            try:
                self._cdp_post(f"/json/close/{tab_id}")
            except Exception:
                pass


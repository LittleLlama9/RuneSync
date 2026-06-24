"""DAEMON — auto rune importer with per-champion overrides.

Amber-CRT terminal skin over the RuneSync engine. The window title and in-UI
brand read "DAEMON"; the repo/exe stay RuneSync. The CRT colour themes are
still called "phosphors" (amber/green/ice) — that's screen terminology, not
the brand. All League logic (monitor.py, lcu.py, ugg_api.py, tray, autostart)
is unchanged — this module is the presentation layer plus a live-theming engine.
"""
import sys, threading, tkinter as tk, ctypes
from tkinter import ttk, messagebox
import os
from lcu import LCUClient, LCUConnectionError
import ugg_api
from ugg_api import UGGClient
from overrides import OverrideManager
from monitor import ChampSelectMonitor
from tray import TrayController, LeaguePoller, is_autostart_enabled, set_autostart

_BASE_DIR = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
_ASSETS_DIR = os.path.join(_BASE_DIR, "assets", "spells")
_FONTS_DIR = os.path.join(_BASE_DIR, "assets", "fonts")

# ── Fonts ────────────────────────────────────────────────────────────────────
# VT323 (pixel terminal) + Space Mono (fine print) are bundled and registered
# privately at startup. Tkinter can't read Google Fonts, so we hand them to GDI
# via AddFontResourceExW(FR_PRIVATE) before any widget is built. If that fails
# (non-Windows, missing files), _FONTS_OK stays False and F() falls back to
# Consolas everywhere — still a terminal, just not retro.
_FONTS_OK = False


def _load_fonts():
    global _FONTS_OK
    if os.name != "nt":
        return
    try:
        loaded = 0
        for f in ("VT323-Regular.ttf", "SpaceMono-Regular.ttf", "SpaceMono-Bold.ttf"):
            p = os.path.join(_FONTS_DIR, f)
            if os.path.exists(p):
                loaded += ctypes.windll.gdi32.AddFontResourceExW(p, 0x10, 0)  # FR_PRIVATE
        _FONTS_OK = loaded > 0
    except Exception:
        _FONTS_OK = False


def F(size, fine=False, bold=False):
    """Font tuple. fine=True → Space Mono (timestamps/legal); else VT323.
    Falls back to Consolas when the bundled faces didn't load. VT323 ships only
    one weight, so bold is honoured only on the fallback/fine faces."""
    if fine:
        fam = "Space Mono" if _FONTS_OK else "Consolas"
        return (fam, size, "bold") if bold else (fam, size)
    fam = "VT323" if _FONTS_OK else "Consolas"
    # VT323 has no bold; only thicken when we fell back to Consolas.
    return (fam, size, "bold") if (bold and not _FONTS_OK) else (fam, size)


# ── Palette ──────────────────────────────────────────────────────────────────
# One ink colour on near-black; brightness (not hue) encodes hierarchy:
# P_DIM < P < P_BRIGHT. BG and DANGER stay constant across all three phosphors.
BG       = "#0c0a06"   # screen background (near-black, warm; constant across phosphors)
DANGER   = "#ff5544"   # stop / errors / unfavorable (constant across phosphors)

PHOSPHORS = {
    "amber": dict(P="#ffb000", P_BRIGHT="#ffd87a", P_DIM="#c98a2e", BORDER="#6b4a00"),
    "green": dict(P="#39ff8c", P_BRIGHT="#bcffd6", P_DIM="#2bbf6a", BORDER="#0f5e35"),
    "ice":   dict(P="#7fdfff", P_BRIGHT="#d2f4ff", P_DIM="#4fa8cf", BORDER="#1a4a5e"),
}

# Static amber palette for the two surfaces NOT wired into the live theme
# registry — the dev Debug console (Ctrl+Shift+D) and the build sub-editor.
# They always render amber regardless of the active phosphor (accepted limit).
_AMBER = PHOSPHORS["amber"]
P, P_BRIGHT, P_DIM, BORDER = _AMBER["P"], _AMBER["P_BRIGHT"], _AMBER["P_DIM"], _AMBER["BORDER"]

import queue as _queue_mod
_log_queue = _queue_mod.Queue()  # replaced by init_logging() at startup


TREES    = ["Precision", "Domination", "Sorcery", "Resolve", "Inspiration"]
KEYSTONES = {
    "Precision":   ["Press the Attack", "Lethal Tempo", "Fleet Footwork", "Conqueror"],
    "Domination":  ["Electrocute", "Predator", "Dark Harvest", "Hail of Blades"],
    "Sorcery":     ["Summon Aery", "Arcane Comet", "Phase Rush"],
    "Resolve":     ["Grasp of the Undying", "Aftershock", "Guardian"],
    "Inspiration": ["Glacial Augment", "First Strike", "Unsealed Spellbook"],
}
ROLES = ["auto", "top", "jungle", "mid", "bot", "support"]

SUMMONER_SPELLS = {
    "— (use u.gg default)": 0,
    "Flash":    4,
    "Ignite":   14,
    "Exhaust":  3,
    "Barrier":  21,
    "Heal":     7,
    "Ghost":    6,
    "Teleport": 12,
    "Cleanse":  1,
    "Smite":    11,
    "Clarity":  13,
}
_SPELL_ID_TO_NAME = {v: k for k, v in SUMMONER_SPELLS.items() if v != 0}


def _spell_label(spell1: int, spell2: int) -> str:
    """'FLASH / HEAL' from two summoner-spell IDs; 'u.gg default' when unset."""
    if not spell1 and not spell2:
        return "u.gg default"
    n1 = _SPELL_ID_TO_NAME.get(spell1, "?") if spell1 else "—"
    n2 = _SPELL_ID_TO_NAME.get(spell2, "?") if spell2 else "—"
    return f"{n1} / {n2}".upper()


def _hex_to_bgr(hex_color: str) -> int:
    """'#rrggbb' → DWM COLORREF (0x00bbggrr)."""
    h = hex_color.lstrip("#")
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    return r | (g << 8) | (b << 16)


def make_btn(parent, text, cmd, bg=BORDER, hov=P_DIM, fg=P_DIM, **kw):
    """Legacy flat button — used only by the static-amber Debug console."""
    b = tk.Label(parent, text=text, font=("Segoe UI", 9),
                 bg=bg, fg=fg, padx=10, pady=5, cursor="hand2", **kw)
    b.bind("<Button-1>", lambda e: cmd())
    b.bind("<Enter>",    lambda e: b.configure(bg=hov))
    b.bind("<Leave>",    lambda e: b.configure(bg=bg))
    return b


# ── Theme engine ─────────────────────────────────────────────────────────────
class Theme:
    """Mutable palette + a registry of (widget, painter) pairs.

    Each themed widget is created through a factory that records a painter
    closure describing its colours in terms of the *current* palette. switch()
    swaps the palette and re-runs every painter, so amber→green→ice re-themes
    the whole UI live. Painters that hit a destroyed widget are pruned.
    """

    def __init__(self, name="amber"):
        self._painters: list = []
        self.BG, self.DANGER = BG, DANGER
        self.set(name)

    def set(self, name):
        pal = PHOSPHORS.get(name, PHOSPHORS["amber"])
        self.name = name if name in PHOSPHORS else "amber"
        self.P, self.PB = pal["P"], pal["P_BRIGHT"]
        self.PD, self.BD = pal["P_DIM"], pal["BORDER"]

    def track(self, widget, painter):
        self._painters.append((widget, painter))
        painter(self)
        return widget

    def switch(self, name):
        self.set(name)
        alive = []
        for w, painter in self._painters:
            try:
                painter(self); alive.append((w, painter))
            except tk.TclError:
                pass  # widget destroyed since last paint — drop it
            except Exception:
                # A buggy painter must not abort the whole re-theme or strand
                # the registry in its pre-switch state; keep the widget and
                # surface the error rather than half-applying the palette.
                import traceback; traceback.print_exc()
                alive.append((w, painter))
        self._painters = alive

    # ── widget factories ──
    def _fg(self, kind):
        return {"body": self.P, "bright": self.PB, "dim": self.PD,
                "danger": self.DANGER}.get(kind, self.P)

    def label(self, parent, text="", kind="body", font=None, **kw):
        w = tk.Label(parent, text=text, font=font or F(14), **kw)
        return self.track(w, lambda t: w.configure(bg=t.BG, fg=t._fg(kind)))

    def frame(self, parent, **kw):
        w = tk.Frame(parent, **kw)
        return self.track(w, lambda t: w.configure(bg=t.BG))

    def border(self, parent, **kw):
        w = tk.Frame(parent, highlightthickness=1, **kw)
        return self.track(w, lambda t: w.configure(
            bg=t.BG, highlightbackground=t.BD, highlightcolor=t.BD))

    def rule(self, parent, **kw):
        w = tk.Frame(parent, height=1, **kw)
        return self.track(w, lambda t: w.configure(bg=t.BD))

    def entry(self, parent, textvariable, width=14, font=None):
        w = tk.Entry(parent, textvariable=textvariable, relief="flat",
                     font=font or F(14), width=width, insertwidth=2)
        return self.track(w, lambda t: w.configure(
            bg=t.BG, fg=t.P, insertbackground=t.P, disabledbackground=t.BG,
            highlightthickness=1, highlightbackground=t.BD, highlightcolor=t.P))


class OverrideEditorPage:
    """Champion override editor, rendered into an existing parent frame.

    Phosphor-styled from a palette snapshot taken at construction time. It's
    rebuilt on every open, so it always matches the active phosphor; it just
    won't live-recolor if the theme is switched while it's open (rare).
    """

    def __init__(self, parent, overrides, theme, champ="", on_save=None, on_back=None, lcu=None):
        self.parent    = parent
        self.overrides = overrides
        self.on_save   = on_save
        self.on_back   = on_back
        self._lcu      = lcu
        self._champ    = champ
        # Palette snapshot — plain colour strings, used directly below.
        self.P, self.PB, self.PD, self.BD = theme.P, theme.PB, theme.PD, theme.BD
        existing = overrides.get(champ) or {}
        self._imported_page_name = existing.get("page_name", "")

        from item_builder import normalize_build
        self._items_build = normalize_build(existing.get("items_build", {}))

        self._build_ui(existing, champ)

    def _build_ui(self, existing: dict, champ: str):
        p = self.parent
        P, PB, PD, BD = self.P, self.PB, self.PD, self.BD

        # ── Header ────────────────────────────────────────────────────────────
        hdr = tk.Frame(p, bg=BG, pady=8); hdr.pack(fill="x")
        back = tk.Label(hdr, text="< back", font=F(15), bg=BG, fg=PD, cursor="hand2", padx=10)
        back.pack(side="left")
        back.bind("<Button-1>", lambda e: self._do_back())
        back.bind("<Enter>",    lambda e: back.configure(fg=PB))
        back.bind("<Leave>",    lambda e: back.configure(fg=PD))
        title = "edit override" if champ else "inscribe override"
        tk.Label(hdr, text=f"~/{title}", font=F(20), bg=BG, fg=PB).pack(side="left", padx=6)
        tk.Frame(p, bg=BD, height=1).pack(fill="x")

        # ── Scrollable content ────────────────────────────────────────────────
        wrap = tk.Frame(p, bg=BG); wrap.pack(fill="both", expand=True)
        canvas = tk.Canvas(wrap, bg=BG, highlightthickness=0)
        vsb = tk.Scrollbar(wrap, orient="vertical", command=canvas.yview, bg=BG)
        canvas.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)
        cf = tk.Frame(canvas, bg=BG)
        _cw = canvas.create_window((0, 0), window=cf, anchor="nw")
        cf.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda e: canvas.itemconfig(_cw, width=e.width))
        canvas.bind_all("<MouseWheel>",
                        lambda e: canvas.yview_scroll(-1 * (e.delta // 120), "units"))

        def row(lbl_text):
            tk.Label(cf, text=lbl_text, font=F(15), bg=BG, fg=PD,
                     anchor="w").pack(fill="x", padx=24, pady=(8, 0))

        def entry(default=""):
            v = tk.StringVar(value=default)
            tk.Entry(cf, textvariable=v, bg=BG, fg=PB, insertbackground=P,
                     relief="flat", highlightthickness=1, highlightbackground=BD,
                     highlightcolor=P, font=F(15)).pack(fill="x", padx=24, pady=(2, 0))
            return v

        def combo(vals, default=""):
            v = tk.StringVar(value=default or vals[0])
            ttk.Combobox(cf, textvariable=v, values=vals, state="readonly",
                         style="Phos.TCombobox", font=F(13)).pack(fill="x", padx=24, pady=(2, 0))
            return v

        row("champion name:");       self.champ_v     = entry(champ)
        row("role:");                self.role_v      = combo(ROLES, existing.get("role", "auto"))
        row("primary rune tree:");   self.primary_v   = combo(TREES, existing.get("primary_tree", "Precision"))
        row("keystone:");            self.keystone_v  = combo(
            KEYSTONES.get(self.primary_v.get(), []), existing.get("keystone", ""))
        row("secondary rune tree:"); self.secondary_v = combo(TREES, existing.get("secondary_tree", "Domination"))
        row("full rune IDs (optional, 9 comma-sep ints):")
        self.runes_v = entry(",".join(str(r) for r in existing.get("rune_ids", [])))

        # Item build row
        row("item build:")
        ibf = tk.Frame(cf, bg=BG); ibf.pack(fill="x", padx=24, pady=(2, 0))
        self._items_summary_lbl = tk.Label(ibf, text=self._items_build_summary(),
                                           font=F(11, fine=True), bg=BG, fg=PD, anchor="w")
        self._items_summary_lbl.pack(side="left", fill="x", expand=True)
        eb = tk.Label(ibf, text="[ edit build ]", font=F(14), bg=BG, fg=P, cursor="hand2")
        eb.bind("<Button-1>", lambda e: self._open_build_editor())
        eb.bind("<Enter>", lambda e: eb.configure(fg=PB))
        eb.bind("<Leave>", lambda e: eb.configure(fg=P))
        eb.pack(side="right")

        row("note (optional):");     self.note_v = entry(existing.get("note", ""))

        # Summoner spells
        spell_names = list(SUMMONER_SPELLS.keys())
        def spell_name_from_id(sid):
            for name, i in SUMMONER_SPELLS.items():
                if i == sid: return name
            return "— (use u.gg default)"

        row("summoner spell 1:")
        self.spell1_v = tk.StringVar(value=spell_name_from_id(existing.get("spell1", 0)))
        ttk.Combobox(cf, textvariable=self.spell1_v, values=spell_names, state="readonly",
                     style="Phos.TCombobox", font=F(13)).pack(fill="x", padx=24, pady=(2, 0))
        row("summoner spell 2:")
        self.spell2_v = tk.StringVar(value=spell_name_from_id(existing.get("spell2", 0)))
        ttk.Combobox(cf, textvariable=self.spell2_v, values=spell_names, state="readonly",
                     style="Phos.TCombobox", font=F(13)).pack(fill="x", padx=24, pady=(2, 0))

        import_f = tk.Frame(cf, bg=BG); import_f.pack(fill="x", padx=24, pady=(10, 0))
        ib = tk.Label(import_f, text="[ import active rune page from client ]",
                      font=F(14), bg=BG, fg=P, cursor="hand2")
        ib.bind("<Button-1>", lambda e: self._import_from_client())
        ib.bind("<Enter>", lambda e: ib.configure(fg=PB))
        ib.bind("<Leave>", lambda e: ib.configure(fg=P))
        ib.pack(side="left")
        self._import_status = tk.Label(import_f, text="", font=F(11, fine=True), bg=BG, fg=PD)
        self._import_status.pack(side="left", padx=(8, 0))

        tk.Frame(cf, bg=BG, height=12).pack()

        # ── Footer ────────────────────────────────────────────────────────────
        tk.Frame(p, bg=BD, height=1).pack(fill="x")
        footer = tk.Frame(p, bg=BG, pady=10); footer.pack(fill="x")
        for text, cmd, kind in (("[ :w  save ]", self._save, P),
                                ("[ :q  cancel ]", self._do_back, PD)):
            b = tk.Label(footer, text=text, font=F(16), bg=BG, fg=kind, cursor="hand2")
            b.bind("<Button-1>", lambda e, c=cmd: c())
            b.bind("<Enter>", lambda e, w=b: w.configure(bg=P, fg=BG))
            b.bind("<Leave>", lambda e, w=b, k=kind: w.configure(bg=BG, fg=k))
            b.pack(side="left", padx=(16, 6))

    # ── Actions ───────────────────────────────────────────────────────────────
    def _open_build_editor(self):
        from item_builder import BuildEditorWindow
        def _on_save(build):
            self._items_build = build
            self._items_summary_lbl.configure(text=self._items_build_summary())
        BuildEditorWindow(
            self.parent.winfo_toplevel(),
            self.champ_v.get().strip() or self._champ or "Champion",
            self.role_v.get() or "auto",
            self._items_build,
            _on_save,
        )

    def _import_from_client(self):
        try:
            from lcu import LCUClient, LCUConnectionError
            lcu = LCUClient(); lcu.connect()
            page = lcu.get_current_rune_page()
            if not page:
                self._import_status.configure(text="No rune page found.", fg=DANGER); return
            perk_ids     = page.get("selectedPerkIds", [])
            primary_id   = page.get("primaryStyleId", 0)
            secondary_id = page.get("subStyleId", 0)
            from lcu import RUNE_TREE_IDS, KEYSTONE_IDS
            id_to_tree = {v: k for k, v in RUNE_TREE_IDS.items()}
            id_to_ks   = {v: k for k, v in KEYSTONE_IDS.items()}
            self.primary_v.set(id_to_tree.get(primary_id, self.primary_v.get()))
            self.secondary_v.set(id_to_tree.get(secondary_id, self.secondary_v.get()))
            ks = id_to_ks.get(perk_ids[0], "") if perk_ids else ""
            if ks: self.keystone_v.set(ks)
            self.runes_v.set(",".join(str(p) for p in perk_ids))
            self._imported_page_name = page.get("name", "")
            detected = self._detect_champ_in_name(self._imported_page_name)
            if detected and not self.champ_v.get().strip():
                self.champ_v.set(detected)
            self._import_status.configure(
                text=f"✓ Imported '{self._imported_page_name or 'page'}'", fg=self.PB)
        except LCUConnectionError as ex:
            self._import_status.configure(text=f"✗ {ex}", fg=DANGER)
        except Exception:
            self._import_status.configure(
                text="✗ Couldn't read your rune page — is League open?", fg=DANGER)

    def _detect_champ_in_name(self, page_name: str) -> str:
        if not page_name: return ""
        name_lower = page_name.lower()
        champ_map = {}
        if self._lcu and self._lcu.connected:
            try: champ_map = self._lcu.get_champion_name_map()
            except Exception: pass
        for name in sorted(champ_map.values(), key=len, reverse=True):
            if name.lower() in name_lower: return name
        return ""

    def _items_build_summary(self) -> str:
        from item_builder import SLOT_DEFS as _SD
        parts = []
        for k, label in _SD:
            items = self._items_build.get(k, [])
            if items:
                names = ", ".join(i["name"] for i in items[:3])
                if len(items) > 3: names += "…"
                parts.append(f"{label}: {names}")
        return "  |  ".join(parts) if parts else "No custom build"

    def _save(self):
        champ = self.champ_v.get().strip()
        if not champ:
            messagebox.showerror("Error", "Enter a champion name."); return
        rids = []
        raw = self.runes_v.get().strip()
        if raw:
            try:
                rids = [int(x.strip()) for x in raw.split(",") if x.strip()]
            except ValueError:
                messagebox.showerror("Error", "Rune IDs must be integers."); return
        self.overrides.set(champ, {
            "role": self.role_v.get(), "primary_tree": self.primary_v.get(),
            "keystone": self.keystone_v.get(), "secondary_tree": self.secondary_v.get(),
            "rune_ids": rids, "note": self.note_v.get().strip(),
            "page_name": self._imported_page_name,
            "spell1": SUMMONER_SPELLS.get(self.spell1_v.get(), 0),
            "spell2": SUMMONER_SPELLS.get(self.spell2_v.get(), 0),
            "items_build": self._items_build,
        })
        if self.on_save: self.on_save()
        self._do_back()

    def _do_back(self):
        if self.on_back: self.on_back()


class RuneSyncApp:
    def __init__(self, root):
        self.root = root
        self.theme = Theme(name="amber")  # real value loaded from settings below
        root.title("DAEMON"); root.geometry("880x720")
        root.resizable(False, False); root.configure(bg=BG)
        try:
            root.iconbitmap(os.path.join(_BASE_DIR, "icon.ico"))
        except Exception:
            pass

        self.lcu       = LCUClient()
        self.overrides = OverrideManager()
        ugg_api.SERVER_URL = self.overrides.settings.get("server_url", ugg_api.SERVER_URL)
        self.theme.set(self.overrides.settings.get("phosphor", "amber"))
        self.ugg       = UGGClient()
        self.monitor   = None
        self.running   = False
        self._connect_lock = threading.Lock()
        self._connecting   = False
        # Game overlay state
        self._game_champ: str       = ""
        self._game_enemy: str       = ""
        self._game_role: str        = ""
        self._in_game_overlay: bool = False
        self._game_frame            = None
        self._game_log_widget       = None
        self._game_match_label      = None
        self._game_wr_label         = None
        self._game_winrate: str     = ""
        self._game_wr_color: str    = self.theme.P
        self._log_buffer: list[tuple[str, str]] = []
        self._log_queue = _log_queue
        # Screen routing
        self._screens: dict = {}
        self._current_frame = None
        self._screen = "monitor"
        self._sel_build = 0
        self._build_champs: list = []
        self._status_kind = "booting"      # booting|connecting|connected|monitoring|waiting
        self._panel_champ = None           # champ the CHAMPION panel currently shows
        self._override_frame = None        # transient editor frame (non-screen)
        self._last_items = None            # (items, is_custom) for re-render on theme switch

        self._build_ui()
        self.root.update_idletasks()
        self._apply_dark_titlebar()
        self._show_screen("monitor")
        self._blink_cursor()
        self._pulse_status()

        threading.Thread(target=self._load_bundle_with_status, daemon=True).start()
        threading.Thread(target=self._try_connect, daemon=True).start()

        if "--minimized" in sys.argv:
            self.root.after(50, self.root.withdraw)

        # ── System tray + League auto-detect ─────────────────────────────
        _icon_path = os.path.join(_BASE_DIR, "icon.ico")
        self._tray = TrayController(
            on_show=self._show_from_tray, on_quit=self._real_quit, icon_path=_icon_path)
        self._tray.start()
        self._league_poller = LeaguePoller(
            on_open=self._on_league_open, on_close=self._on_league_close)
        self._league_poller.start()
        self.root.protocol("WM_DELETE_WINDOW", self._hide_to_tray)

    # ── tray / lifecycle (unchanged logic) ───────────────────────────────────
    def _hide_to_tray(self):
        try:
            self.root.withdraw()
        except Exception:
            pass
        if getattr(self, "_tray", None) and self._tray.available():
            self._tray.notify(
                "DAEMON",
                "Still running in the system tray. Right-click the icon to quit.")

    def _show_from_tray(self):
        self.root.after(0, self._do_show_window)

    def _do_show_window(self):
        try:
            self.root.deiconify(); self.root.lift(); self.root.focus_force()
        except Exception:
            pass

    def _real_quit(self):
        try: self._league_poller.stop()
        except Exception: pass
        try: self._tray.stop()
        except Exception: pass
        self.root.after(0, self.root.destroy)

    def _on_league_open(self):
        self.root.after(0, self._do_show_window)
        if not self.lcu.connected:
            threading.Thread(target=self._try_connect, daemon=True).start()

    def _on_monitor_league_closed(self):
        if self._in_game_overlay:
            self._on_game_end()
        if self.monitor:
            self.monitor._in_game = False
        self._stop()
        self.lcu.connected = False
        self._set_status("waiting")
        self._emit("League client closed — waiting for it to reopen...", "warn")

    def _on_league_close(self):
        if self.running:
            self.root.after(0, self._stop)
        self.lcu.connected = False
        self.root.after(0, lambda: self._set_status("waiting"))

    def _apply_dark_titlebar(self):
        """Dark title bar + a warm border tinted to the active phosphor."""
        try:
            hwnd = ctypes.windll.user32.GetParent(self.root.winfo_id())
            DWMWA_USE_IMMERSIVE_DARK_MODE = 20
            val = ctypes.c_int(1)
            ctypes.windll.dwmapi.DwmSetWindowAttribute(
                hwnd, DWMWA_USE_IMMERSIVE_DARK_MODE,
                ctypes.byref(val), ctypes.sizeof(val))
            DWMWA_BORDER_COLOR = 34
            colorref = ctypes.c_uint32(_hex_to_bgr(self.theme.P))
            ctypes.windll.dwmapi.DwmSetWindowAttribute(
                hwnd, DWMWA_BORDER_COLOR,
                ctypes.byref(colorref), ctypes.sizeof(colorref))
        except Exception:
            pass

    # ── UI shell ──────────────────────────────────────────────────────────────
    def _build_ui(self):
        t = self.theme
        # ttk style for the readonly comboboxes in the override editor.
        st = ttk.Style(); st.theme_use("default")
        st.configure("Phos.TCombobox", fieldbackground=BG, background=BG,
                     foreground=t.PB, arrowcolor=t.P, bordercolor=t.BD,
                     relief="flat")
        st.map("Phos.TCombobox", fieldbackground=[("readonly", BG)],
               foreground=[("readonly", t.PB)])

        # ── Top bar: brand · menu · status ───────────────────────────────────
        top = t.frame(self.root); top.pack(fill="x", padx=16, pady=(11, 0))
        brand = t.frame(top); brand.pack(side="left")
        t.label(brand, text="DAEMON", kind="bright", font=F(24)).pack(side="left")
        t.label(brand, text="v1.0.0", kind="dim", font=F(9, fine=True)).pack(side="left", padx=(8, 0), anchor="s")

        menu = t.frame(top); menu.pack(side="left", expand=True)
        self._menu_items: dict = {}
        for key, text in (("monitor", "[1] MONITOR"), ("builds", "[2] BUILDS"),
                          ("settings", "[3] SETTINGS")):
            lbl = tk.Label(menu, text=text, font=F(15), cursor="hand2", padx=11, pady=1)
            lbl.bind("<Button-1>", lambda e, k=key: self._show_screen(k))
            lbl.pack(side="left", padx=3)
            self._menu_items[key] = lbl

        status = t.frame(top); status.pack(side="right")
        self._status_lbl = t.label(status, text="LEAGUE: BOOTING", kind="bright", font=F(15))
        self._status_lbl.pack(side="left")
        self._status_dot = t.label(status, text="●", kind="bright", font=F(14))
        self._status_dot.pack(side="left", padx=(8, 0))

        t.rule(self.root).pack(fill="x", padx=16, pady=(8, 0))

        # Transient banner (data-load / import status), packed on demand.
        self._banner = t.label(self.root, text="", font=F(13), anchor="center", pady=6)
        self._banner_after = None

        # ── Content area (screens get packed here) ───────────────────────────
        self._content = t.frame(self.root)
        self._content.pack(fill="both", expand=True, padx=16, pady=(8, 0))
        self._build_screen_monitor()
        self._build_screen_builds()
        self._build_screen_settings()

        # ── Command bar ──────────────────────────────────────────────────────
        t.rule(self.root).pack(fill="x", padx=16, pady=(8, 0))
        cmd = t.frame(self.root); cmd.pack(fill="x", padx=16, pady=(7, 10))
        left = t.frame(cmd); left.pack(side="left")
        t.label(left, text="daemon:~$", kind="bright", font=F(15)).pack(side="left")
        self._prompt_lbl = t.label(left, text="", kind="dim", font=F(15))
        self._prompt_lbl.pack(side="left", padx=(6, 0))
        self._cursor = t.label(left, text="█", kind="body", font=F(15))
        self._cursor.pack(side="left", padx=(2, 0))

        right = t.frame(cmd); right.pack(side="right")
        self._start_word = tk.StringVar(value="start")
        self._cmd_item(right, "[s]", self._start_word, self._toggle)
        self._cmd_item(right, "[g]", tk.StringVar(value="overlay"), self._toggle_overlay)
        self._cmd_item(right, "[q]", tk.StringVar(value="tray"), self._hide_to_tray)

        # ── Global keys ──────────────────────────────────────────────────────
        self.root.bind_all("<Key>", self._on_key)
        self.root.bind_all("<Control-Shift-D>", lambda e: self._toggle_debug())
        self._debug_built = False

    def _cmd_item(self, parent, key, word_var, cmd):
        """'[k] word' command-bar entry: bracket in P, word in P_DIM, click runs cmd."""
        t = self.theme
        box = t.frame(parent); box.pack(side="left", padx=(0, 16))
        kb = t.label(box, text=key, kind="body", font=F(15), cursor="hand2"); kb.pack(side="left")
        wl = t.label(box, textvariable=word_var, kind="dim", font=F(15), cursor="hand2")
        wl.pack(side="left", padx=(4, 0))
        for w in (kb, wl):
            w.bind("<Button-1>", lambda e: cmd())
            w.bind("<Enter>", lambda e: wl.configure(fg=t.PB))
            w.bind("<Leave>", lambda e: wl.configure(fg=t.PD))

    def _on_key(self, e):
        # Ignore shortcuts while typing in an input, or when Control is held
        # (so Ctrl+Shift+D opening the debug console doesn't also fire 'd').
        if e.state & 0x0004:  # Control mask
            return
        focus = self.root.focus_get()
        if isinstance(focus, (tk.Entry, ttk.Entry, ttk.Combobox, tk.Text)):
            return
        k = (e.keysym or "").lower()
        if k == "1": self._show_screen("monitor")
        elif k == "2": self._show_screen("builds")
        elif k == "3": self._show_screen("settings")
        elif k == "s": self._toggle()
        elif k == "g": self._toggle_overlay()
        elif k == "q": self._hide_to_tray()
        elif self._screen == "builds":
            if k in ("up", "down"):
                self._move_build_sel(-1 if k == "up" else 1)
            elif k == "a": self._add_ov()
            elif k == "e": self._edit_ov()
            elif k == "d": self._rm_ov()

    # ── screen routing ─────────────────────────────────────────────────────────
    _PROMPTS = {"monitor": "watch --champ-select", "builds": "edit builds.ledger",
                "settings": "vim daemon.conf", "debug": "tail -f runesync.log"}

    def _show_screen(self, key):
        if key not in self._screens or self._in_game_overlay:
            return
        # Navigating away from an open editor must destroy it, not just hide it
        # (it's a transient non-screen frame). _discard nulls _current_frame if
        # it was the editor, so the pack_forget below never hits a dead widget.
        self._discard_override_frame()
        if self._current_frame is not None:
            self._current_frame.pack_forget()
        self._screens[key].pack(fill="both", expand=True)
        self._current_frame = self._screens[key]
        self._screen = key
        self._refresh_menu()
        self._prompt_lbl.configure(text=self._PROMPTS.get(key, ""))

    def _discard_override_frame(self):
        """Destroy the transient override-editor frame if one is open."""
        if self._override_frame is not None:
            if self._current_frame is self._override_frame:
                self._current_frame = None
            self._override_frame.destroy()
            self._override_frame = None

    def _refresh_menu(self):
        t = self.theme
        for k, lbl in self._menu_items.items():
            if k == self._screen:
                lbl.configure(bg=t.P, fg=t.BG)
            else:
                lbl.configure(bg=t.BG, fg=t.PD)

    def _blink_cursor(self):
        try:
            cur = self._cursor.cget("fg")
            self._cursor.configure(fg=BG if cur != BG else self.theme.P)
        except Exception:
            pass
        self.root.after(530, self._blink_cursor)

    def _pulse_status(self):
        try:
            base = self._status_color(self._status_kind)
            cur = self._status_dot.cget("fg")
            self._status_dot.configure(fg=self.theme.PD if cur != self.theme.PD else base)
        except Exception:
            pass
        self.root.after(800, self._pulse_status)

    # ── panel helper ───────────────────────────────────────────────────────────
    def _panel(self, parent, title):
        """Bordered box with its title sitting on the top border (a notch).

        The notch label is a child of `cell` (not the bordered box), placed over
        the box's top edge so its BG fill masks the 1px border line — Tk clips
        children to their own widget, so the label must live one level up.
        Returns (cell, body, title_label).
        """
        t = self.theme
        cell = t.frame(parent)
        box = t.border(cell); box.pack(fill="both", expand=True, pady=(9, 0))
        body = t.frame(box); body.pack(fill="both", expand=True, padx=16, pady=(13, 12))
        tl = t.label(cell, text=title, kind="dim", font=F(13))
        tl.place(x=18, y=9, anchor="w")
        return cell, body, tl

    # ════════════════════════════ MONITOR ══════════════════════════════════════
    def _build_screen_monitor(self):
        t = self.theme
        f = t.frame(self._content); self._screens["monitor"] = f
        for c in (0, 1):
            f.columnconfigure(c, weight=1, uniform="mon")
        f.rowconfigure(2, weight=1)

        # CHAMPION
        cell, body, _ = self._panel(f, "CHAMPION")
        cell.grid(row=0, column=0, sticky="nsew", padx=(0, 8), pady=(0, 12))
        self._champ_name = t.label(body, text="—", kind="bright", font=F(46)); self._champ_name.pack(anchor="w")
        self._champ_sub = t.label(body, text="[ awaiting champ select ]", kind="dim", font=F(14))
        self._champ_sub.pack(anchor="w", pady=(6, 0))
        self._imported_tag = tk.Label(body, text=">> RUNES IMPORTED OK", font=F(13))
        t.track(self._imported_tag, lambda th: self._imported_tag.configure(bg=th.P, fg=th.BG))
        # hidden until an import lands
        self._imported_tag_packed = False

        # MATCHUP
        cell, body, self._matchup_title = self._panel(f, "MATCHUP // idle")
        cell.grid(row=0, column=1, sticky="nsew", padx=(8, 0), pady=(0, 12))
        self._wr_num = t.label(body, text="—", kind="bright", font=F(38)); self._wr_num.pack(anchor="w")
        self._wr_label = t.label(body, text="awaiting matchup", kind="dim", font=F(15))
        self._wr_label.pack(anchor="w")
        self._gauge = MatchupGauge(body, t); self._gauge.pack(fill="x", pady=(10, 0))
        self._wr_sample = t.label(body, text="", kind="dim", font=F(9, fine=True))
        self._wr_sample.pack(anchor="w", pady=(8, 0))
        # matchup override input
        ov = t.frame(body); ov.pack(fill="x", pady=(8, 0))
        t.label(ov, text="vs:", kind="dim", font=F(13)).pack(side="left")
        self.matchup_v = tk.StringVar()
        self.matchup_entry = t.entry(ov, self.matchup_v, width=12, font=F(13))
        self.matchup_entry.pack(side="left", padx=(5, 4))
        self.matchup_entry.bind("<Return>", lambda e: self._submit_matchup_override())
        lk = t.label(ov, text="[look up]", kind="body", font=F(13), cursor="hand2"); lk.pack(side="left")
        lk.bind("<Button-1>", lambda e: self._submit_matchup_override())

        # RUNE PAGE
        cell, body, _ = self._panel(f, "RUNE PAGE")
        cell.grid(row=1, column=0, sticky="nsew", padx=(0, 8), pady=(0, 12))
        self._rune_rows = {}
        for keyname in ("keystone", "primary", "secondary", "summoners"):
            self._rune_rows[keyname] = self._leader_row(body, keyname, "—")

        # BUILD
        cell, body, self._build_title = self._panel(f, "BUILD // idle")
        cell.grid(row=1, column=1, sticky="nsew", padx=(8, 0), pady=(0, 12))
        self._build_body = body
        self._build_placeholder = t.label(body, text="—", kind="dim", font=F(15))
        self._build_placeholder.pack(anchor="w")
        self._build_item_lbls: list = []

        # DISPATCH LOG (full width)
        cell, body, _ = self._panel(f, "DISPATCH LOG")
        cell.grid(row=2, column=0, columnspan=2, sticky="nsew")
        # notch actions on the right edge of the top border
        for i, (text, cmd) in enumerate((("clear", self._clear), ("reimport", self._reimport))):
            a = t.label(cell, text=text, kind="dim", font=F(13), cursor="hand2")
            a.bind("<Button-1>", lambda e, c=cmd: c())
            a.bind("<Enter>", lambda e, w=a: w.configure(fg=self.theme.PB))
            a.bind("<Leave>", lambda e, w=a: w.configure(fg=self.theme.PD))
            a.place(relx=1.0, x=-18 - i * 90, y=9, anchor="e")
        lf = t.frame(body); lf.pack(fill="both", expand=True)
        self.log = tk.Text(lf, font=F(11, fine=True), relief="flat",
                           state="disabled", wrap="word", bd=0, padx=4, pady=2)
        t.track(self.log, lambda th: self.log.configure(bg=th.BG, fg=th.P))
        sb = tk.Scrollbar(lf, command=self.log.yview, bg=BG)
        self.log.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y"); self.log.pack(fill="both", expand=True)
        self._recolor_log_tags()

    def _leader_row(self, parent, key, value):
        """key ........... VALUE  — dotted leader between label and value."""
        t = self.theme
        r = t.frame(parent); r.pack(fill="x", pady=1)
        t.label(r, text=key, kind="body", font=F(14)).pack(side="left")
        val = t.label(r, text=value, kind="bright", font=F(14)); val.pack(side="right")
        dots = t.label(r, text="·" * 40, kind="dim", font=F(14), anchor="w")
        dots.pack(side="left", fill="x", expand=True, padx=4)
        return val

    # ════════════════════════════ BUILDS ═══════════════════════════════════════
    def _build_screen_builds(self):
        t = self.theme
        f = t.frame(self._content); self._screens["builds"] = f

        head = t.frame(f); head.pack(fill="x")
        t.label(head, text="~/builds.ledger", kind="bright", font=F(28)).pack(side="left")
        self._builds_count = t.label(head, text="", kind="dim", font=F(15)); self._builds_count.pack(side="right")
        t.label(f, text="champions here import YOUR runes instead of the crowd's top build.",
                kind="dim", font=F(14)).pack(anchor="w", pady=(2, 0))

        table = t.border(f); table.pack(fill="both", expand=True, pady=(16, 0))
        hdr = t.frame(table); hdr.pack(fill="x", padx=16, pady=(9, 8))
        self._BUILD_COLS = (("№", 4), ("CHAMPION", 15), ("ROLE", 9), ("PATH", 27), ("SUMMONERS", 18))
        for name, w in self._BUILD_COLS:
            t.label(hdr, text=name, kind="dim", font=F(11, fine=True), width=w, anchor="w").pack(side="left")
        t.rule(table).pack(fill="x")
        self._builds_rows_frame = t.frame(table); self._builds_rows_frame.pack(fill="x")

        hints = t.frame(f); hints.pack(fill="x", pady=(14, 0))
        for key, word, cmd in (("[a]", "inscribe", self._add_ov), ("[e]", "edit", self._edit_ov),
                               ("[d]", "delete", self._rm_ov), ("[↑↓]", "select", None)):
            box = t.frame(hints); box.pack(side="left", padx=(0, 22))
            kb = t.label(box, text=key, kind="body", font=F(16)); kb.pack(side="left")
            wl = t.label(box, text=word, kind="dim", font=F(16)); wl.pack(side="left", padx=(5, 0))
            if cmd:
                for wdg in (kb, wl):
                    wdg.configure(cursor="hand2")
                    wdg.bind("<Button-1>", lambda e, c=cmd: c())
        self._refresh_builds()

    def _refresh_builds(self):
        t = self.theme
        for c in self._builds_rows_frame.winfo_children():
            c.destroy()
        self._build_champs = []
        items = list(self.overrides.all().items())
        for i, (champ, d) in enumerate(items):
            self._build_champs.append(champ)
            selected = (i == self._sel_build)
            rbg = t.P if selected else t.BG
            rfg = t.BG if selected else t.PB
            num = t.BG if selected else t.PD
            row = tk.Frame(self._builds_rows_frame, bg=rbg)
            row.pack(fill="x")
            path = f"{d.get('primary_tree', '—')} × {d.get('secondary_tree', '—')}"
            spells = _spell_label(d.get("spell1", 0), d.get("spell2", 0))
            cells = (f"{i + 1:02d}", champ, d.get("role", "auto"), path, spells)
            for (name, w), val in zip(self._BUILD_COLS, cells):
                fg = num if name == "№" else rfg
                fs = F(22) if name in ("№", "CHAMPION", "ROLE") else F(17)
                lbl = tk.Label(row, text=val, bg=rbg, fg=fg, font=fs, width=w, anchor="w")
                lbl.pack(side="left")
                lbl.bind("<Button-1>", lambda e, idx=i: self._select_build(idx))
            row.bind("<Button-1>", lambda e, idx=i: self._select_build(idx))
        if not items:
            tk.Label(self._builds_rows_frame, text="  no custom builds yet — press [a] to inscribe one",
                     bg=t.BG, fg=t.PD, font=F(16), anchor="w").pack(fill="x", pady=8)
        self._builds_count.configure(text=f"{len(items)} champions · everyone else follows u.gg")

    def _select_build(self, idx):
        self._sel_build = idx
        self._refresh_builds()

    def _move_build_sel(self, delta):
        if not self._build_champs:
            return
        self._sel_build = max(0, min(len(self._build_champs) - 1, self._sel_build + delta))
        self._refresh_builds()

    # ════════════════════════════ SETTINGS ═════════════════════════════════════
    def _build_screen_settings(self):
        t = self.theme
        f = t.frame(self._content); self._screens["settings"] = f

        head = t.frame(f); head.pack(fill="x")
        t.label(head, text="~/daemon.conf", kind="bright", font=F(28)).pack(side="left")
        t.label(head, text="%APPDATA%\\RuneSync\\overrides.json", kind="dim",
                font=F(10, fine=True)).pack(side="right", anchor="s")

        body = t.frame(f); body.pack(fill="x", pady=(14, 0), anchor="w")

        self.rank_v   = tk.StringVar(value=self.overrides.settings.get("rank", "Platinum+"))
        self.region_v = tk.StringVar(value=self.overrides.settings.get("region", "World"))
        self.arole_v  = tk.BooleanVar(value=self.overrides.settings.get("auto_role", True))
        self.trig_v   = tk.StringVar(value=self.overrides.settings.get("trigger", "hover"))
        self.autostart_v = tk.BooleanVar(value=is_autostart_enabled())
        # hidden dev setting — points at a local scraper instead of the bundle
        self.server_url_v = tk.StringVar(
            value=self.overrides.settings.get("server_url", ugg_api.SERVER_URL))

        def comment(text):
            c = t.label(body, text=text, kind="dim", font=F(10, fine=True), anchor="w")
            c.pack(fill="x", pady=(10, 2))
            t.rule(body).pack(fill="x", pady=(0, 5))

        def conf_row():
            r = t.frame(body); r.pack(fill="x", anchor="w", pady=3)
            return r

        def key_eq(parent, name):
            t.label(parent, text=name, kind="dim", font=F(16), width=20, anchor="w").pack(side="left")
            t.label(parent, text="=", kind="body", font=F(16)).pack(side="left", padx=(0, 8))

        # where the numbers come from
        comment("# where the numbers come from")
        r = conf_row(); key_eq(r, "rank_filter")
        self._dropdown(r, self.rank_v, ["Iron+", "Bronze+", "Silver+", "Gold+", "Platinum+",
                                        "Emerald+", "Diamond+", "Master+"])
        r = conf_row(); key_eq(r, "region")
        self._dropdown(r, self.region_v, ["World", "NA", "EUW", "EUNE", "KR", "BR", "JP",
                                          "OCE", "LAS", "LAN", "TR", "RU"])

        # how it behaves
        comment("# how it behaves")
        r = conf_row(); key_eq(r, "auto_detect_role")
        self._checkbox(r, self.arole_v)
        r = conf_row(); key_eq(r, "import_on")
        self._radio(r, self.trig_v, "hover", "hover")
        self._radio(r, self.trig_v, "lock", "lock-in", pad=14)

        # appearance
        comment("# appearance")
        r = conf_row(); key_eq(r, "phosphor")
        self._phosphor_val = t.label(r, text=f"{self.theme.name} ▾", kind="bright",
                                     font=F(16), cursor="hand2")
        self._phosphor_val.pack(side="left")
        self._attach_menu(self._phosphor_val, self.theme.name,
                          list(PHOSPHORS.keys()), self._on_pick_phosphor)

        # system
        comment("# system")
        r = conf_row(); key_eq(r, "start_with_windows")
        self._checkbox(r, self.autostart_v, on_toggle=self._toggle_autostart)
        t.label(r, text="# lives in tray, wakes when league opens", kind="dim",
                font=F(11, fine=True)).pack(side="left", padx=(8, 0))

        # save
        save = t.frame(body); save.pack(fill="x", pady=(18, 0), anchor="w")
        sb = t.label(save, text=":w  write config", kind="body", font=F(16), cursor="hand2")
        sb.configure(highlightthickness=1, highlightbackground=self.theme.P, padx=14, pady=3)
        t.track(sb, lambda th: sb.configure(highlightbackground=th.P, highlightcolor=th.P))
        sb.bind("<Button-1>", lambda e: self._save_settings())
        sb.bind("<Enter>", lambda e: sb.configure(bg=self.theme.P, fg=self.theme.BG))
        sb.bind("<Leave>", lambda e: sb.configure(bg=self.theme.BG, fg=self.theme.P))
        sb.pack(side="left")
        self._settings_saved_lbl = t.label(save, text="", kind="bright", font=F(15))
        self._settings_saved_lbl.pack(side="left", padx=(14, 0))
        self._settings_saved_after_id = None

        t.rule(f).pack(fill="x", pady=(24, 8))
        t.label(f, text="data aggregated with attribution from lolalytics & u.gg · not endorsed by riot games",
                kind="dim", font=F(9, fine=True)).pack(anchor="w")
        t.label(f, text="league of legends © riot games, inc. · daemon is gpl-3.0",
                kind="dim", font=F(9, fine=True)).pack(anchor="w")

    def _dropdown(self, parent, var, options):
        lbl = self.theme.label(parent, text=f"{var.get()} ▾", kind="bright",
                               font=F(16), cursor="hand2")
        lbl.pack(side="left")
        self._attach_menu(lbl, var.get(), options,
                          lambda o: (var.set(o), lbl.configure(text=f"{o} ▾")))

    def _attach_menu(self, anchor, current, options, on_pick):
        m = tk.Menu(anchor, tearoff=0)
        t = self.theme
        m.configure(bg=t.BG, fg=t.P, activebackground=t.P, activeforeground=t.BG,
                    relief="flat", bd=0, font=F(14))
        for opt in options:
            m.add_command(label=opt, command=lambda o=opt: on_pick(o))
        anchor.bind("<Button-1>", lambda e: m.tk_popup(e.x_root, e.y_root))

    def _checkbox(self, parent, var, on_toggle=None):
        box = self.theme.label(parent, text=f"[{'x' if var.get() else ' '}]",
                               kind="bright", font=F(16), cursor="hand2")
        def toggle(_e=None):
            new = not var.get()
            if on_toggle is not None and on_toggle(new) is False:
                return  # toggle vetoed (e.g. autostart write failed)
            var.set(new)
            box.configure(text=f"[{'x' if var.get() else ' '}]")
        box.bind("<Button-1>", toggle)
        box.pack(side="left")

    def _radio(self, parent, var, value, label, pad=0):
        t = self.theme
        cell = t.frame(parent); cell.pack(side="left", padx=(pad, 0))
        mark = t.label(cell, text=f"({'•' if var.get() == value else ' '})",
                       kind="bright", font=F(16), cursor="hand2"); mark.pack(side="left")
        txt = t.label(cell, text=label, kind="body", font=F(16), cursor="hand2")
        txt.pack(side="left", padx=(2, 0))
        # remember every radio mark so siblings refresh together
        if not hasattr(self, "_radio_marks"):
            self._radio_marks = []
        self._radio_marks.append((var, value, mark))
        def pick(_e=None):
            var.set(value)
            for v, val, m in self._radio_marks:
                m.configure(text=f"({'•' if v.get() == val else ' '})")
        for w in (mark, txt):
            w.bind("<Button-1>", pick)

    def _toggle_autostart(self, new):
        """Returns False to veto the checkbox flip if the registry write fails."""
        return bool(set_autostart(new))

    def _on_pick_phosphor(self, name):
        self._phosphor_val.configure(text=f"{name} ▾")
        self._switch_theme(name)

    def _switch_theme(self, name):
        self.theme.switch(name)
        self._refresh_menu()
        self._refresh_builds()
        self._recolor_log_tags()
        self._set_status(self._status_kind)          # recompute dot hue for new palette
        self._apply_dark_titlebar()
        if hasattr(self, "_gauge"):
            self._gauge.repaint(self.theme)
        if self._last_items and self._last_items[0]:  # re-render plain item rows
            self._apply_item_build(*self._last_items)

    # ════════════════════════════ DEBUG (lazy, static amber) ═══════════════════
    def _toggle_debug(self):
        if not self._debug_built:
            self._build_screen_debug()
            self._debug_built = True
        self._show_screen("debug")

    def _build_screen_debug(self):
        f = tk.Frame(self._content, bg=BG); self._screens["debug"] = f

        hdr = tk.Frame(f, bg=BG); hdr.pack(fill="x", pady=(0, 4))
        tk.Label(hdr, text="debug console", font=F(20), bg=BG, fg=P).pack(side="left")
        make_btn(hdr, "Clear",    self._debug_clear, BG, BORDER, fg=P_DIM).pack(side="right")
        make_btn(hdr, "Copy All", self._debug_copy,  BG, BORDER, fg=P_DIM).pack(side="right", padx=(0, 6))

        flt = tk.Frame(f, bg=BG); flt.pack(fill="x", pady=(0, 2))
        tk.Label(flt, text="show:", font=F(10, fine=True), bg=BG, fg=P_DIM).pack(side="left")
        self._debug_filters: dict = {}
        for tag_label in ("[ugg]", "[lcu]", "[monitor]", "[unknown]"):
            v = tk.BooleanVar(value=True)
            self._debug_filters[tag_label] = v
            tk.Checkbutton(flt, text=tag_label, variable=v, bg=BG, activebackground=BG,
                           selectcolor=BG, fg=P_DIM, font=F(9, fine=True),
                           command=self._debug_refilter).pack(side="left", padx=(6, 0))

        sev_f = tk.Frame(f, bg=BG); sev_f.pack(fill="x", pady=(0, 4))
        tk.Label(sev_f, text="level:", font=F(10, fine=True), bg=BG, fg=P_DIM).pack(side="left")
        self._debug_min_level = tk.StringVar(value="debug")
        for lvl in ("debug", "info", "warn", "error"):
            tk.Radiobutton(sev_f, text=lvl, variable=self._debug_min_level, value=lvl,
                           bg=BG, activebackground=BG, selectcolor=BG, fg=P_DIM,
                           font=F(9, fine=True), command=self._debug_refilter).pack(side="left", padx=(6, 0))

        lf = tk.Frame(f, bg=BG); lf.pack(fill="both", expand=True, pady=(0, 4))
        self.debug_log = tk.Text(lf, bg=BG, fg=P, font=F(9, fine=True), relief="flat",
                                 state="disabled", wrap="none", bd=0)
        xsb = tk.Scrollbar(lf, orient="horizontal", command=self.debug_log.xview, bg=BG)
        ysb = tk.Scrollbar(lf, command=self.debug_log.yview, bg=BG)
        self.debug_log.configure(xscrollcommand=xsb.set, yscrollcommand=ysb.set)
        xsb.pack(side="bottom", fill="x"); ysb.pack(side="right", fill="y")
        self.debug_log.pack(fill="both", expand=True)

        for name, col in (("t_debug", P_DIM), ("t_info", P), ("t_warn", P_BRIGHT),
                          ("t_error", DANGER), ("t_claude", P_BRIGHT), ("t_ugg", P_BRIGHT),
                          ("t_lcu", P_BRIGHT), ("t_monitor", P), ("t_merge", P_BRIGHT),
                          ("t_unknown", P_DIM), ("t_ts", P_DIM), ("t_tag", P_DIM)):
            self.debug_log.tag_config(name, foreground=col)

        self._debug_count_lbl = tk.Label(f, text="0 entries", font=F(9, fine=True), bg=BG, fg=P_DIM)
        self._debug_count_lbl.pack(side="bottom", anchor="e", pady=(0, 6))

        self._debug_records: list = []
        self._debug_entry_count = 0
        self._debug_drain()

    def _debug_drain(self):
        batch = []
        try:
            while True:
                batch.append(self._log_queue.get_nowait())
        except Exception:
            pass
        if batch:
            _LEVEL_ORDER = {"debug": 0, "info": 1, "warn": 2, "error": 3}
            min_lvl = _LEVEL_ORDER.get(self._debug_min_level.get(), 0)
            self.debug_log.configure(state="normal")
            added = 0
            for record in batch:
                import datetime as _dt
                tag = getattr(record, "rs_tag",      "[unknown]")
                sev = getattr(record, "rs_severity",  "debug")
                ts  = _dt.datetime.fromtimestamp(record.created).strftime("%H:%M:%S")
                msg = record.getMessage()
                self._debug_records.append({"ts": ts, "tag": tag, "sev": sev, "msg": msg})
                if not self._debug_filters.get(tag, tk.BooleanVar(value=True)).get():
                    continue
                if _LEVEL_ORDER.get(sev, 0) < min_lvl:
                    continue
                tag_colour = f"t_{tag[1:-1]}" if tag != "[unknown]" else "t_unknown"
                self.debug_log.insert("end", ts,               "t_ts")
                self.debug_log.insert("end", f"  {tag:<12}",   tag_colour)
                self.debug_log.insert("end", f"  {sev.upper():<6}  ", f"t_{sev}")
                self.debug_log.insert("end", msg + "\n",        f"t_{sev}")
                added += 1
            try:
                line_count = int(self.debug_log.index("end-1c").split(".")[0])
                if line_count > 2100:
                    self.debug_log.delete("1.0", f"{line_count - 2000}.0")
            except Exception:
                pass
            if added:
                self.debug_log.see("end")
                self._debug_entry_count += added
                self._debug_count_lbl.configure(text=f"{self._debug_entry_count} entries")
            self.debug_log.configure(state="disabled")
        self.root.after(500, self._debug_drain)

    def _debug_clear(self):
        self._debug_records.clear()
        self._debug_entry_count = 0
        self.debug_log.configure(state="normal")
        self.debug_log.delete("1.0", "end")
        self.debug_log.configure(state="disabled")
        self._debug_count_lbl.configure(text="0 entries")

    def _debug_copy(self):
        lines = [f"{r['ts']}  {r['tag']:<12}  {r['sev'].upper():<6}  {r['msg']}"
                 for r in self._debug_records]
        self.root.clipboard_clear()
        self.root.clipboard_append("\n".join(lines))

    def _debug_refilter(self):
        _LEVEL_ORDER = {"debug": 0, "info": 1, "warn": 2, "error": 3}
        min_lvl = _LEVEL_ORDER.get(self._debug_min_level.get(), 0)
        self.debug_log.configure(state="normal")
        self.debug_log.delete("1.0", "end")
        count = 0
        for r in self._debug_records:
            tag = r["tag"]; sev = r["sev"]
            if not self._debug_filters.get(tag, tk.BooleanVar(value=True)).get():
                continue
            if _LEVEL_ORDER.get(sev, 0) < min_lvl:
                continue
            tag_colour = f"t_{tag[1:-1]}" if tag != "[unknown]" else "t_unknown"
            self.debug_log.insert("end", r["ts"],             "t_ts")
            self.debug_log.insert("end", f"  {tag:<12}",      tag_colour)
            self.debug_log.insert("end", f"  {sev.upper():<6}  ", f"t_{sev}")
            self.debug_log.insert("end", r["msg"] + "\n",      f"t_{sev}")
            count += 1
        self.debug_log.see("end")
        self.debug_log.configure(state="disabled")
        self._debug_count_lbl.configure(text=f"{count} entries (filtered)")

    # ── helpers ──────────────────────────────────────────────────────────────
    def _recolor_log_tags(self):
        t = self.theme
        m = {"info": t.P, "success": t.PB, "warn": t.PD, "error": t.DANGER, "champ": t.PB}
        for w in (getattr(self, "log", None), getattr(self, "_game_log_widget", None)):
            if w is not None:
                for tag, col in m.items():
                    try: w.tag_config(tag, foreground=col)
                    except Exception: pass

    def _emit(self, msg, tag="info"):
        self._log_buffer.append((msg, tag))
        try:
            print(msg, file=sys.stderr)
        except Exception:
            pass
        def _do():
            self.log.configure(state="normal")
            self.log.insert("end", msg + "\n", tag)
            self.log.see("end")
            self.log.configure(state="disabled")
            if self._in_game_overlay and self._game_log_widget:
                self._game_log_widget.configure(state="normal")
                self._game_log_widget.insert("end", msg + "\n", tag)
                self._game_log_widget.see("end")
                self._game_log_widget.configure(state="disabled")
        self.root.after(0, _do)

    def _clear(self):
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")

    def _status_color(self, kind):
        if kind == "waiting":
            return self.theme.DANGER
        if kind in ("connected", "monitoring"):
            return self.theme.PB
        return self.theme.PD  # booting / connecting

    def _set_status(self, kind):
        """Top-bar status. kind is one of booting|connecting|connected|
        monitoring|waiting — the single source for both label and dot colour,
        so theme switches and the pulse always recompute the right hue."""
        def _do():
            self._status_kind = kind
            self._status_lbl.configure(text="LEAGUE: " + kind.upper())
            self._status_dot.configure(fg=self._status_color(kind))
        self.root.after(0, _do)

    # ── notification banner ─────────────────────────────────────────────────
    def _show_banner(self, text, kind="success", auto_hide_ms=6000):
        t = self.theme
        fg = {"success": t.PB, "info": t.P, "warn": t.DANGER}.get(kind, t.P)
        def _do():
            self._banner.configure(text=text, fg=fg)
            if not self._banner.winfo_ismapped():
                self._banner.pack(fill="x", before=self._content)
            if self._banner_after is not None:
                try: self.root.after_cancel(self._banner_after)
                except Exception: pass
                self._banner_after = None
            if auto_hide_ms:
                self._banner_after = self.root.after(auto_hide_ms, self._hide_banner)
        self.root.after(0, _do)

    def _hide_banner(self):
        self._banner_after = None
        try:
            if self._banner.winfo_ismapped():
                self._banner.pack_forget()
        except Exception:
            pass

    def _load_bundle_with_status(self):
        self._show_banner("loading build data…", "info", auto_hide_ms=0)
        try:
            ok = ugg_api.init_bundle()
        except Exception:
            ok = False
        if ok:
            self.root.after(0, self._hide_banner)
        else:
            self._show_banner(
                "couldn't load build data — using fallback; some builds and "
                "winrates may be missing.", "warn", auto_hide_ms=0)

    def _on_import_success(self, champ: str):
        self._show_banner(f">> runes imported for {champ}", "success")
        def _do():
            self._panel_champ = champ  # the tag asserts THIS champ's runes are in
            if not self._imported_tag_packed:
                self._imported_tag.pack(anchor="w", pady=(14, 0))
                self._imported_tag_packed = True
        self.root.after(0, _do)

    def _hide_imported_tag(self):
        if self._imported_tag_packed:
            self._imported_tag.pack_forget()
            self._imported_tag_packed = False

    def _on_runes_imported(self, info: dict):
        """Marshal the rune-page info onto the Tk thread (called from monitor)."""
        self.root.after(0, self._apply_rune_page, info)

    def _apply_rune_page(self, info: dict):
        ks = (info.get("keystone") or "—").upper()
        self._rune_rows["keystone"].configure(text=ks)
        self._rune_rows["primary"].configure(text=info.get("primary") or "—")
        self._rune_rows["secondary"].configure(text=info.get("secondary") or "—")
        self._rune_rows["summoners"].configure(
            text=_spell_label(info.get("spell1", 0), info.get("spell2", 0)))

    # ── connect (unchanged logic) ──────────────────────────────────────────────
    def _try_connect(self):
        with self._connect_lock:
            if self.lcu.connected or self._connecting:
                return
            self._connecting = True
        try:
            import time
            self._emit("Connecting to League client...", "info")
            self._set_status("connecting")
            delay = 2
            MAX_ATTEMPTS = 8
            for attempt in range(1, MAX_ATTEMPTS + 1):
                try:
                    self.lcu.connect()
                    self._emit("✓ Connected to League Client", "success")
                    self._set_status("connected")
                    self.root.after(0, self._start)
                    return
                except LCUConnectionError:
                    if attempt < MAX_ATTEMPTS:
                        self._set_status("waiting")
                        time.sleep(delay)
                        delay = min(delay * 2, 10)
                    else:
                        self._emit("League client not detected — DAEMON will "
                                   "auto-connect when you open League.", "warn")
                        self._set_status("waiting")
        finally:
            with self._connect_lock:
                self._connecting = False

    # ── monitor ─────────────────────────────────────────────────────────────
    def _toggle(self):
        if not self.running: self._start()
        else:                self._stop()

    def _start(self):
        if not self.lcu.connected:
            messagebox.showerror("Not Connected",
                "League client not connected.\nOpen League and try again."); return
        self.running = True
        self._start_word.set("stop")
        self._set_status("monitoring")
        self._emit("──── Monitoring started ────", "warn")
        self.monitor = ChampSelectMonitor(
            lcu=self.lcu, ugg=self.ugg, overrides=self.overrides,
            on_log=self._emit, trigger=self.trig_v.get(),
            rank=self.rank_v.get(), region=self.region_v.get(),
            auto_role=self.arole_v.get(),
            on_game_start=lambda: self.root.after(0, self._on_game_start),
            on_game_end=lambda: self.root.after(0, self._on_game_end),
            on_league_closed=lambda: self.root.after(0, self._on_monitor_league_closed),
            on_matchup_winrate=self._on_matchup_winrate,
            on_item_build=self._on_item_build,
            on_import=self._on_import_success,
            on_runes_imported=self._on_runes_imported,
        )
        threading.Thread(target=self.monitor.run, daemon=True).start()

    def _stop(self):
        self.running = False
        if self.monitor: self.monitor.stop()
        self._start_word.set("start")
        self._set_status("connected" if self.lcu.connected else "waiting")
        self._emit("──── Monitoring stopped ────", "warn")

    def _reimport(self):
        if not self.monitor or not self.running:
            self._emit("Start monitoring first.", "warn")
            return
        champ = self.monitor._my_champ
        if not champ:
            self._emit("No champion detected yet — pick or hover a champion first.", "warn")
            return
        self._emit(f"Reimporting build for {champ}...", "info")
        session = self.lcu.get_champ_select_session()
        if not session:
            self._emit("Not in champion select.", "warn")
            return
        threading.Thread(
            target=self.monitor._import_runes, args=(champ, session), daemon=True).start()

    def _on_item_build(self, items: list, is_custom: bool):
        self.root.after(0, self._apply_item_build, items, is_custom)

    def _apply_item_build(self, items: list, is_custom: bool):
        # Plain (untracked) widgets, rebuilt on every champ-select. Tracked
        # factory widgets would leak painters into the theme registry on each
        # rebuild; instead we recolor by re-rendering from _last_items on switch.
        t = self.theme
        self._last_items = (items, is_custom)
        self._build_title.configure(text=f"BUILD // {'custom' if is_custom else 'u.gg'}")
        for w in self._build_item_lbls:
            w.destroy()
        self._build_item_lbls = []
        if not items:
            self._build_placeholder.configure(text="—")
            self._build_placeholder.pack(anchor="w")
            return
        self._build_placeholder.pack_forget()
        for i, name in enumerate(items, 1):
            row = tk.Frame(self._build_body, bg=t.BG); row.pack(fill="x")
            tk.Label(row, text=f"{i}", bg=t.BG, fg=t.PD, font=F(15)).pack(side="left")
            tk.Label(row, text=f"  {name}", bg=t.BG, fg=(t.PB if i == 1 else t.P),
                     font=F(15), anchor="w").pack(side="left")
            self._build_item_lbls.append(row)

    def _on_matchup_winrate(self, champ, enemy, role, wr, label, tag):
        self.root.after(0, self._apply_matchup_winrate, champ, enemy, role, wr, label, tag)

    def _apply_matchup_winrate(self, champ, enemy, role, wr, label, tag):
        t = self.theme
        wr_col = {"success": t.PB, "warn": t.PD, "error": t.DANGER}.get(tag, t.P)
        # CHAMPION panel
        self._game_champ, self._game_enemy, self._game_role = champ, enemy, role
        if champ:
            # A new champion invalidates the previous "RUNES IMPORTED OK" badge.
            if champ != self._panel_champ:
                self._panel_champ = champ
                self._hide_imported_tag()
            self._champ_name.configure(text=champ.upper())
            lane = f"{role} lane" if role and role not in ("auto", "") else "lane"
            self._champ_sub.configure(text=f"[ locked · {lane} ]")
        # MATCHUP panel
        self._matchup_title.configure(text=f"MATCHUP // vs {enemy.upper()}" if enemy else "MATCHUP // idle")
        self._wr_num.configure(text=f"{wr:.1f}%", fg=wr_col)
        arrow = "▲" if wr >= 50 else "▼"
        self._wr_label.configure(text=f"{arrow} {label.upper()}",
                                 fg=t.P if wr >= 50 else t.DANGER)
        self._wr_sample.configure(text=f"{self.rank_v.get()} · {self.region_v.get()}".upper())
        self._gauge.set(wr, t)
        # in-game overlay mirror
        self._game_winrate = f"{wr:.1f}% — {label}"
        self._game_wr_color = wr_col
        if self._in_game_overlay:
            self._update_game_overlay()

    # ── in-game overlay ────────────────────────────────────────────────────────
    def _toggle_overlay(self):
        if self._in_game_overlay:
            self._on_game_end()
        else:
            self._on_game_start()

    def _build_game_overlay(self):
        t = self.theme
        # Child of _content so it occupies the same region the screens do —
        # packing into root would leave the (empty) _content + command bar
        # mapped above it and squeeze the overlay into leftover space.
        outer = t.frame(self._content)
        self._game_frame = outer
        cell, body, _ = self._panel(outer, "IN-GAME")
        cell.pack(fill="both", expand=True)
        hdr = t.frame(body); hdr.pack(fill="x")
        self._game_match_label = t.label(hdr, text="in game", kind="bright", font=F(22))
        self._game_match_label.pack(side="left")
        live = t.frame(hdr); live.pack(side="right")
        t.label(live, text="live", kind="dim", font=F(13)).pack(side="left", padx=(0, 4))
        self._game_live_dot = t.label(live, text="●", kind="body", font=F(13)); self._game_live_dot.pack(side="left")
        self._game_wr_label = t.label(body, text="", kind="bright", font=F(34))
        self._game_wr_label.pack(anchor="w", pady=(2, 6))

        log_frame = t.frame(body); log_frame.pack(fill="both", expand=True)
        log_sb = tk.Scrollbar(log_frame, bg=BG)
        self._game_log_widget = tk.Text(log_frame, font=F(11, fine=True), relief="flat",
                                        state="disabled", wrap="word", bd=0, padx=6, pady=4)
        t.track(self._game_log_widget, lambda th: self._game_log_widget.configure(bg=th.BG, fg=th.P))
        log_sb.configure(command=self._game_log_widget.yview)
        self._game_log_widget.configure(yscrollcommand=log_sb.set)
        log_sb.pack(side="right", fill="y")
        self._game_log_widget.pack(fill="both", expand=True)
        self._recolor_log_tags()
        self._game_log_widget.configure(state="normal")
        for m, tg in list(self._log_buffer):
            self._game_log_widget.insert("end", m + "\n", tg)
        self._game_log_widget.see("end")
        self._game_log_widget.configure(state="disabled")

    def _update_game_overlay(self):
        if self._game_match_label:
            role_str = f" ({self._game_role})" if self._game_role not in ("", "auto") else ""
            title = (f"{self._game_champ}{role_str}  vs  {self._game_enemy}"
                     if self._game_champ and self._game_enemy else "in game")
            self._game_match_label.configure(text=title.upper())
        if self._game_wr_label:
            self._game_wr_label.configure(text=self._game_winrate, fg=self._game_wr_color)

    def _on_game_start(self):
        if self._game_frame is None:
            self._build_game_overlay()
        self._discard_override_frame()
        if self._current_frame is not None:
            self._current_frame.pack_forget()
            self._current_frame = None
        self._in_game_overlay = True
        self._game_frame.pack(fill="both", expand=True)
        self._update_game_overlay()
        self.root.lift()
        self.root.attributes("-topmost", True)
        self.root.after(3000, lambda: self.root.attributes("-topmost", False))

    def _on_game_end(self):
        self._in_game_overlay = False
        if self._game_frame:
            self._game_frame.pack_forget()
        self._hide_imported_tag()
        self._panel_champ = None
        self._show_screen("monitor")
        self._emit("Game ended.", "info")

    # ── overrides ─────────────────────────────────────────────────────────────
    def _selected_champ(self):
        if 0 <= self._sel_build < len(self._build_champs):
            return self._build_champs[self._sel_build]
        return None

    def _add_ov(self):
        self._show_override_editor("")

    def _edit_ov(self):
        champ = self._selected_champ()
        if champ:
            self._show_override_editor(champ)

    def _rm_ov(self):
        champ = self._selected_champ()
        if not champ:
            return
        if messagebox.askyesno("Remove", f"Remove custom build for {champ}?"):
            self.overrides.remove(champ)
            self._sel_build = max(0, self._sel_build - 1)
            self._refresh_builds()
            self._emit(f"Removed override for {champ}", "warn")

    def _show_override_editor(self, champ: str):
        self._discard_override_frame()
        if self._current_frame is not None:
            self._current_frame.pack_forget()
        # Plain frame (not theme-tracked): the editor paints itself from a
        # palette snapshot, so the container needs no painter in the registry.
        self._override_frame = tk.Frame(self._content, bg=self.theme.BG)
        self._override_frame.pack(fill="both", expand=True)
        self._current_frame = self._override_frame
        OverrideEditorPage(
            self._override_frame, self.overrides, self.theme,
            champ=champ, on_save=self._refresh_builds,
            on_back=self._close_override_editor, lcu=self.lcu)

    def _close_override_editor(self):
        self._discard_override_frame()
        self._show_screen("builds")

    def _save_settings(self):
        new_url = self.server_url_v.get().strip().rstrip("/")
        if new_url:
            ugg_api.SERVER_URL = new_url
        # save_settings replaces the whole dict — include every key.
        self.overrides.save_settings({
            "rank": self.rank_v.get(), "region": self.region_v.get(),
            "auto_role": self.arole_v.get(), "trigger": self.trig_v.get(),
            "server_url": new_url, "phosphor": self.theme.name})
        self._emit("Settings saved.", "success")
        try:
            if self._settings_saved_after_id is not None:
                self.root.after_cancel(self._settings_saved_after_id)
            self._settings_saved_lbl.configure(text='"daemon.conf" written ✓')
            self._settings_saved_after_id = self.root.after(
                2600, lambda: self._settings_saved_lbl.configure(text=""))
        except Exception:
            pass

    def _submit_matchup_override(self):
        name = self.matchup_v.get().strip()
        if not name:
            return
        if not self.monitor:
            self._emit("Start monitoring first to use matchup override.", "warn")
            return
        self.matchup_v.set("")
        self.monitor.set_matchup_override(name)


class MatchupGauge(tk.Canvas):
    """[ ▮▮▮▮·· ] bracket gauge. Fill = winrate%; turns DANGER below 50."""

    def __init__(self, parent, theme, height=18):
        super().__init__(parent, height=height, highlightthickness=0, bd=0, bg=theme.BG)
        self._wr = 0.0
        self.bind("<Configure>", lambda e: self._draw(theme))
        self._theme = theme

    def set(self, wr, theme):
        self._wr = max(0.0, min(100.0, float(wr)))
        self._theme = theme
        self._draw(theme)

    def repaint(self, theme):
        self._theme = theme
        self.configure(bg=theme.BG)
        self._draw(theme)

    def _draw(self, theme):
        self.delete("all")
        w = self.winfo_width() or 240
        h = int(self["height"])
        pad = 16
        x0, x1 = pad, w - pad
        cy = h // 2
        fill = theme.DANGER if self._wr < 50 else theme.P
        self.create_text(4, cy, text="[", fill=theme.PD, anchor="w", font=F(15))
        self.create_text(w - 4, cy, text="]", fill=theme.PD, anchor="e", font=F(15))
        self.create_rectangle(x0, cy - 6, x1, cy + 6, outline=theme.BD)
        # Segmented fill (▮▮▮·····) to match the mock's dashed bar, not a solid block.
        fw = (x1 - x0) * (self._wr / 100.0)
        seg, gap = 7, 2
        x = x0
        while x < x0 + fw:
            self.create_rectangle(x, cy - 6, min(x + seg, x0 + fw), cy + 6,
                                  outline="", fill=fill)
            x += seg + gap


def _user_data_dir() -> str:
    """%APPDATA%/RuneSync — writable on any install (including Program Files)."""
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    d = os.path.join(base, "RuneSync")
    try:
        os.makedirs(d, exist_ok=True)
    except Exception:
        import tempfile
        d = tempfile.gettempdir()
    return d


if __name__ == "__main__":
    import sys, os, logging
    _mutex = ctypes.windll.kernel32.CreateMutexW(None, False, "RuneSyncSingleInstance")
    if ctypes.windll.kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
        sys.exit(0)
    _load_fonts()
    log_path = os.path.join(_user_data_dir(), "runesync.log")
    from log_setup import init_logging
    try:
        _log_queue = init_logging(log_path)
    except Exception:
        pass
    root = tk.Tk()
    root.report_callback_exception = lambda exc, val, tb: logging.getLogger().error(
        "Uncaught exception in Tk callback", exc_info=(exc, val, tb),
        extra={"rs_tag": "[crash]", "rs_severity": "error"})
    RuneSyncApp(root)
    root.mainloop()

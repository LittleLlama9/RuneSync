"""RuneSync — auto rune importer with per-champion overrides."""
import sys, threading, tkinter as tk, ctypes, ctypes.wintypes
from tkinter import ttk, messagebox
from PIL import Image, ImageTk
import os
from lcu import LCUClient, LCUConnectionError
import ugg_api
from ugg_api import UGGClient
from overrides import OverrideManager
from monitor import ChampSelectMonitor
from tray import TrayController, LeaguePoller, is_autostart_enabled, set_autostart

_ASSETS_DIR = os.path.join(
    getattr(sys, '_MEIPASS', os.path.dirname(os.path.abspath(__file__))),
    "assets", "spells"
)
_SPELL_ICON_SIZE = (20, 20)
_spell_image_cache: dict = {}

BG      = "#0e1117"
PANEL   = "#1e2330"
DARK    = "#111318"
GOLD    = "#c89b3c"
GREEN   = "#1e6b3c"
GREEN_H = "#27894e"
BLUE    = "#1e4a8a"
BLUE_H  = "#2560b0"
RED     = "#5c1e1e"
RED_H   = "#7a2525"

GAME_SIZE = (1100, 750)

import queue as _queue_mod
_log_queue = _queue_mod.Queue()  # replaced by init_logging() at startup


TREES    = ["Precision","Domination","Sorcery","Resolve","Inspiration"]
KEYSTONES = {
    "Precision":   ["Press the Attack","Lethal Tempo","Fleet Footwork","Conqueror"],
    "Domination":  ["Electrocute","Predator","Dark Harvest","Hail of Blades"],
    "Sorcery":     ["Summon Aery","Arcane Comet","Phase Rush"],
    "Resolve":     ["Grasp of the Undying","Aftershock","Guardian"],
    "Inspiration": ["Glacial Augment","First Strike","Unsealed Spellbook"],
}
ROLES = ["auto","top","jungle","mid","bot","support"]

SUMMONER_SPELLS = {
    "— (use u.gg default)": 0,
    "Flash":       4,
    "Ignite":      14,
    "Exhaust":     3,
    "Barrier":     21,
    "Heal":        7,
    "Ghost":       6,
    "Teleport":    12,
    "Cleanse":     1,
    "Smite":       11,
    "Clarity":     13,
}

def _load_spell_icon(name: str) -> "Image.Image | None":
    """Load a PIL Image for a spell icon — used for compositing."""
    key = f"_pil_{name}"
    if key in _spell_image_cache:
        return _spell_image_cache[key]
    for ext in (".png", ".jpg"):
        path = os.path.join(_ASSETS_DIR, name + ext)
        if os.path.exists(path):
            try:
                img = Image.open(path).resize(_SPELL_ICON_SIZE, Image.LANCZOS).convert("RGBA")
                _spell_image_cache[key] = img
                return img
            except Exception:
                pass
    return None

def _make_spell_pair_icon(spell1_id: int, spell2_id: int) -> "ImageTk.PhotoImage | None":
    """Create a composite PhotoImage of two spell icons side by side.
    Returns None when neither spell is explicitly set (both 0 = u.gg default)."""
    if not spell1_id and not spell2_id:
        return None
    id_to_name = {v: k for k, v in SUMMONER_SPELLS.items() if v != 0}
    name1 = id_to_name.get(spell1_id, "auto") if spell1_id else "auto"
    name2 = id_to_name.get(spell2_id, "auto") if spell2_id else "auto"
    cache_key = f"pair_{name1}_{name2}"
    if cache_key in _spell_image_cache:
        return _spell_image_cache[cache_key]
    gap = 3
    w = _SPELL_ICON_SIZE[0] * 2 + gap
    h = _SPELL_ICON_SIZE[1]
    composite = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    img1 = _load_spell_icon(name1)
    img2 = _load_spell_icon(name2)
    if img1: composite.paste(img1, (0, 0))
    if img2: composite.paste(img2, (_SPELL_ICON_SIZE[0] + gap, 0))
    photo = ImageTk.PhotoImage(composite)
    _spell_image_cache[cache_key] = photo
    return photo


def _get_second_monitor_geometry():
    """Return (left, top, width, height) of first non-primary monitor, or None."""
    class RECT(ctypes.Structure):
        _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                    ("right", ctypes.c_long), ("bottom", ctypes.c_long)]
    class MONITORINFO(ctypes.Structure):
        _fields_ = [("cbSize", ctypes.c_ulong), ("rcMonitor", RECT),
                    ("rcWork", RECT), ("dwFlags", ctypes.c_ulong)]
    result = []
    # Use pointer-sized types for handles and LPARAM (64-bit Windows requires this)
    MONITORENUMPROC = ctypes.WINFUNCTYPE(
        ctypes.c_int, ctypes.c_size_t, ctypes.c_size_t,
        ctypes.POINTER(RECT), ctypes.c_size_t)
    def callback(hMon, hdc, lprc, data):
        info = MONITORINFO()
        info.cbSize = ctypes.sizeof(MONITORINFO)
        ok = ctypes.windll.user32.GetMonitorInfoW(hMon, ctypes.byref(info))
        if not ok:
            return 1  # GetMonitorInfoW failed, skip this monitor
        if info.dwFlags != 1:  # not primary
            r = info.rcMonitor
            result.append((r.left, r.top, r.right - r.left, r.bottom - r.top))
            return 0  # stop after first secondary found
        return 1
    cb = MONITORENUMPROC(callback)  # keep reference alive
    ctypes.windll.user32.EnumDisplayMonitors(None, None, cb, 0)
    return result[0] if result else None


def make_btn(parent, text, cmd, bg=BLUE, hov=BLUE_H, **kw):
    b = tk.Label(parent, text=text, font=("Segoe UI",9),
                 bg=bg, fg="white", padx=10, pady=5, cursor="hand2", **kw)
    b.bind("<Button-1>", lambda e: cmd())
    b.bind("<Enter>",    lambda e: b.configure(bg=hov))
    b.bind("<Leave>",    lambda e: b.configure(bg=bg))
    return b


class OverrideEditorPage:
    """Renders the champion override editor into an existing parent frame."""

    def __init__(self, parent, overrides, champ="", on_save=None, on_back=None, lcu=None):
        self.parent    = parent
        self.overrides = overrides
        self.on_save   = on_save
        self.on_back   = on_back
        self._lcu      = lcu
        self._champ    = champ
        existing = overrides.get(champ) or {}
        self._imported_page_name = existing.get("page_name", "")

        from item_builder import normalize_build
        self._items_build = normalize_build(existing.get("items_build", {}))

        self._build_ui(existing, champ)

    def _build_ui(self, existing: dict, champ: str):
        p = self.parent

        # ── Header ────────────────────────────────────────────────────────────
        hdr = tk.Frame(p, bg=PANEL, pady=8)
        hdr.pack(fill="x")
        back = tk.Label(hdr, text="← Back", font=("Segoe UI", 9),
                        bg=PANEL, fg="#666", cursor="hand2", padx=10)
        back.pack(side="left")
        back.bind("<Button-1>", lambda e: self._do_back())
        back.bind("<Enter>",    lambda e: back.configure(fg="#aaa"))
        back.bind("<Leave>",    lambda e: back.configure(fg="#666"))
        title = "Edit Override" if champ else "Add Override"
        tk.Label(hdr, text=title, font=("Segoe UI", 12, "bold"),
                 bg=PANEL, fg=GOLD).pack(side="left", padx=6)

        tk.Frame(p, bg="#2a2a2a", height=1).pack(fill="x")

        # ── Scrollable content ────────────────────────────────────────────────
        wrap = tk.Frame(p, bg=PANEL)
        wrap.pack(fill="both", expand=True)
        canvas = tk.Canvas(wrap, bg=PANEL, highlightthickness=0)
        vsb = tk.Scrollbar(wrap, orient="vertical", command=canvas.yview, bg=DARK)
        canvas.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)
        cf = tk.Frame(canvas, bg=PANEL)
        _cw = canvas.create_window((0, 0), window=cf, anchor="nw")
        cf.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda e: canvas.itemconfig(_cw, width=e.width))
        canvas.bind_all("<MouseWheel>",
                        lambda e: canvas.yview_scroll(-1 * (e.delta // 120), "units"))

        def row(lbl_text):
            tk.Label(cf, text=lbl_text, font=("Segoe UI", 9),
                     bg=PANEL, fg="#aaa", anchor="w").pack(fill="x", padx=24, pady=(8, 0))

        def entry(default=""):
            v = tk.StringVar(value=default)
            tk.Entry(cf, textvariable=v, bg=DARK, fg="#ccc",
                     insertbackground="#ccc", relief="flat",
                     font=("Segoe UI", 10)).pack(fill="x", padx=24, pady=(2, 0))
            return v

        def combo(vals, default=""):
            v = tk.StringVar(value=default or vals[0])
            ttk.Combobox(cf, textvariable=v, values=vals,
                         state="readonly", font=("Segoe UI", 9)
                         ).pack(fill="x", padx=24, pady=(2, 0))
            return v

        row("Champion name:");       self.champ_v    = entry(champ)
        row("Role:");                self.role_v     = combo(ROLES, existing.get("role", "auto"))
        row("Primary rune tree:");   self.primary_v  = combo(TREES, existing.get("primary_tree", "Precision"))
        row("Keystone:");            self.keystone_v = combo(
            KEYSTONES.get(self.primary_v.get(), []), existing.get("keystone", ""))
        row("Secondary rune tree:"); self.secondary_v = combo(TREES, existing.get("secondary_tree", "Domination"))
        row("Full rune IDs (optional, 9 comma-sep ints):")
        self.runes_v = entry(",".join(str(r) for r in existing.get("rune_ids", [])))

        # Item build row
        row("Item build:")
        ibf = tk.Frame(cf, bg=PANEL); ibf.pack(fill="x", padx=24, pady=(2, 0))
        self._items_summary_lbl = tk.Label(
            ibf, text=self._items_build_summary(),
            font=("Segoe UI", 8), bg=PANEL, fg="#666", anchor="w")
        self._items_summary_lbl.pack(side="left", fill="x", expand=True)
        make_btn(ibf, "Edit Build", self._open_build_editor,
                 "#1a3a2a", "#2a4a3a").pack(side="right")

        row("Note (optional):");     self.note_v = entry(existing.get("note", ""))

        # Summoner spells
        spell_names = list(SUMMONER_SPELLS.keys())
        def spell_name_from_id(sid):
            for name, i in SUMMONER_SPELLS.items():
                if i == sid: return name
            return "— (use u.gg default)"

        row("Summoner Spell 1:")
        self.spell1_v = tk.StringVar(value=spell_name_from_id(existing.get("spell1", 0)))
        ttk.Combobox(cf, textvariable=self.spell1_v, values=spell_names,
                     state="readonly", font=("Segoe UI", 9)).pack(fill="x", padx=24, pady=(2, 0))
        row("Summoner Spell 2:")
        self.spell2_v = tk.StringVar(value=spell_name_from_id(existing.get("spell2", 0)))
        ttk.Combobox(cf, textvariable=self.spell2_v, values=spell_names,
                     state="readonly", font=("Segoe UI", 9)).pack(fill="x", padx=24, pady=(2, 0))

        import_f = tk.Frame(cf, bg=PANEL); import_f.pack(fill="x", padx=24, pady=(10, 0))
        make_btn(import_f, "?  Import Active Rune Page from Client",
                 self._import_from_client, "#2a3a2a", "#3a4a3a").pack(side="left")
        self._import_status = tk.Label(import_f, text="", font=("Segoe UI", 8),
                                       bg=PANEL, fg="#888")
        self._import_status.pack(side="left", padx=(8, 0))

        tk.Frame(cf, bg=PANEL, height=12).pack()  # bottom padding

        # ── Footer ────────────────────────────────────────────────────────────
        tk.Frame(p, bg="#2a2a2a", height=1).pack(fill="x")
        footer = tk.Frame(p, bg=PANEL, pady=10)
        footer.pack(fill="x")
        tk.Button(footer, text="Save", command=self._save,
                  bg=BLUE, fg="white", relief="flat", font=("Segoe UI", 9),
                  padx=12).pack(side="left", padx=(16, 6))
        tk.Button(footer, text="Cancel", command=self._do_back,
                  bg="#2a2d3a", fg="white", relief="flat", font=("Segoe UI", 9),
                  padx=12).pack(side="left")

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
                self._import_status.configure(text="No rune page found.", fg="#e05252"); return
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
                text=f"✓ Imported '{self._imported_page_name or 'page'}'", fg="#4caf73")
        except Exception as ex:
            self._import_status.configure(text=f"✗ {ex}", fg="#e05252")

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
        root.title("RuneSync"); root.geometry("780x560")
        root.resizable(False,False); root.configure(bg=BG)
        try:
            root.iconbitmap(os.path.join(getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__))), "icon.ico"))
        except Exception:
            pass

        self.lcu       = LCUClient()
        self.overrides = OverrideManager()
        ugg_api.SERVER_URL = self.overrides.settings.get("server_url", ugg_api.SERVER_URL)
        # Load the GitHub-hosted data bundle in the background. Tries the
        # local disk cache first (instant), falls back to a fresh download.
        # If both fail, UGGClient transparently falls back to the localhost
        # server — so devs running the FastAPI server keep working unchanged.
        threading.Thread(target=ugg_api.init_bundle, daemon=True).start()
        self.ugg       = UGGClient()
        self.monitor   = None
        self.running   = False
        self._saved_geometry: str | None = None
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
        self._game_wr_color: str    = "#aab4c8"
        self._log_buffer: list[tuple[str, str]] = []  # (msg, tag) — replayed into game overlay
        self._log_queue = _log_queue

        self._build_ui()
        self.root.update_idletasks()
        self._apply_dark_titlebar()
        threading.Thread(target=self._try_connect, daemon=True).start()

        # If launched with --minimized (Windows autostart), start hidden in
        # the system tray instead of popping the window on every login.
        if "--minimized" in sys.argv:
            self.root.after(50, self.root.withdraw)

        # ── System tray + League auto-detect ─────────────────────────────
        _icon_path = os.path.join(
            getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__))),
            "icon.ico",
        )
        self._tray = TrayController(
            on_show=self._show_from_tray,
            on_quit=self._real_quit,
            icon_path=_icon_path,
        )
        self._tray.start()
        self._league_poller = LeaguePoller(
            on_open=self._on_league_open,
            on_close=self._on_league_close,
        )
        self._league_poller.start()
        # X button → hide to tray instead of quitting
        self.root.protocol("WM_DELETE_WINDOW", self._hide_to_tray)

    # ── tray / lifecycle ───────────────────────────────────────────────────

    def _hide_to_tray(self):
        """Minimize the window to the system tray (single-instance mutex keeps app alive)."""
        try:
            self.root.withdraw()
        except Exception:
            pass
        if getattr(self, "_tray", None) and self._tray.available():
            self._tray.notify(
                "RuneSync",
                "Still running in the system tray. Right-click the icon to quit.",
            )

    def _show_from_tray(self):
        """Bring the window back from the tray (called from non-UI thread)."""
        self.root.after(0, self._do_show_window)

    def _do_show_window(self):
        try:
            self.root.deiconify()
            self.root.lift()
            self.root.focus_force()
        except Exception:
            pass

    def _real_quit(self):
        """Actually exit the app (from tray "Quit" or programmatic shutdown)."""
        try:
            self._league_poller.stop()
        except Exception:
            pass
        try:
            self._tray.stop()
        except Exception:
            pass
        self.root.after(0, self.root.destroy)

    def _on_league_open(self):
        """League just launched — bring RuneSync to the foreground and reconnect."""
        self.root.after(0, self._do_show_window)
        if not self.lcu.connected:
            threading.Thread(target=self._try_connect, kwargs={"startup": False}, daemon=True).start()

    def _on_league_close(self):
        """League just closed — stay in tray, no-op."""
        pass

    def _toggle_debug_tab(self):
        """Ctrl+Shift+D: build the Debug Log tab on first press, select it after."""
        if not self._debug_tab_built:
            self._tab_debug(self.nb)
            self._debug_tab_built = True
            self.nb.select(self.nb.tabs()[-1])
            return
        for tab_id in self.nb.tabs():
            try:
                if "Debug" in self.nb.tab(tab_id, "text"):
                    self.nb.select(tab_id)
                    return
            except Exception:
                continue

    def _apply_dark_titlebar(self):
        """Tell Windows to render the title bar in dark mode and tint the border."""
        try:
            hwnd = ctypes.windll.user32.GetParent(self.root.winfo_id())
            DWMWA_USE_IMMERSIVE_DARK_MODE = 20
            val = ctypes.c_int(1)
            ctypes.windll.dwmapi.DwmSetWindowAttribute(
                hwnd, DWMWA_USE_IMMERSIVE_DARK_MODE,
                ctypes.byref(val), ctypes.sizeof(val))
            # Set border color to GOLD (#c89b3c) — DWM expects BGR COLORREF
            DWMWA_BORDER_COLOR = 34
            r, g, b = 0xc8, 0x9b, 0x3c
            colorref = ctypes.c_uint32(r | (g << 8) | (b << 16))
            ctypes.windll.dwmapi.DwmSetWindowAttribute(
                hwnd, DWMWA_BORDER_COLOR,
                ctypes.byref(colorref), ctypes.sizeof(colorref))
        except Exception:
            pass  # non-Windows or older Windows — silently skip

    # ── UI ──────────────────────────────────────────────────────────────────
    def _build_ui(self):
        top = tk.Frame(self.root, bg=BG); top.pack(fill="x", padx=16, pady=(12,0))
        tk.Label(top, text="RuneSync", font=("Segoe UI",20,"bold"),
                 bg=BG, fg=GOLD).pack(side="left")
        self.dot = tk.Label(top, text="●", font=("Segoe UI",14), bg=BG, fg="#444")
        self.dot.pack(side="right", padx=(0,4))
        self.slbl = tk.Label(top, text="Disconnected", font=("Segoe UI",10), bg=BG, fg="#888")
        self.slbl.pack(side="right", padx=(0,6))
        tk.Frame(self.root, bg="#2a2a2a", height=1).pack(fill="x", padx=16, pady=8)

        s = ttk.Style(); s.theme_use("default")
        s.configure("D.TNotebook",          background=BG, borderwidth=0)
        s.configure("D.TNotebook.Tab",      background=PANEL, foreground="#aaa",
                    padding=[14,6], font=("Segoe UI",10))
        s.map("D.TNotebook.Tab",            background=[("selected","#232840")],
                                            foreground=[("selected",GOLD)])
        self.nb = ttk.Notebook(self.root, style="D.TNotebook")
        self.nb.pack(fill="both", expand=True, padx=16, pady=(0,10))
        nb = self.nb

        self._tab_monitor(nb)
        self._tab_overrides(nb)
        self._tab_settings(nb)
        # Debug Log tab is lazy: only built when the user presses Ctrl+Shift+D.
        # Keeps end-user UI uncluttered while preserving the dev tool.
        self._debug_tab_built = False
        self.root.bind_all("<Control-Shift-D>", lambda e: self._toggle_debug_tab())

    def _tab_monitor(self, nb):
        f = tk.Frame(nb, bg=PANEL); nb.add(f, text="  Monitor  ")
        tk.Label(f, text="Activity Log", font=("Segoe UI",10,"bold"),
                 bg=PANEL, fg=GOLD).pack(anchor="w", padx=14, pady=(10,4))

        # ── Bottom controls packed FIRST so expand=True log doesn't eat them ──
        bf = tk.Frame(f, bg=PANEL); bf.pack(side="bottom", fill="x", padx=14, pady=(0,10))
        self.sbtn = make_btn(bf, "▶  Start Monitoring", self._toggle, GREEN, GREEN_H)
        self.sbtn.pack(side="left")
        make_btn(bf, "Clear Log", self._clear, "#2a2d3a","#3a3d4a").pack(side="right")

        # Matchup override bar
        mf = tk.Frame(f, bg=PANEL); mf.pack(side="bottom", fill="x", padx=14, pady=(0,2))
        tk.Label(mf, text="⚔ vs:", font=("Segoe UI",9), bg=PANEL, fg="#666").pack(side="left")
        self.matchup_v = tk.StringVar()
        self.matchup_entry = tk.Entry(mf, textvariable=self.matchup_v,
                                      bg=DARK, fg="#ccc", insertbackground="#ccc",
                                      relief="flat", font=("Segoe UI",9), width=18)
        self.matchup_entry.pack(side="left", padx=(5,4))
        self.matchup_entry.bind("<Return>", lambda e: self._submit_matchup_override())
        make_btn(mf, "Look up", self._submit_matchup_override,
                 "#1a2a3a", "#2a3a4a").pack(side="left")

        # Item build display bar
        ibf = tk.Frame(f, bg=PANEL); ibf.pack(side="bottom", fill="x", padx=14, pady=(0,2))
        tk.Label(ibf, text="Build:", font=("Segoe UI",9,"bold"),
                 bg=PANEL, fg="#555").pack(side="left")
        self._build_items_label = tk.Label(ibf, text="—", font=("Segoe UI",9),
                                           bg=PANEL, fg="#aab4c8", anchor="w")
        self._build_items_label.pack(side="left", padx=(5,0), fill="x", expand=True)
        self._build_src_label = tk.Label(ibf, text="", font=("Segoe UI",8),
                                         bg=PANEL, fg="#444")
        self._build_src_label.pack(side="right", padx=(4,0))

        # Log box fills remaining space between header and bottom controls
        lf = tk.Frame(f, bg=DARK); lf.pack(fill="both", expand=True, padx=14, pady=(0,4))
        self.log = tk.Text(lf, bg=DARK, fg="#ccc", font=("Consolas",9),
                           relief="flat", state="disabled", wrap="word")
        sb = tk.Scrollbar(lf, command=self.log.yview, bg=PANEL)
        self.log.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y"); self.log.pack(fill="both", expand=True, padx=5, pady=5)
        for tag,col in [("info","#aab4c8"),("success","#4caf73"),
                        ("warn",GOLD),("error","#e05252"),("champ","#7dbbff")]:
            self.log.tag_config(tag, foreground=col)

    def _tab_overrides(self, nb):
        f = tk.Frame(nb, bg=PANEL); nb.add(f, text="  My Builds  ")
        tk.Label(f, text="Champions listed here use YOUR runes instead of u.gg's top build.",
                 font=("Segoe UI",9), bg=PANEL, fg="#666").pack(anchor="w", padx=14, pady=(10,4))
        lf = tk.Frame(f, bg=DARK); lf.pack(fill="both", expand=True, padx=14, pady=(0,8))
        cols = ("Champion","Role","Primary","Secondary")
        self.tree = ttk.Treeview(lf, columns=cols, show="tree headings", height=10, style="Builds.Treeview")
        ts = ttk.Style()
        ts.configure("Builds.Treeview", background=DARK, foreground="#ccc",
                     fieldbackground=DARK, rowheight=28, font=("Segoe UI",9), indent=0)
        ts.configure("Builds.Treeview.Heading", background=PANEL, foreground=GOLD,
                     font=("Segoe UI",9,"bold"))
        ts.map("Builds.Treeview", background=[("selected","#2a3050")])
        # Remove the expand indicator from the row layout so icons sit at the left edge
        ts.layout("Builds.Treeview.Item", [
            ("Treeitem.padding", {"children": [
                ("Treeitem.image",  {"side": "left", "sticky": ""}),
                ("Treeitem.focus",  {"children": [
                    ("Treeitem.text", {"side": "left", "sticky": ""})
                ], "side": "left", "sticky": ""}),
            ], "sticky": "nsew"}),
        ])
        # #0 is the tree column — used for spell icons
        self.tree.column("#0", width=48, stretch=False, anchor="w")
        self.tree.heading("#0", text="Spells")
        for col, w in [("Champion",110),("Role",70),("Primary",110),("Secondary",110)]:
            self.tree.heading(col, text=col)
            self.tree.column(col, width=w, anchor="center")
        sb2 = tk.Scrollbar(lf, command=self.tree.yview, bg=PANEL)
        self.tree.configure(yscrollcommand=sb2.set)
        sb2.pack(side="right",fill="y"); self.tree.pack(fill="both",expand=True)
        bf = tk.Frame(f, bg=PANEL); bf.pack(fill="x", padx=14, pady=(0,12))
        make_btn(bf,"+ Add",     self._add_ov,  BLUE,      BLUE_H).pack(side="left")
        make_btn(bf,"✎ Edit",   self._edit_ov, "#2a2d3a","#3a3d4a").pack(side="left",padx=(8,0))
        make_btn(bf,"✕ Remove", self._rm_ov,   RED,       RED_H).pack(side="left",padx=(8,0))
        self._refresh_tree()

    def _tab_settings(self, nb):
        f = tk.Frame(nb, bg=PANEL); nb.add(f, text="  Settings  ")
        def row(lbl, wfn):
            r = tk.Frame(f, bg=PANEL); r.pack(fill="x", padx=18, pady=6)
            tk.Label(r, text=lbl, font=("Segoe UI",9), bg=PANEL, fg="#aaa",
                     width=22, anchor="w").pack(side="left")
            wfn(r)
        self.rank_v   = tk.StringVar(value=self.overrides.settings.get("rank","Platinum+"))
        self.region_v = tk.StringVar(value=self.overrides.settings.get("region","World"))
        self.arole_v  = tk.BooleanVar(value=self.overrides.settings.get("auto_role",True))
        self.trig_v   = tk.StringVar(value=self.overrides.settings.get("trigger","hover"))
        row("Rank filter:", lambda p: ttk.Combobox(p, textvariable=self.rank_v,
            values=["Iron+","Bronze+","Silver+","Gold+","Platinum+",
                    "Emerald+","Diamond+","Master+"],
            state="readonly",width=14,font=("Segoe UI",9)).pack(side="left"))
        row("Region:", lambda p: ttk.Combobox(p, textvariable=self.region_v,
            values=["World","NA","EUW","EUNE","KR","BR","JP","OCE","LAS","LAN","TR","RU"],
            state="readonly",width=14,font=("Segoe UI",9)).pack(side="left"))
        def arole_w(p):
            box = tk.Label(p, font=("Segoe UI", 10), bg=PANEL, fg=GOLD, cursor="hand2")
            def _refresh():
                box.configure(text="☑" if self.arole_v.get() else "☐")
            def _toggle():
                self.arole_v.set(not self.arole_v.get()); _refresh()
            box.bind("<Button-1>", lambda e: _toggle())
            _refresh()
            box.pack(side="left")
        row("Auto-detect role:", arole_w)
        def trig_w(p):
            for v,l in [("hover","On hover"),("lock","On lock-in")]:
                tk.Radiobutton(p,text=l,variable=self.trig_v,value=v,
                               bg=PANEL,activebackground=PANEL,selectcolor=BG,
                               fg="#ccc",font=("Segoe UI",9)).pack(side="left",padx=(0,12))
        row("Import trigger:", trig_w)

        # ── Start with Windows toggle (writes HKCU\\...\\Run) ────────────────
        self.autostart_v = tk.BooleanVar(value=is_autostart_enabled())
        def autostart_w(p):
            box = tk.Label(p, font=("Segoe UI", 10), bg=PANEL, fg=GOLD, cursor="hand2")
            def _refresh():
                box.configure(text="☑" if self.autostart_v.get() else "☐")
            def _toggle():
                new = not self.autostart_v.get()
                if set_autostart(new):
                    self.autostart_v.set(new)
                    _refresh()
            box.bind("<Button-1>", lambda e: _toggle())
            _refresh()
            box.pack(side="left")
        row("Start with Windows:", autostart_w)

        # Server URL is kept as a hidden setting for devs who want to point
        # at a local FastAPI scraper instead of the GitHub data bundle.
        # Not surfaced in the UI — end users don't need it.
        self.server_url_v = tk.StringVar(
            value=self.overrides.settings.get("server_url", ugg_api.SERVER_URL))

        tk.Frame(f,bg="#2a2a2a",height=1).pack(fill="x",padx=18,pady=12)
        save_row = tk.Frame(f, bg=PANEL); save_row.pack(fill="x", padx=18)
        make_btn(save_row, "  Save Settings  ", self._save_settings).pack(side="left")
        # Inline confirmation: "Saved ✓" appears after a successful save and
        # fades after ~2.5s. Lives next to the button so it's visible whether
        # or not the user switches back to the Monitor tab.
        self._settings_saved_lbl = tk.Label(
            save_row, text="", font=("Segoe UI", 9, "bold"),
            bg=PANEL, fg="#4caf73")
        self._settings_saved_lbl.pack(side="left", padx=(12, 0))
        self._settings_saved_after_id = None

    def _tab_debug(self, nb):
        import queue as _q
        f = tk.Frame(nb, bg=PANEL); nb.add(f, text="  Debug Log  ")

        # ── Header ────────────────────────────────────────────────────────────
        hdr = tk.Frame(f, bg=PANEL); hdr.pack(fill="x", padx=14, pady=(10, 4))
        tk.Label(hdr, text="Debug Console", font=("Segoe UI", 10, "bold"),
                 bg=PANEL, fg=GOLD).pack(side="left")
        make_btn(hdr, "Clear",    self._debug_clear,
                 "#2a2d3a", "#3a3d4a").pack(side="right")
        make_btn(hdr, "Copy All", self._debug_copy,
                 "#2a2d3a", "#3a3d4a").pack(side="right", padx=(0, 6))

        # ── Module filter checkboxes ──────────────────────────────────────────
        flt = tk.Frame(f, bg=PANEL); flt.pack(fill="x", padx=14, pady=(0, 2))
        tk.Label(flt, text="Show:", font=("Segoe UI", 8),
                 bg=PANEL, fg="#666").pack(side="left")
        self._debug_filters: dict = {}
        for tag_label in ("[ugg]", "[lcu]", "[monitor]", "[unknown]"):
            v = tk.BooleanVar(value=True)
            self._debug_filters[tag_label] = v
            tk.Checkbutton(flt, text=tag_label, variable=v,
                           bg=PANEL, activebackground=PANEL,
                           selectcolor=BG, fg="#aaa",
                           font=("Consolas", 8),
                           command=self._debug_refilter).pack(side="left", padx=(6, 0))

        # ── Level filter radio buttons ────────────────────────────────────────
        sev_f = tk.Frame(f, bg=PANEL); sev_f.pack(fill="x", padx=14, pady=(0, 4))
        tk.Label(sev_f, text="Level:", font=("Segoe UI", 8),
                 bg=PANEL, fg="#666").pack(side="left")
        self._debug_min_level = tk.StringVar(value="debug")
        for lvl in ("debug", "info", "warn", "error"):
            tk.Radiobutton(sev_f, text=lvl, variable=self._debug_min_level,
                           value=lvl, bg=PANEL, activebackground=PANEL,
                           selectcolor=BG, fg="#aaa", font=("Segoe UI", 8),
                           command=self._debug_refilter).pack(side="left", padx=(6, 0))

        # ── Log text area ─────────────────────────────────────────────────────
        lf = tk.Frame(f, bg=DARK); lf.pack(fill="both", expand=True, padx=14, pady=(0, 4))
        self.debug_log = tk.Text(lf, bg=DARK, fg="#ccc",
                                 font=("Consolas", 8),
                                 relief="flat", state="disabled", wrap="none")
        xsb = tk.Scrollbar(lf, orient="horizontal",
                           command=self.debug_log.xview, bg=PANEL)
        ysb = tk.Scrollbar(lf, command=self.debug_log.yview, bg=PANEL)
        self.debug_log.configure(xscrollcommand=xsb.set,
                                  yscrollcommand=ysb.set)
        xsb.pack(side="bottom", fill="x")
        ysb.pack(side="right",  fill="y")
        self.debug_log.pack(fill="both", expand=True)

        # Colour tags
        for name, col in (
            ("t_debug",   "#555e6e"),
            ("t_info",    "#aab4c8"),
            ("t_warn",    GOLD),
            ("t_error",   "#e05252"),
            ("t_claude",  "#c792ea"),
            ("t_ugg",     "#7dbbff"),
            ("t_lcu",     "#82aaff"),
            ("t_monitor", "#4caf73"),
            ("t_merge",   "#ffcb6b"),
            ("t_unknown", "#888"),
            ("t_ts",      "#3a4050"),
            ("t_tag",     "#5a6480"),
        ):
            self.debug_log.tag_config(name, foreground=col)

        # ── Status bar ────────────────────────────────────────────────────────
        self._debug_count_lbl = tk.Label(f, text="0 entries",
                                         font=("Segoe UI", 8), bg=PANEL, fg="#444")
        self._debug_count_lbl.pack(side="bottom", anchor="e", padx=14, pady=(0, 6))

        # In-memory record store (for refiltering without re-draining the queue)
        self._debug_records: list = []
        self._debug_entry_count = 0

        # Start draining the log queue
        self._debug_drain()

    def _debug_drain(self):
        """Pull records from the log queue and append them to the debug widget (every 500ms)."""
        import logging
        batch = []
        try:
            while True:
                batch.append(self._log_queue.get_nowait())
        except Exception:
            pass  # queue.Empty is the normal exit path

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

                # Respect current filters
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

            # Cap widget at 2000 lines to prevent sluggishness in long sessions
            try:
                line_count = int(self.debug_log.index("end-1c").split(".")[0])
                if line_count > 2100:
                    self.debug_log.delete("1.0", f"{line_count - 2000}.0")
            except Exception:
                pass

            if added:
                self.debug_log.see("end")
                self._debug_entry_count += added
                self._debug_count_lbl.configure(
                    text=f"{self._debug_entry_count} entries")

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
        lines = [
            f"{r['ts']}  {r['tag']:<12}  {r['sev'].upper():<6}  {r['msg']}"
            for r in self._debug_records
        ]
        self.root.clipboard_clear()
        self.root.clipboard_append("\n".join(lines))

    def _debug_refilter(self):
        """Rebuild the text widget from the in-memory record list when filters change."""
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
    def _emit(self, msg, tag="info"):
        self._log_buffer.append((msg, tag))
        # Mirror to runesync.log so the rune-import path is diagnosable from the
        # log file (the GUI widgets below are otherwise the only sink).
        try:
            print(msg, file=sys.stderr)
        except Exception:
            pass
        def _do():
            self.log.configure(state="normal")
            self.log.insert("end", msg+"\n", tag)
            self.log.see("end")
            self.log.configure(state="disabled")
            if self._in_game_overlay and self._game_log_widget:
                self._game_log_widget.configure(state="normal")
                self._game_log_widget.insert("end", msg+"\n", tag)
                self._game_log_widget.see("end")
                self._game_log_widget.configure(state="disabled")
        self.root.after(0, _do)

    def _clear(self):
        self.log.configure(state="normal")
        self.log.delete("1.0","end")
        self.log.configure(state="disabled")

    def _set_status(self, txt, col):
        self.root.after(0, lambda: (
            self.slbl.configure(text=txt),
            self.dot.configure(fg=col)))

    # ── connect ───────────────────────────────────────────────────────────────
    def _try_connect(self, *, startup: bool = True):
        if self.lcu.connected:
            return
        self._emit("Connecting to League client...", "info")
        if startup:
            import time; time.sleep(15)
        for attempt in range(1, 4):  # 3 retries, 15s apart
            try:
                self.lcu.connect()
                self._emit("✓ Connected to League Client", "success")
                self._set_status("Connected", "#4caf73")
                self.root.after(0, self._start)
                return
            except LCUConnectionError as e:
                if attempt < 3:
                    self._emit(f"  Attempt {attempt}/3 failed — retrying in 15s...", "info")
                    import time; time.sleep(15)
                else:
                    self._emit(f"✗ {e}", "warn")
                    self._emit("  Open the League client then restart RuneSync.", "warn")
                    self._set_status("Disconnected", GOLD)

    # ── monitor ───────────────────────────────────────────────────────────────
    def _toggle(self):
        if not self.running: self._start()
        else:                self._stop()

    def _start(self):
        if not self.lcu.connected:
            messagebox.showerror("Not Connected",
                "League client not connected.\nOpen League and try again."); return
        self.running = True
        self.sbtn.configure(text="■  Stop Monitoring", bg=RED)
        self.sbtn.bind("<Enter>", lambda e: self.sbtn.configure(bg=RED_H))
        self.sbtn.bind("<Leave>", lambda e: self.sbtn.configure(bg=RED))
        self._emit("──── Monitoring started ────", "warn")
        self.monitor = ChampSelectMonitor(
            lcu=self.lcu, ugg=self.ugg, overrides=self.overrides,
            on_log=self._emit, trigger=self.trig_v.get(),
            rank=self.rank_v.get(), region=self.region_v.get(),
            auto_role=self.arole_v.get(),
            on_game_start=lambda: self.root.after(0, self._on_game_start),
            on_game_end=lambda: self.root.after(0, self._on_game_end),
            on_league_closed=lambda: self.root.after(0, self.root.quit),
            on_matchup_winrate=self._on_matchup_winrate,
            on_item_build=self._on_item_build,
        )
        threading.Thread(target=self.monitor.run, daemon=True).start()

    def _stop(self):
        self.running = False
        if self.monitor: self.monitor.stop()
        self.sbtn.configure(text="▶  Start Monitoring", bg=GREEN)
        self.sbtn.bind("<Enter>", lambda e: self.sbtn.configure(bg=GREEN_H))
        self.sbtn.bind("<Leave>", lambda e: self.sbtn.configure(bg=GREEN))
        self._emit("──── Monitoring stopped ────", "warn")

    def _on_item_build(self, items: list, is_custom: bool):
        text = "  ▸  ".join(str(i) for i in items) if items else "—"
        color = "#4caf73" if is_custom else "#aab4c8"
        src   = "(custom)" if is_custom else "(u.gg)"
        src_color = "#4caf73" if is_custom else "#444"
        def _do():
            self._build_items_label.configure(text=text, fg=color)
            self._build_src_label.configure(text=src, fg=src_color)
        self.root.after(0, _do)

    def _on_matchup_winrate(self, champ: str, enemy: str, role: str, wr: float, label: str, tag: str):
        tag_to_color = {"success": "#4caf73", "warn": GOLD, "error": "#e05252", "info": "#aab4c8"}
        self._game_champ     = champ
        self._game_enemy     = enemy
        self._game_role      = role
        self._game_winrate   = f"{wr:.1f}% WR — {label}"
        self._game_wr_color  = tag_to_color.get(tag, "#aab4c8")
        if self._in_game_overlay:
            self.root.after(0, self._update_game_overlay)

    def _build_game_overlay(self):
        outer = tk.Frame(self.root, bg=BG)
        self._game_frame = outer

        # Header
        hdr = tk.Frame(outer, bg=BG)
        hdr.pack(fill="x", pady=(6, 4))
        self._game_match_label = tk.Label(
            hdr, text="In Game", font=("Segoe UI", 13, "bold"), bg=BG, fg=GOLD)
        self._game_match_label.pack(side="left", padx=8)
        self._game_wr_label = tk.Label(
            hdr, text="", font=("Segoe UI", 13, "bold"), bg=BG, fg="#aab4c8")
        self._game_wr_label.pack(side="right", padx=12)

        # Body — activity log (full width)
        log_frame = tk.Frame(outer, bg=DARK)
        log_frame.pack(fill="both", expand=True)
        log_sb = tk.Scrollbar(log_frame, bg=PANEL)
        self._game_log_widget = tk.Text(
            log_frame, bg=DARK, fg="#ccc",
            font=("Segoe UI", 11), relief="flat",
            state="disabled", wrap="word",
            padx=8, pady=6,
        )
        log_sb.configure(command=self._game_log_widget.yview)
        self._game_log_widget.configure(yscrollcommand=log_sb.set)
        log_sb.pack(side="right", fill="y")
        self._game_log_widget.pack(fill="both", expand=True)
        for tag, col in [("info","#aab4c8"),("success","#4caf73"),
                         ("warn",GOLD),("error","#e05252"),("champ","#7dbbff")]:
            self._game_log_widget.tag_config(tag, foreground=col)
        # Populate with existing log history
        self._game_log_widget.configure(state="normal")
        for m, t in self._log_buffer:
            self._game_log_widget.insert("end", m+"\n", t)
        self._game_log_widget.see("end")
        self._game_log_widget.configure(state="disabled")

    def _update_game_overlay(self):
        if self._game_match_label:
            role_str = f" ({self._game_role})" if self._game_role not in ("", "auto") else ""
            title = (f"{self._game_champ}{role_str}  vs  {self._game_enemy}"
                     if self._game_champ and self._game_enemy else "In Game")
            self._game_match_label.configure(text=title)
        if self._game_wr_label:
            self._game_wr_label.configure(text=self._game_winrate, fg=self._game_wr_color)

    def _on_game_start(self):
        self.nb.select(0)
        if self._game_frame is None:
            self._build_game_overlay()
        self._in_game_overlay = True
        self.nb.pack_forget()
        self._game_frame.pack(fill="both", expand=True, padx=16, pady=(0, 10))
        self._update_game_overlay()

        mon = _get_second_monitor_geometry()
        if mon is None:
            self._emit("  ⚠ No second monitor — overlay shown on current screen", "warn")
            return
        self._saved_geometry = self.root.geometry()
        ml, mt, mw, mh = mon
        x = ml + mw - GAME_SIZE[0] - 40
        y = mt + (mh - GAME_SIZE[1]) // 2 - 50
        self._emit(f"  → Moving to monitor at ({ml},{mt}) → window +{x}+{y}", "info")
        self.root.resizable(True, True)
        self.root.geometry(f"{GAME_SIZE[0]}x{GAME_SIZE[1]}+{x}+{y}")
        self.root.resizable(False, False)
        self.root.lift()
        self.root.attributes("-topmost", True)
        self.root.after(3000, lambda: self.root.attributes("-topmost", False))
        self._emit("Game started — overlay active on second monitor", "success")

    def _on_game_end(self):
        self._in_game_overlay = False
        if self._game_frame:
            self._game_frame.pack_forget()
        self.nb.pack(fill="both", expand=True, padx=16, pady=(0, 10))
        self.nb.select(0)
        if self._saved_geometry:
            self.root.resizable(True, True)
            self.root.geometry(self._saved_geometry)
            self.root.resizable(False, False)
            self._saved_geometry = None
        self._emit("Game ended — window restored", "info")

    # ── overrides ─────────────────────────────────────────────────────────────
    def _refresh_tree(self):
        for r in self.tree.get_children():
            self.tree.delete(r)
        for champ, d in self.overrides.all().items():
            try:
                photo = _make_spell_pair_icon(d.get("spell1", 0), d.get("spell2", 0))
            except Exception:
                photo = None
            values = (
                champ,
                d.get("role", "auto"),
                d.get("primary_tree", "—"),
                d.get("secondary_tree", "—"),
            )
            # ttk.Treeview.insert with image=None shifts the values tuple into
            # the option-name slot ("Azir auto Precision Sorcery" → unknown
            # option). Only pass image= when we actually have a PhotoImage.
            if photo is not None:
                self.tree.insert("", "end", image=photo, values=values)
            else:
                self.tree.insert("", "end", values=values)

    def _add_ov(self):
        self._show_override_editor("")

    def _edit_ov(self):
        sel = self.tree.selection()
        if not sel: return
        champ = self.tree.item(sel[0])["values"][0]
        self._show_override_editor(champ)

    def _rm_ov(self):
        sel = self.tree.selection()
        if not sel: return
        champ = self.tree.item(sel[0])["values"][0]
        if messagebox.askyesno("Remove", f"Remove custom build for {champ}?"):
            self.overrides.remove(champ); self._refresh_tree()
            self._emit(f"Removed override for {champ}", "warn")

    def _show_override_editor(self, champ: str):
        self.nb.pack_forget()
        self._override_frame = tk.Frame(self.root, bg=PANEL)
        self._override_frame.pack(fill="both", expand=True, padx=16, pady=(0, 10))
        OverrideEditorPage(
            self._override_frame, self.overrides,
            champ=champ,
            on_save=self._refresh_tree,
            on_back=self._close_override_editor,
            lcu=self.lcu,
        )

    def _close_override_editor(self):
        if getattr(self, "_override_frame", None):
            self._override_frame.destroy()
            self._override_frame = None
        self.nb.pack(fill="both", expand=True, padx=16, pady=(0, 10))
        self.nb.select(1)  # return to My Builds tab

    def _save_settings(self):
        new_url = self.server_url_v.get().strip().rstrip("/")
        if new_url:
            ugg_api.SERVER_URL = new_url
        self.overrides.save_settings({"rank": self.rank_v.get(),
            "region": self.region_v.get(), "auto_role": self.arole_v.get(),
            "trigger": self.trig_v.get(),
            "server_url": new_url})
        self._emit("Settings saved.", "success")
        # Inline confirmation next to the button — visible on the Settings tab.
        try:
            if self._settings_saved_after_id is not None:
                self.root.after_cancel(self._settings_saved_after_id)
            self._settings_saved_lbl.configure(text="Saved ✓")
            self._settings_saved_after_id = self.root.after(
                2500,
                lambda: self._settings_saved_lbl.configure(text=""))
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


def _user_data_dir() -> str:
    """%APPDATA%/RuneSync — writable on any install (including Program Files)."""
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    d = os.path.join(base, "RuneSync")
    try:
        os.makedirs(d, exist_ok=True)
    except Exception:
        # Fallback to a tmp dir so we don't crash on launch
        import tempfile
        d = tempfile.gettempdir()
    return d


if __name__ == "__main__":
    import sys, os
    # Single-instance guard: silently exit if RuneSync is already running
    _mutex = ctypes.windll.kernel32.CreateMutexW(None, False, "RuneSyncSingleInstance")
    if ctypes.windll.kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
        sys.exit(0)
    # Log to %APPDATA%/RuneSync so install location (Program Files etc) doesn't
    # matter — exe directory is write-protected for non-admin users there.
    log_path = os.path.join(_user_data_dir(), "runesync.log")
    try:
        sys.stderr = open(log_path, "a", buffering=1, encoding="utf-8")
    except Exception:
        pass  # Worst case stderr stays attached to the console (none in --windowed)
    from log_setup import init_logging
    _log_queue = init_logging(log_path)
    root = tk.Tk()
    RuneSyncApp(root)
    root.mainloop()

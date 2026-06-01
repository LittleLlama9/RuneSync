"""
item_builder.py — BuildEditorPage + BuildEditorWindow
BuildEditorPage renders into any parent frame (used as a Toplevel by BuildEditorWindow).
"""

import tkinter as tk
from tkinter import ttk
import item_data

# ── Colour palette (mirrors main.py) ──────────────────────────────────────────
BG      = "#0e1117"
PANEL   = "#1e2330"
DARK    = "#111318"
GOLD    = "#c89b3c"
BLUE    = "#1e4a8a"
BLUE_H  = "#2560b0"
RED     = "#5c1e1e"
RED_H   = "#7a2525"
PILL_BG = "#1a2438"
PILL_HL = "#253050"
DROP_BG = "#141926"
DROP_HL = "#1e2a40"

SLOT_DEFS = [
    ("starter", "Starter Items"),
    ("core",    "Core Build"),
    ("fourth",  "4th Item Options"),
    ("fifth",   "5th Item Options"),
    ("sixth",   "6th Item Options"),
]


def normalize_build(raw) -> dict:
    """Convert legacy flat list or None → structured dict."""
    empty = {k: [] for k, _ in SLOT_DEFS}
    if isinstance(raw, dict):
        merged = dict(empty)
        merged.update({k: list(v) for k, v in raw.items() if k in merged})
        return merged
    return empty


class BuildEditorPage:
    """
    Renders a full item build editor into `parent` (any tk widget).

    on_save(build_dict)  — called when user saves; build_dict keys match SLOT_DEFS.
    on_back()            — called when user cancels / goes back; caller handles cleanup.
    Each slot value is a list of {"id": int, "name": str}.
    """

    def __init__(self, parent, champion: str, role: str, current_build,
                 on_save=None, on_back=None):
        self.parent    = parent
        self.champion  = champion
        self.role      = role
        self.on_save   = on_save
        self.on_back   = on_back
        self._build    = normalize_build(current_build)
        self._active   = "starter"
        self._popup    = None
        self._results  = []
        self._slot_lbls  = {}
        self._slot_pills = {}

        item_data.init()
        self._build_ui()

    # ── UI construction ────────────────────────────────────────────────────────

    def _build_ui(self):
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

        tk.Label(hdr, text=f"Item Build  —  {self.champion}  ({self.role})",
                 font=("Segoe UI", 12, "bold"), bg=PANEL, fg=GOLD).pack(side="left", padx=6)

        # ── Search bar ────────────────────────────────────────────────────────
        sf = tk.Frame(p, bg=BG, pady=8)
        sf.pack(fill="x", padx=14)

        tk.Label(sf, text="🔍", font=("Segoe UI", 11), bg=BG, fg="#555").pack(side="left")

        self._search_var = tk.StringVar()
        self._search_var.trace_add("write", self._on_search_changed)
        self._search_entry = tk.Entry(
            sf, textvariable=self._search_var,
            bg=DARK, fg="#ccc", insertbackground="#ccc",
            relief="flat", font=("Segoe UI", 10),
        )
        self._search_entry.pack(side="left", padx=8, ipady=5, fill="x", expand=True)
        self._search_entry.bind("<FocusOut>", self._on_search_blur)
        self._search_entry.bind("<Return>",   self._on_search_return)
        self._search_entry.bind("<Escape>",   lambda e: self._close_popup())

        self._target_lbl = tk.Label(
            sf, text=f"→ {dict(SLOT_DEFS)[self._active]}",
            font=("Segoe UI", 9), bg=BG, fg="#4caf73", width=20, anchor="w",
        )
        self._target_lbl.pack(side="left", padx=(8, 0))

        tk.Frame(p, bg="#2a2a2a", height=1).pack(fill="x")

        # ── Scrollable slots ──────────────────────────────────────────────────
        wrap = tk.Frame(p, bg=BG)
        wrap.pack(fill="both", expand=True)

        canvas = tk.Canvas(wrap, bg=BG, highlightthickness=0)
        vsb    = tk.Scrollbar(wrap, orient="vertical", command=canvas.yview, bg=PANEL)
        canvas.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)

        self._slots_frame = tk.Frame(canvas, bg=BG)
        _cw = canvas.create_window((0, 0), window=self._slots_frame, anchor="nw")

        self._slots_frame.bind("<Configure>",
                               lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>",
                    lambda e: canvas.itemconfig(_cw, width=e.width))
        canvas.bind_all("<MouseWheel>",
                        lambda e: canvas.yview_scroll(-1 * (e.delta // 120), "units"))

        for slot_key, slot_label in SLOT_DEFS:
            self._make_slot_section(slot_key, slot_label)

        # ── Footer ────────────────────────────────────────────────────────────
        tk.Frame(p, bg="#2a2a2a", height=1).pack(fill="x")
        footer = tk.Frame(p, bg=PANEL, pady=10)
        footer.pack(fill="x")

        self._btn(footer, "  Save Build  ", self._do_save,
                  BLUE, BLUE_H).pack(side="left", padx=(14, 6))
        self._btn(footer, "  Clear All  ", self._clear_all,
                  "#2a2d3a", "#3a3d4a").pack(side="left", padx=4)
        self._btn(footer, "  Cancel  ", self._do_back,
                  RED, RED_H).pack(side="right", padx=14)

        self._search_entry.focus_set()

    def _btn(self, parent, text, cmd, bg, hov):
        b = tk.Label(parent, text=text, font=("Segoe UI", 9),
                     bg=bg, fg="white", padx=10, pady=5, cursor="hand2")
        b.bind("<Button-1>", lambda e: cmd())
        b.bind("<Enter>",    lambda e: b.configure(bg=hov))
        b.bind("<Leave>",    lambda e: b.configure(bg=bg))
        return b

    # ── Slot sections ──────────────────────────────────────────────────────────

    def _make_slot_section(self, slot_key: str, slot_label: str):
        section = tk.Frame(self._slots_frame, bg=BG)
        section.pack(fill="x", padx=14, pady=(10, 4))

        hrow = tk.Frame(section, bg=BG)
        hrow.pack(fill="x")

        lbl = tk.Label(hrow, text=slot_label.upper(),
                       font=("Segoe UI", 8, "bold"), bg=BG, fg="#555", cursor="hand2")
        lbl.pack(side="left")
        lbl.bind("<Button-1>", lambda e, k=slot_key: self._select_slot(k))
        lbl.bind("<Enter>",    lambda e: lbl.configure(fg=GOLD))
        lbl.bind("<Leave>",    lambda e: self._refresh_slot_label(slot_key))
        self._slot_lbls[slot_key] = lbl

        clr = tk.Label(hrow, text="Clear", font=("Segoe UI", 8),
                       bg=BG, fg="#444", cursor="hand2")
        clr.pack(side="right")
        clr.bind("<Button-1>", lambda e, k=slot_key: self._clear_slot(k))
        clr.bind("<Enter>",    lambda e: clr.configure(fg="#e05252"))
        clr.bind("<Leave>",    lambda e: clr.configure(fg="#444"))

        pills = tk.Frame(section, bg=BG, pady=4)
        pills.pack(fill="x")
        self._slot_pills[slot_key] = pills

        tk.Frame(section, bg="#1e2330", height=1).pack(fill="x", pady=(4, 0))

        self._rebuild_pills(slot_key)

    def _rebuild_pills(self, slot_key: str):
        frame = self._slot_pills[slot_key]
        for w in frame.winfo_children():
            w.destroy()

        for item in self._build.get(slot_key, []):
            pill = self._make_pill(frame, item, slot_key)
            pill.pack(side="left", padx=(0, 6), pady=2)

        add = tk.Label(frame, text="+ Add", font=("Segoe UI", 8),
                       bg="#1e2330", fg="#555", padx=8, pady=4, cursor="hand2")
        add.pack(side="left", pady=2)
        add.bind("<Button-1>", lambda e, k=slot_key: self._select_slot(k))
        add.bind("<Enter>",    lambda e: add.configure(fg=GOLD))
        add.bind("<Leave>",    lambda e: add.configure(fg="#555"))

        self._refresh_slot_label(slot_key)

    def _make_pill(self, parent, item: dict, slot_key: str) -> tk.Frame:
        pill = tk.Frame(parent, bg=PILL_BG, padx=6, pady=4)

        icon_lbl = tk.Label(pill, bg=PILL_BG, width=2, height=2)
        icon_lbl.pack(side="left")

        def _set_icon(photo, lbl=icon_lbl):
            if photo and self._alive(lbl):
                lbl.configure(image=photo, width=0, height=0)
                lbl._ref = photo

        item_data.get_icon_async(
            item["id"],
            lambda p, lbl=icon_lbl: self._safe_after(lambda: _set_icon(p, lbl)),
        )

        tk.Label(pill, text=item["name"], font=("Segoe UI", 9),
                 bg=PILL_BG, fg="#ccc").pack(side="left", padx=(6, 4))

        rm = tk.Label(pill, text="×", font=("Segoe UI", 10),
                      bg=PILL_BG, fg="#555", cursor="hand2")
        rm.pack(side="left")
        rm.bind("<Button-1>", lambda e, i=item, k=slot_key: self._remove_item(k, i))
        rm.bind("<Enter>",    lambda e: rm.configure(fg="#e05252"))
        rm.bind("<Leave>",    lambda e: rm.configure(fg="#555"))

        def _hl(on):
            c = PILL_HL if on else PILL_BG
            pill.configure(bg=c)
            for w in pill.winfo_children():
                w.configure(bg=c)

        pill.bind("<Enter>", lambda e: _hl(True))
        pill.bind("<Leave>", lambda e: _hl(False))
        return pill

    # ── Slot actions ───────────────────────────────────────────────────────────

    def _select_slot(self, slot_key: str):
        self._active = slot_key
        self._target_lbl.configure(text=f"→ {dict(SLOT_DEFS)[slot_key]}")
        for k, _ in SLOT_DEFS:
            self._refresh_slot_label(k)
        self._search_entry.focus_set()

    def _refresh_slot_label(self, slot_key: str):
        lbl = self._slot_lbls.get(slot_key)
        if lbl and self._alive(lbl):
            lbl.configure(fg=GOLD if slot_key == self._active else "#555")

    def _remove_item(self, slot_key: str, item: dict):
        self._build[slot_key] = [i for i in self._build[slot_key] if i["id"] != item["id"]]
        self._rebuild_pills(slot_key)

    def _clear_slot(self, slot_key: str):
        self._build[slot_key] = []
        self._rebuild_pills(slot_key)

    def _clear_all(self):
        for k, _ in SLOT_DEFS:
            self._build[k] = []
            self._rebuild_pills(k)

    def _add_item(self, slot_key: str, item: dict):
        slot = self._build.setdefault(slot_key, [])
        if any(i["id"] == item["id"] for i in slot):
            return
        slot.append({"id": item["id"], "name": item["name"]})
        self._rebuild_pills(slot_key)

    # ── Search / autocomplete ──────────────────────────────────────────────────

    def _on_search_changed(self, *_):
        query = self._search_var.get().strip()
        if not query:
            self._close_popup()
            return
        results = item_data.search(query, max_results=10)
        self._results = results
        self._show_popup(results)

    def _show_popup(self, results: list):
        self._close_popup()
        if not results:
            return

        self._search_entry.update_idletasks()
        x = self._search_entry.winfo_rootx()
        y = self._search_entry.winfo_rooty() + self._search_entry.winfo_height() + 2
        w = self._search_entry.winfo_width() + 160

        try:
            root = self._search_entry.winfo_toplevel()
        except Exception:
            return

        popup = tk.Toplevel(root)
        popup.wm_overrideredirect(True)
        popup.wm_geometry(f"{w}x{min(len(results) * 42, 420)}+{x}+{y}")
        popup.configure(bg="#0a0d14")
        popup.attributes("-topmost", True)
        self._popup = popup

        for item in results:
            row = tk.Frame(popup, bg=DROP_BG, pady=6, padx=10)
            row.pack(fill="x")

            icon_lbl = tk.Label(row, bg=DROP_BG, width=2)
            icon_lbl.pack(side="left")

            def _set_icon(photo, lbl=icon_lbl):
                if photo and popup.winfo_exists() and self._alive(lbl):
                    lbl.configure(image=photo, width=0)
                    lbl._ref = photo

            item_data.get_icon_async(
                item["id"],
                lambda p, lbl=icon_lbl: self._safe_after(lambda: _set_icon(p, lbl)),
            )

            name_lbl = tk.Label(row, text=item["name"], font=("Segoe UI", 9),
                                bg=DROP_BG, fg="#ccc", anchor="w", cursor="hand2")
            name_lbl.pack(side="left", padx=(10, 0), fill="x", expand=True)

            def _click(e, i=item):
                self._add_item(self._active, i)
                self._close_popup()
                self._search_var.set("")
                self._search_entry.focus_set()

            def _hover(on, r=row):
                c = DROP_HL if on else DROP_BG
                r.configure(bg=c)
                for w in r.winfo_children():
                    w.configure(bg=c)

            for widget in [row, icon_lbl, name_lbl]:
                widget.bind("<Button-1>", _click)
                widget.bind("<Enter>",    lambda e, r=row: _hover(True,  r))
                widget.bind("<Leave>",    lambda e, r=row: _hover(False, r))

    def _close_popup(self):
        if self._popup:
            try:
                if self._popup.winfo_exists():
                    self._popup.destroy()
            except Exception:
                pass
            self._popup = None

    def _on_search_blur(self, e):
        self._safe_after(self._close_popup, delay=200)

    def _on_search_return(self, e):
        if self._results:
            self._add_item(self._active, self._results[0])
            self._close_popup()
            self._search_var.set("")

    # ── Save / back ────────────────────────────────────────────────────────────

    def _do_save(self):
        self._close_popup()
        if self.on_save:
            self.on_save(self._build)
        # on_save is expected to call on_back / clean up the frame

    def _do_back(self):
        self._close_popup()
        if self.on_back:
            self.on_back()

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _safe_after(self, fn, delay=0):
        try:
            root = self.parent.winfo_toplevel()
            if root.winfo_exists():
                root.after(delay, fn)
        except Exception:
            pass

    @staticmethod
    def _alive(w) -> bool:
        try:
            return bool(w.winfo_exists())
        except Exception:
            return False


class BuildEditorWindow:
    """Opens the item build editor in its own Toplevel window."""

    def __init__(self, parent, champion: str, role: str, current_build, on_save=None):
        self.win = tk.Toplevel(parent)
        self.win.title(f"Item Build — {champion}")
        self.win.geometry("660x560")
        self.win.configure(bg=BG)
        self.win.grab_set()

        def _on_save(build):
            if on_save:
                on_save(build)
            self.win.destroy()

        BuildEditorPage(
            self.win, champion, role, current_build,
            on_save=_on_save,
            on_back=self.win.destroy,
        )


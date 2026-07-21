"""
SCP GUI — FileZilla-style two-panel file manager
=================================================
Left panel  : Local PC
Right panel : Remote host via SSH (SCP/SFTP, port 22)

Features:
• Recursive directory upload/download (PC <-> Remote) over SFTP
• Drag-and-drop between panels
• Overwrite prompt: Yes / Yes to All / No / Cancel per batch
• Auto-refresh destination panel after every transfer
• INI file stores connection settings next to this file
• Sortable columns, async worker threads, live transfer log + progress bar
"""

import tkinter as tk
from tkinter import ttk, messagebox, filedialog
import paramiko
import os
import threading
import stat
import queue
import posixpath
import configparser
import datetime

# ─────────────────────────────────────────────
# Theme (matches proxy_ftp)
# ─────────────────────────────────────────────
BG = "#1a1d23"
BG2 = "#22262f"
BG3 = "#2b303b"
ACCENT = "#00d4aa"
ACCENT2 = "#f0a500"
TXT = "#e0e6f0"
TXT_DIM = "#7a8499"
SEL_BG = "#2e4a6e"
ERR = "#e05c5c"

FONT_MONO = ("Courier New", 10)
FONT_UI = ("Segoe UI", 10) if os.name == "nt" else ("DejaVu Sans", 10)
FONT_HDR = ("Segoe UI", 11, "bold") if os.name == "nt" else ("DejaVu Sans", 11, "bold")


# ─────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────
def human_size(n):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


def icon(kind):
    return {"dir": "📁", "file": "📄", "link": "🔗"}.get(kind, "📄")


# ─────────────────────────────────────────────
# Overwrite decision constants
# ─────────────────────────────────────────────
OW_YES = "yes"
OW_YES_TO_ALL = "yes_to_all"
OW_NO = "no"
OW_CANCEL = "cancel"


class OverwriteDialog(tk.Toplevel):
    def __init__(self, parent, name):
        super().__init__(parent)
        self.title("File Exists")
        self.configure(bg=BG)
        self.resizable(False, False)
        self.result = OW_NO

        tk.Label(self, text=" ⚠ File already exists — overwrite?",
                 bg=BG, fg=ACCENT2, font=FONT_HDR).pack(pady=(14, 4), padx=20, anchor="w")
        tk.Label(self, text=f" {name}", bg=BG, fg=TXT, font=FONT_MONO,
                 wraplength=400, justify="left").pack(padx=20, pady=(0, 12), anchor="w")

        fr = tk.Frame(self, bg=BG)
        fr.pack(pady=(0, 14))
        for text, val, color in [
            ("Yes", OW_YES, ACCENT),
            ("Yes to All", OW_YES_TO_ALL, ACCENT2),
            ("No", OW_NO, BG3),
            ("Cancel", OW_CANCEL, ERR),
        ]:
            fg = BG if color != BG3 else TXT_DIM
            tk.Button(fr, text=text, bg=color, fg=fg, font=FONT_UI,
                      relief="flat", padx=12, pady=4, cursor="hand2",
                      command=lambda v=val: self._pick(v)).pack(side="left", padx=4)

        self.grab_set()
        self.wait_window()

    def _pick(self, val):
        self.result = val
        self.destroy()


# ─────────────────────────────────────────────
# INI config (stored next to this file)
# ─────────────────────────────────────────────
INI_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scp_config.ini")


class ConfigManager:
    SECTIONS = {
        "ssh": {"host": "", "port": "22", "username": "", "password": "", "keyfile": ""},
    }

    def __init__(self):
        self._cfg = configparser.ConfigParser()
        for section, defaults in self.SECTIONS.items():
            if not self._cfg.has_section(section):
                self._cfg.add_section(section)
            for key, val in defaults.items():
                if not self._cfg.has_option(section, key):
                    self._cfg.set(section, key, val)
        self.load()

    def load(self):
        if os.path.exists(INI_PATH):
            self._cfg.read(INI_PATH, encoding="utf-8")
        else:
            self.save()

    def save(self):
        with open(INI_PATH, "w", encoding="utf-8") as fh:
            self._cfg.write(fh)

    def get_section(self, section):
        return dict(self._cfg[section])

    def set_section(self, section, data):
        for key, val in data.items():
            if val is not None:
                self._cfg.set(section, key, str(val))
        self.save()


# ─────────────────────────────────────────────
# Connect dialog
# ─────────────────────────────────────────────
class ConnectDialog(tk.Toplevel):
    def __init__(self, parent, title, defaults):
        super().__init__(parent)
        self.title(title)
        self.configure(bg=BG)
        self.resizable(False, False)
        self.result = None

        fields = [("Host", defaults.get("host", "")),
                  ("Port", defaults.get("port", "22")),
                  ("Username", defaults.get("username", "")),
                  ("Password", defaults.get("password", ""))]

        self.entries = {}
        for i, (label, val) in enumerate(fields):
            tk.Label(self, text=label, bg=BG, fg=TXT, font=FONT_UI,
                     anchor="w", width=10).grid(row=i, column=0, padx=12, pady=6, sticky="w")
            e = tk.Entry(self, bg=BG3, fg=TXT, insertbackground=ACCENT,
                         font=FONT_MONO, relief="flat", bd=4,
                         show="*" if label == "Password" else "")
            e.insert(0, val)
            e.grid(row=i, column=1, padx=12, pady=6, ipadx=4, ipady=4, sticky="ew")
            self.entries[label.lower()] = e

        row = len(fields)
        tk.Label(self, text="Key file", bg=BG, fg=TXT, font=FONT_UI,
                 anchor="w", width=10).grid(row=row, column=0, padx=12, pady=6, sticky="w")
        self.key_var = tk.StringVar(value=defaults.get("keyfile", ""))
        ke = tk.Entry(self, textvariable=self.key_var, bg=BG3, fg=TXT,
                      insertbackground=ACCENT, font=FONT_MONO, relief="flat", bd=4)
        ke.grid(row=row, column=1, padx=12, pady=6, ipadx=4, ipady=4, sticky="ew")
        tk.Button(self, text="…", bg=BG3, fg=ACCENT, font=FONT_UI,
                  relief="flat", command=self._browse_key
                  ).grid(row=row, column=2, padx=4)
        row += 1

        tk.Label(self, text=" Leave password blank to use the key file only.",
                 bg=BG, fg=TXT_DIM, font=("Courier New", 8)
                 ).grid(row=row, column=0, columnspan=3, padx=12, sticky="w")
        row += 1

        self.remember_var = tk.BooleanVar(value=defaults.get("_remember", True))
        tk.Checkbutton(self, text="Remember in INI file", variable=self.remember_var,
                       bg=BG, fg=TXT_DIM, selectcolor=BG3, activebackground=BG,
                       activeforeground=ACCENT, font=FONT_UI,
                       ).grid(row=row, column=0, columnspan=3, padx=12, pady=(4, 2), sticky="w")
        row += 1
        tk.Label(self, text=f" 📄 {INI_PATH}", bg=BG, fg=TXT_DIM,
                 font=("Courier New", 8), anchor="w"
                 ).grid(row=row, column=0, columnspan=3, padx=12, sticky="w")
        row += 1

        fr = tk.Frame(self, bg=BG)
        fr.grid(row=row, column=0, columnspan=3, pady=10)
        tk.Button(fr, text="Connect", bg=ACCENT, fg=BG, font=FONT_UI,
                  relief="flat", padx=14, pady=4, cursor="hand2",
                  command=self._ok).pack(side="left", padx=6)
        tk.Button(fr, text="Cancel", bg=BG3, fg=TXT_DIM, font=FONT_UI,
                  relief="flat", padx=14, pady=4, cursor="hand2",
                  command=self.destroy).pack(side="left", padx=6)

        self.columnconfigure(1, weight=1)
        self.grab_set()
        self.wait_window()

    def _browse_key(self):
        path = filedialog.askopenfilename(title="Select private key file")
        if path:
            self.key_var.set(path)

    def _ok(self):
        self.result = {k: e.get() for k, e in self.entries.items()}
        self.result["keyfile"] = self.key_var.get()
        self.result["_remember"] = self.remember_var.get()
        self.destroy()


# ─────────────────────────────────────────────
# File panel (with drag-and-drop, sorting)
# ─────────────────────────────────────────────
class FilePanel(tk.Frame):
    def __init__(self, parent, label, color_accent=ACCENT, **kw):
        super().__init__(parent, bg=BG2,
                          highlightthickness=2,
                          highlightbackground=BG3,
                          highlightcolor=BG3,
                          **kw)
        self.label = label
        self.accent = color_accent
        self._items = []
        self._current_path = "/"
        self._nav_callback = lambda p: None
        self._drop_callback = None  # (source_panel, names) -> None

        self._sort_col = "#0"
        self._sort_rev = False

        self._drag = {"active": False, "x0": 0, "y0": 0, "ghost": None}
        self._build()

    def _build(self):
        hdr = tk.Frame(self, bg=self.accent, height=28)
        hdr.pack(fill="x")
        tk.Label(hdr, text=f" {self.label}", bg=self.accent, fg=BG,
                 font=FONT_HDR, anchor="w").pack(side="left", pady=3)
        tk.Button(hdr, text="⟳", bg=self.accent, fg=BG,
                  font=FONT_HDR, relief="flat", cursor="hand2", padx=6,
                  activebackground=BG, activeforeground=self.accent,
                  command=self.refresh).pack(side="right", pady=2, padx=4)

        path_fr = tk.Frame(self, bg=BG3, pady=2)
        path_fr.pack(fill="x", padx=2, pady=(2, 0))
        tk.Label(path_fr, text="Path:", bg=BG3, fg=TXT_DIM,
                  font=FONT_UI).pack(side="left", padx=6)
        self.path_var = tk.StringVar(value="/")
        pe = tk.Entry(path_fr, textvariable=self.path_var, bg=BG,
                      fg=self.accent, font=FONT_MONO, relief="flat",
                      insertbackground=self.accent, bd=2)
        pe.pack(side="left", fill="x", expand=True, ipady=3, padx=4)
        pe.bind("<Return>", lambda e: self._navigate_to(self.path_var.get()))
        tk.Button(path_fr, text="↑", bg=BG3, fg=self.accent, font=FONT_HDR,
                  relief="flat", cursor="hand2",
                  command=self._go_up).pack(side="left", padx=2)

        tree_fr = tk.Frame(self, bg=BG2)
        tree_fr.pack(fill="both", expand=True, padx=2, pady=2)

        uid = self.label.replace(" ", "_")
        style = ttk.Style()
        style.theme_use("default")
        style.configure(f"{uid}.Treeview",
                        background=BG2, fieldbackground=BG2, foreground=TXT,
                        rowheight=22, font=FONT_MONO, borderwidth=0)
        style.configure(f"{uid}.Treeview.Heading",
                        background=BG3, foreground=self.accent,
                        font=FONT_UI, relief="flat")
        style.map(f"{uid}.Treeview",
                  background=[("selected", SEL_BG)],
                  foreground=[("selected", TXT)])

        self.tree = ttk.Treeview(tree_fr, style=f"{uid}.Treeview",
                                  columns=("size", "type", "modified"),
                                  selectmode="extended")
        self.tree.heading("#0", text="Name", anchor="w",
                           command=lambda: self._sort_by("#0"))
        self.tree.heading("size", text="Size", anchor="e",
                           command=lambda: self._sort_by("size"))
        self.tree.heading("type", text="Type", anchor="w",
                           command=lambda: self._sort_by("type"))
        self.tree.heading("modified", text="Modified", anchor="w",
                           command=lambda: self._sort_by("modified"))
        self.tree.column("#0", width=220, stretch=True)
        self.tree.column("size", width=80, anchor="e", stretch=False)
        self.tree.column("type", width=60, anchor="w", stretch=False)
        self.tree.column("modified", width=140, anchor="w", stretch=False)

        vsb = ttk.Scrollbar(tree_fr, orient="vertical", command=self.tree.yview)
        hsb = ttk.Scrollbar(tree_fr, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")
        tree_fr.rowconfigure(0, weight=1)
        tree_fr.columnconfigure(0, weight=1)

        self.tree.bind("<Double-1>", self._on_double_click)
        self.tree.bind("<ButtonPress-1>", self._on_press)
        self.tree.bind("<B1-Motion>", self._drag_motion)
        self.tree.bind("<ButtonRelease-1>", self._drag_release)
        self.tree.bind("<FocusIn>", lambda e: self._set_focus(True))
        self.tree.bind("<FocusOut>", lambda e: self._set_focus(False))
        self.tree.bind("<Control-a>", lambda e: self._select_all())
        self.tree.bind("<Control-A>", lambda e: self._select_all())

        self.status_var = tk.StringVar(value="Not connected")
        tk.Label(self, textvariable=self.status_var, bg=BG, fg=TXT_DIM,
                 font=("Courier New", 9), anchor="w").pack(fill="x", padx=6, pady=2)

    def _set_focus(self, focused):
        color = self.accent if focused else BG3
        self.config(highlightbackground=color, highlightcolor=color)

    def _select_all(self):
        for item in self.tree.get_children():
            self.tree.selection_add(item)
        return "break"

    def populate(self, items, path):
        self._items = items
        self._current_path = path
        self.path_var.set(path)
        self._render_sorted()
        dirs = [i for i in items if i["kind"] == "dir"]
        files = [i for i in items if i["kind"] != "dir"]
        self.status_var.set(f"{len(dirs)} dirs, {len(files)} files — {path}")

    def _render_sorted(self):
        col = self._sort_col
        rev = self._sort_rev

        def sort_key(entry):
            if col == "#0":
                return entry["name"].lower()
            elif col == "size":
                return entry.get("size", 0)
            elif col == "type":
                return entry.get("kind", "")
            elif col == "modified":
                return entry.get("mtime", "")
            return ""

        dirs = [i for i in self._items if i["kind"] == "dir"]
        files = [i for i in self._items if i["kind"] != "dir"]
        dirs_sorted = sorted(dirs, key=sort_key, reverse=rev)
        files_sorted = sorted(files, key=sort_key, reverse=rev)

        self.tree.delete(*self.tree.get_children())
        for entry in dirs_sorted + files_sorted:
            ic = icon(entry["kind"])
            sz = human_size(entry.get("size", 0)) if entry["kind"] == "file" else ""
            self.tree.insert("", "end", text=f" {ic} {entry['name']}",
                              values=(sz, entry["kind"], entry.get("mtime", "")),
                              tags=(entry["kind"],))

        labels = {"#0": "Name", "size": "Size", "type": "Type", "modified": "Modified"}
        anchors = {"#0": "w", "size": "e", "type": "w", "modified": "w"}
        for cid, base in labels.items():
            if cid == col:
                arrow = " ▲" if not rev else " ▼"
                self.tree.heading(cid, text=base + arrow, anchor=anchors[cid])
            else:
                self.tree.heading(cid, text=base, anchor=anchors[cid])

    def _sort_by(self, col):
        if self._sort_col == col:
            self._sort_rev = not self._sort_rev
        else:
            self._sort_col = col
            self._sort_rev = False
        self._render_sorted()

    def set_status(self, msg):
        self.status_var.set(msg)

    def selected_names(self):
        return [self.tree.item(i, "text").strip().split(" ", 1)[-1]
                for i in self.tree.selection()]

    def current_path(self):
        return self._current_path

    def set_nav_callback(self, cb):
        self._nav_callback = cb

    def set_drop_callback(self, cb):
        self._drop_callback = cb

    def refresh(self):
        self._nav_callback(self._current_path)

    def _on_double_click(self, _event):
        sel = self.tree.selection()
        if not sel:
            return
        name = self.tree.item(sel[0], "text").strip().split(" ", 1)[-1]
        kind = self.tree.item(sel[0], "values")[1]
        if kind == "dir":
            self._navigate_to(posixpath.join(self._current_path, name)
                               if self._current_path != "\\" else name)

    def _go_up(self):
        if os.sep == "\\" and self.label.startswith("💻"):
            parent = os.path.dirname(self._current_path.rstrip("\\/")) or self._current_path
        else:
            parent = posixpath.dirname(self._current_path.rstrip("/")) or "/"
        self._navigate_to(parent)

    def _navigate_to(self, path):
        self._nav_callback(path)

    def _on_press(self, event):
        self.tree.focus_set()
        item = self.tree.identify_row(event.y)
        self._drag["active"] = False
        self._drag["x0"] = event.x_root
        self._drag["y0"] = event.y_root
        self._drag["press_item"] = item
        self._drag["deferred"] = False
        if self._drag.get("ghost"):
            self._drag["ghost"].destroy()
            self._drag["ghost"] = None
        if item and item in self.tree.selection():
            self._drag["deferred"] = True
            return "break"

    def _drag_motion(self, event):
        dx = abs(event.x_root - self._drag["x0"])
        dy = abs(event.y_root - self._drag["y0"])
        if dx > 8 or dy > 8:
            self._drag["active"] = True
            root = self.winfo_toplevel()
            rx = event.x_root - root.winfo_rootx()
            ry = event.y_root - root.winfo_rooty()
            if not self._drag.get("ghost"):
                names = self.selected_names()
                lbl = names[0] if len(names) == 1 else f"{len(names)} items"
                g = tk.Label(root, text=f" ✈ {lbl} ",
                             bg=SEL_BG, fg=TXT, font=FONT_UI,
                             relief="solid", bd=1)
                g.place(x=rx + 14, y=ry + 10)
                self._drag["ghost"] = g
            else:
                self._drag["ghost"].place(x=rx + 14, y=ry + 10)

    def _drag_release(self, event):
        ghost = self._drag.get("ghost")
        if ghost:
            ghost.destroy()
            self._drag["ghost"] = None
        if not self._drag["active"]:
            if self._drag.get("deferred"):
                item = self._drag.get("press_item")
                if item:
                    self.tree.selection_set(item)
                    self.tree.focus(item)
                self._drag["deferred"] = False
            return
        self._drag["active"] = False
        self._drag["deferred"] = False

        rx, ry = event.x_root, event.y_root

        def _find_panel(widget):
            if isinstance(widget, FilePanel) and widget is not self:
                wx = widget.winfo_rootx()
                wy = widget.winfo_rooty()
                if wx <= rx <= wx + widget.winfo_width() and \
                        wy <= ry <= wy + widget.winfo_height():
                    return widget
            for child in widget.winfo_children():
                r = _find_panel(child)
                if r:
                    return r
            return None

        target = _find_panel(self.winfo_toplevel())
        if target and target._drop_callback:
            names = self.selected_names()
            if names:
                target._drop_callback(self, names)


# ─────────────────────────────────────────────
# Transfer log
# ─────────────────────────────────────────────
class LogPane(tk.Frame):
    def __init__(self, parent, **kw):
        super().__init__(parent, bg=BG, **kw)

        hdr = tk.Frame(self, bg=BG3)
        hdr.pack(fill="x")
        tk.Label(hdr, text=" Transfer Log", bg=BG3, fg=ACCENT2,
                 font=FONT_HDR, anchor="w").pack(side="left")
        tk.Button(hdr, text="✕ Clear", bg=BG3, fg=TXT_DIM, font=FONT_UI,
                  relief="flat", cursor="hand2", padx=8,
                  command=self.clear_log).pack(side="right", padx=4, pady=2)

        cmd_fr = tk.Frame(self, bg=BG)
        cmd_fr.pack(fill="x", padx=4, pady=(2, 0))
        tk.Label(cmd_fr, text="CMD:", bg=BG, fg=TXT_DIM,
                  font=FONT_UI).pack(side="left")
        self._cmd_var = tk.StringVar(value="—")
        tk.Label(cmd_fr, textvariable=self._cmd_var, bg=BG, fg=ACCENT,
                  font=FONT_MONO, anchor="w").pack(side="left", padx=6, fill="x", expand=True)

        prog_fr = tk.Frame(self, bg=BG)
        prog_fr.pack(fill="x", padx=4, pady=(2, 2))
        self._prog_label = tk.Label(prog_fr, text="", bg=BG, fg=TXT_DIM,
                                     font=FONT_UI, width=28, anchor="w")
        self._prog_label.pack(side="left")
        self._prog_bar = ttk.Progressbar(prog_fr, orient="horizontal",
                                          mode="determinate", maximum=100)
        self._prog_bar.pack(side="left", fill="x", expand=True, padx=(4, 0))
        self._prog_pct = tk.Label(prog_fr, text="", bg=BG, fg=ACCENT2,
                                   font=FONT_UI, width=6, anchor="e")
        self._prog_pct.pack(side="left", padx=(4, 0))

        txt_fr = tk.Frame(self, bg=BG)
        txt_fr.pack(fill="both", expand=True)
        self.text = tk.Text(txt_fr, bg=BG, fg=TXT_DIM, font=("Courier New", 9),
                             relief="flat", state="disabled", height=6,
                             wrap="none", insertbackground=ACCENT)
        sb = ttk.Scrollbar(txt_fr, command=self.text.yview)
        self.text.configure(yscrollcommand=sb.set)
        self.text.pack(side="left", fill="both", expand=True, padx=(4, 0), pady=2)
        sb.pack(side="right", fill="y")

    def log(self, msg, tag="info"):
        colours = {"info": TXT_DIM, "ok": ACCENT, "err": ERR, "xfer": ACCENT2}
        self.text.configure(state="normal")
        self.text.tag_configure(tag, foreground=colours.get(tag, TXT_DIM))
        ts = datetime.datetime.now().strftime("%H:%M:%S")
        self.text.insert("end", f"[{ts}] {msg}\n", tag)
        self.text.see("end")
        self.text.configure(state="disabled")

    def set_cmd(self, cmd):
        self._cmd_var.set(cmd or "—")

    def set_progress(self, label, pct):
        self._prog_label.config(text=label[:36] if label else "")
        self._prog_bar["value"] = max(0, min(100, pct))
        self._prog_pct.config(text=f"{int(pct):3d}%" if pct > 0 else "")

    def clear_progress(self):
        self._prog_label.config(text="")
        self._prog_bar["value"] = 0
        self._prog_pct.config(text="")
        self._cmd_var.set("—")

    def clear_log(self):
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        self.text.configure(state="disabled")
        self.clear_progress()


# ─────────────────────────────────────────────
# Main Application
# ─────────────────────────────────────────────
class ScpGui(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("SCP GUI — PC ▶ Remote Host (SSH/SCP)")
        self.geometry("1180x780")
        self.configure(bg=BG)
        self.minsize(820, 560)

        self._ssh: paramiko.SSHClient | None = None
        self._sftp: paramiko.SFTPClient | None = None
        self._remote_info = {}

        self._cfg = ConfigManager()
        self._q = queue.Queue()
        self._busy_count = 0
        self._spinner_idx = 0

        self._build_ui()
        self._after_poll()
        self._spin_tick()

    # ── UI ────────────────────────────────────────────────────────────────
    def _build_ui(self):
        tb = tk.Frame(self, bg=BG3, pady=4)
        tb.pack(fill="x")

        def tbtn(text, cmd, color=ACCENT):
            return tk.Button(tb, text=text, command=cmd, bg=BG3, fg=color,
                              font=FONT_UI, relief="flat", padx=10, pady=3,
                              activebackground=BG, activeforeground=color,
                              cursor="hand2")

        for widget, side in [
            (tbtn("🔌 Connect", self._connect_remote), "left"),
            (tbtn("🔌 Disconnect", self._disconnect_remote, ERR), "left"),
            (tk.Frame(tb, bg=TXT_DIM, width=1), "left"),
            (tbtn("⬆ Upload (PC → Remote)", self._upload_selected, "#c084fc"), "left"),
            (tbtn("⬇ Download (Remote → PC)", self._download_selected, "#c084fc"), "left"),
            (tk.Frame(tb, bg=TXT_DIM, width=1), "left"),
            (tbtn("🗑 Delete", self._delete_selected, ERR), "left"),
            (tbtn("📁 New Folder", self._new_folder), "left"),
        ]:
            kw = {"side": side, "padx": 3}
            if isinstance(widget, tk.Frame):
                kw.update({"fill": "y", "pady": 4})
            widget.pack(**kw)

        self._remote_dot = tk.Label(tb, text="● Remote: disconnected", bg=BG3, fg=ERR, font=FONT_UI)
        self._busy_label = tk.Label(tb, text="", bg=BG3, fg=ACCENT, font=FONT_MONO, width=3)
        self._remote_dot.pack(side="right", padx=10)
        self._busy_label.pack(side="right", padx=(0, 4))

        panels = tk.PanedWindow(self, orient="horizontal", bg=BG,
                                 sashwidth=5, sashrelief="flat", sashpad=2)
        panels.pack(fill="both", expand=True, padx=4, pady=4)

        self.local_pane = FilePanel(panels, "💻 Local (This PC)", color_accent="#5c9eff")
        self.remote_pane = FilePanel(panels, "🌐 Remote [SSH/SCP:22]", color_accent=ACCENT)
        panels.add(self.local_pane, minsize=280, stretch="always")
        panels.add(self.remote_pane, minsize=280, stretch="always")

        self.local_pane.set_nav_callback(self._local_navigate)
        self.remote_pane.set_nav_callback(self._remote_navigate)

        self.local_pane.set_drop_callback(self._drop_onto_local_from_remote)
        self.remote_pane.set_drop_callback(self._drop_onto_remote_from_local)

        self.log = LogPane(self)
        self.log.pack(fill="x", padx=4, pady=(0, 4))

        self._local_navigate(os.path.expanduser("~"))

    # ── Busy / spinner ───────────────────────────────────────────────────
    SPINNER = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]

    def _set_busy(self, busy: bool):
        self._busy_count += 1 if busy else -1
        self._busy_count = max(0, self._busy_count)

    def _spin_tick(self):
        if self._busy_count > 0:
            self._busy_label.config(text=self.SPINNER[self._spinner_idx % len(self.SPINNER)])
            self._spinner_idx += 1
        else:
            self._busy_label.config(text="")
            self._spinner_idx = 0
        self.after(100, self._spin_tick)

    # ── Message pump ─────────────────────────────────────────────────────
    def _after_poll(self):
        try:
            while True:
                msg = self._q.get_nowait()
                kind = msg.get("kind")
                if kind == "log":
                    self.log.log(msg["text"], msg.get("tag", "info"))
                elif kind == "remote_populate":
                    self.remote_pane.populate(msg["items"], msg["path"])
                elif kind == "remote_connected":
                    self._remote_dot.config(text="● Remote: connected", fg=ACCENT)
                elif kind == "remote_disconnected":
                    self._remote_dot.config(text="● Remote: disconnected", fg=ERR)
                    self._sftp = None
                    self._ssh = None
                    self.remote_pane.populate([], "/")
                    self.remote_pane.set_status("Not connected")
                elif kind == "ask_overwrite":
                    dlg = OverwriteDialog(self, msg["name"])
                    msg["holder"][0] = dlg.result
                    msg["event"].set()
                elif kind == "local_populate":
                    self.local_pane.populate(msg["items"], msg["path"])
                elif kind == "local_error":
                    messagebox.showerror("Local Error", msg["text"])
                elif kind == "refresh_local":
                    self._local_navigate(self.local_pane.current_path())
                elif kind == "refresh_remote":
                    self._start_worker(self._sftp_list, args=(self.remote_pane.current_path(),))
                elif kind == "busy":
                    self._set_busy(msg["state"])
                elif kind == "set_cmd":
                    self.log.set_cmd(msg["text"])
                elif kind == "set_progress":
                    self.log.set_progress(msg["label"], msg["pct"])
                elif kind == "clear_progress":
                    self.log.clear_progress()
        except queue.Empty:
            pass
        self.after(120, self._after_poll)

    def _post(self, **kw):
        self._q.put(kw)

    def _start_worker(self, target, args=(), daemon=True):
        def _wrap(*a):
            self._post(kind="busy", state=True)
            try:
                target(*a)
            finally:
                self._post(kind="busy", state=False)
        t = threading.Thread(target=_wrap, args=args, daemon=daemon)
        t.start()
        return t

    def _ask_overwrite(self, name, yes_to_all):
        if yes_to_all[0]:
            return OW_YES
        ev = threading.Event()
        holder = [None]
        self._post(kind="ask_overwrite", name=name, event=ev, holder=holder)
        ev.wait()
        decision = holder[0]
        if decision == OW_YES_TO_ALL:
            yes_to_all[0] = True
            return OW_YES
        return decision

    # ── Local navigation ─────────────────────────────────────────────────
    def _local_navigate(self, path):
        self._start_worker(self._local_list, args=(path,))

    def _local_list(self, path):
        try:
            entries = []
            for name in os.listdir(path):
                full = os.path.join(path, name)
                try:
                    st = os.stat(full)
                    kind = "dir" if os.path.isdir(full) else "file"
                    mt = datetime.datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M")
                    entries.append({"name": name, "size": st.st_size, "kind": kind, "mtime": mt})
                except Exception:
                    entries.append({"name": name, "size": 0, "kind": "file", "mtime": ""})
            self._post(kind="local_populate", items=entries, path=path)
        except Exception as ex:
            self._post(kind="local_error", text=str(ex))

    # ── Remote / SSH connect ─────────────────────────────────────────────
    def _connect_remote(self):
        saved = self._cfg.get_section("ssh")
        dlg = ConnectDialog(self, "Connect to Remote Host (SSH/SCP)", saved)
        if not dlg.result:
            return
        self._remote_info = dlg.result
        if dlg.result.get("_remember"):
            self._cfg.set_section("ssh", {k: v for k, v in dlg.result.items()
                                           if not k.startswith("_")})
        self._start_worker(self._ssh_connect_thread, args=(dlg.result,))

    def _disconnect_remote(self):
        try:
            if self._sftp:
                self._sftp.close()
            if self._ssh:
                self._ssh.close()
        except Exception:
            pass
        self._post(kind="remote_disconnected")
        self._post(kind="log", text="Disconnected from remote host", tag="info")

    def _ssh_connect_thread(self, info):
        self._post(kind="log", text=f"Connecting to {info['host']}:{info['port']} …", tag="info")
        try:
            client = paramiko.SSHClient()
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            kw = dict(hostname=info["host"],
                      port=int(info.get("port") or 22),
                      username=info["username"],
                      timeout=15)
            if info.get("keyfile"):
                kw["key_filename"] = info["keyfile"]
            if info.get("password"):
                kw["password"] = info["password"]
            client.connect(**kw)
            sftp = client.open_sftp()
            self._ssh = client
            self._sftp = sftp
            self._post(kind="remote_connected")
            self._post(kind="log", text=f"✓ Connected to {info['host']} (SFTP/SCP channel open)", tag="ok")
            try:
                start_path = sftp.normalize(".")
            except Exception:
                start_path = "/"
            self._sftp_list(start_path)
        except Exception as ex:
            self._post(kind="log", text=f"✗ SSH error: {ex}", tag="err")

    def _sftp_list(self, path):
        if not self._sftp:
            self._post(kind="log", text="Not connected to a remote host.", tag="err")
            return
        try:
            items = []
            for attr in self._sftp.listdir_attr(path):
                kind = "dir" if stat.S_ISDIR(attr.st_mode or 0) else "file"
                mt = datetime.datetime.fromtimestamp(attr.st_mtime or 0).strftime(
                    "%Y-%m-%d %H:%M") if attr.st_mtime else ""
                items.append({"name": attr.filename, "size": attr.st_size or 0,
                              "kind": kind, "mtime": mt})
            self._post(kind="remote_populate", items=items, path=path)
        except Exception as ex:
            self._post(kind="log", text=f"SFTP list error: {ex}", tag="err")

    def _remote_navigate(self, path):
        if not self._sftp:
            messagebox.showinfo("Not connected", "Connect to a remote host first.")
            return
        self._start_worker(self._sftp_list, args=(path,))

    # ── Transfers ─────────────────────────────────────────────────────────
    def _upload_selected(self):
        if not self._sftp:
            messagebox.showinfo("Not connected", "Connect to a remote host first.")
            return
        names = self.local_pane.selected_names()
        if not names:
            messagebox.showinfo("Nothing selected", "Select file(s)/folder(s) in the Local panel.")
            return
        self._start_worker(self._upload_thread,
                            args=(self.local_pane.current_path(), names, self.remote_pane.current_path()))

    def _download_selected(self):
        if not self._sftp:
            messagebox.showinfo("Not connected", "Connect to a remote host first.")
            return
        names = self.remote_pane.selected_names()
        if not names:
            messagebox.showinfo("Nothing selected", "Select file(s)/folder(s) in the Remote panel.")
            return
        self._start_worker(self._download_thread,
                            args=(self.remote_pane.current_path(), names, self.local_pane.current_path()))

    def _drop_onto_remote_from_local(self, source_panel, names):
        if not self._sftp:
            self._post(kind="log", text="Connect to a remote host first.", tag="err")
            return
        self._start_worker(self._upload_thread,
                            args=(self.local_pane.current_path(), names, self.remote_pane.current_path()))

    def _drop_onto_local_from_remote(self, source_panel, names):
        if not self._sftp:
            self._post(kind="log", text="Connect to a remote host first.", tag="err")
            return
        self._start_worker(self._download_thread,
                            args=(self.remote_pane.current_path(), names, self.local_pane.current_path()))

    def _progress_cb_factory(self, label):
        def cb(transferred, total):
            pct = (transferred / total * 100) if total else 0
            self._post(kind="set_progress", label=label, pct=pct)
        return cb

    def _upload_thread(self, local_dir, names, remote_dir):
        yes_to_all = [False]
        for name in names:
            local_path = os.path.join(local_dir, name)
            remote_path = posixpath.join(remote_dir, name)
            try:
                if os.path.isdir(local_path):
                    self._upload_dir(local_path, remote_path, yes_to_all)
                else:
                    self._upload_file(local_path, remote_path, yes_to_all)
            except _Cancelled:
                self._post(kind="log", text="Upload cancelled.", tag="info")
                break
            except Exception as ex:
                self._post(kind="log", text=f"✗ Upload error ({name}): {ex}", tag="err")
        self._post(kind="clear_progress")
        self._post(kind="refresh_remote")

    def _download_thread(self, remote_dir, names, local_dir):
        yes_to_all = [False]
        for name in names:
            remote_path = posixpath.join(remote_dir, name)
            local_path = os.path.join(local_dir, name)
            try:
                if self._remote_is_dir(remote_path):
                    self._download_dir(remote_path, local_path, yes_to_all)
                else:
                    self._download_file(remote_path, local_path, yes_to_all)
            except _Cancelled:
                self._post(kind="log", text="Download cancelled.", tag="info")
                break
            except Exception as ex:
                self._post(kind="log", text=f"✗ Download error ({name}): {ex}", tag="err")
        self._post(kind="clear_progress")
        self._post(kind="refresh_local")

    def _remote_is_dir(self, path):
        try:
            return stat.S_ISDIR(self._sftp.stat(path).st_mode)
        except Exception:
            return False

    def _remote_exists(self, path):
        try:
            self._sftp.stat(path)
            return True
        except IOError:
            return False

    def _upload_file(self, local_path, remote_path, yes_to_all):
        name = os.path.basename(local_path)
        if self._remote_exists(remote_path):
            decision = self._ask_overwrite(name, yes_to_all)
            if decision == OW_CANCEL:
                raise _Cancelled()
            if decision == OW_NO:
                self._post(kind="log", text=f"⤼ Skipped {name}", tag="info")
                return
        self._post(kind="set_cmd", text=f"scp put {local_path} -> {remote_path}")
        self._post(kind="log", text=f"⬆ Uploading {name} …", tag="xfer")
        self._sftp.put(local_path, remote_path, callback=self._progress_cb_factory(name))
        self._post(kind="log", text=f"✓ Uploaded {name}", tag="ok")

    def _upload_dir(self, local_path, remote_path, yes_to_all):
        name = os.path.basename(local_path.rstrip(os.sep))
        if not self._remote_exists(remote_path):
            self._sftp.mkdir(remote_path)
            self._post(kind="log", text=f"📁 Created remote dir {remote_path}", tag="info")
        for entry in os.listdir(local_path):
            lp = os.path.join(local_path, entry)
            rp = posixpath.join(remote_path, entry)
            if os.path.isdir(lp):
                self._upload_dir(lp, rp, yes_to_all)
            else:
                self._upload_file(lp, rp, yes_to_all)

    def _download_file(self, remote_path, local_path, yes_to_all):
        name = os.path.basename(remote_path)
        if os.path.exists(local_path):
            decision = self._ask_overwrite(name, yes_to_all)
            if decision == OW_CANCEL:
                raise _Cancelled()
            if decision == OW_NO:
                self._post(kind="log", text=f"⤼ Skipped {name}", tag="info")
                return
        self._post(kind="set_cmd", text=f"scp get {remote_path} -> {local_path}")
        self._post(kind="log", text=f"⬇ Downloading {name} …", tag="xfer")
        self._sftp.get(remote_path, local_path, callback=self._progress_cb_factory(name))
        self._post(kind="log", text=f"✓ Downloaded {name}", tag="ok")

    def _download_dir(self, remote_path, local_path, yes_to_all):
        os.makedirs(local_path, exist_ok=True)
        for attr in self._sftp.listdir_attr(remote_path):
            rp = posixpath.join(remote_path, attr.filename)
            lp = os.path.join(local_path, attr.filename)
            if stat.S_ISDIR(attr.st_mode or 0):
                self._download_dir(rp, lp, yes_to_all)
            else:
                self._download_file(rp, lp, yes_to_all)

    # ── Delete / New Folder ──────────────────────────────────────────────
    def _delete_selected(self):
        focused = self.focus_get()
        panel = self.remote_pane if self._widget_in(focused, self.remote_pane) else self.local_pane
        names = panel.selected_names()
        if not names:
            messagebox.showinfo("Nothing selected", "Select item(s) to delete first.")
            return
        if not messagebox.askyesno("Confirm Delete", f"Delete {len(names)} item(s) from "
                                                       f"{'Remote' if panel is self.remote_pane else 'Local'}?"):
            return
        if panel is self.remote_pane:
            if not self._sftp:
                return
            self._start_worker(self._delete_remote_thread, args=(panel.current_path(), names))
        else:
            self._start_worker(self._delete_local_thread, args=(panel.current_path(), names))

    def _widget_in(self, widget, panel):
        while widget is not None:
            if widget is panel:
                return True
            widget = widget.master
        return False

    def _delete_remote_thread(self, path, names):
        for name in names:
            full = posixpath.join(path, name)
            try:
                self._delete_remote_recursive(full)
                self._post(kind="log", text=f"🗑 Deleted {name} (remote)", tag="ok")
            except Exception as ex:
                self._post(kind="log", text=f"✗ Delete error ({name}): {ex}", tag="err")
        self._post(kind="refresh_remote")

    def _delete_remote_recursive(self, path):
        if self._remote_is_dir(path):
            for attr in self._sftp.listdir_attr(path):
                self._delete_remote_recursive(posixpath.join(path, attr.filename))
            self._sftp.rmdir(path)
        else:
            self._sftp.remove(path)

    def _delete_local_thread(self, path, names):
        import shutil
        for name in names:
            full = os.path.join(path, name)
            try:
                if os.path.isdir(full):
                    shutil.rmtree(full)
                else:
                    os.remove(full)
                self._post(kind="log", text=f"🗑 Deleted {name} (local)", tag="ok")
            except Exception as ex:
                self._post(kind="log", text=f"✗ Delete error ({name}): {ex}", tag="err")
        self._post(kind="refresh_local")

    def _new_folder(self):
        focused = self.focus_get()
        panel = self.remote_pane if self._widget_in(focused, self.remote_pane) else self.local_pane
        name = _ask_string(self, "New Folder", "Folder name:")
        if not name:
            return
        if panel is self.remote_pane:
            if not self._sftp:
                messagebox.showinfo("Not connected", "Connect to a remote host first.")
                return
            self._start_worker(self._mkdir_remote_thread, args=(panel.current_path(), name))
        else:
            self._start_worker(self._mkdir_local_thread, args=(panel.current_path(), name))

    def _mkdir_remote_thread(self, path, name):
        try:
            self._sftp.mkdir(posixpath.join(path, name))
            self._post(kind="log", text=f"📁 Created remote folder {name}", tag="ok")
        except Exception as ex:
            self._post(kind="log", text=f"✗ mkdir error: {ex}", tag="err")
        self._post(kind="refresh_remote")

    def _mkdir_local_thread(self, path, name):
        try:
            os.makedirs(os.path.join(path, name), exist_ok=False)
            self._post(kind="log", text=f"📁 Created local folder {name}", tag="ok")
        except Exception as ex:
            self._post(kind="log", text=f"✗ mkdir error: {ex}", tag="err")
        self._post(kind="refresh_local")


class _Cancelled(Exception):
    pass


def _ask_string(parent, title, prompt):
    """Small themed replacement for simpledialog.askstring."""
    dlg = tk.Toplevel(parent)
    dlg.title(title)
    dlg.configure(bg=BG)
    dlg.resizable(False, False)
    result = {"value": None}

    tk.Label(dlg, text=prompt, bg=BG, fg=TXT, font=FONT_UI).pack(padx=16, pady=(14, 4), anchor="w")
    e = tk.Entry(dlg, bg=BG3, fg=TXT, insertbackground=ACCENT, font=FONT_MONO, relief="flat", bd=4)
    e.pack(padx=16, pady=(0, 10), fill="x", ipady=4)
    e.focus_set()

    def ok(_=None):
        result["value"] = e.get().strip()
        dlg.destroy()

    fr = tk.Frame(dlg, bg=BG)
    fr.pack(pady=(0, 14))
    tk.Button(fr, text="OK", bg=ACCENT, fg=BG, font=FONT_UI, relief="flat",
              padx=14, pady=4, cursor="hand2", command=ok).pack(side="left", padx=6)
    tk.Button(fr, text="Cancel", bg=BG3, fg=TXT_DIM, font=FONT_UI, relief="flat",
              padx=14, pady=4, cursor="hand2", command=dlg.destroy).pack(side="left", padx=6)
    e.bind("<Return>", ok)

    dlg.grab_set()
    dlg.wait_window()
    return result["value"]


if __name__ == "__main__":
    app = ScpGui()
    app.mainloop()

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
import difflib
import shlex
import socket
import ftplib
import ssl
import re
import io

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
# Native SCP protocol (legacy "scp -t"/"scp -f" wire protocol)
# Directory browsing/listing/mkdir/delete always go through SFTP —
# this is only used for the actual file transfer when the user
# picks "SCP" as the transfer protocol in the connect dialog.
# ─────────────────────────────────────────────
class ScpProtocolError(IOError):
    pass


def _scp_read_ack(channel):
    data = channel.recv(1)
    if not data:
        raise ScpProtocolError("SCP: connection closed unexpectedly")
    if data[0] != 0:
        msg = b""
        while True:
            b = channel.recv(1)
            if not b or b == b"\n":
                break
            msg += b
        raise ScpProtocolError(f"SCP error: {msg.decode(errors='replace')}")


def _scp_read_line(channel):
    buf = b""
    while True:
        b = channel.recv(1)
        if not b or b == b"\n":
            break
        buf += b
    return buf.decode(errors="replace")


def scp_put(ssh_client, local_path, remote_path, callback=None):
    """Upload local_path -> remote_path using the raw SCP protocol."""
    size = os.path.getsize(local_path)
    mode = oct(os.stat(local_path).st_mode & 0o777)[2:].zfill(4)
    remote_dir = posixpath.dirname(remote_path) or "."
    remote_name = posixpath.basename(remote_path)

    channel = ssh_client.get_transport().open_session()
    channel.exec_command(f"scp -t {shlex.quote(remote_dir)}")
    try:
        _scp_read_ack(channel)
        channel.sendall(f"C{mode} {size} {remote_name}\n".encode())
        _scp_read_ack(channel)
        sent = 0
        with open(local_path, "rb") as fh:
            while True:
                chunk = fh.read(32768)
                if not chunk:
                    break
                channel.sendall(chunk)
                sent += len(chunk)
                if callback:
                    callback(sent, size)
        channel.sendall(b"\x00")
        _scp_read_ack(channel)
    finally:
        channel.close()


def scp_get(ssh_client, remote_path, local_path, callback=None):
    """Download remote_path -> local_path using the raw SCP protocol."""
    channel = ssh_client.get_transport().open_session()
    channel.exec_command(f"scp -f {shlex.quote(remote_path)}")
    try:
        channel.sendall(b"\x00")
        header = _scp_read_line(channel)
        if not header.startswith("C"):
            raise ScpProtocolError(f"SCP: unexpected header {header!r}")
        parts = header.split(" ", 2)
        size = int(parts[1])
        channel.sendall(b"\x00")
        received = 0
        with open(local_path, "wb") as fh:
            while received < size:
                chunk = channel.recv(min(32768, size - received))
                if not chunk:
                    break
                fh.write(chunk)
                received += len(chunk)
                if callback:
                    callback(received, size)
        channel.recv(1)  # trailing 0x00
        channel.sendall(b"\x00")
    finally:
        channel.close()


# ─────────────────────────────────────────────
# FTPS (FTP over explicit TLS) — stdlib ftplib, no extra dependency
# This is a completely separate connection kind from the SSH-based
# SCP/SFTP path above: no paramiko client/channel involved at all.
# ─────────────────────────────────────────────
_FTPS_LIST_RE_UNIX = re.compile(
    r"^([\-dl])\S{9}\s+\d+\s+\S+\s+\S+\s+(\d+)\s+(\w+\s+\d+\s+[\d:]+)\s+(.+)$")
# Windows/IIS-style LIST output — this is what Azure App Service's FTP server
# (and many other Windows-hosted FTP/FTPS servers) returns; it has no Unix
# permission bits at all, so servers that only speak this dialect produced a
# silently-empty listing (no error, just zero entries) before this was added.
#   10-25-24  10:15AM       <DIR>          wwwroot
#   10-25-24  10:16AM              1024   file.txt
_FTPS_LIST_RE_DOS = re.compile(
    r"^(\d{2}-\d{2}-(?:\d{2}|\d{4}))\s+(\d{2}:\d{2}(?:AM|PM))\s+(<DIR>|\d+)\s+(.+)$",
    re.IGNORECASE)


class _SessionReusedSslSocket(ssl.SSLSocket):
    """Prevents ftplib from tearing down the shared TLS session on data-socket close."""
    def unwrap(self):
        return self


class _FtpTls(ftplib.FTP_TLS):
    """FTP_TLS that reuses the control channel's TLS session for data connections.

    Azure App Service (and several other FTPS servers) require the data channel
    to resume the control channel's TLS session as an anti-hijacking measure.
    Stock ftplib opens a brand-new, unrelated TLS session per data connection,
    which these servers silently refuse — the transfer then just hangs until
    it times out. Reusing the session (the standard workaround for this) fixes it.

    This is "explicit" FTPS: the control connection starts in plaintext and
    upgrades to TLS via an AUTH TLS command — the conventional behaviour on
    port 21. Use _ImplicitFtpTls instead for port 990.
    """
    def ntransfercmd(self, cmd, rest=None):
        conn, size = ftplib.FTP.ntransfercmd(self, cmd, rest)
        if self._prot_p:
            conn = self.context.wrap_socket(conn, server_hostname=self.host,
                                             session=self.sock.session)
            conn.__class__ = _SessionReusedSslSocket
        return conn, size


class _ImplicitFtpTls(_FtpTls):
    """Implicit FTPS (conventionally port 990): the control connection is TLS

    from the very first byte — there's no plaintext AUTH TLS handshake at all.
    Connecting to an implicit-only endpoint with plain explicit-mode ftplib
    (as stock ftplib.FTP_TLS always does) sends plaintext FTP commands into
    what the server treats as a TLS stream — the server just waits for a TLS
    ClientHello that never comes, and the connection hangs until it eventually
    times out. Wrapping the socket in TLS immediately on connect, before any
    command is sent, fixes that; ftplib.FTP_TLS.login() already knows to skip
    the AUTH TLS step whenever self.sock is already an SSLSocket.
    """
    def connect(self, host="", port=0, timeout=-999, source_address=None):
        if host:
            self.host = host
        if port > 0:
            self.port = port
        if timeout != -999:
            self.timeout = timeout
        if source_address is not None:
            self.source_address = source_address
        raw_sock = socket.create_connection((self.host, self.port), self.timeout,
                                             source_address=self.source_address)
        self.sock = self.context.wrap_socket(raw_sock, server_hostname=self.host)
        self.af = self.sock.family
        self.file = self.sock.makefile("r", encoding=self.encoding)
        self.welcome = self.getresp()
        return self.welcome


def ftps_connect(host, port, username, password, timeout=20):
    port = port or 21
    # Convention: port 990 is implicit FTPS (TLS from byte one); everything
    # else — 21 above all — is explicit FTPS (plaintext, then AUTH TLS).
    cls = _ImplicitFtpTls if port == 990 else _FtpTls
    ftp = cls(timeout=timeout)
    ftp.connect(host, port, timeout=timeout)
    ftp.login(username or "anonymous", password or "")
    ftp.prot_p()  # secure the data channel too, not just the control channel
    ftp.set_pasv(True)  # passive mode — required through most firewalls/NAT/Azure; let failures surface
    return ftp


def ftps_listdir(ftp, path, unparsed_lines_out=None):
    """Return [{'name','size','kind','mtime'}, ...] for path, MLSD first, LIST as fallback.

    If a LIST fallback happens and neither known format matches any line,
    the raw lines are appended to unparsed_lines_out (if given) so the
    caller can surface them — a silent empty listing is otherwise
    indistinguishable from a genuinely empty directory.
    """
    entries = []
    try:
        for name, facts in ftp.mlsd(path or "."):
            if name in (".", ".."):
                continue
            kind = "dir" if facts.get("type") == "dir" else "file"
            try:
                size = int(facts.get("size", 0) or 0)
            except ValueError:
                size = 0
            mt = ""
            modify = facts.get("modify", "")
            if modify:
                try:
                    mt = datetime.datetime.strptime(modify[:14], "%Y%m%d%H%M%S").strftime("%Y-%m-%d %H:%M")
                except ValueError:
                    mt = ""
            entries.append({"name": name, "size": size, "kind": kind, "mtime": mt})
        return entries
    except ftplib.all_errors:
        pass  # MLSD not supported by this server (e.g. Azure's Windows-based FTP) — fall back to LIST

    lines = []
    ftp.retrlines(f"LIST {path}" if path else "LIST", lines.append)
    matched_any = False
    for raw_line in lines:
        line = raw_line.rstrip("\r\n")
        if not line.strip():
            continue
        m = _FTPS_LIST_RE_UNIX.match(line)
        if m:
            matched_any = True
            flag, size, _date, name = m.groups()
            if name in (".", ".."):
                continue
            entries.append({"name": name, "size": int(size),
                             "kind": "dir" if flag == "d" else "file", "mtime": ""})
            continue
        m = _FTPS_LIST_RE_DOS.match(line.strip())
        if m:
            matched_any = True
            date_s, time_s, size_or_dir, name = m.groups()
            if name in (".", ".."):
                continue
            is_dir = size_or_dir.upper() == "<DIR>"
            size = 0 if is_dir else int(size_or_dir)
            mtime = ""
            fmt = "%m-%d-%y %I:%M%p" if len(date_s.split("-")[2]) == 2 else "%m-%d-%Y %I:%M%p"
            try:
                mtime = datetime.datetime.strptime(f"{date_s} {time_s.upper()}", fmt).strftime("%Y-%m-%d %H:%M")
            except ValueError:
                pass
            entries.append({"name": name, "size": size,
                             "kind": "dir" if is_dir else "file", "mtime": mtime})
    if not matched_any and lines and unparsed_lines_out is not None:
        unparsed_lines_out.extend(lines)
    return entries


def ftps_is_dir(ftp, path):
    cur = None
    try:
        cur = ftp.pwd()
        ftp.cwd(path)
        return True
    except Exception:
        return False
    finally:
        if cur is not None:
            try:
                ftp.cwd(cur)
            except Exception:
                pass


def ftps_exists(ftp, path):
    parent = posixpath.dirname(path) or "/"
    name = posixpath.basename(path)
    try:
        return any(e["name"] == name for e in ftps_listdir(ftp, parent))
    except Exception:
        return False


def ftps_put(ftp, local_path, remote_path, callback=None):
    size = os.path.getsize(local_path)
    sent = 0

    def handler(block):
        nonlocal sent
        sent += len(block)
        if callback:
            callback(sent, size)

    with open(local_path, "rb") as fh:
        ftp.storbinary(f"STOR {remote_path}", fh, blocksize=32768, callback=handler)


def ftps_get(ftp, remote_path, local_path, callback=None):
    try:
        ftp.voidcmd("TYPE I")
        size = ftp.size(remote_path)
    except Exception:
        size = 0
    received = 0
    with open(local_path, "wb") as fh:
        def handler(block):
            nonlocal received
            fh.write(block)
            received += len(block)
            if callback:
                callback(received, size or received)

        ftp.retrbinary(f"RETR {remote_path}", handler, blocksize=32768)


def ftps_read_bytes(ftp, remote_path):
    """Read a remote file fully into memory (used by Compare)."""
    buf = io.BytesIO()
    ftp.retrbinary(f"RETR {remote_path}", buf.write, blocksize=32768)
    return buf.getvalue()


def ftps_delete_recursive(ftp, path):
    if ftps_is_dir(ftp, path):
        for entry in ftps_listdir(ftp, path):
            ftps_delete_recursive(ftp, posixpath.join(path, entry["name"]))
        ftp.rmd(path)
    else:
        ftp.delete(path)


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
    PROFILE_KEYS = ["host", "port", "username", "password", "keyfile", "protocol", "start_dir"]
    PROFILE_DEFAULTS = {"host": "", "port": "22", "username": "", "password": "",
                         "keyfile": "", "protocol": "scp", "start_dir": ""}
    _PREFIX = "profile:"

    def __init__(self):
        self._cfg = configparser.ConfigParser()
        self._cfg.read(INI_PATH, encoding="utf-8") if os.path.exists(INI_PATH) else None
        self._migrate_legacy_section()
        if not self._cfg.has_section("general"):
            self._cfg.add_section("general")
        if not self._cfg.has_option("general", "last_profile"):
            self._cfg.set("general", "last_profile", "")
        self.save()

    def _migrate_legacy_section(self):
        # Older versions of this app stored one connection under [ssh].
        if self._cfg.has_section("ssh") and not self._profile_sections():
            data = dict(self._cfg["ssh"])
            name = data.get("host") or "Default"
            self.save_profile(name, data)
            self._cfg.remove_section("ssh")

    def save(self):
        with open(INI_PATH, "w", encoding="utf-8") as fh:
            self._cfg.write(fh)

    def _section(self, name):
        return f"{self._PREFIX}{name}"

    def _profile_sections(self):
        return [s for s in self._cfg.sections() if s.startswith(self._PREFIX)]

    def list_profiles(self):
        return sorted(s[len(self._PREFIX):] for s in self._profile_sections())

    def get_profile(self, name):
        sec = self._section(name)
        data = dict(self.PROFILE_DEFAULTS)
        if self._cfg.has_section(sec):
            data.update(dict(self._cfg[sec]))
        return data

    def save_profile(self, name, data):
        sec = self._section(name)
        if not self._cfg.has_section(sec):
            self._cfg.add_section(sec)
        for key in self.PROFILE_KEYS:
            if key in data and data[key] is not None:
                self._cfg.set(sec, key, str(data[key]))
        self.save()

    def delete_profile(self, name):
        sec = self._section(name)
        if self._cfg.has_section(sec):
            self._cfg.remove_section(sec)
            self.save()

    def get_last_profile(self):
        return self._cfg.get("general", "last_profile", fallback="")

    def set_last_profile(self, name):
        self._cfg.set("general", "last_profile", name)
        self.save()


# ─────────────────────────────────────────────
# Connect dialog
# ─────────────────────────────────────────────
class ConnectDialog(tk.Toplevel):
    def __init__(self, parent, title, cfg: "ConfigManager"):
        super().__init__(parent)
        self.title(title)
        self.configure(bg=BG)
        self.resizable(False, False)
        self.result = None
        self.cfg = cfg

        profiles = self.cfg.list_profiles()
        last = self.cfg.get_last_profile()
        initial_name = last if last in profiles else (profiles[0] if profiles else "")
        defaults = self.cfg.get_profile(initial_name) if initial_name else dict(self.cfg.PROFILE_DEFAULTS)

        style = ttk.Style()
        style.configure("Dark.TCombobox", fieldbackground=BG3, background=BG3,
                        foreground=TXT, arrowcolor=ACCENT)

        row = 0
        tk.Label(self, text="Saved", bg=BG, fg=TXT, font=FONT_UI,
                 anchor="w", width=10).grid(row=row, column=0, padx=12, pady=6, sticky="w")
        self.profile_var = tk.StringVar(value=initial_name)
        self.profile_box = ttk.Combobox(self, textvariable=self.profile_var, values=profiles,
                                         state="readonly" if profiles else "disabled",
                                         style="Dark.TCombobox", font=FONT_MONO)
        self.profile_box.grid(row=row, column=1, padx=12, pady=6, ipady=2, sticky="ew")
        self.profile_box.bind("<<ComboboxSelected>>", self._load_selected)
        tk.Button(self, text="🗑", bg=BG3, fg=ERR, font=FONT_UI, relief="flat",
                  cursor="hand2", command=self._delete_selected
                  ).grid(row=row, column=2, padx=4)
        row += 1

        tk.Label(self, text="Name", bg=BG, fg=TXT, font=FONT_UI,
                 anchor="w", width=10).grid(row=row, column=0, padx=12, pady=6, sticky="w")
        self.name_var = tk.StringVar(value=initial_name)
        tk.Entry(self, textvariable=self.name_var, bg=BG3, fg=TXT,
                 insertbackground=ACCENT, font=FONT_MONO, relief="flat", bd=4
                 ).grid(row=row, column=1, padx=12, pady=6, ipadx=4, ipady=4, sticky="ew")
        row += 1

        fields = [("Host", "host"), ("Port", "port"), ("Username", "username"), ("Password", "password")]
        self.entries = {}
        for label, key in fields:
            tk.Label(self, text=label, bg=BG, fg=TXT, font=FONT_UI,
                     anchor="w", width=10).grid(row=row, column=0, padx=12, pady=6, sticky="w")
            e = tk.Entry(self, bg=BG3, fg=TXT, insertbackground=ACCENT,
                         font=FONT_MONO, relief="flat", bd=4,
                         show="*" if key == "password" else "")
            value = defaults.get(key, "")
            if key == "host" and defaults.get("start_dir", "").strip():
                value += "/" + defaults["start_dir"].strip("/")
            e.insert(0, value)
            e.grid(row=row, column=1, padx=12, pady=6, ipadx=4, ipady=4, sticky="ew")
            self.entries[key] = e
            if key == "password":
                self.password_entry = e
                self._password_visible = False
                self.eye_btn = tk.Button(self, text="👁", bg=BG3, fg=ACCENT, font=FONT_UI,
                                          relief="flat", cursor="hand2",
                                          command=self._toggle_password_visibility)
                self.eye_btn.grid(row=row, column=2, padx=4)
            row += 1

        tk.Label(self, text=" Tip: append a path to Host to set the start directory — "
                             "e.g. example.net/www or ftps://example.net/www",
                 bg=BG, fg=TXT_DIM, font=("Courier New", 8), justify="left"
                 ).grid(row=row, column=0, columnspan=3, padx=12, sticky="w")
        row += 1

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

        tk.Label(self, text="Protocol", bg=BG, fg=TXT, font=FONT_UI,
                 anchor="w", width=10).grid(row=row, column=0, padx=12, pady=6, sticky="w")
        self.protocol_var = tk.StringVar(value="scp")
        self.proto_box = ttk.Combobox(self, textvariable=self.protocol_var,
                                       values=["scp", "sftp", "ftps"], state="readonly",
                                       style="Dark.TCombobox", font=FONT_MONO)
        self.proto_box.grid(row=row, column=1, padx=12, pady=6, ipady=2, sticky="ew")
        self.proto_box.bind("<<ComboboxSelected>>", self._on_protocol_change)
        row += 1
        self.protocol_help = tk.Label(
            self, bg=BG, fg=TXT_DIM, font=("Courier New", 8), justify="left")
        self.protocol_help.grid(row=row, column=0, columnspan=3, padx=12, sticky="w")
        self._set_protocol(defaults.get("protocol", "scp"))
        row += 1

        self.remember_var = tk.BooleanVar(value=True)
        tk.Checkbutton(self, text="Save this connection", variable=self.remember_var,
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

    def _set_protocol(self, value):
        value = (value or "scp").strip().lower()
        values = list(self.proto_box.cget("values"))
        if value not in values:
            value = "scp"
        try:
            self.proto_box.current(values.index(value))
        except (ValueError, tk.TclError):
            self.proto_box.set(value)
        self.protocol_var.set(value)
        self._update_protocol_help()

    def _load_selected(self, _event=None):
        name = self.profile_var.get()
        if not name:
            return
        data = self.cfg.get_profile(name)
        self.name_var.set(name)
        for key, entry in self.entries.items():
            entry.delete(0, tk.END)
            entry.insert(0, data.get(key, ""))
        start_dir = data.get("start_dir", "").strip()
        if start_dir:
            host_entry = self.entries["host"]
            host_entry.insert(tk.END, "/" + start_dir.strip("/"))
        self.key_var.set(data.get("keyfile", ""))
        self._set_protocol(data.get("protocol", "scp"))

    def _on_protocol_change(self, _event=None):
        proto = self.protocol_var.get()
        port_entry = self.entries["port"]
        current = port_entry.get().strip()
        if proto == "ftps" and current in ("", "22"):
            port_entry.delete(0, tk.END)
            port_entry.insert(0, "21")
        elif proto in ("scp", "sftp") and current in ("", "21"):
            port_entry.delete(0, tk.END)
            port_entry.insert(0, "22")
        self._update_protocol_help()

    def _update_protocol_help(self):
        proto = self.protocol_var.get()
        text = {
            "scp": " SCP: legacy 'scp' wire protocol over SSH — for servers that "
                   "disable the SFTP subsystem.",
            "sftp": " SFTP: modern subsystem over SSH, generally faster & more robust.",
            "ftps": " FTPS: FTP over TLS, port 21 by default. Not SSH-based — "
                    "the key file above is ignored for this protocol.",
        }.get(proto, "")
        self.protocol_help.config(text=text)

    def _delete_selected(self):
        name = self.profile_var.get()
        if not name:
            return
        if not messagebox.askyesno("Delete Connection", f"Delete saved connection '{name}'?", parent=self):
            return
        self.cfg.delete_profile(name)
        profiles = self.cfg.list_profiles()
        self.profile_box.config(values=profiles, state="readonly" if profiles else "disabled")
        self.profile_var.set("")
        self.name_var.set("")

    def _toggle_password_visibility(self):
        self._password_visible = not self._password_visible
        self.password_entry.config(show="" if self._password_visible else "*")
        self.eye_btn.config(text="🙈" if self._password_visible else "👁")

    def _browse_key(self):
        path = filedialog.askopenfilename(title="Select private key file")
        if path:
            self.key_var.set(path)

    def _ok(self):
        self.result = {k: e.get().strip() for k, e in self.entries.items()}
        host = self.result.get("host", "")
        for prefix in ("ssh://", "sftp://", "scp://", "ftps://", "ftp://"):
            if host.lower().startswith(prefix):
                host = host[len(prefix):]
        # allow a pasted "host/some/dir" — that becomes the start directory
        extracted_dir = ""
        if "/" in host:
            host, _, extracted_dir = host.partition("/")
        host = host.rstrip("/")
        # allow a pasted "host:port" — split it out if the Port field was left at default
        if ":" in host and not self.entries["port"].get().strip():
            host, _, maybe_port = host.rpartition(":")
            if maybe_port.isdigit():
                self.result["port"] = maybe_port
        self.result["host"] = host
        self.result["keyfile"] = self.key_var.get().strip()
        self.result["protocol"] = self.protocol_var.get()
        self.result["start_dir"] = "/" + extracted_dir.strip("/") if extracted_dir else ""
        self.result["_remember"] = self.remember_var.get()
        self.result["_name"] = self.name_var.get().strip() or self.result.get("host", "Connection")
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

    def kind_of(self, name):
        for entry in self._items:
            if entry["name"] == name:
                return entry["kind"]
        return None

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
# Diff window — Notepad++-style side-by-side compare
# (mirrors github.com/pangchi/compfile, in the app's dark theme)
# ─────────────────────────────────────────────
DIFF_ADD_BG = "#163826"
DIFF_DEL_BG = "#3a1c1c"
DIFF_CHG_BG = "#3a3316"
DIFF_CHAR_BG = "#5c4a12"
DIFF_ADD_GUTTER = "#33d17a"
DIFF_DEL_GUTTER = ERR
DIFF_CHG_GUTTER = ACCENT2


class DiffWindow(tk.Toplevel):
    def __init__(self, parent, text1, text2, label1, label2,
                 accent1="#5c9eff", accent2=ACCENT):
        super().__init__(parent)
        self.title(f"Compare — {label1}  ⇄  {label2}")
        self.geometry("1400x820")
        self.configure(bg=BG)
        self.minsize(700, 400)

        self.line_types = []
        self._build(text1, text2, label1, label2, accent1, accent2)
        self.compare(text1, text2)

    # ── UI ────────────────────────────────────────────────────────────────
    def _build(self, text1, text2, label1, label2, accent1, accent2):
        hdr = tk.Frame(self, bg=BG3)
        hdr.pack(fill="x")
        tk.Label(hdr, text=f" 💻 {label1}", bg=accent1, fg=BG, font=FONT_HDR,
                  anchor="w").pack(side="left", fill="x", expand=True, ipady=4)
        tk.Label(hdr, text=f" 🌐 {label2} ", bg=accent2, fg=BG, font=FONT_HDR,
                  anchor="w").pack(side="left", fill="x", expand=True, ipady=4)

        legend = tk.Frame(self, bg=BG, pady=3)
        legend.pack(fill="x")
        for text, color in [("■ Added", DIFF_ADD_GUTTER),
                             ("■ Deleted", DIFF_DEL_GUTTER),
                             ("■ Changed", DIFF_CHG_GUTTER)]:
            tk.Label(legend, text=text, bg=BG, fg=color, font=FONT_UI).pack(side="left", padx=10)

        main = tk.Frame(self, bg=BG)
        main.pack(fill="both", expand=True)

        def linenum_widget(parent):
            w = tk.Text(parent, width=5, wrap="none", bg=BG3, fg=TXT_DIM,
                        font=FONT_MONO, relief="flat", borderwidth=0,
                        state="disabled", takefocus=0, cursor="arrow")
            w.tag_configure("num", justify="right")
            return w

        self.linenum1 = linenum_widget(main)
        self.linenum1.pack(side="left", fill="y")
        self.indicator1 = tk.Canvas(main, width=8, bg=BG2, highlightthickness=0)
        self.indicator1.pack(side="left", fill="y")
        self.text1 = tk.Text(main, wrap="none", bg=BG2, fg=TXT,
                              insertbackground=ACCENT, font=FONT_MONO,
                              relief="flat", borderwidth=0)
        self.text1.pack(side="left", fill="both", expand=True)

        sep = tk.Frame(main, width=2, bg=BG3)
        sep.pack(side="left", fill="y")

        self.linenum2 = linenum_widget(main)
        self.linenum2.pack(side="left", fill="y")
        self.indicator2 = tk.Canvas(main, width=8, bg=BG2, highlightthickness=0)
        self.indicator2.pack(side="left", fill="y")
        self.text2 = tk.Text(main, wrap="none", bg=BG2, fg=TXT,
                              insertbackground=ACCENT, font=FONT_MONO,
                              relief="flat", borderwidth=0)
        self.text2.pack(side="left", fill="both", expand=True)

        vsb = ttk.Scrollbar(main, orient="vertical", command=self._sync_scroll)
        vsb.pack(side="right", fill="y")
        self.text1.config(yscrollcommand=vsb.set)
        self.text2.config(yscrollcommand=lambda *a: None)

        hsb = ttk.Scrollbar(self, orient="horizontal", command=self._sync_xscroll)
        hsb.pack(fill="x")
        self.text1.config(xscrollcommand=hsb.set)
        self.text2.config(xscrollcommand=lambda *a: None)

        self._configure_tags()

        for w in (self.text1, self.text2):
            w.bind("<MouseWheel>", self._on_mousewheel)
            w.bind("<Button-4>", self._on_mousewheel)
            w.bind("<Button-5>", self._on_mousewheel)
            w.bind("<Configure>", lambda e: self.after(5, self._redraw_indicators))
            w.bind("<KeyPress>", lambda e: "break")  # read-only
        for w in (self.linenum1, self.linenum2):
            w.bind("<MouseWheel>", self._on_mousewheel)
            w.bind("<Button-4>", self._on_mousewheel)
            w.bind("<Button-5>", self._on_mousewheel)

    def _configure_tags(self):
        for txt in (self.text1, self.text2):
            txt.tag_config("added", background=DIFF_ADD_BG)
            txt.tag_config("deleted", background=DIFF_DEL_BG)
            txt.tag_config("changed", background=DIFF_CHG_BG)
            txt.tag_config("char_diff", background=DIFF_CHAR_BG)

    # ── Scroll sync ──────────────────────────────────────────────────────
    def _sync_scroll(self, *args):
        self.text1.yview(*args)
        self.text2.yview(*args)
        self.linenum1.yview(*args)
        self.linenum2.yview(*args)
        self._redraw_indicators()

    def _sync_xscroll(self, *args):
        self.text1.xview(*args)
        self.text2.xview(*args)

    def _on_mousewheel(self, event):
        if getattr(event, "num", None) == 5 or getattr(event, "delta", 0) < 0:
            steps = 1
        else:
            steps = -1
        for w in (self.text1, self.text2, self.linenum1, self.linenum2):
            w.yview_scroll(steps, "units")
        self._redraw_indicators()
        return "break"

    # ── Compare ──────────────────────────────────────────────────────────
    def compare(self, text1, text2):
        lines1 = text1.splitlines(keepends=True) or [""]
        lines2 = text2.splitlines(keepends=True) or [""]

        self.text1.delete("1.0", tk.END)
        self.text2.delete("1.0", tk.END)
        self.indicator1.delete("all")
        self.indicator2.delete("all")
        self.line_types = []
        left_numbers = []
        right_numbers = []
        n1 = 0
        n2 = 0

        matcher = difflib.SequenceMatcher(None, lines1, lines2)
        for tag, i1, i2, j1, j2 in matcher.get_opcodes():
            if tag == "equal":
                for l1, l2 in zip(lines1[i1:i2], lines2[j1:j2]):
                    self.text1.insert(tk.END, l1)
                    self.text2.insert(tk.END, l2)
                    self.line_types.append("equal")
                    n1 += 1; n2 += 1
                    left_numbers.append(n1); right_numbers.append(n2)
            elif tag == "delete":
                for line in lines1[i1:i2]:
                    pos = self.text1.index(tk.END)
                    self.text1.insert(tk.END, line)
                    self.text1.tag_add("deleted", pos, f"{pos} lineend")
                    self.text2.insert(tk.END, "\n")
                    self.line_types.append("deleted")
                    n1 += 1
                    left_numbers.append(n1); right_numbers.append(None)
            elif tag == "insert":
                for line in lines2[j1:j2]:
                    pos = self.text2.index(tk.END)
                    self.text2.insert(tk.END, line)
                    self.text2.tag_add("added", pos, f"{pos} lineend")
                    self.text1.insert(tk.END, "\n")
                    self.line_types.append("added")
                    n2 += 1
                    left_numbers.append(None); right_numbers.append(n2)
            elif tag == "replace":
                max_len = max(i2 - i1, j2 - j1)
                for k in range(max_len):
                    has_l1 = i1 + k < i2
                    has_l2 = j1 + k < j2
                    l1 = lines1[i1 + k] if has_l1 else "\n"
                    l2 = lines2[j1 + k] if has_l2 else "\n"
                    pos1 = self.text1.index(tk.END)
                    pos2 = self.text2.index(tk.END)
                    self.text1.insert(tk.END, l1)
                    self.text2.insert(tk.END, l2)
                    self.text1.tag_add("changed", pos1, f"{pos1} lineend")
                    self.text2.tag_add("changed", pos2, f"{pos2} lineend")
                    self._highlight_char_diff(pos1, pos2, l1, l2)
                    self.line_types.append("changed")
                    if has_l1:
                        n1 += 1
                        left_numbers.append(n1)
                    else:
                        left_numbers.append(None)
                    if has_l2:
                        n2 += 1
                        right_numbers.append(n2)
                    else:
                        right_numbers.append(None)

        self._render_linenums(left_numbers, right_numbers)
        self.after(30, self._redraw_indicators)

    def _render_linenums(self, left_numbers, right_numbers):
        for widget, numbers in ((self.linenum1, left_numbers), (self.linenum2, right_numbers)):
            width = max(4, len(str(max([n for n in numbers if n], default=1))) + 1)
            widget.config(width=width, state="normal")
            widget.delete("1.0", tk.END)
            lines = [f"{n:>{width}}" if n is not None else "" for n in numbers]
            widget.insert("1.0", "\n".join(lines))
            widget.config(state="disabled")

    def _highlight_char_diff(self, pos1, pos2, l1, l2):
        sm = difflib.SequenceMatcher(None, l1, l2)
        for tag, i1, i2, j1, j2 in sm.get_opcodes():
            if tag != "equal":
                self.text1.tag_add("char_diff", f"{pos1}+{i1}c", f"{pos1}+{i2}c")
                self.text2.tag_add("char_diff", f"{pos2}+{j1}c", f"{pos2}+{j2}c")

    # ── Gutter indicators ────────────────────────────────────────────────
    def _redraw_indicators(self):
        self.indicator1.delete("all")
        self.indicator2.delete("all")
        index = self.text1.index("@0,0")
        line_number = int(index.split(".")[0])
        while True:
            dline = self.text1.dlineinfo(f"{line_number}.0")
            if not dline:
                break
            y = dline[1]
            height = dline[3]
            if line_number - 1 < len(self.line_types):
                linetype = self.line_types[line_number - 1]
                color = {"added": DIFF_ADD_GUTTER, "deleted": DIFF_DEL_GUTTER,
                         "changed": DIFF_CHG_GUTTER}.get(linetype)
                if color:
                    self.indicator1.create_rectangle(0, y, 8, y + height, fill=color, outline="")
                    self.indicator2.create_rectangle(0, y, 8, y + height, fill=color, outline="")
            line_number += 1


# ─────────────────────────────────────────────
# Main Application
# ─────────────────────────────────────────────
class ScpGui(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("SCP GUI — PC ▶ Remote Host (SCP / SFTP / FTPS)")
        self.geometry("1180x780")
        self.configure(bg=BG)
        self.minsize(820, 560)

        self._ssh: paramiko.SSHClient | None = None
        self._sftp: paramiko.SFTPClient | None = None
        self._ftp: ftplib.FTP_TLS | None = None
        self._remote_info = {}
        self._protocol = "scp"

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
            (tk.Frame(tb, bg=TXT_DIM, width=1), "left"),
            (tbtn("⇄ Compare", self._compare_selected, "#7fd6ff"), "left"),
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
        self.remote_pane = FilePanel(panels, "🌐 Remote", color_accent=ACCENT)
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
                    self._remote_dot.config(
                        text=f"● Remote: connected ({msg.get('protocol', 'sftp').upper()})", fg=ACCENT)
                elif kind == "remote_disconnected":
                    self._remote_dot.config(text="● Remote: disconnected", fg=ERR)
                    self._sftp = None
                    self._ssh = None
                    self._ftp = None
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
                    self._start_worker(self._list_remote_dir, args=(self.remote_pane.current_path(),))
                elif kind == "busy":
                    self._set_busy(msg["state"])
                elif kind == "set_cmd":
                    self.log.set_cmd(msg["text"])
                elif kind == "set_progress":
                    self.log.set_progress(msg["label"], msg["pct"])
                elif kind == "clear_progress":
                    self.log.clear_progress()
                elif kind == "show_diff":
                    DiffWindow(self, msg["text1"], msg["text2"], msg["label1"], msg["label2"])
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
        dlg = ConnectDialog(self, "Connect to Remote Host (SSH/SCP/SFTP/FTPS)", self._cfg)
        if not dlg.result:
            return
        self._remote_info = dlg.result
        name = dlg.result.get("_name", "").strip() or dlg.result.get("host", "Connection")
        if dlg.result.get("_remember"):
            self._cfg.save_profile(name, {k: v for k, v in dlg.result.items()
                                           if not k.startswith("_")})
            self._cfg.set_last_profile(name)
        if (dlg.result.get("protocol") or "").lower() == "ftps":
            self._start_worker(self._ftps_connect_thread, args=(dlg.result,))
        else:
            self._start_worker(self._ssh_connect_thread, args=(dlg.result,))

    def _is_connected(self):
        return bool(self._sftp or self._ftp)

    def _disconnect_remote(self):
        try:
            if self._sftp:
                self._sftp.close()
            if self._ssh:
                self._ssh.close()
            if self._ftp:
                self._ftp.close()
        except Exception:
            pass
        self._post(kind="remote_disconnected")
        self._post(kind="log", text="Disconnected from remote host", tag="info")

    def _ssh_connect_thread(self, info):
        protocol = (info.get("protocol") or "scp").lower()
        self._post(kind="log", text=f"Connecting to {info['host']}:{info['port']} via {protocol.upper()} …", tag="info")
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
            sftp = client.open_sftp()  # always opened: directory browsing needs it either way
            self._ssh = client
            self._sftp = sftp
            self._protocol = protocol
            self._post(kind="remote_connected", protocol=protocol)
            self._post(kind="log",
                        text=f"✓ Connected to {info['host']} — browsing via SFTP, "
                             f"transfers via {protocol.upper()}", tag="ok")
            try:
                start_path = sftp.normalize(".")
            except Exception:
                start_path = "/"
            requested_dir = info.get("start_dir", "").strip()
            if requested_dir:
                try:
                    sftp.listdir_attr(requested_dir)  # validate it exists and is browsable
                    start_path = requested_dir
                except Exception:
                    self._post(kind="log",
                                text=f"⚠ Directory '{requested_dir}' not found — using the "
                                     f"server's default directory instead.", tag="info")
            self._sftp_list(start_path)
        except socket.gaierror:
            self._post(kind="log",
                        text=f"✗ Couldn't resolve host '{info['host']}' — check for typos, "
                             f"and that you have network/DNS access to it (try pinging it).",
                        tag="err")
        except socket.timeout:
            self._post(kind="log",
                        text=f"✗ Connection to {info['host']}:{info['port']} timed out — "
                             f"check the host/port and that a firewall isn't blocking it.",
                        tag="err")
        except ConnectionRefusedError:
            self._post(kind="log",
                        text=f"✗ Connection refused by {info['host']}:{info['port']} — "
                             f"is an SSH server actually listening on that port?",
                        tag="err")
        except paramiko.AuthenticationException:
            self._post(kind="log",
                        text="✗ Authentication failed — check the username, password, "
                             "or key file.", tag="err")
        except Exception as ex:
            self._post(kind="log", text=f"✗ SSH error: {ex}", tag="err")

    def _ftps_connect_thread(self, info):
        port = int(info.get("port") or 21)
        mode = "implicit — TLS from connect" if port == 990 else "explicit — AUTH TLS"
        self._post(kind="log", text=f"Connecting to {info['host']}:{port} via FTPS ({mode}) …", tag="info")
        try:
            ftp = ftps_connect(info["host"], port, info.get("username"), info.get("password"))
            self._ftp = ftp
            self._ssh = None
            self._sftp = None
            self._protocol = "ftps"
            self._post(kind="remote_connected", protocol="ftps")
            self._post(kind="log", text=f"✓ Connected to {info['host']} via FTPS (passive mode)", tag="ok")
            try:
                start_path = ftp.pwd()
            except Exception:
                start_path = "/"
            requested_dir = info.get("start_dir", "").strip()
            if requested_dir:
                try:
                    ftp.cwd(requested_dir)
                    start_path = ftp.pwd()
                except Exception:
                    self._post(kind="log",
                                text=f"⚠ Directory '{requested_dir}' not found — using the "
                                     f"server's default directory instead.", tag="info")
            self._ftps_list(start_path)
        except socket.gaierror:
            self._post(kind="log",
                        text=f"✗ Couldn't resolve host '{info['host']}' — check for typos, "
                             f"and that you have network/DNS access to it (try pinging it).",
                        tag="err")
        except socket.timeout:
            self._post(kind="log",
                        text=f"✗ Connection to {info['host']}:{port} timed out. "
                             f"If it hung right after connecting — before login even completed — "
                             f"double check the port matches the mode: 990 is implicit FTPS "
                             f"(TLS from the first byte), 21 is explicit FTPS (AUTH TLS). Using "
                             f"the wrong one for that port makes the server wait for a TLS "
                             f"handshake it never gets, which just hangs instead of failing fast. "
                             f"If login succeeded and it hung after that instead, it's almost "
                             f"always the passive-mode data port range being blocked — for Azure "
                             f"App Service, open ports 10001–10020 outbound (that's the FTPS "
                             f"data-channel range Azure uses) and enable passive-mode data "
                             f"connections on any firewall/NSG in between.",
                        tag="err")
        except ConnectionRefusedError:
            self._post(kind="log",
                        text=f"✗ Connection refused by {info['host']}:{port} — "
                             f"is an FTPS server actually listening on that port?",
                        tag="err")
        except ftplib.error_perm as ex:
            self._post(kind="log", text=f"✗ Login rejected: {ex}", tag="err")
        except ftplib.all_errors as ex:
            self._post(kind="log", text=f"✗ FTPS error: {ex}", tag="err")
        except Exception as ex:
            self._post(kind="log", text=f"✗ FTPS error: {ex}", tag="err")

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

    def _ftps_list(self, path):
        if not self._ftp:
            self._post(kind="log", text="Not connected to a remote host.", tag="err")
            return
        try:
            unparsed = []
            items = ftps_listdir(self._ftp, path, unparsed_lines_out=unparsed)
            if unparsed:
                sample = " | ".join(unparsed[:3])
                self._post(kind="log",
                            text=f"⚠ Server's directory listing didn't match a known format "
                                 f"(neither MLSD, Unix LIST, nor Windows/IIS LIST) — showing an "
                                 f"empty panel instead of guessing. Raw line(s): {sample}",
                            tag="err")
            self._post(kind="remote_populate", items=items, path=path)
        except Exception as ex:
            self._post(kind="log", text=f"FTPS list error: {ex}", tag="err")

    def _list_remote_dir(self, path):
        if self._protocol == "ftps":
            self._ftps_list(path)
        else:
            self._sftp_list(path)

    def _remote_navigate(self, path):
        if not self._is_connected():
            messagebox.showinfo("Not connected", "Connect to a remote host first.")
            return
        self._start_worker(self._list_remote_dir, args=(path,))

    # ── Transfers ─────────────────────────────────────────────────────────
    def _upload_selected(self):
        if not self._is_connected():
            messagebox.showinfo("Not connected", "Connect to a remote host first.")
            return
        names = self.local_pane.selected_names()
        if not names:
            messagebox.showinfo("Nothing selected", "Select file(s)/folder(s) in the Local panel.")
            return
        self._start_worker(self._upload_thread,
                            args=(self.local_pane.current_path(), names, self.remote_pane.current_path()))

    def _download_selected(self):
        if not self._is_connected():
            messagebox.showinfo("Not connected", "Connect to a remote host first.")
            return
        names = self.remote_pane.selected_names()
        if not names:
            messagebox.showinfo("Nothing selected", "Select file(s)/folder(s) in the Remote panel.")
            return
        self._start_worker(self._download_thread,
                            args=(self.remote_pane.current_path(), names, self.local_pane.current_path()))

    def _drop_onto_remote_from_local(self, source_panel, names):
        if not self._is_connected():
            self._post(kind="log", text="Connect to a remote host first.", tag="err")
            return
        self._start_worker(self._upload_thread,
                            args=(self.local_pane.current_path(), names, self.remote_pane.current_path()))

    def _drop_onto_local_from_remote(self, source_panel, names):
        if not self._is_connected():
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
        if self._protocol == "ftps":
            return ftps_is_dir(self._ftp, path)
        try:
            return stat.S_ISDIR(self._sftp.stat(path).st_mode)
        except Exception:
            return False

    def _remote_exists(self, path):
        if self._protocol == "ftps":
            return ftps_exists(self._ftp, path)
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
        proto = self._protocol
        self._post(kind="set_cmd", text=f"{proto} put {local_path} -> {remote_path}")
        self._post(kind="log", text=f"⬆ Uploading {name} via {proto.upper()} …", tag="xfer")
        if proto == "scp":
            scp_put(self._ssh, local_path, remote_path, callback=self._progress_cb_factory(name))
        elif proto == "ftps":
            ftps_put(self._ftp, local_path, remote_path, callback=self._progress_cb_factory(name))
        else:
            self._sftp.put(local_path, remote_path, callback=self._progress_cb_factory(name))
        self._post(kind="log", text=f"✓ Uploaded {name}", tag="ok")

    def _upload_dir(self, local_path, remote_path, yes_to_all):
        name = os.path.basename(local_path.rstrip(os.sep))
        if not self._remote_exists(remote_path):
            if self._protocol == "ftps":
                self._ftp.mkd(remote_path)
            else:
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
        proto = self._protocol
        self._post(kind="set_cmd", text=f"{proto} get {remote_path} -> {local_path}")
        self._post(kind="log", text=f"⬇ Downloading {name} via {proto.upper()} …", tag="xfer")
        if proto == "scp":
            scp_get(self._ssh, remote_path, local_path, callback=self._progress_cb_factory(name))
        elif proto == "ftps":
            ftps_get(self._ftp, remote_path, local_path, callback=self._progress_cb_factory(name))
        else:
            self._sftp.get(remote_path, local_path, callback=self._progress_cb_factory(name))
        self._post(kind="log", text=f"✓ Downloaded {name}", tag="ok")

    def _download_dir(self, remote_path, local_path, yes_to_all):
        os.makedirs(local_path, exist_ok=True)
        if self._protocol == "ftps":
            entries = ftps_listdir(self._ftp, remote_path)
            for entry in entries:
                rp = posixpath.join(remote_path, entry["name"])
                lp = os.path.join(local_path, entry["name"])
                if entry["kind"] == "dir":
                    self._download_dir(rp, lp, yes_to_all)
                else:
                    self._download_file(rp, lp, yes_to_all)
            return
        for attr in self._sftp.listdir_attr(remote_path):
            rp = posixpath.join(remote_path, attr.filename)
            lp = os.path.join(local_path, attr.filename)
            if stat.S_ISDIR(attr.st_mode or 0):
                self._download_dir(rp, lp, yes_to_all)
            else:
                self._download_file(rp, lp, yes_to_all)

    # ── Compare ───────────────────────────────────────────────────────────
    def _compare_selected(self):
        local_sel = self.local_pane.selected_names()
        remote_sel = self.remote_pane.selected_names()
        if len(local_sel) != 1 or len(remote_sel) != 1:
            messagebox.showinfo("Compare",
                                 "Select exactly one file in the Local panel and "
                                 "one file in the Remote panel, then click Compare.")
            return
        lname, rname = local_sel[0], remote_sel[0]
        if self.local_pane.kind_of(lname) != "file":
            messagebox.showinfo("Compare", "The local selection must be a file, not a folder.")
            return
        if not self._is_connected():
            messagebox.showinfo("Not connected", "Connect to a remote host first.")
            return
        if self.remote_pane.kind_of(rname) != "file":
            messagebox.showinfo("Compare", "The remote selection must be a file, not a folder.")
            return
        local_path = os.path.join(self.local_pane.current_path(), lname)
        remote_path = posixpath.join(self.remote_pane.current_path(), rname)
        self._start_worker(self._compare_thread, args=(local_path, remote_path, lname, rname))

    def _compare_thread(self, local_path, remote_path, lname, rname):
        MAX_BYTES = 20 * 1024 * 1024  # 20 MB sanity cap for an in-memory text diff
        try:
            if os.path.getsize(local_path) > MAX_BYTES:
                self._post(kind="log", text=f"✗ {lname} is too large to compare (>20 MB).", tag="err")
                return
            with open(local_path, "r", encoding="utf-8", errors="replace") as f:
                text1 = f.read()
        except Exception as ex:
            self._post(kind="log", text=f"✗ Compare error reading local file: {ex}", tag="err")
            return
        try:
            if self._protocol == "ftps":
                try:
                    self._ftp.voidcmd("TYPE I")
                    if self._ftp.size(remote_path) and self._ftp.size(remote_path) > MAX_BYTES:
                        self._post(kind="log", text=f"✗ {rname} is too large to compare (>20 MB).", tag="err")
                        return
                except ftplib.all_errors:
                    pass  # some servers don't support SIZE — just try the read
                raw = ftps_read_bytes(self._ftp, remote_path)
            else:
                if self._sftp.stat(remote_path).st_size > MAX_BYTES:
                    self._post(kind="log", text=f"✗ {rname} is too large to compare (>20 MB).", tag="err")
                    return
                with self._sftp.open(remote_path, "r") as rf:
                    raw = rf.read()
            text2 = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else raw
        except Exception as ex:
            self._post(kind="log", text=f"✗ Compare error reading remote file: {ex}", tag="err")
            return
        self._post(kind="log", text=f"⇄ Comparing {lname} ⟷ {rname}", tag="info")
        self._post(kind="show_diff", text1=text1, text2=text2, label1=lname, label2=rname)

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
            if not self._is_connected():
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
        if self._protocol == "ftps":
            ftps_delete_recursive(self._ftp, path)
            return
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
            if not self._is_connected():
                messagebox.showinfo("Not connected", "Connect to a remote host first.")
                return
            self._start_worker(self._mkdir_remote_thread, args=(panel.current_path(), name))
        else:
            self._start_worker(self._mkdir_local_thread, args=(panel.current_path(), name))

    def _mkdir_remote_thread(self, path, name):
        try:
            if self._protocol == "ftps":
                self._ftp.mkd(posixpath.join(path, name))
            else:
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

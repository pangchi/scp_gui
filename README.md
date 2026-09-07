SCP GUI
A FileZilla-style two-panel file manager built in Python / Tkinter, for transferring files to/from a remote host — with a choice of SCP, SFTP (both over SSH), or FTPS (FTP over TLS).
```
Your PC ──── SSH (port 22) ────▶ Remote Host      (SCP or SFTP)
Your PC ──── FTPS (port 21) ───▶ Remote Host      (FTP over TLS)
```
Same dark UI theme, panel layout, drag-and-drop, and transfer log as `proxy_ftp`, trimmed down to a single hop (no Pi relay) — just local ↔ remote, over whichever protocol the server speaks.
Requirements
Requirement	Notes
Python 3.10+	
paramiko	`pip install paramiko` — only needed for SCP/SFTP (SSH). FTPS uses the stdlib `ftplib`, no extra install.
Tkinter	Bundled on Windows & macOS. Linux: `sudo apt install python3-tk`
Quick Start
```
pip install paramiko
python scp_gui.pyw
```
Click 🔌 Connect. The dialog remembers any number of connections:
Saved — dropdown of every connection you've saved; picking one fills in the rest of the form.
Name — the label this connection is saved under (defaults to the host if left blank).
🗑 next to Saved — deletes the currently selected saved connection.
Fill in host/port/username, a password or a private key file (leave password blank to use key-only auth) — click 👁 next to Password to reveal it while typing, and pick a Protocol:
Protocol	Transport	When to use it
SCP (default)	SSH, port 22	The legacy `scp -t`/`scp -f` wire protocol, implemented natively (no external `scp` binary needed). Use it for servers that have disabled the SFTP subsystem but still allow the `scp` command over SSH.
SFTP	SSH, port 22	Modern subsystem, generally faster and more robust. Works on almost every SSH server.
FTPS	FTP over TLS, port 21	For plain FTP(S) servers — not SSH-based at all. The key file field is ignored for this protocol; only username/password apply.
Picking Protocol auto-fills the Port field (21 for FTPS, 22 for SCP/SFTP) if you haven't typed a custom one. There's no separate Directory field — the folder to open into after connecting is taken straight from the Host field: paste (or type) a path after the host, e.g. `example.net/www` or the full `ftps://example.net/www`, and it's split apart automatically — protocol prefix stripped, port pulled out if present (when Port was left blank), and the trailing path becomes the start directory. Leave it off to land wherever the server puts you by default. If that folder doesn't exist, the log shows a warning and the panel falls back to the default directory instead of failing the connection. A saved connection's start directory is folded back into the Host field (`host/dir`) whenever it's loaded — both when the dialog opens pre-filled with your last-used connection, and when you pick a different one from Saved — so it's always visible and editable in place. Uncheck Save this connection to connect once without adding/updating a saved entry.
For SCP/SFTP, directory browsing, new folders, and deletes always go through SFTP (needed for listing either way) — the Protocol choice only changes which wire protocol actual file transfers use. For FTPS, browsing and transfers both go through the same FTPS session (MLSD for listings where the server supports it, with a `LIST`-parsing fallback for older servers).
Interface
💻 Local (This PC) — left panel, your machine's filesystem
🌐 Remote — right panel, the remote host's filesystem (its title bar doesn't change; the connection status dot in the toolbar shows the active protocol, e.g. "connected (FTPS)")
Each panel: coloured header with refresh, editable path bar, ↑ up-directory, sortable columns (click header, click again to reverse), status bar, focus-glow border.
Toolbar
Button	Action
🔌 Connect	Open a connection to the remote host — SCP or SFTP over SSH, or FTPS
🔌 Disconnect	Close the session
⬆ Upload (PC → Remote)	Upload selected local items
⬇ Download (Remote → PC)	Download selected remote items
🗑 Delete	Delete selection in whichever panel last had focus
📁 New Folder	Create a directory in whichever panel last had focus
⇄ Compare	Diff the selected local file against the selected remote file
Drag and drop works directly between the two panels. Transfers are recursive for folders, and multi-selection is supported (Click / Shift+Click / Ctrl+Click / Ctrl+A / Shift+↑ / Shift+↓).
Compare
Select exactly one file in each panel (Local and Remote), then click ⇄ Compare. Both files are read in the background — the remote file is streamed straight off the active session (SFTP, SCP-over-SSH, or FTPS), no temp file — and a side-by-side diff window opens, in the style of compfile:
Synchronized scrolling between the two panes
Per-file line numbers in the gutter — a line only present on one side leaves the other blank, so numbers always match the real file
Line-level highlighting: added / deleted / changed
Character-level highlighting within changed lines
A colour-coded gutter strip on both panes for a quick overview
Only text files are supported, and each file is capped at 20 MB for the in-memory diff.
Overwrite Prompt
Yes / Yes to All / No / Cancel — same batch semantics as `proxy_ftp`.
Connection Errors
The connect dialog also cleans up common paste mistakes before connecting: it trims stray whitespace, strips an accidental `ssh://`/`sftp://`/`scp://`/`ftps://`/`ftp://` prefix from the Host field, and splits a pasted `host:port` into the two fields if Port was left blank.
If the connection still fails, the log shows a plain-language reason instead of a raw OS error code (for both the SSH and FTPS paths):
Message	Meaning
Couldn't resolve host '...'	DNS lookup failed — check for typos, or that you have network/VPN access to resolve that hostname (this is what a raw `getaddrinfo failed` / errno 11001 means)
Connection timed out	Reached the network but got no response — check host/port and firewalls
Connection refused	Reached the host but nothing is listening on that port — confirm the SSH/FTPS server is running there
Authentication failed / Login rejected	Reached the server but the username/password/key was rejected
FTPS timing out (e.g. against Azure App Service)
FTPS timeouts fall into two very different buckets depending on when the hang happens — whether the connection even completed login, or got further than that.
Hangs immediately, before login even finishes — usually port/mode mismatch. FTPS has two incompatible startup modes and the port has to match the one the server expects:
Port 990 — implicit FTPS. The control connection is TLS from the very first byte; there's no plaintext step at all.
Port 21 (or any other) — explicit FTPS. The control connection starts in plaintext and upgrades via an `AUTH TLS` command.
Point a client using the wrong mode at a given port and it doesn't fail fast — it just hangs, because the server is waiting for a TLS handshake that never arrives (or vice versa) until it eventually gives up. This app auto-selects the mode from the port you enter — port 990 uses implicit mode (`_ImplicitFtpTls` in the source), everything else uses explicit — so this is handled automatically; the fix, if it still happens, is simply using the port your server actually expects. Azure App Service FTPS deployment endpoints (`*.ftp.azurewebsites.windows.net`) support both — port 21 for explicit and port 990 for implicit.
Hangs later, once it's browsing or transferring — usually one of two other well-known gotchas:
TLS session reuse. Some FTPS servers — Azure App Service in particular — require the data channel to resume the control channel's TLS session as an anti-hijacking check. Plain `ftplib` doesn't do this, so the data connection is silently refused and just hangs. This app's FTPS client (`_FtpTls` in the source) explicitly reuses the session, so this shouldn't bite — but it's the first thing to check if you've patched in a different FTPS library.
Blocked passive-mode data port range. After session reuse, the remaining cause is almost always a firewall/NSG blocking the passive-mode data ports. For Azure App Service, that's outbound TCP 10001–10020 — make sure that range (and whichever control port you're using, 21 or 990) is open between you and the server.
FTPS connects fine but the Remote panel shows nothing
This is a listing-format mismatch, not a connection problem — the log wouldn't show an error for it (an empty directory looks the same as a mis-parsed one) unless you hit a third format, in which case the log now shows the raw server line(s). Azure App Service's FTP server doesn't support `MLSD` and doesn't return Unix-style `LIST` output — it returns Windows/IIS-style output instead. Both are handled automatically, so this shouldn't come up against Azure specifically anymore; if it still happens against some other server, that raw-line log output is exactly what's needed to add support for whatever format it's using.
Configuration — `scp_config.ini`
Created automatically next to `scp_gui.pyw` on first launch. Every saved connection gets its own section, plus a `[general]` section that remembers which one you used last:
```
[general]
last_profile = My Server

[profile:My Server]
host      = 10.0.0.5
port      = 22
username  = alan
password  =
keyfile   = /home/alan/.ssh/id_ed25519
protocol  = scp
start_dir =

[profile:Staging Box]
host      = staging.example.com
port      = 22
username  = deploy
password  =
keyfile   =
protocol  = sftp
start_dir = /var/www/staging

[profile:Public Drop]
host      = ftps.example.com
port      = 21
username  = uploader
password  =
keyfile   =
protocol  = ftps
start_dir = /incoming
```
Uncheck Save this connection in the connect dialog to connect once without adding/updating a saved entry. A config file from an earlier single-connection version of this app (with a `[ssh]` section) is migrated automatically into a profile the first time you run this version.
> **Security note:** passwords are stored as plain text if saved. Prefer a key file and leave the password blank (SCP/SFTP only — FTPS always uses username/password).
Under the hood
For SCP/SFTP, directory listing, mkdir, and delete always use paramiko's SFTP subsystem. Actual file transfer uses whichever protocol you picked at connect time:
SFTP — paramiko's `SFTPClient.put`/`get`, with progress callbacks.
SCP — a small native implementation of the classic `scp -t`/`scp -f` wire protocol (see `scp_put`/`scp_get` in the source), run over a plain SSH exec channel. No external `scp` binary or extra Python package required.
For FTPS, everything — browsing, mkdir, delete, and transfer — goes through a small subclass of `ftplib.FTP_TLS`: TLS on the control channel plus `PROT P` to secure the data channel too, passive mode explicitly enabled, and each data connection reuses the control channel's TLS session (`ntransfercmd` override in `_FtpTls`) rather than opening an unrelated one, which is what several servers — Azure App Service among them — silently require. The port also selects the startup mode automatically: port 990 connects as implicit FTPS (`_ImplicitFtpTls` — TLS wraps the socket immediately, before any command is sent), anything else connects as explicit FTPS (plaintext control connection, then `AUTH TLS`). Directory listing tries the modern `MLSD` command first; if the server doesn't support it (Azure's Windows-based FTP server doesn't), it falls back to parsing `LIST` output, trying both the Unix format (`-rwxr-xr-x ...`) and the Windows/IIS format Azure actually returns (`10-25-24  10:15AM  <DIR>  foldername`). If a server ever returns some third format neither parser recognizes, the panel shows empty and the log prints the raw line(s) instead of failing silently.

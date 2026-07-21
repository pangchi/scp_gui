# SCP GUI

A FileZilla-style two-panel file manager built in Python / Tkinter, for transferring files to/from a remote host over SSH (SCP/SFTP).

```
Your PC ──── SSH / SFTP (port 22) ────▶ Remote Host
```

Same dark UI theme, panel layout, drag-and-drop, and transfer log as `proxy_ftp`, trimmed down to a single hop (no Pi relay) — just local ↔ remote over SSH.

## Requirements

| Requirement | Notes |
|---|---|
| Python 3.10+ | |
| [paramiko](https://www.paramiko.org/) | `pip install paramiko` |
| Tkinter | Bundled on Windows & macOS. Linux: `sudo apt install python3-tk` |

## Quick Start

```
pip install paramiko
python scp_gui.pyw
```

Click **🔌 Connect**, fill in host/port/username, and either a password or a private key file (leave password blank to use key-only auth).

## Interface

- **💻 Local (This PC)** — left panel, your machine's filesystem
- **🌐 Remote [SSH/SCP:22]** — right panel, the remote host's filesystem

Each panel: coloured header with refresh, editable path bar, **↑** up-directory, sortable columns (click header, click again to reverse), status bar, focus-glow border.

## Toolbar

| Button | Action |
|---|---|
| 🔌 Connect | Open SSH/SFTP session to the remote host |
| 🔌 Disconnect | Close the session |
| ⬆ Upload (PC → Remote) | Upload selected local items |
| ⬇ Download (Remote → PC) | Download selected remote items |
| 🗑 Delete | Delete selection in whichever panel last had focus |
| 📁 New Folder | Create a directory in whichever panel last had focus |

Drag and drop works directly between the two panels. Transfers are recursive for folders, and multi-selection is supported (Click / Shift+Click / Ctrl+Click / Ctrl+A).

## Overwrite Prompt

Yes / Yes to All / No / Cancel — same batch semantics as `proxy_ftp`.

## Configuration — `scp_config.ini`

Created automatically next to `scp_gui.pyw` on first launch:

```
[ssh]
host     =
port     = 22
username =
password =
keyfile  =
```

Uncheck **Remember in INI file** in the connect dialog to connect once without saving.

> **Security note:** passwords are stored as plain text if remembered. Prefer a key file and leave the password blank.

## Under the hood

All transfers use paramiko's SFTP subsystem over the SSH connection (the same channel `scp` uses) — put/get with progress callbacks, so the two-panel workflow behaves like a graphical `scp`.

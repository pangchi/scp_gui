# Changelog

All notable changes to **SCP GUI** (`scp_gui.pyw`) are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Entries are listed newest first; version numbers were assigned retroactively to group the changes in the order they were made.

## [0.12.0]

### Added
- **Auto-install of `paramiko`.** If the package is missing at launch, the app runs `python -m pip install paramiko` itself (retrying with `--user`), showing a small "first run setup" window. On failure it shows the exact manual command and exits.

## [0.11.0]

### Added
- **F2** renames the selected item in the focused panel (local, or remote over SFTP/SCP/FTPS). The prompt is pre-filled with the current name and selects only the base name; renaming onto an existing name is refused.
- **F5** refreshes both panels (the remote panel only when connected).
- The overwrite prompt now **greys out the main window** behind it with a semi-transparent layer and centres the dialog on top. Clicking the dimmed area beeps and returns focus to the prompt. Closing the prompt with the window's X counts as **No**.
- Keyboard Shortcuts section in the README.

## [0.10.1]

### Fixed
- **Terminal: Backspace now works.** The output filter was discarding the backspace character the server echoes to erase a letter. Terminal output now goes through a small screen model that applies backspace, Delete, carriage return, erase-line, delete/insert-char, cursor moves and clear-screen. Command-line editing, history recall (↑/↓) and `clear` now display correctly.
- **Terminal: Ctrl+C, Ctrl+D, Ctrl+L and Ctrl+Z** were swallowed by the key handler and never reached the server. They are now sent through.
- **Terminal: accented characters and emoji** split across two network reads were mangled. Output now uses an incremental UTF-8 decoder.

### Added
- Terminal: Ctrl+V / Shift+Insert / Ctrl+Shift+V paste, Ctrl+Shift+C copies the selection.
- Terminal: the remote pty is resized to match the window so long command lines wrap correctly.
- Terminal: scrollback capped at 5,000 lines.

## [0.10.0]

### Added
- **🖥 Terminal** toolbar button: an interactive shell on the remote host in its own window (SCP/SFTP connections only), opened on a separate SSH channel alongside the SFTP session. FTPS connections get an explanatory message instead.

## [0.9.1]

### Fixed
- **Shift+↑ / Shift+↓ multi-selection** did nothing, because Tk's Treeview has no built-in range selection for arrow keys. An anchor row is now tracked and the selection grows or shrinks from it. Plain ↑/↓ reset the anchor, and mouse clicks keep the anchor in sync so Shift+Click and Shift+Arrow compose.

## [0.9.0]

### Fixed
- **FTPS: Remote panel empty after connecting to Azure App Service.** Azure's FTP server supports neither `MLSD` nor Unix-style `LIST` output; it returns Windows/IIS format (`10-25-24  10:15AM  <DIR>  name`), which was silently discarded. Both formats are now parsed.
- If a server returns a listing in a format neither parser recognizes, the log now shows the raw lines instead of an unexplained empty panel.

## [0.8.1]

### Fixed
- **FTPS: connection hung on port 990.** Port 990 is implicit FTPS (TLS from the first byte), but the app only spoke explicit FTPS (plaintext, then `AUTH TLS`). Port 990 now connects in implicit mode; other ports use explicit mode. The connect log states which mode was chosen.
- The FTPS timeout message now distinguishes a port/mode mismatch from a blocked passive-mode data port range.

## [0.8.0]

### Changed
- FTPS passive mode is now enabled explicitly and no longer wrapped in a silent `try/except`, so a failure is visible. The connect log reads "(passive mode)".

### Fixed
- Protocol dropdown not showing the saved value when loading a saved connection. The selection is now set through the combobox's own `current()` (with a fallback), and unknown stored values fall back to `scp`.

## [0.7.0]

### Added
- **Password reveal.** A 👁 button next to the Password field toggles showing the password while typing.

### Fixed
- **FTPS timeouts against Azure App Service.** The FTPS data channel now reuses the control channel's TLS session, which Azure requires; stock `ftplib` opens an unrelated session and the transfer hangs.
- Protocol dropdown not populated from a saved connection (first attempt at the fix).

## [0.6.0]

### Changed
- **Removed the separate Directory field.** The start directory is taken straight from the Host field (`example.net/www`, `ftps://example.net/www`).

### Fixed
- A saved connection's start directory is shown folded back into the Host field (`host/dir`) both when the dialog opens pre-filled with the last-used connection and when picking one from the Saved dropdown.

## [0.5.0]

### Added
- **FTPS support** (FTP over TLS) alongside SCP and SFTP, using the stdlib `ftplib`, with MLSD listings and a `LIST` fallback.
- **Multiple saved connections.** The connect dialog has a Saved dropdown, a Name field and a 🗑 delete button. Each connection is a `[profile:<name>]` section in `scp_config.ini`, and the last-used one is remembered. A config file from the older single-connection version (`[ssh]` section) is migrated automatically.
- **Protocol selector** (SCP / SFTP / FTPS) with the port auto-filled (21 for FTPS, 22 otherwise).
- **Connection strings.** Host accepts a pasted `scheme://host:port/dir`; the scheme is stripped, the port split out when Port was blank, and the path becomes the start directory.
- Plain-language connection errors (DNS failure, timeout, connection refused, authentication failure) for both the SSH and FTPS paths.
- Native implementation of the `scp -t` / `scp -f` wire protocol, so the SCP choice is genuine SCP rather than SFTP under another name.
- Compare, delete, new folder, overwrite checks and recursive transfers all work over FTPS as well as SSH.

## [0.4.0]

### Added
- `README.md` shipped alongside the tool and kept in step with each change.

## [0.3.0]

### Added
- **Line numbers in the Compare window**, per file: a line present on only one side leaves the other gutter blank, so numbers always match the real file. The gutters scroll in step with both text panes.

## [0.2.0]

### Added
- **⇄ Compare**: select one file in each panel and open a side-by-side diff window in the style of [compfile](https://github.com/pangchi/compfile), in the app's dark theme. It has synchronized scrolling, added / deleted / changed line highlighting, character-level highlighting within changed lines, and a colour-coded gutter strip. The remote file is read straight off the SFTP session with no temp file, and files over 20 MB are refused.

## [0.1.0]

### Added
- Initial release: a FileZilla-style two-panel file manager in Python / Tkinter, modelled on the look and feel of [proxy_ftp](https://github.com/pangchi/proxy_ftp) but with a single hop (local ⇄ remote over SSH).
- Dark theme, coloured panel headers, editable path bar, ↑ up-directory, sortable columns (directories always first), focus-glow border and a status bar per panel.
- Upload / download, recursive for folders, with multi-selection (Click / Shift+Click / Ctrl+Click / Ctrl+A) and drag-and-drop between panels.
- Overwrite prompt: Yes / Yes to All / No / Cancel per batch.
- Delete and New Folder in the focused panel.
- Transfer log with per-file command line, live progress bar and a busy spinner; all work runs on background threads.
- Auto-refresh of the affected panel after each transfer, delete or new folder.
- Connection settings stored in an INI file next to the script (`scp_config.ini`), with optional key-file authentication.

# Dual Pane SCP Explorer

A lightweight desktop file manager for transferring files between your local machine and a remote server over SSH/SFTP. Built with Python and Tkinter, it provides a familiar dual-pane interface similar to classic file managers like WinSCP or FileZilla.

## Features

- Dual-pane view showing local and remote filesystems side by side
- Browse directories on both local and remote systems
- Upload and download files with a single click
- Drag-and-drop file upload from your desktop or file manager
- Delete files on the remote server
- Sortable columns (name, size, type, last modified)
- Folder and file icons for quick visual distinction
- Configuration via a simple `config.ini` file

## Requirements

- Python 3.7+
- The following Python packages:
  - `paramiko`
  - `tkinterdnd2`

Install dependencies with:

```bash
pip install paramiko tkinterdnd2
```

## Configuration

Before running the application, create a `config.ini` file in the same directory as the script:

```ini
[ssh]
host = your.server.com
port = 22
username = your_username
password = your_password

[ui]
default_remote_path = /home/your_username
```

The `default_remote_path` is optional and defaults to `/` if not specified.

> **Security note:** Storing passwords in plaintext is convenient for local use but not recommended in shared or production environments. Consider using SSH key authentication via Paramiko's `connect()` method as an alternative.

## Usage

Run the application with:

```bash
python scp_gui.py
```

Once open, the left pane shows your local filesystem and the right pane shows the remote filesystem. You can:

- **Double-click** a folder to navigate into it, or double-click `..` to go up a level
- **Select a local file** and click **Upload →** to transfer it to the current remote directory
- **Select a remote file** and click **← Download** to save it to the current local directory
- **Drag and drop** files from your desktop onto the application window to upload them
- **Select a remote file** and click **Delete Remote** to remove it from the server (with confirmation)
- **Click any column header** to sort the listing by that column; click again to reverse the order

## Project Structure

```
.
├── scp_gui.py       # Main application
└── config.ini       # SSH and UI configuration (you create this)
```

## Limitations

- Only individual files can be uploaded or downloaded; recursive directory transfer is not currently supported
- Drag-and-drop only supports uploading (local to remote)
- Remote file deletion is limited to files; directories cannot be deleted through the UI
- SSH host key verification is set to auto-accept, which may not be suitable for security-sensitive environments

## Licence

MIT

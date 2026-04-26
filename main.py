import paramiko
import os
import posixpath
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from tkinterdnd2 import DND_FILES, TkinterDnD
import configparser
import stat
import time

# ---------- HELPERS ----------
def format_size(size):
    for unit in ['B','KB','MB','GB','TB']:
        if size < 1024:
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} PB"

def format_mtime(epoch):
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(epoch))


# ---------- CONFIG ----------
def load_config():
    config = configparser.ConfigParser()
    config.read("config.ini")

    return {
        "host": config.get("ssh", "host"),
        "port": config.getint("ssh", "port"),
        "username": config.get("ssh", "username"),
        "password": config.get("ssh", "password"),
        "remote_path": config.get("ui", "default_remote_path", fallback="/"),
        "local_path": os.getcwd()
    }


class SCPGui:
    def __init__(self, root):
        self.root = root
        self.root.title("Dual Pane SCP Explorer")

        self.config = load_config()

        self.ssh = None
        self.sftp = None

        self.current_remote = self.config["remote_path"]
        self.current_local = self.config["local_path"]

        self.connect_ssh()
        self.create_widgets()

        self.load_local_dir(self.current_local)
        self.load_remote_dir(self.current_remote)

    # ---------- SSH ----------
    def connect_ssh(self):
        try:
            self.ssh = paramiko.SSHClient()
            self.ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())

            self.ssh.connect(
                self.config["host"],
                port=self.config["port"],
                username=self.config["username"],
                password=self.config["password"]
            )

            self.sftp = self.ssh.open_sftp()

        except Exception as e:
            messagebox.showerror("SSH Error", str(e))

    # ---------- GUI ----------
    def create_widgets(self):
        frame = ttk.Frame(self.root)
        frame.pack(fill="both", expand=True)

        columns = ("name", "size", "type", "mtime")

        # icons
        self.folder_icon = tk.PhotoImage(width=16, height=16)
        self.file_icon = tk.PhotoImage(width=16, height=16)
        self.folder_icon.put(("yellow",), to=(0,0,15,15))
        self.file_icon.put(("lightblue",), to=(0,0,15,15))

        # ---------- LOCAL ----------
        local_frame = ttk.Frame(frame)
        local_frame.pack(side="left", fill="both", expand=True)

        ttk.Label(local_frame, text="Local").pack()

        local_tree_frame = ttk.Frame(local_frame)
        local_tree_frame.pack(fill="both", expand=True)

        self.local_tree = ttk.Treeview(local_tree_frame, columns=columns, show="tree headings")
        self.local_tree.pack(side="left", fill="both", expand=True)

        local_scroll_y = ttk.Scrollbar(local_tree_frame, orient="vertical", command=self.local_tree.yview)
        local_scroll_y.pack(side="right", fill="y")

        local_scroll_x = ttk.Scrollbar(local_frame, orient="horizontal", command=self.local_tree.xview)
        local_scroll_x.pack(fill="x")

        self.local_tree.configure(yscrollcommand=local_scroll_y.set,
                                  xscrollcommand=local_scroll_x.set)

        # ---------- REMOTE ----------
        remote_frame = ttk.Frame(frame)
        remote_frame.pack(side="right", fill="both", expand=True)

        ttk.Label(remote_frame, text="Remote").pack()

        remote_tree_frame = ttk.Frame(remote_frame)
        remote_tree_frame.pack(fill="both", expand=True)

        self.remote_tree = ttk.Treeview(remote_tree_frame, columns=columns, show="tree headings")
        self.remote_tree.pack(side="left", fill="both", expand=True)

        remote_scroll_y = ttk.Scrollbar(remote_tree_frame, orient="vertical", command=self.remote_tree.yview)
        remote_scroll_y.pack(side="right", fill="y")

        remote_scroll_x = ttk.Scrollbar(remote_frame, orient="horizontal", command=self.remote_tree.xview)
        remote_scroll_x.pack(fill="x")

        self.remote_tree.configure(yscrollcommand=remote_scroll_y.set,
                                   xscrollcommand=remote_scroll_x.set)

        # headings + sorting
        for tree in (self.local_tree, self.remote_tree):
            tree.heading("#0", text="", anchor="w")
            tree.heading("name", text="Name", command=lambda c="name", t=tree: self.sort_column(t, c, False))
            tree.heading("size", text="Size", command=lambda c="size", t=tree: self.sort_column(t, c, False))
            tree.heading("type", text="Type", command=lambda c="type", t=tree: self.sort_column(t, c, False))
            tree.heading("mtime", text="Modified", command=lambda c="mtime", t=tree: self.sort_column(t, c, False))

            tree.column("#0", width=30)
            tree.column("name", width=250)
            tree.column("size", width=100, anchor="e")
            tree.column("type", width=100)
            tree.column("mtime", width=150)

        self.local_tree.bind("<Double-1>", self.local_double_click)
        self.remote_tree.bind("<Double-1>", self.remote_double_click)

        # drag & drop
        self.root.drop_target_register(DND_FILES)
        self.root.dnd_bind('<<Drop>>', self.upload_file)

        # buttons
        btn_frame = ttk.Frame(self.root)
        btn_frame.pack(fill="x")

        ttk.Button(btn_frame, text="Upload →", command=self.upload_selected).pack(side="left")
        ttk.Button(btn_frame, text="← Download", command=self.download_selected).pack(side="left")
        ttk.Button(btn_frame, text="Delete Remote", command=self.delete_remote).pack(side="left")

    # ---------- LOCAL ----------
    def load_local_dir(self, path):
        self.current_local = path
        self.local_tree.delete(*self.local_tree.get_children())

        if path != os.path.abspath(os.sep):
            self.local_tree.insert("", "end", text="..", values=("..", "", "Folder", ""))

        for f in os.listdir(path):
            full = os.path.join(path, f)

            if os.path.isdir(full):
                size = ""
                ftype = "Folder"
                icon = self.folder_icon
            else:
                size = format_size(os.path.getsize(full))
                ftype = "File"
                icon = self.file_icon

            mtime = format_mtime(os.path.getmtime(full))

            self.local_tree.insert("", "end", text="", image=icon,
                                   values=(f, size, ftype, mtime))

    def local_double_click(self, event):
        selected = self.local_tree.selection()
        if not selected:
            return

        name = self.local_tree.item(selected[0])["values"][0]

        if name == "..":
            new_path = os.path.dirname(self.current_local)
        else:
            new_path = os.path.join(self.current_local, name)

        if os.path.isdir(new_path):
            self.load_local_dir(new_path)

    # ---------- REMOTE ----------
    def load_remote_dir(self, path):
        try:
            self.current_remote = self.sftp.normalize(path)
            self.remote_tree.delete(*self.remote_tree.get_children())

            if self.current_remote != "/":
                self.remote_tree.insert("", "end", text="..", values=("..", "", "Folder", ""))

            for f in self.sftp.listdir_attr(self.current_remote):
                name = f.filename

                if stat.S_ISDIR(f.st_mode):
                    size = ""
                    ftype = "Folder"
                    icon = self.folder_icon
                else:
                    size = format_size(f.st_size)
                    ftype = "File"
                    icon = self.file_icon

                mtime = format_mtime(f.st_mtime)

                self.remote_tree.insert("", "end", text="", image=icon,
                                        values=(name, size, ftype, mtime))
        except Exception as e:
            messagebox.showerror("Remote Error", str(e))

    def remote_double_click(self, event):
        selected = self.remote_tree.selection()
        if not selected:
            return

        name = self.remote_tree.item(selected[0])["values"][0]

        if name == "..":
            new_path = posixpath.dirname(self.current_remote.rstrip("/")) or "/"
        else:
            new_path = posixpath.join(self.current_remote, name)

        if self.is_remote_dir(new_path):
            self.load_remote_dir(new_path)

    def is_remote_dir(self, path):
        try:
            return stat.S_ISDIR(self.sftp.stat(path).st_mode)
        except:
            return False

    # ---------- SORT ----------
    def sort_column(self, tree, col, reverse):
        data = [(tree.set(k, col), k) for k in tree.get_children('')]

        def convert(val):
            try:
                return float(val.split()[0])
            except:
                return val.lower()

        data.sort(key=lambda t: convert(t[0]), reverse=reverse)

        for index, (val, k) in enumerate(data):
            tree.move(k, '', index)

        tree.heading(col, command=lambda: self.sort_column(tree, col, not reverse))

    # ---------- TRANSFER ----------
    def upload_selected(self):
        selected = self.local_tree.selection()
        if not selected:
            return

        name = self.local_tree.item(selected[0])["values"][0]
        local_path = os.path.join(self.current_local, name)

        if os.path.isfile(local_path):
            remote_path = posixpath.join(self.current_remote, name)
            self.sftp.put(local_path, remote_path)
            self.load_remote_dir(self.current_remote)

    def download_selected(self):
        selected = self.remote_tree.selection()
        if not selected:
            return

        name = self.remote_tree.item(selected[0])["values"][0]
        remote_path = posixpath.join(self.current_remote, name)
        local_path = os.path.join(self.current_local, name)

        if not self.is_remote_dir(remote_path):
            self.sftp.get(remote_path, local_path)
            self.load_local_dir(self.current_local)

    def upload_file(self, event):
        files = self.root.tk.splitlist(event.data)
        for f in files:
            name = os.path.basename(f)
            remote_path = posixpath.join(self.current_remote, name)
            self.sftp.put(f, remote_path)

        self.load_remote_dir(self.current_remote)

    # ---------- DELETE ----------
    def delete_remote(self):
        selected = self.remote_tree.selection()
        if not selected:
            return

        name = self.remote_tree.item(selected[0])["values"][0]
        if name == "..":
            return

        remote_path = posixpath.join(self.current_remote, name)

        if messagebox.askyesno("Confirm", f"Delete {name}?"):
            if not self.is_remote_dir(remote_path):
                self.sftp.remove(remote_path)
                self.load_remote_dir(self.current_remote)


# ---------- RUN ----------
if __name__ == "__main__":
    root = TkinterDnD.Tk()
    app = SCPGui(root)
    root.mainloop()

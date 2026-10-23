from tkinter import filedialog
from tkinter import messagebox
from tkinter import ttk
import logging
import os
import threading
import tkinter as tk

from ..export import write_csv
from ..fetch import Cancelled
from ..pipeline import run_batch


class BatchDialog(tk.Toplevel):
    """Analyze many videos, playlists or channels into one CSV, with the
    main window's options."""

    def __init__(self, app):
        super().__init__(app)
        self.app, self.cancel = app, None
        self.title("Batch analysis")
        self.geometry("680x440")
        self.transient(app)
        pad = {"padx": 10, "pady": 4}
        ttk.Label(self, text="One URL per line: videos, playlists or "
                  "channels. Uses the options in the main window.").pack(
            anchor="w", padx=10, pady=(10, 4))
        self.text = tk.Text(self, height=10, wrap="none")
        self.text.pack(fill="both", expand=True, **pad)
        if app.url_var.get().strip():
            self.text.insert("1.0", app.url_var.get().strip() + "\n")

        row = ttk.Frame(self)
        row.pack(fill="x", **pad)
        ttk.Label(row, text="Max videos per playlist or channel:").pack(
            side="left")
        self.limit_var = tk.IntVar(value=25)
        ttk.Spinbox(row, from_=1, to=500, width=5,
                    textvariable=self.limit_var).pack(side="left", padx=4)
        self.refresh_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(row, text="Reload cached videos from YouTube",
                        variable=self.refresh_var).pack(side="left", padx=16)

        self.bar = ttk.Progressbar(self)
        self.bar.pack(fill="x", **pad)
        self.msg = tk.StringVar(value="Videos already in the cache load "
                                "instantly.")
        ttk.Label(self, textvariable=self.msg, foreground="#666",
                  wraplength=640).pack(anchor="w", **pad)

        btns = ttk.Frame(self)
        btns.pack(fill="x", padx=10, pady=(4, 10))
        ttk.Button(btns, text="Close", command=self.close).pack(side="right")
        self.cancel_btn = ttk.Button(btns, text="Cancel", state="disabled",
                                     command=self.stop)
        self.cancel_btn.pack(side="right", padx=6)
        self.run_btn = ttk.Button(btns, text="Run…", command=self.run)
        self.run_btn.pack(side="right")
        self.protocol("WM_DELETE_WINDOW", self.close)

    def post(self, fn, *args):
        self.app.after(0, lambda: self.winfo_exists() and fn(*args))

    def run(self):
        urls = self.text.get("1.0", "end").split()
        if not urls:
            messagebox.showinfo("No URLs", "Paste at least one URL.",
                                parent=self)
            return
        try:
            opts = self.app.score_opts()
            max_comments = self.app.read_opts()[0]
            limit = self.limit_var.get()
        except tk.TclError:
            messagebox.showerror("Bad option", "Options must be whole "
                                 "numbers.", parent=self)
            return
        path = filedialog.asksaveasfilename(
            parent=self, defaultextension=".csv",
            initialfile="hotspots_batch.csv", filetypes=[("CSV", "*.csv")])
        if not path:
            return
        self.cancel = threading.Event()
        self.run_btn.config(state="disabled")
        self.cancel_btn.config(state="normal")
        self.bar.config(value=0)
        threading.Thread(target=self.work, daemon=True, args=(
            urls, opts, max_comments, limit, self.refresh_var.get(), path,
            self.cancel)).start()

    def work(self, urls, opts, max_comments, limit, refresh, path, cancel):
        def log(msg):
            logging.info(msg)
            self.post(self.msg.set, msg)

        def on_video(i, n, url, entry, result, err):
            text = (f"[{i}/{n}] failed: {err}" if err else
                    f"[{i}/{n}] {entry['info'].get('title')}: "
                    f"{len(result['ranges'])} ranges")
            self.post(self.step, i, n, text)
        try:
            rows, failed = run_batch(urls, opts, max_comments, limit, refresh,
                                     log, cancel, on_video)
            if rows:
                write_csv(path, rows)
                n_videos = len({r[0] for r in rows})
                text = (f"Saved {len(rows)} ranges from {n_videos} videos "
                        f"to {os.path.basename(path)}.")
            else:
                text = "No ranges, nothing saved."
            if failed:
                text += f" {len(failed)} failed, see Error log."
            if cancel.is_set():
                text += " Cancelled before the end."
        except Cancelled:
            text = "Cancelled."
        except Exception as e:
            logging.exception("Batch failed")
            text = f"Failed: {e}"
        self.post(self.finish, text)

    def step(self, i, n, text):
        self.bar.config(maximum=n, value=i)
        self.msg.set(text)

    def finish(self, text):
        self.cancel = None
        self.run_btn.config(state="normal")
        self.cancel_btn.config(state="disabled")
        self.msg.set(text)

    def stop(self):
        if self.cancel:
            self.cancel.set()
            self.msg.set("Cancelling after the current step...")

    def close(self):
        self.stop()
        self.destroy()

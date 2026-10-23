from tkinter import filedialog
from tkinter import font as tkfont
from tkinter import messagebox
from tkinter import simpledialog
from tkinter import ttk
import logging
import os
import shutil
import tempfile
import threading
import tkinter as tk
import webbrowser

from ..cache import cache_set_sponsors, clear_cache, load_video, recent_videos, url_problem
from ..clips import clip_filename, clip_paths, concat_clips, download_clip, parse_join_order, safe_name
from ..config import DEFAULT_WEIGHTS, LOG_PATH, SIGNAL_NAMES, load_settings, save_settings
from ..export import csv_rows, markers_edl, markers_xmeml, write_csv
from ..fetch import Cancelled, sponsor_segments_or_none
from .batch import BatchDialog
from ..pipeline import score_video
from ..ranges import chapter_at, chapters_text, fmt_breakdown
from ..thumbs import fetch_thumbnails, moment_url, storyboard
from ..util import fmt_num, fmt_time, normalize, per_view


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("YouTube Hotspot Finder")
        self.geometry("1100x830")
        self.minsize(980, 720)
        self.info = self.segments = self.result = None
        self.sponsors = None    # SponsorBlock segments; None = not loaded
        self.sheets = {}        # storyboard sheet url -> PIL image
        self.thumb_pil = []     # preview frame per range, in rank order
        self.thumb_msg = ""     # text shown on the strip instead of images
        self.thumb_imgs = []    # PhotoImages on the strip (Tk needs a ref)
        self.cache_key = None   # (url, max_comments) of the loaded data
        self.entry = None       # load_video() entry of the shown video
        self.cancel = None      # threading.Event while a fetch is running
        self.sort_key = "score"  # results table: score, time or length
        self.run_id = 0         # results from older runs are ignored
        self._rescore_job = None
        self._build(load_settings())
        self.protocol("WM_DELETE_WINDOW", self.on_close)

    def _build(self, cfg):
        pad = {"padx": 10, "pady": 5}

        top = ttk.Frame(self)
        top.pack(fill="x", **pad)
        ttk.Label(top, text="YouTube URL:").pack(side="left")
        self.url_var = tk.StringVar()
        entry = ttk.Entry(top, textvariable=self.url_var)
        entry.pack(side="left", fill="x", expand=True, padx=8)
        entry.bind("<Return>", lambda e: self.start())
        ttk.Button(top, text="Paste", command=self.paste_url).pack(
            side="left", padx=(0, 6))
        self.btn = ttk.Button(top, text="Analyze", command=self.start)
        self.btn.pack(side="left")
        self.refresh_btn = ttk.Button(top, text="Refresh",
                                      command=lambda: self.start(refresh=True))
        self.refresh_btn.pack(side="left", padx=(6, 0))
        self.cancel_btn = ttk.Button(top, text="Cancel", state="disabled",
                                     command=self.cancel_run)
        self.cancel_btn.pack(side="left", padx=(6, 0))
        self.recent_menu = tk.Menu(self, tearoff=False,
                                   postcommand=self.fill_recent_menu)
        ttk.Menubutton(top, text="Recent", menu=self.recent_menu).pack(
            side="left", padx=(6, 0))
        ttk.Button(top, text="Batch…", command=lambda: BatchDialog(self)).pack(
            side="left", padx=(6, 0))

        opts = ttk.Frame(self)
        opts.pack(fill="x", **pad)
        ttk.Label(opts, text="Clip length (s):").pack(side="left")
        self.clip_var = tk.IntVar(value=cfg.get("clip_len", 30))
        ttk.Spinbox(opts, from_=10, to=180, increment=5, width=5,
                    textvariable=self.clip_var,
                    command=self.schedule_rescore).pack(side="left",
                                                        padx=(4, 16))
        ttk.Label(opts, text="Top results:").pack(side="left")
        self.top_var = tk.IntVar(value=cfg.get("top_n", 8))
        ttk.Spinbox(opts, from_=1, to=20, width=4, textvariable=self.top_var,
                    command=self.schedule_rescore).pack(side="left", padx=4)
        ttk.Label(opts, text="Max comments:").pack(side="left", padx=(16, 0))
        self.cmt_var = tk.IntVar(value=cfg.get("max_comments", 800))
        ttk.Spinbox(opts, from_=100, to=5000, increment=100, width=6,
                    textvariable=self.cmt_var).pack(side="left", padx=4)
        self.fit_var = tk.BooleanVar(value=cfg.get("fit_peaks", False))
        ttk.Checkbutton(opts, text="Fit length to peaks",
                        variable=self.fit_var,
                        command=self.schedule_rescore).pack(side="left",
                                                            padx=(16, 0))

        skip = ttk.Frame(self)
        skip.pack(fill="x", **pad)
        ttk.Label(skip, text="Skip first and last (s):").pack(side="left")
        self.skip_var = tk.IntVar(value=cfg.get("skip_s", 15))
        ttk.Spinbox(skip, from_=0, to=300, increment=5, width=5,
                    textvariable=self.skip_var,
                    command=self.schedule_rescore).pack(side="left",
                                                        padx=(4, 16))
        self.sb_var = tk.BooleanVar(value=cfg.get("sponsorblock", True))
        ttk.Checkbutton(skip, text="Skip sponsors, self-promo and "
                        "like/subscribe reminders (SponsorBlock)",
                        variable=self.sb_var,
                        command=self.on_sponsor_toggle).pack(side="left")
        ttk.Label(skip, text="Min strength (%):").pack(side="left",
                                                       padx=(16, 0))
        self.strength_var = tk.IntVar(value=cfg.get("min_strength", 25))
        ttk.Spinbox(skip, from_=-100, to=95, increment=5, width=5,
                    textvariable=self.strength_var,
                    command=self.schedule_rescore).pack(side="left", padx=4)

        wf = ttk.LabelFrame(self, text="Signal weights (relative; 0 turns a "
                            "signal off, changes re-score instantly)")
        wf.pack(fill="x", **pad)
        saved = {**DEFAULT_WEIGHTS, **cfg.get("weights", {})}
        self.w_vars, self.w_lbls = {}, {}
        for k in DEFAULT_WEIGHTS:
            ttk.Label(wf, text=SIGNAL_NAMES[k].capitalize() + ":").pack(
                side="left", padx=(8, 2), pady=6)
            var = tk.DoubleVar(value=saved[k])
            ttk.Scale(wf, from_=0, to=100, length=110, variable=var,
                      command=self.on_weight).pack(side="left")
            lbl = ttk.Label(wf, width=3)
            lbl.pack(side="left", padx=(2, 6))
            self.w_vars[k], self.w_lbls[k] = var, lbl
        ttk.Button(wf, text="Reset", command=self.reset_weights).pack(
            side="right", padx=8)
        self.on_weight(rescore=False)

        stats = ttk.LabelFrame(self, text="Video stats")
        stats.pack(fill="x", **pad)
        self.stats_lbl = ttk.Label(stats, text="—", justify="left")
        self.stats_lbl.pack(anchor="w", padx=8, pady=6)

        self.small_font = tkfont.Font(size=8)
        self.canvas = tk.Canvas(self, height=125, bg="#1e1e1e",
                                highlightthickness=0)
        self.canvas.pack(fill="x", **pad)
        self.canvas.bind("<Configure>", lambda e: self.draw_graph())
        self.canvas.bind("<Motion>", self.on_graph_hover)
        self.canvas.bind("<Leave>", lambda e: self.canvas.delete("hover"))
        self.canvas.bind("<Button-1>", self.on_graph_click)

        self.thumbs = tk.Canvas(self, height=84, highlightthickness=0)
        self.thumbs.pack(fill="x", padx=10)
        self.thumbs.bind("<Configure>", lambda e: self.draw_thumbs())

        res = ttk.LabelFrame(self, text="Likely best moments "
                             "(double-click to open, Ctrl or Shift-click "
                             "to select several)")
        res.pack(fill="both", expand=True, **pad)
        cols = ("rank", "range", "length", "strength", "confidence", "title",
                "why", "transcript")
        self.tree = ttk.Treeview(res, columns=cols, show="headings", height=6,
                                 selectmode="extended")
        sort_of = {"rank": "score", "range": "time", "length": "length"}
        for c, w, anchor in zip(cols, (62, 115, 78, 60, 75, 240, 270, 200),
                                ("center",) * 5 + ("w",) * 3):
            kw = {"command": (lambda k=sort_of[c]: self.sort_rows(k))} \
                if c in sort_of else {}
            self.tree.heading(c, text=c.capitalize(), **kw)
            self.tree.column(c, width=w, anchor=anchor,
                             stretch=c == "transcript")
        self.tree.pack(fill="both", expand=True, padx=8, pady=(6, 0))
        self.tree.bind("<Double-1>", self.open_selected)
        self.tree.bind("<<TreeviewSelect>>", self.show_detail)
        self.tree.bind("<Control-c>", lambda e: self.copy_link())
        self.detail = ttk.Label(res, text="Select a row to see why it ranked "
                                "and what is said.", justify="left")
        self.detail.pack(fill="x", padx=8, pady=6)
        res.bind("<Configure>",
                 lambda e: self.detail.config(wraplength=e.width - 24))

        bottom = ttk.Frame(self)
        # Packed before the results so it keeps its space on short screens.
        bottom.pack(fill="x", side="bottom", before=res, **pad)
        ttk.Button(bottom, text="Export CSV",
                   command=self.export_csv).pack(side="left")
        for text, cmd in (("Copy list", self.copy_list),
                          ("Copy link", self.copy_link),
                          ("Copy chapters", self.copy_chapters),
                          ("Export markers", self.export_markers),
                          ("Export clip", self.export_clip),
                          ("Error log", self.open_log)):
            ttk.Button(bottom, text=text, command=cmd).pack(side="left",
                                                            padx=(6, 0))
        self.progress = ttk.Progressbar(bottom, length=120)
        self.progress.pack(side="left", padx=(12, 0))
        self.status = tk.StringVar(value="Paste a URL and press Analyze.")
        ttk.Label(bottom, textvariable=self.status,
                  foreground="#666").pack(side="left", padx=12)

    # --- settings ---
    def weights(self):
        return {k: round(v.get()) for k, v in self.w_vars.items()}

    def on_weight(self, _value=None, rescore=True):
        for k, w in self.weights().items():
            self.w_lbls[k].config(text=str(w))
        if rescore:
            self.schedule_rescore()

    def reset_weights(self):
        for k, v in self.w_vars.items():
            v.set(DEFAULT_WEIGHTS[k])
        self.on_weight()

    def read_opts(self):
        """(max_comments, clip_len, top_n); raises tk.TclError if bad."""
        return self.cmt_var.get(), self.clip_var.get(), self.top_var.get()

    def score_opts(self):
        """Options for score_video(); raises tk.TclError if bad."""
        _, clip_len, top_n = self.read_opts()
        return {"clip_len": clip_len, "top_n": top_n,
                "weights": self.weights(), "fit_peaks": self.fit_var.get(),
                "skip_s": self.skip_var.get(),
                "sponsorblock": self.sb_var.get(),
                "min_strength": self.strength_var.get() / 100}

    def save(self):
        cfg = {"weights": self.weights(), "fit_peaks": self.fit_var.get(),
               "sponsorblock": self.sb_var.get()}
        try:
            cfg["max_comments"], cfg["clip_len"], cfg["top_n"] = \
                self.read_opts()
            cfg["skip_s"] = self.skip_var.get()
            cfg["min_strength"] = self.strength_var.get()
        except tk.TclError:
            pass
        save_settings(cfg)

    def on_close(self):
        self.save()
        self.destroy()

    # --- actions ---
    def report_callback_exception(self, *exc):
        # Errors inside button handlers etc. would otherwise go to the
        # hidden console.
        logging.error("GUI error", exc_info=exc)
        messagebox.showerror("Error", f"{exc[1]}\n\nDetails: Error log button.")

    def open_log(self):
        if not os.path.exists(LOG_PATH):
            messagebox.showinfo("Error log", "No log yet.")
        elif hasattr(os, "startfile"):
            os.startfile(LOG_PATH)
        else:
            webbrowser.open("file://" + LOG_PATH)

    def start(self, refresh=False):
        """Analyze the URL: from memory or the disk cache if possible, else
        from YouTube. refresh=True always reloads from YouTube."""
        url = self.url_var.get().strip()
        if not url:
            messagebox.showinfo("URL needed", "Please paste a YouTube URL.")
            return
        problem = url_problem(url)
        if problem:
            messagebox.showinfo("Not a video", problem)
            return
        try:  # read Tk vars here, on the main thread
            max_comments = self.read_opts()[0]
        except tk.TclError:
            messagebox.showerror("Bad option", "Options must be whole numbers.")
            return
        self.save()
        if not refresh and (url, max_comments) == self.cache_key:
            if self.rescore():
                self.status.set("Re-scored with the already loaded data.")
            return
        self.run_id += 1
        self.cancel = threading.Event()
        self.set_busy(True)
        self.tree.delete(*self.tree.get_children())
        threading.Thread(target=self.work, daemon=True, args=(
            self.run_id, url, max_comments, self.cancel,
            self.sb_var.get(), refresh)).start()

    def post(self, run_id, fn, *args):
        """Run fn on the GUI thread, unless the run was cancelled or
        replaced meanwhile."""
        self.after(0, lambda: run_id == self.run_id and fn(*args))

    def work(self, run_id, url, max_comments, cancel, sponsorblock, refresh):
        def log(msg):
            logging.info(msg)
            self.post(run_id, self.status.set, msg)
        try:
            entry, from_cache = load_video(
                url, max_comments, sponsorblock, log, cancel,
                lambda done, total: self.post(run_id, self.set_progress,
                                              done, total), refresh)
            self.post(run_id, self.fetched, url, max_comments, entry,
                      from_cache)
        except Cancelled:
            logging.info("Cancelled %s", url)
        except Exception as e:
            logging.exception("Analysis failed for %s", url)
            self.post(run_id, self.fail, e)

    def cancel_run(self):
        if self.cancel:
            self.cancel.set()
            self.run_id += 1
            self.set_busy(False)
            self.status.set("Cancelled.")

    def fill_recent_menu(self):
        m = self.recent_menu
        m.delete(0, "end")
        items = recent_videos()
        for r in items:
            title = r["title"] if len(r["title"]) <= 70 \
                else r["title"][:69] + "…"
            m.add_command(label=f"{title}   ({r['when']})",
                          command=lambda u=r["url"]: self.open_recent(u))
        if not items:
            m.add_command(label="No videos yet", state="disabled")
        m.add_separator()
        m.add_command(label="Clear history and cache…",
                      command=self.clear_history)

    def open_recent(self, url):
        if self.cancel:
            return
        self.url_var.set(url)
        self.start()

    def clear_history(self):
        if messagebox.askyesno("Clear history", "Delete the recent list and "
                               "all cached video data?"):
            clear_cache()
            self.status.set("History and cache cleared.")

    def set_busy(self, busy):
        self.btn.config(state="disabled" if busy else "normal")
        self.refresh_btn.config(state="disabled" if busy else "normal")
        self.cancel_btn.config(state="normal" if busy else "disabled")
        self.progress.stop()
        if busy:
            self.progress.config(mode="indeterminate")
            self.progress.start(12)
        else:
            self.cancel = None
            self.progress.config(mode="determinate", value=0)

    def set_progress(self, done, total, text=None):
        if str(self.progress.cget("mode")) == "indeterminate":
            self.progress.stop()
            self.progress.config(mode="determinate")
        self.progress.config(maximum=total, value=min(done, total))
        self.status.set(text if text is not None else
                        f"Loading comments: {done} / ~{total}")

    def fetched(self, url, max_comments, entry, from_cache):
        self.set_busy(False)
        self.entry = entry
        self.info, self.segments = entry["info"], entry["segments"]
        self.sponsors = entry["sponsors"]
        self.cache_key = (url, max_comments)
        self.sheets = {}
        if self.rescore():
            self.status.set(
                f"Done, from cache (fetched {entry['fetched_at']}). "
                "Refresh reloads from YouTube." if from_cache else "Done.")
        else:
            self.info = self.cache_key = None

    def on_sponsor_toggle(self):
        info = self.info
        if not (self.sb_var.get() and info and self.sponsors is None
                and not self.cancel):
            return self.schedule_rescore()
        self.status.set("Fetching SponsorBlock segments...")

        def work():
            sponsors = sponsor_segments_or_none(info.get("id"))
            if sponsors is not None:
                cache_set_sponsors(info.get("id"), sponsors)
            self.after(0, lambda: info is self.info and self.sponsors_loaded(
                sponsors))
        threading.Thread(target=work, daemon=True).start()

    def sponsors_loaded(self, sponsors):
        self.sponsors = self.entry["sponsors"] = sponsors
        if self.rescore():
            self.status.set("Done." if sponsors is not None
                            else "SponsorBlock lookup failed. See Error log.")

    def schedule_rescore(self):
        if self._rescore_job:
            self.after_cancel(self._rescore_job)
        self._rescore_job = self.after(150, self.rescore)

    def rescore(self):
        """Score the loaded data with the current options. No network."""
        self._rescore_job = None
        if not self.info or self.cancel:
            return False
        try:
            opts = self.score_opts()
        except tk.TclError:
            return False
        try:
            self.result = score_video(
                {"info": self.info, "segments": self.segments,
                 "sponsors": self.sponsors}, opts)
        except Exception as e:
            logging.exception("Scoring failed")
            self.fail(e)
            return False
        self.show()
        return True

    def fail(self, e):
        self.set_busy(False)
        self.status.set("Failed. See Error log.")
        messagebox.showerror("Error", f"{e}\n\nDetails: Error log button.")

    def show(self):
        info, result = self.info, self.result
        n_comments = len(info.get("comments") or [])
        names = {"heat": "Most-replayed heatmap",
                 "comm": f"Comments ({result['ts_hits']} timestamps, "
                         f"{result['quote_hits']} quotes)",
                 "trans": "Transcript",
                 "chap": "Chapters / description timestamps"}
        used = ", ".join(names[s] for s in result["signals"]) or "none"
        lines = [
            f"Title:    {info.get('title', 'n/a')}",
            f"Channel:  {info.get('channel') or info.get('uploader', 'n/a')}"
            f"   (subscribers: {fmt_num(info.get('channel_follower_count'))})",
            f"Views:    {fmt_num(info.get('view_count'))}    "
            f"Likes: {fmt_num(info.get('like_count'))}"
            f"{per_view(info.get('like_count'), info.get('view_count'))}    "
            f"Comments loaded: {n_comments} / "
            f"{fmt_num(info.get('comment_count'))}"
            f"{per_view(info.get('comment_count'), info.get('view_count'))}",
            f"Duration: {fmt_time(info.get('duration') or 0)}    "
            f"Uploaded: {info.get('upload_date', 'n/a')}",
            f"Signals used: {used}",
        ]
        lines.append("Skipped:  " + self.skipped_text())
        if result["hidden"]:
            lines.append(f"Hidden:   {result['hidden']} weaker ranges below "
                         f"the minimum strength")
        if not result["ranges"]:
            lines.append("No range stands out. Lower Min strength to see "
                         "weaker ones.")
        if not info.get("heatmap"):
            lines.append("Note: no 'Most replayed' data for this video, so "
                         "results are estimates.")
        self.stats_lbl.config(text="\n".join(lines))
        self.tree.delete(*self.tree.get_children())
        for i, r in enumerate(result["ranges"], 1):
            text = r["quote"]
            self.tree.insert("", "end", iid=str(i - 1), values=(
                i, f"{fmt_time(r['start'])} – {fmt_time(r['end'])}",
                f"{int(r['end'] - r['start'])}s", f"{r['strength']:.0%}",
                r["confidence"], r["title"], fmt_breakdown(r["shares"]),
                text[:120] + "…" if len(text) > 120 else text))
        self.detail.config(text="Select a row to see why it ranked and "
                           "what is said.")
        self.sort_rows(self.sort_key)
        self.draw_graph()
        self.load_thumbs()

    def sort_rows(self, key):
        """Reorder the table. Row ids stay the index into result ranges,
        so selection and the preview strip keep pointing at the same range."""
        self.sort_key = key
        heads = ("rank", "range", "length", "strength", "confidence",
                 "title", "why", "transcript")
        active = {"score": "rank", "time": "range", "length": "length"}[key]
        sort_of = {"rank": "score", "range": "time", "length": "length"}
        for col in heads:
            kw = {"text": col.capitalize() + (" ↓" if col == active else "")}
            if col in sort_of:
                kw["command"] = lambda k=sort_of[col]: self.sort_rows(k)
            self.tree.heading(col, **kw)
        if not self.result:
            return
        ranges = self.result["ranges"]
        keyfn = {"score": lambda r: -r["score"],
                 "time": lambda r: r["start"],
                 "length": lambda r: r["start"] - r["end"]}[key]
        for pos, i in enumerate(sorted(range(len(ranges)),
                                       key=lambda i: keyfn(ranges[i]))):
            self.tree.move(str(i), "", pos)

    def paste_url(self):
        try:
            self.url_var.set(self.clipboard_get().strip())
        except tk.TclError:
            self.status.set("Nothing to paste.")

    def skipped_text(self):
        parts = []
        spans = self.result["excluded"]
        edges = [s for s in spans if s[2] in ("start", "end")]
        if edges:
            parts.append(f"first and last {fmt_time(edges[0][1])}")
        if not self.sb_var.get():
            parts.append("SponsorBlock off")
        elif self.sponsors is None:
            parts.append("SponsorBlock unavailable")
        else:
            sb = [f"{c} {fmt_time(s)}–{fmt_time(e)}"
                  for s, e, c in spans if c not in ("start", "end")]
            parts.append(", ".join(sb) if sb else "no SponsorBlock segments")
        return "; ".join(parts)

    def show_detail(self, _event=None):
        r = self.selected_range()
        if not r:
            return
        q = r["quote"]
        q = q[:400].rsplit(" ", 1)[0] + " …" if len(q) > 400 else q
        said = f"“{q}”" if q else "(no transcript)"
        agree = ", ".join(SIGNAL_NAMES[k] for k in r["agree"]) or "none"
        self.detail.config(
            text=f"Title: {r['title']}\n"
                 f"Why: {fmt_breakdown(r['shares'])}\n"
                 f"Confidence: {r['confidence']} (in the top quarter of: "
                 f"{agree}). Strength {r['strength']:.0%}, score "
                 f"{r['score']:.3f}\n"
                 f"Said: {said}")

    def graph_x(self, t):
        w = self.canvas.winfo_width()
        return 10 + (w - 20) * t / self.result["duration"]

    def graph_time(self, x):
        """Video second under canvas x, or None if there is no graph."""
        if not self.result or not self.result["score"]:
            return None
        w = self.canvas.winfo_width()
        frac = min(1, max(0, (x - 10) / max(1, w - 20)))
        return frac * self.result["duration"]

    def fit_text(self, text, width):
        if self.small_font.measure(text) <= width:
            return text
        while text and self.small_font.measure(text + "…") > width:
            text = text[:-1]
        return text + "…" if text else ""

    def draw_graph(self):
        c = self.canvas
        c.delete("all")
        if not self.result or not self.result["score"]:
            c.config(cursor="")
            return
        c.config(cursor="hand2")
        w, h = c.winfo_width(), c.winfo_height()
        sc = normalize(self.result["score"])
        n = len(sc)
        dur = self.result["duration"]
        chapters = sorted(self.info.get("chapters") or [],
                          key=lambda ch: ch.get("start_time") or 0)
        top = 18 if chapters else 4   # room for chapter titles
        # Highlights first so the score line stays on top. Solid fill:
        # stipple patterns render as grey hatching on Windows.
        for s, e, _ in self.result["excluded"]:
            c.create_rectangle(self.graph_x(s), top,
                               self.graph_x(min(e, dur)), h - 4,
                               outline="", fill="#333333")
        for r in self.result["ranges"]:
            x1 = self.graph_x(r["start"])
            x2 = max(self.graph_x(r["end"]), x1 + 2)
            c.create_rectangle(x1, top, x2, h - 4, outline="", fill="#4a3b12")
            c.create_rectangle(x1, h - 6, x2, h - 4, outline="",
                               fill="#ffb300")
        for i, ch in enumerate(chapters):
            x = self.graph_x(ch.get("start_time") or 0)
            nxt = (self.graph_x(chapters[i + 1].get("start_time") or 0)
                   if i + 1 < len(chapters) else w - 10)
            c.create_line(x, 2, x, h - 4, fill="#6b6b6b", dash=(2, 3))
            label = self.fit_text(ch.get("title") or "", nxt - x - 6)
            if len(label) > 2:
                c.create_text(x + 3, 3, anchor="nw", text=label,
                              fill="#b0b0b0", font=self.small_font)
        pts = []
        for i, v in enumerate(sc):
            pts += [10 + (w - 20) * i / max(1, n - 1),
                    h - 10 - (h - 14 - top) * v]
        if len(pts) >= 4:
            c.create_line(*pts, fill="#4fc3f7", width=2)

    def on_graph_hover(self, e):
        c = self.canvas
        c.delete("hover")
        t = self.graph_time(e.x)
        if t is None:
            return
        res = self.result
        score = res["score"][min(len(res["score"]) - 1, int(t // res["bin"]))]
        parts = [fmt_time(t), f"score {score:.2f}"]
        parts += [f"#{k} {r['title']}"
                  for k, r in enumerate(res["ranges"], 1)
                  if r["start"] <= t < r["end"]]
        parts += [f"skipped ({why})" for s, e2, why in res["excluded"]
                  if s <= t < e2][:1]
        if chapter_at(self.info, t):
            parts.append(f"chapter: {chapter_at(self.info, t)}")
        w, h = c.winfo_width(), c.winfo_height()
        c.create_line(e.x, 0, e.x, h, fill="#ffffff", tags="hover")
        left = e.x < w / 2
        item = c.create_text(e.x + (8 if left else -8), h - 8,
                             anchor="sw" if left else "se", fill="#ffffff",
                             text=" · ".join(parts), font=self.small_font,
                             tags="hover")
        x1, y1, x2, y2 = c.bbox(item)
        box = c.create_rectangle(x1 - 4, y1 - 2, x2 + 4, y2 + 2,
                                 fill="#000000", outline="#777777",
                                 tags="hover")
        c.tag_lower(box, item)

    def on_graph_click(self, e):
        t = self.graph_time(e.x)
        if t is not None:
            webbrowser.open(moment_url(self.info, t))

    def load_thumbs(self):
        """Fetch the preview frame of each range in the background."""
        self.thumb_pil = []
        result, info = self.result, self.info
        sb = storyboard(info)
        if not sb or not result["ranges"]:
            return self.draw_thumbs("No preview images for this video.")
        try:
            from PIL import ImageTk
        except ImportError:
            return self.draw_thumbs("Install Pillow to see preview images: "
                                    "python -m pip install pillow")
        times = [(r["start"] + r["end"]) / 2 for r in result["ranges"]]
        cache = self.sheets

        def work():
            try:
                imgs = fetch_thumbnails(sb, times, cache)
            except Exception:
                logging.exception("Preview images failed")
                imgs = None
            self.after(0, lambda: result is self.result and done(imgs))

        def done(imgs):
            if imgs is None:
                return self.draw_thumbs("Could not load preview images. "
                                        "See Error log.")
            self.thumb_pil = imgs
            self.draw_thumbs("")
        self.thumb_pil = []
        self.draw_thumbs("Loading preview images...")
        threading.Thread(target=work, daemon=True).start()

    def draw_thumbs(self, message=None):
        """message replaces the strip's text until the next message;
        "" clears it."""
        if message is not None:
            self.thumb_msg = message
        c = self.thumbs
        c.delete("all")
        self.thumb_imgs = []
        if self.thumb_msg:
            c.create_text(4, 40, anchor="w", text=self.thumb_msg, fill="#666")
            return
        pil = self.thumb_pil
        if not pil or not self.result:
            return
        from PIL import ImageTk
        w, h = c.winfo_width(), c.winfo_height()
        slot = max(40, min(160, (w - 4) / len(pil)))
        for i, (im, r) in enumerate(zip(pil, self.result["ranges"])):
            x = i * slot
            tag = f"thumb{i}"
            k = min((slot - 6) / im.width, (h - 18) / im.height)
            shown = ImageTk.PhotoImage(im.resize(
                (max(1, int(im.width * k)), max(1, int(im.height * k)))))
            self.thumb_imgs.append(shown)
            c.create_image(x + 2, 2, anchor="nw", image=shown, tags=tag)
            c.create_text(x + 2, h - 2, anchor="sw", font=self.small_font,
                          text=f"#{i + 1}  {fmt_time(r['start'])}", tags=tag)
            c.tag_bind(tag, "<Button-1>", lambda e, i=i: self.pick_row(i))
            c.tag_bind(tag, "<Double-1>", lambda e, i=i: (
                self.pick_row(i), self.open_selected()))
        c.config(cursor="hand2")

    def pick_row(self, i):
        self.tree.selection_set(str(i))
        self.tree.see(str(i))

    def selected_ranges(self):
        """Selected rows in the order shown in the table."""
        if not self.result:
            return []
        chosen = set(self.tree.selection())
        return [self.result["ranges"][int(i)]
                for i in self.tree.get_children() if i in chosen]

    def selected_range(self):
        ranges = self.selected_ranges()
        return ranges[0] if ranges else None

    def open_selected(self, _event=None):
        r = self.selected_range()
        if r and self.info:
            webbrowser.open(moment_url(self.info, r["start"]))

    def export_csv(self):
        if not self.result or not self.result["ranges"]:
            return
        path = filedialog.asksaveasfilename(defaultextension=".csv",
                                            filetypes=[("CSV", "*.csv")])
        if path:
            write_csv(path, csv_rows(self.info, self.result))

    def export_clip(self):
        """Download the selected rows. Several rows can be separate files
        or one file joined in the order shown."""
        ranges = self.selected_ranges()
        if not ranges or not self.info:
            self.status.set("Select a row first. Ctrl-click or Shift-click "
                            "to select more.")
            return
        if self.cancel:
            self.status.set("Wait until the current task finishes, or cancel it.")
            return
        combine = False
        if len(ranges) > 1:
            combine = messagebox.askyesnocancel(
                "Download clips",
                f"Download these {len(ranges)} clips as one combined video?\n\n"
                "Yes joins them, in an order you choose.\n"
                "No saves a separate file for each clip.")
            if combine is None:
                return
            if combine:
                ranges = self.ask_join_order(ranges)
                if not ranges:
                    return
        spans = [(r["start"], r["end"]) for r in ranges]
        combine_to, dests = None, None
        if len(ranges) == 1 or combine:
            initial = (clip_filename(self.info, ranges[0]) if len(ranges) == 1
                       else f"{safe_name(self.info.get('title'))} "
                            f"{len(ranges)} clips.mp4")
            path = filedialog.asksaveasfilename(
                defaultextension=".mp4", initialfile=initial,
                filetypes=[("MP4 video", "*.mp4")])
            if not path:
                return
            if combine:
                combine_to = path
            else:
                dests = [path]
        else:
            folder = filedialog.askdirectory(title="Save clips")
            if not folder:
                return
            dests = clip_paths(folder, [clip_filename(self.info, r)
                                        for r in ranges])
        url = (self.info.get("webpage_url")
               or f"https://www.youtube.com/watch?v={self.info.get('id')}")
        self.run_id += 1
        self.cancel = threading.Event()
        self.set_busy(True)
        self.status.set(f"Downloading {len(spans)} clip"
                        f"{'' if len(spans) == 1 else 's'} ...")
        threading.Thread(
            target=self._download_clips, daemon=True, args=(
                self.run_id, url, spans, dests, combine_to, self.cancel)
        ).start()

    def ask_join_order(self, ranges):
        """Ask for a rank order. Returns the ranges in that order, or None."""
        ranks = [self.result["ranges"].index(r) + 1 for r in ranges]
        by_rank = dict(zip(ranks, ranges))
        default = ", ".join(str(n) for n in ranks)
        while True:
            text = simpledialog.askstring(
                "Join order",
                "Order to join, using the Rank numbers.\n"
                "Any order, for example 1, 4, 7 or 9, 5, 3, 1.",
                initialvalue=default, parent=self)
            if text is None:
                return None
            try:
                order = parse_join_order(text, ranks)
            except ValueError as e:
                messagebox.showerror("Join order", str(e), parent=self)
                default = text
                continue
            return [by_rank[n] for n in order]

    def _download_clips(self, run_id, url, spans, dests, combine_to, cancel):
        def log(msg):
            logging.info(msg)
            self.post(run_id, self.status.set, msg)

        def prog(done, total, text):
            self.post(run_id, self.set_progress, done, total, text)

        tmp = None
        try:
            targets = dests
            if combine_to:
                tmp = tempfile.mkdtemp(prefix="yt_clips_")
                targets = [os.path.join(tmp, f"{i}.mp4")
                           for i in range(len(spans))]
            saved = []
            n = len(spans)
            for i, ((start, end), dest) in enumerate(zip(spans, targets), 1):
                if cancel.is_set():
                    raise Cancelled()
                saved.append(download_clip(
                    url, start, end, dest, log, cancel, prog,
                    label=f"Clip {i} of {n}" if n > 1 else "Downloading clip"))
            if combine_to:
                log("Joining clips...")
                concat_clips(saved, combine_to)
                saved = [combine_to]
            self.post(run_id, self.clips_saved, saved)
        except Cancelled:
            logging.info("Clip download cancelled")
        except Exception as e:
            logging.exception("Clip download failed")
            self.post(run_id, self.fail, e)
        finally:
            if tmp:
                shutil.rmtree(tmp, ignore_errors=True)

    def clips_saved(self, paths):
        self.set_busy(False)
        if len(paths) == 1:
            self.status.set(f"Saved clip to {os.path.basename(paths[0])}")
        else:
            folder = os.path.basename(os.path.dirname(paths[0])) or paths[0]
            self.status.set(f"Saved {len(paths)} clips to {folder}.")

    def copy_list(self):
        if self.result and self.result["ranges"]:
            self.clipboard_clear()
            self.clipboard_append("\n".join(
                f"{i}. {fmt_time(r['start'])} - {fmt_time(r['end'])} "
                f"{r['title']} (strength {r['strength']:.0%}, "
                f"{r['confidence']} confidence; {fmt_breakdown(r['shares'])})"
                for i, r in enumerate(self.result["ranges"], 1)))
            self.status.set("Copied to clipboard.")

    def copy_link(self):
        r = self.selected_range()
        if not r:
            self.status.set("Select a row first.")
            return
        url = moment_url(self.info, r["start"])
        self.clipboard_clear()
        self.clipboard_append(url)
        self.status.set(f"Copied {url}")

    def copy_chapters(self):
        if not self.result or not self.result["ranges"]:
            return
        text = chapters_text(self.result["ranges"], self.result["duration"])
        self.clipboard_clear()
        self.clipboard_append(text)
        n = text.count("\n") + 1
        self.status.set(f"Copied {n} chapters." if n >= 3 else
                        f"Copied {n} chapters; YouTube needs at least 3.")

    def export_markers(self):
        if not self.result or not self.result["ranges"]:
            return
        path = filedialog.asksaveasfilename(
            defaultextension=".edl",
            filetypes=[("DaVinci Resolve markers", "*.edl"),
                       ("Premiere Pro (Final Cut XML)", "*.xml")])
        if not path:
            return
        fps = round(self.info.get("fps") or 30)
        name = self.info.get("title") or "Hotspots"
        ranges = self.result["ranges"]
        xml = path.lower().endswith(".xml")
        text = (markers_xmeml(ranges, self.result["duration"], fps, name)
                if xml else markers_edl(ranges, fps, name))
        with open(path, "w", encoding="utf-8",
                  newline="\n" if xml else "\r\n") as f:
            f.write(text)
        self.status.set(f"Saved {len(ranges)} markers at {fps} fps.")

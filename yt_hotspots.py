"""
YouTube Hotspot Finder  (Tkinter GUI)

Paste a YouTube URL -> get video stats and a ranked list of time ranges that
are most likely to be the "best" moments, using:
  1. YouTube's "Most replayed" heatmap (real viewer data, when available)
  2. Timestamps mentioned in comments (weighted by comment likes)
  3. Transcript energy (speech density, questions, emphasis, hook words)

Install:   pip install yt-dlp youtube-transcript-api
Run:       python yt_hotspots.py
"""

import csv
import logging
import logging.handlers
import math
import os
import re
import sys
import threading
import tkinter as tk
import webbrowser
from tkinter import filedialog, messagebox, ttk

# --------------------------------------------------------------------------
# Core logic (no GUI) -------------------------------------------------------
# --------------------------------------------------------------------------

LOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "yt_hotspots.log")

TS_RE = re.compile(r"(?<![\d:])(?:(\d{1,2}):)?(\d{1,2}):(\d{2})(?![\d:])")
HOOK_WORDS = {
    "secret", "never", "always", "mistake", "truth", "actually", "crazy",
    "insane", "shocking", "biggest", "worst", "best", "unbelievable",
    "important", "warning", "stop", "wrong", "why", "how", "nobody",
    "everyone", "free", "million", "billion", "story", "lesson", "tip",
}


def fmt_time(sec):
    sec = int(max(0, sec))
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def fmt_num(n):
    return f"{n:,}" if isinstance(n, (int, float)) else "n/a"


def parse_timestamps(text, duration):
    """Return list of seconds mentioned in a comment, e.g. '3:45', '1:02:10'."""
    out = []
    for m in TS_RE.finditer(text or ""):
        h = int(m.group(1) or 0)
        mi = int(m.group(2))
        s = int(m.group(3))
        if s >= 60 or (h and mi >= 60):
            continue
        total = h * 3600 + mi * 60 + s
        if 0 <= total <= duration:
            out.append(total)
    return out


def normalize(values):
    if not values:
        return values
    lo, hi = min(values), max(values)
    if hi - lo < 1e-9:
        return [0.0] * len(values)
    return [(v - lo) / (hi - lo) for v in values]


def smooth(values, radius):
    """Simple moving average."""
    if radius <= 0:
        return values[:]
    out = []
    n = len(values)
    for i in range(n):
        a, b = max(0, i - radius), min(n, i + radius + 1)
        out.append(sum(values[a:b]) / (b - a))
    return out


def heatmap_signal(heatmap, duration, bin_s):
    """Resample YouTube heatmap segments onto our bins."""
    n = math.ceil(duration / bin_s)
    if not heatmap:
        return None
    vals = [0.0] * n
    for i in range(n):
        centre = i * bin_s + bin_s / 2
        for seg in heatmap:
            if seg["start_time"] <= centre < seg["end_time"]:
                vals[i] = float(seg["value"])
                break
    return vals


def comment_signal(comments, duration, bin_s):
    """Spread weighted timestamp mentions over nearby bins."""
    n = math.ceil(duration / bin_s)
    vals = [0.0] * n
    hits = 0
    for c in comments or []:
        stamps = parse_timestamps(c.get("text", ""), duration)
        if not stamps:
            continue
        weight = 1.0 + math.log1p(max(0, c.get("like_count") or 0))
        for t in stamps:
            idx = min(n - 1, int(t // bin_s))
            vals[idx] += weight
            hits += 1
    if hits == 0:
        return None, 0
    return smooth(vals, 2), hits


def transcript_signal(segments, duration, bin_s):
    """Heuristic 'energy' from the transcript: density + emphasis + hooks."""
    n = math.ceil(duration / bin_s)
    if not segments:
        return None
    vals = [0.0] * n
    for seg in segments:
        text = seg["text"]
        words = re.findall(r"[A-Za-z']+", text.lower())
        if not words:
            continue
        score = len(words)
        score += 3 * text.count("?") + 3 * text.count("!")
        score += 2 * sum(1 for w in words if w in HOOK_WORDS)
        idx = min(n - 1, int(seg["start"] // bin_s))
        vals[idx] += score
    return smooth(vals, 1)


def combine_signals(duration, bin_s, heat, comm, trans):
    """Weighted blend; weights re-normalised over the signals that exist."""
    weights = {"heat": 0.45, "comm": 0.35, "trans": 0.20}
    present = {}
    if heat is not None:
        present["heat"] = normalize(heat)
    if comm is not None:
        present["comm"] = normalize(comm)
    if trans is not None:
        present["trans"] = normalize(trans)
    if not present:
        return [], []
    total_w = sum(weights[k] for k in present)
    n = len(next(iter(present.values())))
    score = [0.0] * n
    for k, arr in present.items():
        for i in range(n):
            score[i] += (weights[k] / total_w) * arr[i]
    return smooth(score, 1), list(present.keys())


def top_ranges(score, bin_s, duration, clip_len, top_n):
    """Pick the best non-overlapping windows of ~clip_len seconds."""
    if not score:
        return []
    win = max(1, round(clip_len / bin_s))
    n = len(score)
    if win > n:
        win = n
    cands = []
    for i in range(0, n - win + 1):
        cands.append((sum(score[i:i + win]) / win, i))
    cands.sort(reverse=True)
    picked, used = [], []
    for sc, i in cands:
        if any(not (i + win <= a or i >= b) for a, b in used):
            continue
        used.append((i, i + win))
        start = i * bin_s
        end = min(duration, (i + win) * bin_s)
        picked.append({"start": start, "end": end, "score": sc})
        if len(picked) >= top_n:
            break
    return picked


def snap_to_sentences(rng, segments, duration):
    """Nudge range edges to the nearest transcript segment boundary."""
    if not segments:
        return rng
    starts = [s["start"] for s in segments]
    ends = [s["start"] + s.get("duration", 0) for s in segments]
    new_start = min(starts, key=lambda t: abs(t - rng["start"]))
    new_end = min(ends, key=lambda t: abs(t - rng["end"]))
    if new_end - new_start >= 8 and abs(new_start - rng["start"]) < 8:
        rng["start"], rng["end"] = new_start, min(duration, new_end)
    return rng


# --------------------------------------------------------------------------
# Data fetching -------------------------------------------------------------
# --------------------------------------------------------------------------

def fetch_video(url, max_comments=800, log=lambda m: None):
    try:
        import yt_dlp
    except ImportError:
        raise RuntimeError("Missing dependency. Run:  pip install yt-dlp")

    log("Fetching video info, heatmap and comments...")
    opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "getcomments": True,
        "extractor_args": {
            "youtube": {"max_comments": [str(max_comments), "all", "all", "0"],
                        "comment_sort": ["top"]}
        },
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)

    segments = fetch_transcript(info.get("id"), log)
    return info, segments


def fetch_transcript(video_id, log):
    try:
        from youtube_transcript_api import YouTubeTranscriptApi
    except ImportError:
        log("youtube-transcript-api not installed; skipping transcript.")
        return None
    log("Fetching transcript...")
    try:
        try:  # newer API (1.x)
            data = YouTubeTranscriptApi().fetch(video_id)
            return [{"text": s.text, "start": s.start, "duration": s.duration}
                    for s in data]
        except AttributeError:  # older API
            data = YouTubeTranscriptApi.get_transcript(video_id)
            return [{"text": s["text"], "start": s["start"],
                     "duration": s.get("duration", 0)} for s in data]
    except Exception as e:  # no captions, disabled, blocked...
        log(f"No transcript available ({type(e).__name__}).")
        return None


def analyze(info, segments, bin_s=10, clip_len=30, top_n=8):
    duration = info.get("duration") or 0
    if duration <= 0:
        raise RuntimeError("Could not determine video duration (live stream?).")
    heat = heatmap_signal(info.get("heatmap"), duration, bin_s)
    comm, hits = comment_signal(info.get("comments"), duration, bin_s)
    trans = transcript_signal(segments, duration, bin_s)
    score, used = combine_signals(duration, bin_s, heat, comm, trans)
    ranges = top_ranges(score, bin_s, duration, clip_len, top_n)
    # Snapping must never create overlaps: keep a snap only if it collides
    # with no other range.
    for i, r in enumerate(ranges):
        s = snap_to_sentences(dict(r), segments, duration)
        if all(s["end"] <= o["start"] or s["start"] >= o["end"]
               for j, o in enumerate(ranges) if j != i):
            ranges[i] = s
    ranges.sort(key=lambda r: -r["score"])
    return {"score": score, "bin": bin_s, "ranges": ranges,
            "signals": used, "ts_hits": hits, "duration": duration}


# --------------------------------------------------------------------------
# GUI -----------------------------------------------------------------------
# --------------------------------------------------------------------------

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("YouTube Hotspot Finder")
        self.geometry("860x700")
        self.minsize(760, 600)
        self.info = None
        self.result = None
        self._build()

    def _build(self):
        pad = {"padx": 10, "pady": 5}

        top = ttk.Frame(self)
        top.pack(fill="x", **pad)
        ttk.Label(top, text="YouTube URL:").pack(side="left")
        self.url_var = tk.StringVar()
        entry = ttk.Entry(top, textvariable=self.url_var)
        entry.pack(side="left", fill="x", expand=True, padx=8)
        entry.bind("<Return>", lambda e: self.start())
        self.btn = ttk.Button(top, text="Analyze", command=self.start)
        self.btn.pack(side="left")

        opts = ttk.Frame(self)
        opts.pack(fill="x", **pad)
        ttk.Label(opts, text="Clip length (s):").pack(side="left")
        self.clip_var = tk.IntVar(value=30)
        ttk.Spinbox(opts, from_=10, to=180, increment=5, width=5,
                    textvariable=self.clip_var).pack(side="left", padx=(4, 16))
        ttk.Label(opts, text="Top results:").pack(side="left")
        self.top_var = tk.IntVar(value=8)
        ttk.Spinbox(opts, from_=1, to=20, width=4,
                    textvariable=self.top_var).pack(side="left", padx=4)
        ttk.Label(opts, text="Max comments:").pack(side="left", padx=(16, 0))
        self.cmt_var = tk.IntVar(value=800)
        ttk.Spinbox(opts, from_=100, to=5000, increment=100, width=6,
                    textvariable=self.cmt_var).pack(side="left", padx=4)

        stats = ttk.LabelFrame(self, text="Video stats")
        stats.pack(fill="x", **pad)
        self.stats_lbl = ttk.Label(stats, text="—", justify="left")
        self.stats_lbl.pack(anchor="w", padx=8, pady=6)

        self.canvas = tk.Canvas(self, height=110, bg="#1e1e1e",
                                highlightthickness=0)
        self.canvas.pack(fill="x", **pad)
        self.canvas.bind("<Configure>", lambda e: self.draw_graph())

        res = ttk.LabelFrame(self, text="Likely best moments "
                             "(double-click to open on YouTube)")
        res.pack(fill="both", expand=True, **pad)
        cols = ("rank", "range", "length", "score")
        self.tree = ttk.Treeview(res, columns=cols, show="headings", height=8)
        for c, w in zip(cols, (50, 220, 90, 90)):
            self.tree.heading(c, text=c.capitalize())
            self.tree.column(c, width=w, anchor="center")
        self.tree.pack(fill="both", expand=True, padx=8, pady=6)
        self.tree.bind("<Double-1>", self.open_selected)

        bottom = ttk.Frame(self)
        bottom.pack(fill="x", **pad)
        ttk.Button(bottom, text="Export CSV",
                   command=self.export_csv).pack(side="left")
        ttk.Button(bottom, text="Copy list",
                   command=self.copy_list).pack(side="left", padx=6)
        ttk.Button(bottom, text="Error log",
                   command=self.open_log).pack(side="left")
        self.status = tk.StringVar(value="Paste a URL and press Analyze.")
        ttk.Label(bottom, textvariable=self.status,
                  foreground="#666").pack(side="left", padx=12)

    # --- actions ---
    def log(self, msg):
        logging.info(msg)
        self.after(0, lambda: self.status.set(msg))

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

    def start(self):
        url = self.url_var.get().strip()
        if not url:
            messagebox.showinfo("URL needed", "Please paste a YouTube URL.")
            return
        try:  # read Tk vars here, on the main thread
            opts = (self.cmt_var.get(), self.clip_var.get(), self.top_var.get())
        except tk.TclError:
            messagebox.showerror("Bad option", "Options must be whole numbers.")
            return
        self.btn.config(state="disabled")
        self.tree.delete(*self.tree.get_children())
        threading.Thread(target=self.work, args=(url, *opts),
                         daemon=True).start()

    def work(self, url, max_comments, clip_len, top_n):
        try:
            info, segments = fetch_video(url, max_comments, self.log)
            self.log("Scoring moments...")
            result = analyze(info, segments, clip_len=clip_len, top_n=top_n)
            self.after(0, lambda: self.show(info, result))
        except Exception as e:
            logging.exception("Analysis failed for %s", url)
            self.after(0, lambda err=e: self.fail(err))

    def fail(self, e):
        self.btn.config(state="normal")
        self.status.set("Failed. See Error log.")
        messagebox.showerror("Error", f"{e}\n\nDetails: Error log button.")

    def show(self, info, result):
        self.info, self.result = info, result
        self.btn.config(state="normal")
        n_comments = len(info.get("comments") or [])
        names = {"heat": "Most-replayed heatmap",
                 "comm": f"Comment timestamps ({result['ts_hits']} found)",
                 "trans": "Transcript"}
        used = ", ".join(names[s] for s in result["signals"]) or "none"
        lines = [
            f"Title:    {info.get('title', 'n/a')}",
            f"Channel:  {info.get('channel') or info.get('uploader', 'n/a')}"
            f"   (subscribers: {fmt_num(info.get('channel_follower_count'))})",
            f"Views:    {fmt_num(info.get('view_count'))}    "
            f"Likes: {fmt_num(info.get('like_count'))}    "
            f"Comments loaded: {n_comments} / {fmt_num(info.get('comment_count'))}",
            f"Duration: {fmt_time(info.get('duration') or 0)}    "
            f"Uploaded: {info.get('upload_date', 'n/a')}",
            f"Signals used: {used}",
        ]
        if "heat" not in result["signals"]:
            lines.append("Note: no 'Most replayed' data for this video, so "
                         "results are estimates.")
        self.stats_lbl.config(text="\n".join(lines))
        for i, r in enumerate(result["ranges"], 1):
            self.tree.insert("", "end", iid=str(i - 1), values=(
                i, f"{fmt_time(r['start'])} – {fmt_time(r['end'])}",
                f"{int(r['end'] - r['start'])}s", f"{r['score']:.2f}"))
        self.draw_graph()
        self.status.set("Done.")

    def draw_graph(self):
        c = self.canvas
        c.delete("all")
        if not self.result or not self.result["score"]:
            return
        w, h = c.winfo_width(), c.winfo_height()
        sc = normalize(self.result["score"])
        n = len(sc)
        pts = []
        for i, v in enumerate(sc):
            pts += [10 + (w - 20) * i / max(1, n - 1), h - 10 - (h - 24) * v]
        if len(pts) >= 4:
            c.create_line(*pts, fill="#4fc3f7", width=2)
        dur = self.result["duration"]
        for r in self.result["ranges"]:
            x1 = 10 + (w - 20) * r["start"] / dur
            x2 = 10 + (w - 20) * r["end"] / dur
            c.create_rectangle(x1, 4, max(x2, x1 + 2), h - 4,
                               outline="", fill="#ffb300", stipple="gray25")

    def selected_range(self):
        sel = self.tree.selection()
        if not sel or not self.result:
            return None
        return self.result["ranges"][int(sel[0])]

    def open_selected(self, _event=None):
        r = self.selected_range()
        if r and self.info:
            base = self.info.get("webpage_url") or self.url_var.get()
            sep = "&" if "?" in base else "?"
            webbrowser.open(f"{base}{sep}t={int(r['start'])}s")

    def _rows(self):
        if not self.result:
            return []
        return [(i, fmt_time(r["start"]), fmt_time(r["end"]),
                 round(r["score"], 3))
                for i, r in enumerate(self.result["ranges"], 1)]

    def export_csv(self):
        rows = self._rows()
        if not rows:
            return
        path = filedialog.asksaveasfilename(defaultextension=".csv",
                                            filetypes=[("CSV", "*.csv")])
        if path:
            with open(path, "w", newline="", encoding="utf-8") as f:
                wr = csv.writer(f)
                wr.writerow(["rank", "start", "end", "score"])
                wr.writerows(rows)

    def copy_list(self):
        rows = self._rows()
        if rows:
            self.clipboard_clear()
            self.clipboard_append("\n".join(
                f"{a}. {b} - {c} (score {d})" for a, b, c, d in rows))
            self.status.set("Copied to clipboard.")


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.handlers.RotatingFileHandler(
            LOG_PATH, maxBytes=1_000_000, backupCount=1, encoding="utf-8")])
    sys.excepthook = lambda *exc: logging.critical("Crash", exc_info=exc)
    if sys.stderr is None:  # pythonw: no console, stray writes would crash
        sys.stdout = sys.stderr = open(os.devnull, "w")
    logging.info("App started")
    App().mainloop()

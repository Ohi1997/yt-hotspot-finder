import os
import re
import shutil
import subprocess
import threading
import time

from .fetch import Cancelled, YdlLogger
from .util import fmt_time


def safe_name(text, limit=60):
    """A file name fragment Windows will accept."""
    text = re.sub(r'[<>:"/\\|?*\n\r]+', " ", text or "")
    return re.sub(r"\s+", " ", text).strip(" .")[:limit] or "clip"


def clip_filename(info, rng):
    """An mp4 name for one range. Colons are illegal in Windows file names."""
    a = fmt_time(rng["start"]).replace(":", ".")
    b = fmt_time(rng["end"]).replace(":", ".")
    return f"{safe_name(info.get('title'))} {a}-{b}.mp4"


def parse_join_order(text, ranks):
    """'9, 5, 3, 1' -> [9, 5, 3, 1]. Must use each of ranks once."""
    nums = [int(n) for n in re.findall(r"\d+", text or "")]
    if sorted(nums) != sorted(ranks):
        show = ", ".join(str(n) for n in ranks)
        raise ValueError(f"Use each selected rank once: {show}")
    return nums


def clip_paths(folder, names):
    """Paths in folder for names, skipping names that already exist."""
    used, out = set(), []
    for name in names:
        stem, ext = os.path.splitext(name)
        candidate, n = name, 2
        while (candidate.lower() in used
               or os.path.exists(os.path.join(folder, candidate))):
            candidate = f"{stem} {n}{ext}"
            n += 1
        used.add(candidate.lower())
        out.append(os.path.join(folder, candidate))
    return out


def ffmpeg_bin():
    exe = shutil.which("ffmpeg")
    if not exe:
        raise RuntimeError(
            "ffmpeg is required to save a clip.\n\n"
            "Install it, then reopen the app:\n"
            "winget install Gyan.FFmpeg")
    return exe


def download_clip(url, start, end, path, log=lambda m: None, cancel=None,
                  progress=lambda done, total, text: None,
                  label="Downloading clip"):
    """Save url's [start, end] seconds to path. Needs ffmpeg on PATH.
    The cut is re-encoded so it starts at that second, not the nearest
    keyframe. progress(done_bytes, total_bytes, status_text)."""
    ffmpeg = ffmpeg_bin()
    try:
        import yt_dlp
        from yt_dlp.utils import download_range_func
    except ImportError:
        raise RuntimeError("Missing dependency. Run:  pip install yt-dlp")
    cancel = cancel or threading.Event()
    last = 0

    def hook(d):
        nonlocal last
        if cancel.is_set():
            raise Cancelled()
        now = time.time()
        if d.get("status") != "downloading" or now - last < 0.4:
            return
        last = now
        total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
        done = d.get("downloaded_bytes") or 0
        if total:
            progress(done, total, f"{label}: {100 * done / total:.0f}%")

    # yt-dlp treats "\" as an escape and "%" as a template.
    stem = os.path.splitext(path)[0].replace("%", "%%").replace("\\", "/")
    log(f"{label} ({fmt_time(start)} – {fmt_time(end)}) ...")
    try:
        with yt_dlp.YoutubeDL({
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "format": "bv*[ext=mp4]+ba[ext=m4a]/b[ext=mp4]/bv*+ba/b",
            "merge_output_format": "mp4",
            "download_ranges": download_range_func(None, [(start, end)]),
            "force_keyframes_at_cuts": True,
            "ffmpeg_location": os.path.dirname(ffmpeg),
            "outtmpl": stem + ".%(ext)s",
            "overwrites": True,
            "noprogress": True,
            "logger": YdlLogger(cancel, lambda n, est: None),
            "progress_hooks": [hook],
        }) as ydl:
            ydl.download([url])
    except Cancelled:
        raise
    except Exception:
        if cancel.is_set():
            raise Cancelled()
        raise
    if cancel.is_set():
        raise Cancelled()
    saved = os.path.splitext(path)[0] + ".mp4"
    if not os.path.exists(saved):
        raise RuntimeError("The download finished, but the clip file is missing.")
    return saved


def concat_clips(paths, dest):
    """Join mp4s, in the given order, into dest without re-encoding."""
    ffmpeg = ffmpeg_bin()
    lst = dest + ".txt"
    try:
        with open(lst, "w", encoding="utf-8") as f:
            for p in paths:
                name = os.path.abspath(p).replace("\\", "/").replace("'", r"'\''")
                f.write(f"file '{name}'\n")
        proc = subprocess.run(
            [ffmpeg, "-y", "-f", "concat", "-safe", "0", "-i", lst,
             "-c", "copy", dest],
            capture_output=True, text=True, encoding="utf-8", errors="replace")
        if proc.returncode != 0 or not os.path.exists(dest):
            tail = (proc.stderr or "").strip().splitlines()
            raise RuntimeError("Could not join the clips."
                               + (f"\n{tail[-1]}" if tail else ""))
    finally:
        if os.path.exists(lst):
            os.remove(lst)
    return dest

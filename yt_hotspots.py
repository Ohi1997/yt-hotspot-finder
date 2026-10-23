"""
YouTube Hotspot Finder  (Tkinter GUI)

Paste a YouTube URL -> get video stats and a ranked list of time ranges that
are most likely to be the "best" moments, using:
  1. YouTube's "Most replayed" heatmap (real viewer data, when available)
  2. Timestamps mentioned in comments (weighted by comment likes)
  3. Transcript energy (speech density, questions, emphasis, hook words)
  4. Chapters and timestamps in the video description

Install:   pip install yt-dlp youtube-transcript-api
           ffmpeg on PATH, to save a range as an mp4 (Export clip)
Run:       python yt_hotspots.py
"""

import argparse
import csv
import hashlib
import json
import logging
import logging.handlers
import math
import os
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import tkinter as tk
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from collections import Counter
from tkinter import filedialog, messagebox, simpledialog, ttk
from tkinter import font as tkfont
from xml.sax.saxutils import escape

# --------------------------------------------------------------------------
# Core logic (no GUI) -------------------------------------------------------
# --------------------------------------------------------------------------

APP_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_PATH = os.path.join(APP_DIR, "yt_hotspots.log")
SETTINGS_PATH = os.path.join(APP_DIR, "yt_hotspots_settings.json")

DEFAULT_WEIGHTS = {"heat": 45, "comm": 35, "trans": 20, "chap": 15}
SIGNAL_NAMES = {"heat": "replays", "comm": "comments", "trans": "speech",
                "chap": "chapters"}
COMMENT_PROG_RE = re.compile(r"comment.*\((\d+)/~(\d+)\)")
SPONSOR_CATEGORIES = ("sponsor", "selfpromo", "interaction", "intro", "outro")

TS_RE = re.compile(r"(?<![\d:])(?:(\d{1,2}):)?(\d{1,2}):(\d{2})(?![\d:])")
HOOK_WORDS = {
    "secret", "never", "always", "mistake", "truth", "actually", "crazy",
    "insane", "shocking", "biggest", "worst", "best", "unbelievable",
    "important", "warning", "stop", "wrong", "why", "how", "nobody",
    "everyone", "free", "million", "billion", "story", "lesson", "tip",
}
STOPWORDS = set("""
    a an and are as at be but by for from has have he her his i if in into is
    it its just like me my no not of on or our out so that the their them then
    there these they this to up us was we what when which who will with you
    your i'm it's that's don't can't you're we're they're there's what's do
    does did been being would could should about all also any because can get
    got go going know really right say said see some than think very well
    yeah yes oh okay ok um uh one two here now how why where more much
""".split())
# Sound tags in captions, e.g. [Laughter], (applause), [audience laughs].
SOUND_RE = re.compile(r"[\[(][^\])]*?\b(laugh\w*|applause|cheer\w*|clapp\w*|"
                      r"gasps?)\b[^\])]*[\])]", re.I)
QUOTE_WORDS = 4   # a comment must quote this many words in a row


def fmt_time(sec):
    sec = int(max(0, sec))
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def fmt_num(n):
    return f"{n:,}" if isinstance(n, (int, float)) else "n/a"


def per_view(count, views):
    """' (4.20%)' when both numbers exist, else ''."""
    if isinstance(count, (int, float)) and isinstance(views, (int, float)) \
            and views > 0:
        return f" ({count / views:.2%})"
    return ""


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


def words_of(text):
    return re.findall(r"[a-z0-9']+", SOUND_RE.sub(" ", text or "").lower())


def quote_index(segments, n=QUOTE_WORDS):
    """{n-word phrase: [times]} for distinctive phrases of the transcript:
    at least 2 non-stopwords, said at most twice."""
    words, times = [], []
    for seg in segments or []:
        ws = words_of(seg["text"])
        for k, w in enumerate(ws):
            words.append(w)
            times.append(seg["start"] + seg.get("duration", 0) * k / len(ws))
    index = {}
    for i in range(len(words) - n + 1):
        gram = tuple(words[i:i + n])
        if sum(w not in STOPWORDS for w in gram) >= 2:
            index.setdefault(gram, []).append(times[i])
    return {g: t for g, t in index.items() if len(t) <= 2}


def comment_signal(comments, duration, bin_s, segments=None):
    """Spread weighted mentions over nearby bins: timestamps like 3:45,
    and, for comments without one, phrases quoted from the transcript.

    Returns (signal or None, timestamp hits, quote hits)."""
    n = math.ceil(duration / bin_s)
    vals = [0.0] * n
    hits = quote_hits = 0
    quotes = quote_index(segments) if segments else {}
    for c in comments or []:
        text = c.get("text", "")
        weight = 1.0 + math.log1p(max(0, c.get("like_count") or 0))
        stamps = parse_timestamps(text, duration)
        for t in stamps:
            vals[min(n - 1, int(t // bin_s))] += weight
            hits += 1
        if stamps or not quotes:
            continue
        ws, found = words_of(text), []
        for i in range(len(ws) - QUOTE_WORDS + 1):
            for t in quotes.get(tuple(ws[i:i + QUOTE_WORDS]), ()):
                if all(abs(t - f) > 15 for f in found):
                    found.append(t)
        for t in found[:3]:  # quoting is less certain than a timestamp
            vals[min(n - 1, int(t // bin_s))] += 0.6 * weight
            quote_hits += 1
    if hits + quote_hits == 0:
        return None, 0, 0
    return smooth(vals, 2), hits, quote_hits


def topic_shifts(bin_words, win=3, min_words=5):
    """How much the vocabulary changes at each bin (1 - cosine similarity
    of the windows before and after), marking the start of a new topic and
    the bin after. None if the transcript is too short or sparse."""
    n = len(bin_words)
    shift = [0.0] * n
    for i in range(win, n - win + 1):
        left = sum(bin_words[i - win:i], Counter())
        right = sum(bin_words[i:i + win], Counter())
        if sum(left.values()) < min_words or sum(right.values()) < min_words:
            continue
        dot = sum(v * right[k] for k, v in left.items())
        norm = math.sqrt(sum(v * v for v in left.values())
                         * sum(v * v for v in right.values()))
        for k in range(i, min(n, i + 2)):
            shift[k] = max(shift[k], 1 - dot / norm)
    return normalize(shift) if any(shift) else None


def transcript_signal(segments, duration, bin_s):
    """Heuristic 'energy' from the transcript: speech density, questions,
    exclamations, hook words, CAPS and doubled words, laughter and
    applause tags, phrases repeated across the video, and topic shifts."""
    n = math.ceil(duration / bin_s)
    if not segments:
        return None
    vals = [0.0] * n
    bin_words = [Counter() for _ in range(n)]
    per_seg = []
    for seg in segments:
        text = seg["text"]
        sounds = len(SOUND_RE.findall(text))
        clean = SOUND_RE.sub(" ", text)
        raw = re.findall(r"[A-Za-z']+", clean)
        words = [w.lower() for w in raw]
        idx = min(n - 1, int(seg["start"] // bin_s))
        per_seg.append((idx, words))
        if not words and not sounds:
            continue
        score = len(words)
        score += 3 * clean.count("?") + 3 * clean.count("!")
        score += 2 * sum(1 for w in words if w in HOOK_WORDS)
        score += 2 * sum(1 for w in raw if len(w) >= 3 and w.isupper())
        score += 2 * sum(1 for a, b in zip(words, words[1:])
                         if a == b and len(a) > 1)
        score += 15 * sounds
        vals[idx] += score
        bin_words[idx].update(w for w in words
                              if len(w) >= 4 and w not in STOPWORDS)
    # Phrases said 3+ times are the video's key points or its hook.
    grams = Counter()
    for _, words in per_seg:
        grams.update(g for g in zip(words, words[1:], words[2:])
                     if sum(w not in STOPWORDS for w in g) >= 2)
    for idx, words in per_seg:
        vals[idx] += 3 * min(3, sum(1 for g in zip(words, words[1:],
                                                   words[2:])
                                    if grams[g] >= 3))
    energy = smooth(vals, 1)
    shift = topic_shifts(bin_words)
    if shift is None:
        return energy
    return [0.75 * e + 0.25 * s for e, s in zip(normalize(energy), shift)]


def chapter_signal(info, duration, bin_s, spread_s=30):
    """Chapter starts and description timestamps, spread over the next
    spread_s seconds. 0:00 is skipped: every chapter list starts there."""
    n = math.ceil(duration / bin_s)
    stamps = {c.get("start_time") or 0 for c in info.get("chapters") or []}
    stamps |= set(parse_timestamps(info.get("description"), duration))
    stamps = {t for t in stamps if 0 < t < duration}
    if not stamps:
        return None
    vals = [0.0] * n
    for t in stamps:
        idx = int(t // bin_s)
        for k in range(idx, min(n, idx + max(1, round(spread_s / bin_s)))):
            vals[k] += 1
    return vals


def combine_signals(signals, weights=None):
    """Weighted blend of the signals that exist; weights are relative and
    re-normalised over them. A signal with weight 0 is left out.

    Returns (score, used keys, components) where components[k] is signal k's
    share of the score, bin by bin (the components sum to the score)."""
    weights = {**DEFAULT_WEIGHTS, **(weights or {})}
    present = {k: v for k, v in signals.items() if v is not None}
    if present and not any(weights[k] > 0 for k in present):
        weights = DEFAULT_WEIGHTS
    present = {k: normalize(v) for k, v in present.items() if weights[k] > 0}
    if not present:
        return [], [], {}
    total_w = sum(weights[k] for k in present)
    comps = {k: smooth([weights[k] / total_w * x for x in arr], 1)
             for k, arr in present.items()}
    score = [sum(col) for col in zip(*comps.values())]
    return score, list(present), comps


def excluded_spans(duration, skip_s=0, sponsors=()):
    """(start, end, reason) spans that no range may touch: the first and
    last skip_s seconds, plus SponsorBlock segments."""
    spans = []
    if skip_s > 0:
        spans += [(0, skip_s, "start"),
                  (max(0, duration - skip_s), duration, "end")]
    return spans + [(s, e, c) for s, e, c in sponsors or () if e > s]


def blocked_bins(spans, n, bin_s):
    out = [False] * n
    for s, e, _ in spans:
        for i in range(max(0, int(s // bin_s)), min(n, math.ceil(e / bin_s))):
            out[i] = True
    return out


def top_ranges(score, bin_s, duration, clip_len, top_n, blocked=None):
    """Pick the best non-overlapping windows of ~clip_len seconds."""
    if not score:
        return []
    win = max(1, round(clip_len / bin_s))
    n = len(score)
    if win > n:
        win = n
    cands = []
    for i in range(0, n - win + 1):
        if blocked and any(blocked[i:i + win]):
            continue
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


def peak_ranges(score, bin_s, duration, top_n, min_len, max_len, drop=0.6,
                blocked=None):
    """Grow a range around each peak while the score stays above `drop` of
    the peak height, between min_len and max_len seconds. No overlaps."""
    n = len(score)
    if not n:
        return []
    lo = min(score)
    min_w = max(1, round(min_len / bin_s))
    max_w = max(min_w, round(max_len / bin_s))
    used = list(blocked) if blocked else [False] * n
    picked = []
    for peak in sorted(range(n), key=lambda i: -score[i]):
        if used[peak]:
            continue
        cut = lo + drop * (score[peak] - lo)
        a = b = peak
        while b - a + 1 < max_w:
            nbrs = [i for i in (a - 1, b + 1) if 0 <= i < n and not used[i]]
            if not nbrs:
                break
            i = max(nbrs, key=lambda j: score[j])
            if score[i] < cut and b - a + 1 >= min_w:
                break
            a, b = min(a, i), max(b, i)
        if b - a + 1 < min_w:
            continue
        for i in range(a, b + 1):
            used[i] = True
        picked.append({"start": a * bin_s,
                       "end": min(duration, (b + 1) * bin_s),
                       "score": sum(score[a:b + 1]) / (b - a + 1)})
        if len(picked) >= top_n:
            break
    return picked


def range_bins(rng, n, bin_s):
    """(first, last + 1) bin index covered by the range."""
    a = min(n - 1, int(rng["start"] // bin_s))
    return a, max(a + 1, min(n, math.ceil(rng["end"] / bin_s)))


def breakdown(rng, comps, bin_s):
    """Each signal's share (0..1) of the score inside the range."""
    if not comps:
        return {}
    a, b = range_bins(rng, len(next(iter(comps.values()))), bin_s)
    sums = {k: sum(v[a:b]) for k, v in comps.items()}
    total = sum(sums.values())
    return {k: s / total for k, s in sums.items()} if total > 0 else {}


def agreeing(rng, norms, bin_s, pct=0.75):
    """Signals for which the range is in their top quarter: its mean is
    above `pct` of that signal's bins."""
    out = []
    for k, arr in norms.items():
        a, b = range_bins(rng, len(arr), bin_s)
        mean = sum(arr[a:b]) / (b - a)
        if sum(v < mean for v in arr) >= pct * len(arr):
            out.append(k)
    return out


def confidence(agree):
    """High, Medium or Low, from the signals that agree. The heatmap is
    real viewer data, so it counts for more."""
    heat = "heat" in agree
    if len(agree) >= 3 or (heat and len(agree) >= 2):
        return "High"
    if len(agree) >= 2 or heat:
        return "Medium"
    return "Low"


def fmt_breakdown(shares):
    return " · ".join(f"{SIGNAL_NAMES[k]} {v:.0%}"
                      for k, v in sorted(shares.items(), key=lambda kv: -kv[1])
                      if v >= 0.005)


def quote(rng, segments):
    """Transcript text spoken inside the range."""
    parts = [s["text"] for s in segments or []
             if s["start"] < rng["end"]
             and s["start"] + s.get("duration", 0) > rng["start"]]
    return " ".join(" ".join(parts).split())


def suggest_title(text, max_words=8):
    """Pick the catchiest short sentence of the text as a clip title.
    Auto-captions have no punctuation, so there the text is cut into
    chunks instead."""
    text = re.sub(r"\[[^\]]*\]|♪", " ", text or "")  # [Music], lyric marks
    if re.search(r"[.!?]", text):
        cands = [s.split() for s in re.split(r"(?<=[.!?])\s+", text)]
        # A range rarely starts on a sentence boundary: its first sentence
        # is usually the tail of one, so it only counts if nothing else does.
        if not cands[0][:1] or not cands[0][0][0].isupper():
            cands = cands[1:] + [cands[0]] * (len(cands) == 1)
    else:
        words = text.split()
        cands = [words[i:i + max_words]
                 for i in range(0, len(words), max_words)]
    best, best_sc = None, None
    for words in cands:
        plain = re.findall(r"[a-z']+", " ".join(words).lower())
        if len(plain) < 3:
            continue
        sc = (2 * sum(w in HOOK_WORDS for w in plain)
              + 2 * words[-1].endswith("?") + words[-1].endswith("!")
              - max(0, len(words) - max_words) / 3
              - max(0, 4 - len(words)))
        if best_sc is None or sc > best_sc:
            best, best_sc = words, sc
    if not best:
        return ""
    title = " ".join(best[:max_words]).rstrip(",;:-")
    if len(best) > max_words:
        title += "…"
    return title[0].upper() + title[1:]


def chapter_at(info, t):
    for c in info.get("chapters") or []:
        if (c.get("start_time") or 0) <= t < (c.get("end_time") or math.inf):
            return c.get("title") or ""
    return ""


def chapters_text(ranges, duration, min_len=10):
    """A YouTube chapter list for the description: it must start at 0:00,
    go up in time, and every chapter must be at least 10 s long."""
    lines, last = [(0, "Intro")], 0
    for r in sorted(ranges, key=lambda r: r["start"]):
        t = int(r["start"])
        if t - last >= min_len and duration - t >= min_len:
            lines.append((t, r["title"]))
            last = t
    return "\n".join(f"{fmt_time(t)} {title}" for t, title in lines)


def timecode(sec, fps, hours=0):
    frames = round(sec * fps)
    s, f = divmod(frames, fps)
    return f"{hours + s // 3600:02d}:{s // 60 % 60:02d}:{s % 60:02d}:{f:02d}"


def markers_edl(ranges, fps=30, name="Hotspots"):
    """DaVinci Resolve marker EDL (Timeline > Import > Timeline Markers from
    EDL). Timecodes assume the timeline starts at 01:00:00:00, Resolve's
    default."""
    out = [f"TITLE: {name}", "FCM: NON-DROP FRAME", ""]
    for i, r in enumerate(sorted(ranges, key=lambda r: r["start"]), 1):
        a = timecode(r["start"], fps, 1)
        b = timecode(r["start"] + 1 / fps, fps, 1)
        title = r["title"].replace("|", "/")
        dur = max(1, round((r["end"] - r["start"]) * fps))
        out += [f"{i:03d}  001      V     C        {a} {b} {a} {b}",
                f" |C:ResolveColorYellow |M:{title} |D:{dur}", ""]
    return "\n".join(out)


def markers_xmeml(ranges, duration, fps=30, name="Hotspots"):
    """Final Cut Pro 7 XML: an empty sequence with one marker per range.
    Premiere Pro imports it with File > Import."""
    marks = "".join(
        f"<marker><name>{escape(r['title'])}</name>"
        f"<comment>#{i}, score {r['score']:.2f}</comment>"
        f"<in>{round(r['start'] * fps)}</in><out>{round(r['end'] * fps)}</out>"
        "</marker>"
        for i, r in enumerate(ranges, 1))
    return ('<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE xmeml>\n'
            f'<xmeml version="4"><sequence><name>{escape(name)}</name>'
            f"<duration>{round(duration * fps)}</duration>"
            f"<rate><timebase>{fps}</timebase><ntsc>FALSE</ntsc></rate>"
            "<media><video><track/></video><audio><track/></audio></media>"
            f"{marks}</sequence></xmeml>\n")


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


def moment_url(info, start):
    vid = info.get("id")
    if vid:
        return f"https://youtu.be/{vid}?t={int(start)}"
    base = info.get("webpage_url") or ""
    return f"{base}{'&' if '?' in base else '?'}t={int(start)}s"


def storyboard(info, max_width=200):
    """The storyboard format (sheets of preview frames) to use: the largest
    one not wider than max_width, else the smallest."""
    sbs = [f for f in info.get("formats") or []
           if f.get("format_note") == "storyboard" and f.get("fragments")
           and f.get("fps") and f.get("rows") and f.get("columns")]
    if not sbs:
        return None
    fits = [f for f in sbs if (f.get("width") or 0) <= max_width]
    return (max(fits, key=lambda f: f["width"]) if fits
            else min(sbs, key=lambda f: f.get("width") or 0))


def storyboard_tile(sb, t):
    """(sheet url, crop box) of the preview frame shown at t seconds."""
    per = sb["rows"] * sb["columns"]
    sheet, k = divmod(int(max(0, t) * sb["fps"]), per)
    if sheet >= len(sb["fragments"]):
        sheet, k = len(sb["fragments"]) - 1, per - 1
    row, col = divmod(k, sb["columns"])
    w, h = sb["width"], sb["height"]
    return (sb["fragments"][sheet]["url"],
            (col * w, row * h, (col + 1) * w, (row + 1) * h))


def fetch_thumbnails(sb, times, cache, timeout=10):
    """PIL images of the preview frames at the given times. Sheets are
    downloaded once and kept in cache (url -> image). Needs Pillow."""
    import io
    from PIL import Image
    out = []
    for t in times:
        url, box = storyboard_tile(sb, t)
        if url not in cache:
            req = urllib.request.Request(
                url, headers={"User-Agent": "yt-hotspot-finder"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                cache[url] = Image.open(io.BytesIO(resp.read())).convert("RGB")
        sheet = cache[url]
        # The last sheet is often only partly filled: use its last row.
        w, h = box[2] - box[0], box[3] - box[1]
        if box[3] > sheet.height:
            y1 = max(0, sheet.height // h - 1) * h
            box = (max(0, sheet.width - w), y1, sheet.width, y1 + h)
        out.append(sheet.crop(box))
    return out


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

class Cancelled(Exception):
    pass


class YdlLogger:
    """Receives yt-dlp's messages: reports comment progress, and stops the
    fetch by raising Cancelled from inside yt-dlp once cancel is set."""

    def __init__(self, cancel, progress):
        self.cancel, self.progress = cancel, progress

    def debug(self, msg):
        if self.cancel.is_set():
            raise Cancelled()
        m = COMMENT_PROG_RE.search(msg)
        if m:
            self.progress(int(m.group(1)), int(m.group(2)))

    info = debug

    def warning(self, msg):
        if self.cancel.is_set():
            raise Cancelled()
        logging.warning("yt-dlp: %s", msg)

    def error(self, msg):
        logging.error("yt-dlp: %s", msg)


def fetch_video(url, max_comments=800, log=lambda m: None, cancel=None,
                progress=lambda done, total: None):
    """progress(done, total) is called while comments load."""
    try:
        import yt_dlp
    except ImportError:
        raise RuntimeError("Missing dependency. Run:  pip install yt-dlp")
    cancel = cancel or threading.Event()

    log("Fetching video info, heatmap and comments...")
    opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,   # watch?v=...&list=... means just this video
        "getcomments": True,
        "logger": YdlLogger(
            cancel, lambda n, est: progress(n, min(max_comments, est) or 1)),
        "extractor_args": {
            "youtube": {"max_comments": [str(max_comments), "all", "all", "0"],
                        "comment_sort": ["top"]}
        },
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)

    if cancel.is_set():
        raise Cancelled()
    if info.get("is_live") or info.get("live_status") in (
            "is_live", "is_upcoming"):
        raise RuntimeError("This is a live stream or premiere. "
                           "Analyze it after the video ends.")
    segments = fetch_transcript(info.get("id"), log)
    if cancel.is_set():
        raise Cancelled()
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


def fetch_sponsor_segments(video_id, timeout=8):
    """SponsorBlock segments as [(start, end, category)]. Uses the hashed
    lookup, so only a 4-character hash prefix of the video ID is sent."""
    prefix = hashlib.sha256(video_id.encode()).hexdigest()[:4]
    query = urllib.parse.urlencode(
        {"categories": json.dumps(SPONSOR_CATEGORIES)})
    req = urllib.request.Request(
        f"https://sponsor.ajay.app/api/skipSegments/{prefix}?{query}",
        headers={"User-Agent": "yt-hotspot-finder"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            videos = json.load(resp)
    except urllib.error.HTTPError as e:
        if e.code == 404:  # no segments for any video with this prefix
            return []
        raise
    for v in videos:
        if v.get("videoID") == video_id:
            return [(s["segment"][0], s["segment"][1], s["category"])
                    for s in v.get("segments", [])]
    return []


def sponsor_segments_or_none(video_id):
    """SponsorBlock segments, or None if the lookup failed."""
    try:
        return fetch_sponsor_segments(video_id or "")
    except Exception:
        logging.exception("SponsorBlock lookup failed")
        return None


# --------------------------------------------------------------------------
# Cache, recent videos and batches -----------------------------------------
# --------------------------------------------------------------------------

CACHE_DIR = os.path.join(APP_DIR, "cache")
RECENT_PATH = os.path.join(CACHE_DIR, "recent.json")
MAX_RECENT = 20
INFO_KEYS = ("id", "title", "channel", "uploader", "channel_follower_count",
             "view_count", "like_count", "comment_count", "duration",
             "upload_date", "webpage_url", "heatmap", "chapters",
             "description", "fps")
VIDEO_ID_RE = re.compile(
    r"(?:[?&]v=|youtu\.be/|/shorts/|/live/|/embed/)([\w-]{11})(?![\w-])")
_cache_lock = threading.Lock()


def video_id(url):
    """The 11-character video ID in a YouTube URL (or a bare ID), else
    None, e.g. for playlists and channels."""
    url = (url or "").strip()
    m = VIDEO_ID_RE.search(url)
    if m:
        return m.group(1)
    return url if re.fullmatch(r"[\w-]{11}", url) else None


def url_problem(url):
    """Why this URL can't be analyzed, or None if it is one video.
    A watch link that also has a playlist id is still that one video."""
    if re.search(r"/shorts/", url, re.I):
        return "Shorts aren't supported. Paste a regular video URL."
    if re.search(r"/live/", url, re.I):
        return ("This is a live stream. Analyze it after it ends, "
                "using its normal watch URL.")
    if video_id(url):
        return None
    if re.search(r"list=|/playlist|/@|/channel/|/c/|/user/", url, re.I):
        return ("This is a playlist or channel. Use Batch to analyze "
                "its videos into one CSV.")
    return "Paste a link to one YouTube video."


def slim_info(info):
    """Only the parts of yt-dlp's info the app uses, small enough to cache."""
    out = {k: info.get(k) for k in INFO_KEYS}
    out["comments"] = [{"text": c.get("text") or "",
                        "like_count": c.get("like_count")}
                       for c in info.get("comments") or []]
    sb = storyboard(info)
    out["formats"] = [{
        **{k: sb.get(k) for k in ("format_note", "width", "height", "fps",
                                  "rows", "columns")},
        "fragments": [{"url": f["url"]} for f in sb["fragments"]]}] \
        if sb else []
    return out


def _cache_path(vid):
    return os.path.join(CACHE_DIR, f"{vid}.json")


def _read_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def _write_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(tmp, path)


def cache_load(vid, max_comments):
    """The cached entry for a video, with at most max_comments comments,
    or None if it is missing or was saved with fewer comments than wanted
    (unless the video has no more)."""
    with _cache_lock:
        entry = _read_json(_cache_path(vid), None)
    if not entry or "info" not in entry:
        return None
    comments = entry["info"].get("comments") or []
    if entry["max_comments"] < max_comments and \
            len(comments) >= entry["max_comments"]:
        return None
    entry["info"]["comments"] = comments[:max_comments]
    return entry


def cache_save(entry):
    with _cache_lock:
        _write_json(_cache_path(entry["info"]["id"]), entry)


def cache_set_sponsors(vid, sponsors):
    with _cache_lock:
        entry = _read_json(_cache_path(vid), None)
        if entry:
            entry["sponsors"] = sponsors
            _write_json(_cache_path(vid), entry)


def recent_videos():
    """[{id, title, url, when}], newest first."""
    with _cache_lock:
        return _read_json(RECENT_PATH, [])


def add_recent(info):
    item = {"id": info.get("id"), "title": info.get("title") or "",
            "url": info.get("webpage_url")
            or f"https://www.youtube.com/watch?v={info.get('id')}",
            "when": time.strftime("%Y-%m-%d %H:%M")}
    with _cache_lock:
        items = [r for r in _read_json(RECENT_PATH, [])
                 if r.get("id") != item["id"]]
        _write_json(RECENT_PATH, ([item] + items)[:MAX_RECENT])


def clear_cache():
    with _cache_lock:
        for name in os.listdir(CACHE_DIR) if os.path.isdir(CACHE_DIR) else []:
            if name.endswith(".json"):
                os.remove(os.path.join(CACHE_DIR, name))


def load_video(url, max_comments=800, sponsorblock=True, log=lambda m: None,
               cancel=None, progress=lambda done, total: None, refresh=False):
    """Video data from the cache, or fetched from YouTube and cached.

    Returns (entry, from_cache); entry has info, segments, sponsors
    (None = unknown), max_comments and fetched_at."""
    vid = video_id(url)
    entry = None if refresh or not vid else cache_load(vid, max_comments)
    from_cache = entry is not None
    changed = not from_cache
    if from_cache:
        log(f"Loaded from cache (fetched {entry['fetched_at']}).")
    else:
        info, segments = fetch_video(url, max_comments, log, cancel, progress)
        entry = {"info": slim_info(info), "segments": segments,
                 "sponsors": None, "max_comments": max_comments,
                 "fetched_at": time.strftime("%Y-%m-%d %H:%M")}
    if sponsorblock and entry["sponsors"] is None:
        log("Fetching SponsorBlock segments...")
        entry["sponsors"] = sponsor_segments_or_none(entry["info"]["id"])
        changed = changed or entry["sponsors"] is not None
    if cancel is not None and cancel.is_set():
        raise Cancelled()
    if changed:
        if from_cache:
            cache_set_sponsors(entry["info"]["id"], entry["sponsors"])
        else:
            cache_save(entry)
    add_recent(entry["info"])
    return entry, from_cache


def expand_urls(urls, limit=50, log=lambda m: None, cancel=None):
    """Video URLs from a list of video, playlist and channel URLs. Each
    playlist or channel adds at most `limit` videos. No duplicates."""
    cancel = cancel or threading.Event()
    out = []
    for url in urls:
        url = url.strip()
        if not url or url.startswith("#"):
            continue
        if video_id(url):
            out.append(f"https://www.youtube.com/watch?v={video_id(url)}")
            continue
        import yt_dlp
        # A channel's main page lists its tabs, not videos: then retry with
        # its Videos tab.
        attempts = [url]
        if re.search(r"youtube\.com/(@|channel/|c/|user/)", url) and \
                not url.rstrip("/").endswith("/videos"):
            attempts.append(url.rstrip("/") + "/videos")
        for attempt in attempts:
            log(f"Listing videos in {attempt} ...")
            opts = {"quiet": True, "no_warnings": True,
                    "extract_flat": "in_playlist", "playlistend": limit,
                    "logger": YdlLogger(cancel, lambda d, t: None)}
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(attempt, download=False)
            ids = [e.get("id") for e in info.get("entries") or []
                   if e and e.get("ie_key") == "Youtube"
                   and video_id(e.get("id"))][:limit]
            if ids:
                break
        if not ids:
            raise RuntimeError(f"No videos found in {url}")
        out += [f"https://www.youtube.com/watch?v={i}" for i in ids]
    return list(dict.fromkeys(out))


def analyze(info, segments, bin_s=10, clip_len=30, top_n=8, weights=None,
            fit_peaks=False, exclude=(), min_strength=0.0):
    """exclude: (start, end, reason) spans no range may overlap, see
    excluded_spans(). Ranges whose strength (how far their score is from
    the video's median towards its peak, 0..1) is below min_strength are
    left out."""
    duration = info.get("duration") or 0
    if duration <= 0:
        raise RuntimeError("This is a live stream or premiere, or it has no "
                           "duration yet. Analyze it after the video ends.")
    comm, hits, quote_hits = comment_signal(info.get("comments"), duration,
                                            bin_s, segments)
    signals = {"heat": heatmap_signal(info.get("heatmap"), duration, bin_s),
               "comm": comm,
               "trans": transcript_signal(segments, duration, bin_s),
               "chap": chapter_signal(info, duration, bin_s)}
    score, used, comps = combine_signals(signals, weights)
    exclude = list(exclude or ())
    blocked = blocked_bins(exclude, len(score), bin_s) if exclude else None
    if fit_peaks:
        ranges = peak_ranges(score, bin_s, duration, top_n,
                             max(bin_s, clip_len / 3), clip_len * 2,
                             blocked=blocked)
    else:
        ranges = top_ranges(score, bin_s, duration, clip_len, top_n,
                            blocked=blocked)
    # Snapping must never create overlaps, with other ranges or with
    # excluded spans: keep a snap only if it collides with nothing.
    for i, r in enumerate(ranges):
        s = snap_to_sentences(dict(r), segments, duration)
        others = [(o["start"], o["end"]) for j, o in enumerate(ranges)
                  if j != i] + [(a, b) for a, b, _ in exclude]
        if all(s["end"] <= a or s["start"] >= b for a, b in others):
            ranges[i] = s
    norms = {k: normalize(signals[k]) for k in used}
    med, peak = (statistics.median(score), max(score)) if score else (0, 0)
    for r in ranges:
        r["shares"] = breakdown(r, comps, bin_s)
        r["quote"] = quote(r, segments)
        r["title"] = (suggest_title(r["quote"]) or chapter_at(info, r["start"])
                      or f"Moment at {fmt_time(r['start'])}")
        r["agree"] = agreeing(r, norms, bin_s)
        r["confidence"] = confidence(r["agree"])
        r["strength"] = (r["score"] - med) / (peak - med) if peak > med else 0
    ranges.sort(key=lambda r: -r["score"])
    kept = [r for r in ranges if r["strength"] >= min_strength]
    return {"score": score, "bin": bin_s, "ranges": kept,
            "hidden": len(ranges) - len(kept), "signals": used,
            "ts_hits": hits, "quote_hits": quote_hits, "duration": duration,
            "excluded": exclude}


DEFAULT_OPTS = {"clip_len": 30, "top_n": 8, "weights": DEFAULT_WEIGHTS,
                "fit_peaks": False, "skip_s": 15, "sponsorblock": True,
                "min_strength": 0.25}


def score_video(entry, opts=None):
    """analyze() a load_video() entry with the app's options."""
    o = {**DEFAULT_OPTS, **(opts or {})}
    info = entry["info"]
    exclude = excluded_spans(info.get("duration") or 0, o["skip_s"],
                             entry["sponsors"] if o["sponsorblock"] else None)
    return analyze(info, entry["segments"], clip_len=o["clip_len"],
                   top_n=o["top_n"], weights=o["weights"],
                   fit_peaks=o["fit_peaks"], exclude=exclude,
                   min_strength=o["min_strength"])


CSV_HEADER = (["video_id", "video_title", "rank", "start", "end", "score",
               "strength", "confidence", "agreeing", "title"]
              + [SIGNAL_NAMES[k] for k in DEFAULT_WEIGHTS]
              + ["link", "transcript"])


def csv_rows(info, result):
    return [[info.get("id"), info.get("title"), i, fmt_time(r["start"]),
             fmt_time(r["end"]), round(r["score"], 3),
             round(r["strength"], 3), r["confidence"],
             " ".join(SIGNAL_NAMES[k] for k in r["agree"]), r["title"]]
            + [round(r["shares"].get(k, 0), 3) for k in DEFAULT_WEIGHTS]
            + [moment_url(info, r["start"]), r["quote"]]
            for i, r in enumerate(result["ranges"], 1)]


def write_csv(path, rows):
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        wr = csv.writer(f)
        wr.writerow(CSV_HEADER)
        wr.writerows(rows)


def run_batch(urls, opts=None, max_comments=800, limit=50, refresh=False,
              log=lambda m: None, cancel=None, on_video=lambda *a: None):
    """Score every video in urls (videos, playlists, channels), one after
    another. on_video(index, total, url, entry, result, error) is called
    after each; error is None on success. Returns (rows, failed urls).
    Stops early, keeping what is done, if cancel is set."""
    cancel = cancel or threading.Event()
    o = {**DEFAULT_OPTS, **(opts or {})}
    videos = expand_urls(urls, limit, log, cancel)
    rows, failed = [], []
    for i, url in enumerate(videos, 1):
        if cancel.is_set():
            break
        log(f"[{i}/{len(videos)}] {url}")
        try:
            entry, _ = load_video(url, max_comments, o["sponsorblock"],
                                  log, cancel, refresh=refresh)
            result = score_video(entry, o)
        except Cancelled:
            break
        except Exception as e:
            logging.exception("Batch: failed on %s", url)
            failed.append(url)
            on_video(i, len(videos), url, None, None, e)
            continue
        rows += csv_rows(entry["info"], result)
        on_video(i, len(videos), url, entry, result, None)
    return rows, failed


# --------------------------------------------------------------------------
# GUI -----------------------------------------------------------------------
# --------------------------------------------------------------------------

def load_settings():
    try:
        with open(SETTINGS_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_settings(data):
    try:
        with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except OSError:
        logging.exception("Could not save settings")


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


# --------------------------------------------------------------------------
# Command line ---------------------------------------------------------------
# --------------------------------------------------------------------------

def parse_weights(text):
    """'replays=45,comments=35' -> {'heat': 45.0, 'comm': 35.0}."""
    names = {v: k for k, v in SIGNAL_NAMES.items()}
    out = {}
    for part in filter(None, (p.strip() for p in text.split(","))):
        name, _, value = part.partition("=")
        key = names.get(name.strip().lower(), name.strip().lower())
        if key not in DEFAULT_WEIGHTS:
            raise argparse.ArgumentTypeError(
                f"unknown signal {name!r}; use "
                + ", ".join(SIGNAL_NAMES.values()))
        try:
            out[key] = float(value)
        except ValueError:
            raise argparse.ArgumentTypeError(f"bad weight in {part!r}")
    return out


def parse_args(argv=None):
    p = argparse.ArgumentParser(
        prog="yt_hotspots.py",
        description="Find the best moments of YouTube videos. Without URLs, "
                    "opens the app.")
    p.add_argument("urls", nargs="*",
                   help="video, playlist or channel URLs")
    p.add_argument("--clip", type=int, default=30,
                   help="clip length in seconds (default 30)")
    p.add_argument("--top", type=int, default=8,
                   help="ranges per video (default 8)")
    p.add_argument("--comments", type=int, default=800,
                   help="max comments to load (default 800)")
    p.add_argument("--fit", action="store_true",
                   help="fit each range's length to its peak")
    p.add_argument("--skip", type=int, default=15,
                   help="skip the first and last N seconds (default 15)")
    p.add_argument("--no-sponsorblock", action="store_true",
                   help="do not skip SponsorBlock segments")
    p.add_argument("--min-strength", type=int, default=25, metavar="PCT",
                   help="hide ranges weaker than this (default 25)")
    p.add_argument("--weights", type=parse_weights, default={},
                   help='relative signal weights, e.g. '
                        '"replays=45,comments=35,speech=20,chapters=15"')
    p.add_argument("--limit", type=int, default=50,
                   help="max videos per playlist or channel (default 50)")
    p.add_argument("--refresh", action="store_true",
                   help="reload from YouTube even if cached")
    p.add_argument("--csv", metavar="PATH", help="also save all ranges here")
    return p.parse_args(argv)


def run_cli(args):
    """Prints the ranges of every video; returns the exit code."""
    for stream in (sys.stdout, sys.stderr):  # "–", "·" etc. when piped
        if hasattr(stream, "reconfigure") and not stream.isatty():
            stream.reconfigure(encoding="utf-8")
    opts = {"clip_len": args.clip, "top_n": args.top,
            "weights": args.weights, "fit_peaks": args.fit,
            "skip_s": args.skip, "sponsorblock": not args.no_sponsorblock,
            "min_strength": args.min_strength / 100}

    def log(msg):
        logging.info(msg)
        print(msg, file=sys.stderr, flush=True)

    def on_video(i, n, url, entry, result, err):
        if err:
            print(f"\n[{i}/{n}] {url}\n  failed: {err}", flush=True)
            return
        info = entry["info"]
        print(f"\n[{i}/{n}] {info.get('title')}  "
              f"({fmt_time(info.get('duration') or 0)})")
        for k, r in enumerate(result["ranges"], 1):
            print(f"  {k:>2}. {fmt_time(r['start']):>7} – "
                  f"{fmt_time(r['end']):<7} {r['strength']:>4.0%}  "
                  f"{r['confidence']:<6}  {r['title']}")
            print(f"      {fmt_breakdown(r['shares'])}   "
                  f"{moment_url(info, r['start'])}")
        if result["hidden"]:
            print(f"  ({result['hidden']} weaker ranges hidden; "
                  "see --min-strength)")
        sys.stdout.flush()

    try:
        rows, failed = run_batch(args.urls, opts, args.comments, args.limit,
                                 args.refresh, log, on_video=on_video)
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130
    except Exception as e:
        logging.exception("Command line run failed")
        print(f"Error: {e}", file=sys.stderr)
        return 1
    if args.csv:
        write_csv(args.csv, rows)
        print(f"\nSaved {len(rows)} ranges to {args.csv}", file=sys.stderr)
    if failed:
        print(f"{len(failed)} failed; details in {LOG_PATH}", file=sys.stderr)
    return 1 if failed else 0


def main(argv=None):
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.handlers.RotatingFileHandler(
            LOG_PATH, maxBytes=1_000_000, backupCount=1, encoding="utf-8")])
    sys.excepthook = lambda *exc: logging.critical("Crash", exc_info=exc)
    if sys.stderr is None:  # pythonw: no console, stray writes would crash
        sys.stdout = sys.stderr = open(os.devnull, "w")
    args = parse_args(argv)
    if args.urls:
        return run_cli(args)
    logging.info("App started")
    App().mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())


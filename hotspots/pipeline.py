import logging
import statistics
import threading

from .cache import expand_urls, load_video
from .config import DEFAULT_WEIGHTS
from .export import csv_rows
from .fetch import Cancelled
from .ranges import agreeing, blocked_bins, breakdown, chapter_at, confidence, excluded_spans, peak_ranges, quote, snap_to_sentences, suggest_title, top_ranges
from .signals import chapter_signal, combine_signals, comment_signal, heatmap_signal, transcript_signal
from .util import fmt_time, normalize


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

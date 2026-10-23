import math
import re

from .config import HOOK_WORDS, SIGNAL_NAMES
from .util import fmt_time


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

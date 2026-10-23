from collections import Counter
import math
import re

from .config import DEFAULT_WEIGHTS, HOOK_WORDS, QUOTE_WORDS, SOUND_RE, STOPWORDS
from .util import normalize, parse_timestamps, smooth


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

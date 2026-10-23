from .config import TS_RE


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

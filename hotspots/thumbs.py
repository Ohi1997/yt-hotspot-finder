import urllib.error
import urllib.parse
import urllib.request


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

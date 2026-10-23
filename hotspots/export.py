from xml.sax.saxutils import escape
import csv

from .config import DEFAULT_WEIGHTS, SIGNAL_NAMES
from .thumbs import moment_url
from .util import fmt_time


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

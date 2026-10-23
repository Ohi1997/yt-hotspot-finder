"""Offline self-check. Run: python test_hotspots.py"""
import shutil
import subprocess
from collections import Counter
from hotspots import cache
from hotspots import cli
from hotspots import clips
from hotspots import config
from hotspots import export
from hotspots import fetch
from hotspots import pipeline
from hotspots import ranges
from hotspots import signals
from hotspots import thumbs
from hotspots import util

assert util.parse_timestamps("best part 3:45 and 1:02:10, not 9:99", 4000) == [225, 3730]

# Transcript segments that straddle range edges must not cause overlaps.
segs = [{"text": "why? wow! secret " * (i % 7), "start": i * 3.1, "duration": 3.3}
        for i in range(100)]
info = {"duration": 310,
        "heatmap": [{"start_time": t, "end_time": t + 10, "value": (t * 7) % 13}
                    for t in range(0, 310, 10)],
        "comments": [{"text": "2:05 lol", "like_count": 50}]}
r = pipeline.analyze(info, segs, top_n=8)
s = sorted(r["ranges"], key=lambda x: x["start"])
assert all(a["end"] <= b["start"] for a, b in zip(s, s[1:])), s
assert r["signals"] == ["heat", "comm", "trans"] and r["ts_hits"] == 1

# Breakdown: shares per range sum to 1; each range carries its transcript.
for rng in r["ranges"]:
    assert abs(sum(rng["shares"].values()) - 1) < 1e-9, rng["shares"]
    assert rng["quote"] == ranges.quote(rng, segs) and "secret" in rng["quote"]
assert ranges.quote({"start": 0, "end": 7}, segs).startswith("why? wow! secret")
assert ranges.fmt_breakdown({"heat": 0.7, "comm": 0.3}) == "replays 70% · comments 30%"

# Chapters and description timestamps; 0:00 never counts.
ch = {"duration": 600, "chapters": [{"start_time": 0}, {"start_time": 300}],
      "description": "0:00 intro\n7:30 the big reveal"}
r = pipeline.analyze(ch, None, top_n=2)
assert r["signals"] == ["chap"]
assert sorted(x["start"] for x in r["ranges"]) == [300, 450], r["ranges"]
assert signals.chapter_signal({"description": "0:00 intro"}, 600, 10) is None

# Weights: 0 drops a signal, the ranking follows what is left, and all-zero
# falls back to the defaults.
two = {"duration": 300,
       "heatmap": [{"start_time": t, "end_time": t + 10,
                    "value": 1.0 if t == 50 else 0.0} for t in range(0, 300, 10)],
       "comments": [{"text": "4:00 !!!", "like_count": 900}]}
heat_only = pipeline.analyze(two, None, top_n=1, weights={"comm": 0})
assert heat_only["signals"] == ["heat"]
assert heat_only["ranges"][0]["start"] <= 50 < heat_only["ranges"][0]["end"]
assert heat_only["ranges"][0]["shares"] == {"heat": 1.0}
comm_only = pipeline.analyze(two, None, top_n=1, weights={"heat": 0})
assert comm_only["ranges"][0]["start"] <= 240 < comm_only["ranges"][0]["end"]
assert pipeline.analyze(two, None, weights={"heat": 0, "comm": 0})["signals"] == \
    ["heat", "comm"]

# Fit to peaks: a wide plateau gets a long range, a spike a short one.
score = [0.0] * 60
score[5:17] = [1.0] * 12   # 120 s plateau
score[40] = 0.9            # single spike
rs = ranges.peak_ranges(score, 10, 600, 2, min_len=10, max_len=150)
assert [(x["start"], x["end"]) for x in rs] == [(50, 170), (400, 410)], rs
rs = ranges.peak_ranges(score, 10, 600, 2, min_len=30, max_len=60)
assert all(30 <= x["end"] - x["start"] <= 60 for x in rs), rs
fit = pipeline.analyze(info, segs, top_n=8, fit_peaks=True)
s = sorted(fit["ranges"], key=lambda x: x["start"])
assert all(a["end"] <= b["start"] for a, b in zip(s, s[1:])), s

# Cancel: yt-dlp's logger reports comment progress and raises once cancelled.
import threading
ev, seen = threading.Event(), []
lg = fetch.YdlLogger(ev, lambda d, t: seen.append((d, t)))
lg.debug("[youtube] abc: Downloading comment API JSON page 3 (60/~1234)")
lg.debug("[youtube] abc: Downloading webpage")
assert seen == [(60, 1234)], seen
ev.set()
try:
    lg.debug("[youtube] abc: Downloading comment API JSON page 4 (80/~1234)")
    raise AssertionError("not cancelled")
except fetch.Cancelled:
    pass

# Exclusions: no range touches the first/last 15 s or a sponsor segment,
# even when that is where the score peaks.
edge = {"duration": 300,
        "heatmap": [{"start_time": t, "end_time": t + 10,
                     "value": 1.0 if t in (0, 100, 290) else 0.0}
                    for t in range(0, 300, 10)]}
spans = ranges.excluded_spans(300, 15, [(95, 130, "sponsor")])
assert spans == [(0, 15, "start"), (285, 300, "end"), (95, 130, "sponsor")]
for fit in (False, True):
    rs = pipeline.analyze(edge, segs, top_n=8, fit_peaks=fit, exclude=spans)["ranges"]
    assert rs, fit
    assert all(r["end"] <= a or r["start"] >= b
               for r in rs for a, b, _ in spans), (fit, rs)
assert ranges.blocked_bins([(95, 130, "x")], 30, 10)[9:14] == \
    [True, True, True, True, False]

# Titles: the catchiest sentence, short; captions without punctuation get
# chunked; no transcript falls back to the chapter, then to the time.
assert ranges.suggest_title("So yeah. This is the biggest mistake nobody "
                       "talks about! Anyway, moving on to the next part now.") \
    == "This is the biggest mistake nobody talks about!"
t = ranges.suggest_title("um so " * 10 + "here is the secret nobody tells you "
                    + "and then " * 10)
assert "secret" in t and len(t.split()) <= 8, t
assert ranges.suggest_title("") == "" and ranges.suggest_title("ok") == ""
assert ranges.suggest_title("♪ never gonna give you up ♪ [Music]") == \
    "Never gonna give you up"
assert ranges.suggest_title("of the bias. That additional number is the bias. "
                       "Next.") == "That additional number is the bias."
ch_info = {"duration": 600, "chapters": [
    {"start_time": 0, "end_time": 300, "title": "Setup"},
    {"start_time": 300, "end_time": 600, "title": "The reveal"}]}
r = pipeline.analyze(ch_info, None, top_n=1)["ranges"][0]
assert r["title"] == "The reveal", r
assert pipeline.analyze({"duration": 300, "heatmap": two["heatmap"]}, None,
                 top_n=1)["ranges"][0]["title"].startswith("Moment at ")

# YouTube chapters: start at 0:00, ascending, each at least 10 s long.
rs = [{"start": 5, "title": "Too early"}, {"start": 400, "title": "B"},
      {"start": 120, "title": "A"}, {"start": 405, "title": "Too close"},
      {"start": 595, "title": "Too late"}]
assert ranges.chapters_text(rs, 600) == "0:00 Intro\n2:00 A\n6:40 B"

# Links and editor markers.
assert thumbs.moment_url({"id": "abc"}, 75.6) == "https://youtu.be/abc?t=75"
assert thumbs.moment_url({"webpage_url": "https://x/watch?v=1"}, 9) == \
    "https://x/watch?v=1&t=9s"
assert export.timecode(3661.5, 30, 1) == "02:01:01:15"
mk = [{"start": 65, "end": 95, "score": 0.9, "title": "A | B"},
      {"start": 10, "end": 20, "score": 0.5, "title": "Q & A <live>"}]
edl = export.markers_edl(mk, 30).splitlines()
assert edl[3] == "001  001      V     C        01:00:10:00 01:00:10:01 " \
                 "01:00:10:00 01:00:10:01"
assert edl[4] == " |C:ResolveColorYellow |M:Q & A <live> |D:300"
assert "|M:A / B |D:900" in edl[7]
import xml.etree.ElementTree as ET
seq = ET.fromstring(export.markers_xmeml(mk, 120, 30).split("\n", 2)[2]).find(
    "sequence")
assert [(m.findtext("name"), m.findtext("in"), m.findtext("out"))
        for m in seq.findall("marker")] == \
    [("A | B", "1950", "2850"), ("Q & A <live>", "300", "600")]
assert seq.findtext("duration") == "3600"

# Storyboards: pick the right size, then find the tile for a time.
sb_info = {"formats": [
    {"format_note": "storyboard", "width": w, "height": w * 9 // 16,
     "fps": 0.1, "rows": r, "columns": r,
     "fragments": [{"url": f"sb{w}/M{k}.jpg"} for k in range(n)]}
    for w, r, n in ((48, 10, 1), (160, 5, 4), (320, 3, 12))]
    + [{"format_note": "1080p", "width": 1920}]}
sb = thumbs.storyboard(sb_info)
assert sb["width"] == 160 and thumbs.storyboard({"formats": []}) is None
assert thumbs.storyboard(sb_info, max_width=10)["width"] == 48
# 0.1 fps -> one frame per 10 s; 25 frames per 5x5 sheet.
assert thumbs.storyboard_tile(sb, 0) == ("sb160/M0.jpg", (0, 0, 160, 90))
assert thumbs.storyboard_tile(sb, 75) == ("sb160/M0.jpg", (320, 90, 480, 180))
assert thumbs.storyboard_tile(sb, 255) == ("sb160/M1.jpg", (0, 0, 160, 90))
assert thumbs.storyboard_tile(sb, 265) == ("sb160/M1.jpg", (160, 0, 320, 90))
assert thumbs.storyboard_tile(sb, 99999) == ("sb160/M3.jpg", (640, 360, 800, 450))
try:
    from PIL import Image
except ImportError:
    print("Pillow not installed; skipping the thumbnail crop check.")
else:
    full = Image.new("RGB", (800, 450))
    full.paste((255, 0, 0), (320, 90, 480, 180))
    part = Image.new("RGB", (800, 180), (0, 0, 255))  # only 2 rows filled
    tiles = {"sb160/M0.jpg": full, "sb160/M3.jpg": part}
    a, b = thumbs.fetch_thumbnails(sb, [75, 99999], tiles)
    assert a.size == (160, 90) and a.getpixel((80, 45)) == (255, 0, 0)
    assert b.size == (160, 90) and b.getpixel((80, 45)) == (0, 0, 255)

# Video IDs from the usual URL shapes; playlists and channels have none.
VID = "aircAruvnKk"
for u in (VID, f"https://www.youtube.com/watch?v={VID}&t=5s",
          f"https://youtube.com/watch?feature=x&v={VID}", f"https://youtu.be/{VID}?si=x",
          f"https://www.youtube.com/shorts/{VID}", f"https://www.youtube.com/embed/{VID}",
          f"https://www.youtube.com/watch?v={VID}&list=PL123"):
    assert cache.video_id(u) == VID, u
for u in ("https://www.youtube.com/playlist?list=PL123",
          "https://www.youtube.com/@3blue1brown", "", "not a url"):
    assert cache.video_id(u) is None, u

# Cache and recent list, in a scratch folder.
import contextlib, io, os, shutil, tempfile, csv as csvmod
tmp = tempfile.mkdtemp()
cache.CACHE_DIR, cache.RECENT_PATH = tmp, os.path.join(tmp, "recent.json")
full = {"id": VID, "title": "Neural nets", "duration": 310, "junk": "x" * 999,
        "heatmap": info["heatmap"], "webpage_url": f"https://www.youtube.com/watch?v={VID}",
        "comments": [{"text": f"{i // 60}:{i % 60:02d} wow", "like_count": i,
                      "author": "someone"} for i in range(0, 300, 3)],
        "formats": sb_info["formats"]}
slim = cache.slim_info(full)
assert "junk" not in slim and set(slim["comments"][0]) == {"text", "like_count"}
assert [f["width"] for f in slim["formats"]] == [160]
cache.cache_save({"info": slim, "segments": segs, "sponsors": [(0, 10, "intro")],
              "max_comments": 100, "fetched_at": "2026-10-23 15:00"})
e = cache.cache_load(VID, 40)
assert len(e["info"]["comments"]) == 40 and e["segments"] == segs
assert cache.cache_load(VID, 800) is None   # saved with 100 of more comments
cache.cache_save({**e, "info": {**slim, "comments": slim["comments"][:7]}})
assert cache.cache_load(VID, 800) is not None  # the video only has 7
cache.cache_save({**e, "info": slim})
cache.cache_set_sponsors(VID, [(50, 60, "sponsor")])
entry, from_cache = cache.load_video(f"https://youtu.be/{VID}", 100, sponsorblock=True)
assert from_cache and entry["sponsors"] == [[50, 60, "sponsor"]]
for i in range(cache.MAX_RECENT + 3):
    cache.add_recent({"id": f"id{i}", "title": f"T{i}"})
cache.add_recent(slim)
rec = cache.recent_videos()
assert len(rec) == cache.MAX_RECENT and rec[0]["id"] == VID and rec[1]["id"] == "id22"
assert rec[0]["url"].endswith(VID)

# Scoring a cached entry; CSV rows carry the video.
res = pipeline.score_video(entry, {"top_n": 3, "skip_s": 0, "min_strength": 0})
assert all(not (r["start"] < 60 and r["end"] > 50) for r in res["ranges"])
rows = export.csv_rows(entry["info"], res)
assert len(rows) == 3 and rows[0][:3] == [VID, "Neural nets", 1]
assert len(rows[0]) == len(export.CSV_HEADER)

# Batch and command line, offline: the video is cached; a bad URL fails
# without stopping the rest.
assert cache.expand_urls([VID, f"https://youtu.be/{VID}", "# note", ""]) == \
    [f"https://www.youtube.com/watch?v={VID}"]
assert cli.parse_weights("replays=0, speech=50,chap=5") == \
    {"heat": 0.0, "trans": 50.0, "chap": 5.0}
args = cli.parse_args([VID, "--top", "2", "--comments", "100", "--min-strength", "0",
                     "--no-sponsorblock", "--weights",
                     "comments=0", "--csv", os.path.join(tmp, "out.csv")])
out = io.StringIO()
with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
    assert cli.run_cli(args) == 0
text = out.getvalue()
assert "[1/1] Neural nets  (5:10)" in text and text.count("https://youtu.be/") == 2, text
with open(os.path.join(tmp, "out.csv"), encoding="utf-8-sig") as f:
    got = list(csvmod.reader(f))
assert got[0] == export.CSV_HEADER and len(got) == 3
col = got[0].index("comments")
assert got[1][col] == "0"  # weight 0: signal off
seen = []
rows, failed = pipeline.run_batch([VID], {"top_n": 1, "min_strength": 0}, 100,
                           on_video=lambda *a: seen.append(a[5]))
assert len(rows) == 1 and failed == [] and seen == [None]
cache.clear_cache()
assert cache.recent_videos() == [] and os.listdir(tmp) == ["out.csv"]
shutil.rmtree(tmp)

# Transcript: laughter tags, CAPS and doubled words, and phrases repeated
# across the video all add energy; same word counts otherwise.
def seg(t, text):
    return {"start": t, "duration": 9, "text": text}
base = "we look at the plan for today and then move along"
sig = signals.transcript_signal([seg(0, base), seg(30, base + " [Laughter]"),
                           seg(60, base), seg(90, base.upper()),
                           seg(120, base + " no no"), seg(150, base)], 200, 10)
assert sig[3] > sig[0] and sig[3] > sig[6]           # laughter
assert sig[9] > sig[6] and sig[12] > sig[15]         # CAPS, "no no"
assert config.SOUND_RE.sub("", "so (audience laughs) yes [Music]") == "so  yes [Music]"
fillers = iter(["plain words said only once each", "other filler text appears here",
                "completely different sentence goes there"])
rep = [seg(t, "gradient descent works nicely" if t in (0, 60, 120)
           else next(fillers)) for t in range(0, 180, 30)]
sig = signals.transcript_signal(rep, 180, 10)
assert min(sig[0], sig[6], sig[12]) > max(sig[3], sig[9], sig[15]), sig
# Topic shift: vocabulary changes at 60 s.
fruit, space = "apple banana cherry orange melon grape", "rocket engine fuel orbit launch thrust"
bw = [Counter((fruit if i < 6 else space).split()) for i in range(12)]
sh = signals.topic_shifts(bw)
assert sh.index(max(sh)) == 6 and sh[2] == 0, sh
assert signals.topic_shifts([Counter()] * 12) is None

# Comments that quote a distinctive line count at that moment; timestamps
# win over quotes, and common phrases don't match.
qsegs = [seg(t, "and so the mitochondria is the powerhouse of the cell"
             if t == 200 else "so we talk about the thing over here now")
         for t in range(0, 300, 10)]
sig, ts, qh = signals.comment_signal(
    [{"text": "lol the powerhouse of the cell!!", "like_count": 40},
     {"text": "we talk about the thing over here", "like_count": 900},
     {"text": "2:30 powerhouse of the cell", "like_count": 0}], 300, 10, qsegs)
assert (ts, qh) == (1, 1) and sig[20] == max(sig) > sig[15], (ts, qh, sig)
assert signals.comment_signal([{"text": "powerhouse of the cell"}], 300, 10) == (None, 0, 0)
r = pipeline.analyze({"duration": 300, "comments": [{"text": "the powerhouse of the cell"}]},
              qsegs, top_n=1)
assert r["quote_hits"] == 1 and r["ranges"][0]["start"] <= 200 < r["ranges"][0]["end"]

# Confidence: how many signals put the range in their top quarter.
norms = {"heat": [0, 0, 0, 0, 1, 1, 0, 0], "comm": [0, 0, 0, 0, 1, 0, 0, 0],
         "trans": [1, 0, 0, 0, 0, 0, 0, 0]}
assert ranges.agreeing({"start": 40, "end": 60}, norms, 10) == ["heat", "comm"]
assert ranges.agreeing({"start": 0, "end": 10}, norms, 10) == ["trans"]
assert [ranges.confidence(a) for a in (["heat", "comm"], ["comm", "trans", "chap"],
        ["comm", "trans"], ["heat"], ["trans"], [])] == \
    ["High", "High", "Medium", "Medium", "Low", "Low"]
both = {"duration": 300, "heatmap": two["heatmap"],
        "comments": [{"text": "0:50 here", "like_count": 10}]}
r = pipeline.analyze(both, None, top_n=2)
assert r["ranges"][0]["confidence"] == "High" and r["ranges"][0]["agree"] == ["heat", "comm"]

# Minimum strength: a single spike leaves one range; a flat video none.
spike = {"duration": 300, "heatmap": two["heatmap"]}
assert len(pipeline.analyze(spike, None, top_n=5)["ranges"]) == 5
r = pipeline.analyze(spike, None, top_n=5, min_strength=0.25)
assert len(r["ranges"]) == 1 and r["hidden"] == 4 and r["ranges"][0]["strength"] > 0.9
flat = {"duration": 300, "heatmap": [{"start_time": t, "end_time": t + 10, "value": 0.5}
                                     for t in range(0, 300, 10)]}
r = pipeline.analyze(flat, None, top_n=5, min_strength=0.25)
assert r["ranges"] == [] and r["hidden"] == 5
assert len(pipeline.score_video({"info": spike, "segments": None, "sponsors": None},
                         {"top_n": 5, "skip_s": 0})["ranges"]) == 1  # default 25%

# Likes and comments as a share of views; missing numbers stay blank.
assert util.per_view(42, 1000) == " (4.20%)" and util.per_view(5, 1000) == " (0.50%)"
assert util.per_view(None, 1000) == util.per_view(5, 0) == util.per_view(5, None) == ""

# Reject Shorts, live, playlists and channels before any fetch. A video
# that merely sits inside a playlist is still that video.
vid = "abcdefghijk"
assert "Shorts" in cache.url_problem(f"https://www.youtube.com/shorts/{vid}")
assert "live" in cache.url_problem(f"https://www.youtube.com/live/{vid}")
assert "Batch" in cache.url_problem("https://www.youtube.com/@3blue1brown")
assert "Batch" in cache.url_problem("https://www.youtube.com/playlist?list=PL123")
assert "Batch" in cache.url_problem("https://www.youtube.com/user/someuser/videos")
assert cache.url_problem("not a url").startswith("Paste a link")
assert cache.url_problem(f"https://youtu.be/{vid}") is None
assert cache.url_problem(f"https://www.youtube.com/watch?v={vid}&list=PL123") is None
assert cache.url_problem(vid) is None
try:
    pipeline.analyze({"duration": 0}, None)
    raise AssertionError("a live video should be rejected")
except RuntimeError as e:
    assert "live" in str(e)

# Clip file names keep the range and drop characters Windows forbids.
assert clips.clip_filename({"title": 'A/B: "best"'}, {"start": 65, "end": 125}) == \
    "A B best 1.05-2.05.mp4"
assert clips.clip_filename({"title": "///"}, {"start": 0, "end": 1}) == "clip 0.00-0.01.mp4"
assert clips.parse_join_order("9, 5, 3, 1", [1, 3, 5, 9]) == [9, 5, 3, 1]
assert clips.parse_join_order("1 6 2 9 4 5", [1, 2, 4, 5, 6, 9]) == [1, 6, 2, 9, 4, 5]
try:
    clips.parse_join_order("1, 1, 4", [1, 4, 7])
    raise AssertionError("a repeated rank should be rejected")
except ValueError as e:
    assert "1, 4, 7" in str(e)
folder = tempfile.mkdtemp()
open(os.path.join(folder, "a.mp4"), "w").close()
got = [os.path.basename(p) for p in clips.clip_paths(folder, ["a.mp4", "a.mp4", "b.mp4"])]
assert got == ["a 2.mp4", "a 3.mp4", "b.mp4"], got
recorded = {}

def fake_run(cmd, **kw):
    recorded["cmd"] = cmd
    recorded["list"] = open(cmd[cmd.index("-i") + 1], encoding="utf-8").read()
    open(cmd[-1], "wb").close()
    return type("P", (), {"returncode": 0, "stderr": ""})()

old_which, old_run = shutil.which, subprocess.run
shutil.which = lambda name: r"C:\ffmpeg\ffmpeg.exe"
subprocess.run = fake_run
try:
    out = os.path.join(folder, "out.mp4")
    clips.concat_clips([os.path.join(folder, "a.mp4"), os.path.join(folder, "b.mp4")], out)
finally:
    shutil.which, subprocess.run = old_which, old_run
assert recorded["cmd"][0].endswith("ffmpeg.exe")
assert "copy" in recorded["cmd"]
text = recorded["list"]
assert text.index("a.mp4") < text.index("b.mp4")
assert os.path.exists(out) and not os.path.exists(out + ".txt")
shutil.rmtree(folder)
old_which = shutil.which
shutil.which = lambda name: None
try:
    clips.download_clip("https://example.com", 0, 1, "clip.mp4")
    raise AssertionError("missing ffmpeg should fail before any download")
except RuntimeError as e:
    assert "ffmpeg" in str(e)
finally:
    shutil.which = old_which
print("OK")

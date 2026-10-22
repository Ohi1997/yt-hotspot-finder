"""Offline self-check. Run: python test_yt_hotspots.py"""
import yt_hotspots as y

assert y.parse_timestamps("best part 3:45 and 1:02:10, not 9:99", 4000) == [225, 3730]

# Transcript segments that straddle range edges must not cause overlaps.
segs = [{"text": "why? wow! secret " * (i % 7), "start": i * 3.1, "duration": 3.3}
        for i in range(100)]
info = {"duration": 310,
        "heatmap": [{"start_time": t, "end_time": t + 10, "value": (t * 7) % 13}
                    for t in range(0, 310, 10)],
        "comments": [{"text": "2:05 lol", "like_count": 50}]}
r = y.analyze(info, segs, top_n=8)
s = sorted(r["ranges"], key=lambda x: x["start"])
assert all(a["end"] <= b["start"] for a, b in zip(s, s[1:])), s
assert r["signals"] == ["heat", "comm", "trans"] and r["ts_hits"] == 1
print("OK")

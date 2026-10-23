import json
import os
import re
import threading
import time

from .config import APP_DIR
from .fetch import Cancelled, YdlLogger, fetch_video, sponsor_segments_or_none
from .thumbs import storyboard


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

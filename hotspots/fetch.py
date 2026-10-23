import hashlib
import json
import logging
import threading
import urllib.error
import urllib.parse
import urllib.request

from .config import COMMENT_PROG_RE, SPONSOR_CATEGORIES


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

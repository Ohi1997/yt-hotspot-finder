import json
import logging
import os
import re


APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG_PATH = os.path.join(APP_DIR, "yt_hotspots.log")
SETTINGS_PATH = os.path.join(APP_DIR, "yt_hotspots_settings.json")

DEFAULT_WEIGHTS = {"heat": 45, "comm": 35, "trans": 20, "chap": 15}
SIGNAL_NAMES = {"heat": "replays", "comm": "comments", "trans": "speech",
                "chap": "chapters"}
COMMENT_PROG_RE = re.compile(r"comment.*\((\d+)/~(\d+)\)")
SPONSOR_CATEGORIES = ("sponsor", "selfpromo", "interaction", "intro", "outro")

TS_RE = re.compile(r"(?<![\d:])(?:(\d{1,2}):)?(\d{1,2}):(\d{2})(?![\d:])")
HOOK_WORDS = {
    "secret", "never", "always", "mistake", "truth", "actually", "crazy",
    "insane", "shocking", "biggest", "worst", "best", "unbelievable",
    "important", "warning", "stop", "wrong", "why", "how", "nobody",
    "everyone", "free", "million", "billion", "story", "lesson", "tip",
}
STOPWORDS = set("""
    a an and are as at be but by for from has have he her his i if in into is
    it its just like me my no not of on or our out so that the their them then
    there these they this to up us was we what when which who will with you
    your i'm it's that's don't can't you're we're they're there's what's do
    does did been being would could should about all also any because can get
    got go going know really right say said see some than think very well
    yeah yes oh okay ok um uh one two here now how why where more much
""".split())
# Sound tags in captions, e.g. [Laughter], (applause), [audience laughs].
SOUND_RE = re.compile(r"[\[(][^\])]*?\b(laugh\w*|applause|cheer\w*|clapp\w*|"
                      r"gasps?)\b[^\])]*[\])]", re.I)
QUOTE_WORDS = 4   # a comment must quote this many words in a row


def load_settings():
    try:
        with open(SETTINGS_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_settings(data):
    try:
        with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except OSError:
        logging.exception("Could not save settings")

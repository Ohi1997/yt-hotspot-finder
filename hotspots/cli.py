import argparse
import logging
import logging.handlers
import os
import sys

from .config import DEFAULT_WEIGHTS, LOG_PATH, SIGNAL_NAMES
from .export import write_csv
from .gui.app import App
from .pipeline import run_batch
from .ranges import fmt_breakdown
from .thumbs import moment_url
from .util import fmt_time


def parse_weights(text):
    """'replays=45,comments=35' -> {'heat': 45.0, 'comm': 35.0}."""
    names = {v: k for k, v in SIGNAL_NAMES.items()}
    out = {}
    for part in filter(None, (p.strip() for p in text.split(","))):
        name, _, value = part.partition("=")
        key = names.get(name.strip().lower(), name.strip().lower())
        if key not in DEFAULT_WEIGHTS:
            raise argparse.ArgumentTypeError(
                f"unknown signal {name!r}; use "
                + ", ".join(SIGNAL_NAMES.values()))
        try:
            out[key] = float(value)
        except ValueError:
            raise argparse.ArgumentTypeError(f"bad weight in {part!r}")
    return out


def parse_args(argv=None):
    p = argparse.ArgumentParser(
        prog="yt_hotspots.py",
        description="Find the best moments of YouTube videos. Without URLs, "
                    "opens the app.")
    p.add_argument("urls", nargs="*",
                   help="video, playlist or channel URLs")
    p.add_argument("--clip", type=int, default=30,
                   help="clip length in seconds (default 30)")
    p.add_argument("--top", type=int, default=8,
                   help="ranges per video (default 8)")
    p.add_argument("--comments", type=int, default=800,
                   help="max comments to load (default 800)")
    p.add_argument("--fit", action="store_true",
                   help="fit each range's length to its peak")
    p.add_argument("--skip", type=int, default=15,
                   help="skip the first and last N seconds (default 15)")
    p.add_argument("--no-sponsorblock", action="store_true",
                   help="do not skip SponsorBlock segments")
    p.add_argument("--min-strength", type=int, default=25, metavar="PCT",
                   help="hide ranges weaker than this (default 25)")
    p.add_argument("--weights", type=parse_weights, default={},
                   help='relative signal weights, e.g. '
                        '"replays=45,comments=35,speech=20,chapters=15"')
    p.add_argument("--limit", type=int, default=50,
                   help="max videos per playlist or channel (default 50)")
    p.add_argument("--refresh", action="store_true",
                   help="reload from YouTube even if cached")
    p.add_argument("--csv", metavar="PATH", help="also save all ranges here")
    return p.parse_args(argv)


def run_cli(args):
    """Prints the ranges of every video; returns the exit code."""
    for stream in (sys.stdout, sys.stderr):  # "–", "·" etc. when piped
        if hasattr(stream, "reconfigure") and not stream.isatty():
            stream.reconfigure(encoding="utf-8")
    opts = {"clip_len": args.clip, "top_n": args.top,
            "weights": args.weights, "fit_peaks": args.fit,
            "skip_s": args.skip, "sponsorblock": not args.no_sponsorblock,
            "min_strength": args.min_strength / 100}

    def log(msg):
        logging.info(msg)
        print(msg, file=sys.stderr, flush=True)

    def on_video(i, n, url, entry, result, err):
        if err:
            print(f"\n[{i}/{n}] {url}\n  failed: {err}", flush=True)
            return
        info = entry["info"]
        print(f"\n[{i}/{n}] {info.get('title')}  "
              f"({fmt_time(info.get('duration') or 0)})")
        for k, r in enumerate(result["ranges"], 1):
            print(f"  {k:>2}. {fmt_time(r['start']):>7} – "
                  f"{fmt_time(r['end']):<7} {r['strength']:>4.0%}  "
                  f"{r['confidence']:<6}  {r['title']}")
            print(f"      {fmt_breakdown(r['shares'])}   "
                  f"{moment_url(info, r['start'])}")
        if result["hidden"]:
            print(f"  ({result['hidden']} weaker ranges hidden; "
                  "see --min-strength)")
        sys.stdout.flush()

    try:
        rows, failed = run_batch(args.urls, opts, args.comments, args.limit,
                                 args.refresh, log, on_video=on_video)
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130
    except Exception as e:
        logging.exception("Command line run failed")
        print(f"Error: {e}", file=sys.stderr)
        return 1
    if args.csv:
        write_csv(args.csv, rows)
        print(f"\nSaved {len(rows)} ranges to {args.csv}", file=sys.stderr)
    if failed:
        print(f"{len(failed)} failed; details in {LOG_PATH}", file=sys.stderr)
    return 1 if failed else 0


def main(argv=None):
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.handlers.RotatingFileHandler(
            LOG_PATH, maxBytes=1_000_000, backupCount=1, encoding="utf-8")])
    sys.excepthook = lambda *exc: logging.critical("Crash", exc_info=exc)
    if sys.stderr is None:  # pythonw: no console, stray writes would crash
        sys.stdout = sys.stderr = open(os.devnull, "w")
    args = parse_args(argv)
    if args.urls:
        return run_cli(args)
    logging.info("App started")
    App().mainloop()
    return 0

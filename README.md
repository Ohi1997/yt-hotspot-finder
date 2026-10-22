# YouTube Hotspot Finder

[![test](../../actions/workflows/test.yml/badge.svg)](../../actions/workflows/test.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
![Python 3.9+](https://img.shields.io/badge/python-3.9%2B-blue.svg)

A small Python desktop app (Tkinter). Paste a YouTube URL, and it shows the video's stats and a ranked list of the time ranges most likely to be the best moments.

## Features

- Video stats: title, channel, subscribers, views, likes, comments, duration, upload date
- A score timeline graph with the best moments highlighted
- A ranked list of time ranges, for example `5:00 – 5:30`
- Double-click a range to open the video at that moment
- Export the list to CSV, or copy it to the clipboard
- An error log, opened with one click from the app

## How it finds the best moments

It blends up to three signals and uses the ones the video has:

| Signal | What it is | Weight |
|---|---|---|
| "Most replayed" heatmap | Real viewer data from YouTube (only on some videos) | 45% |
| Comment timestamps | Times like `3:45` mentioned in comments, weighted by likes | 35% |
| Transcript energy | Speech density, questions, emphasis, hook words | 20% |

If a signal is missing, the remaining weights are rescaled. If the video has no heatmap, the app says so. The results are then estimates, not measured viewing data.

## Requirements

- Python 3.9 or newer, with Tkinter (included with Python on Windows and macOS)
- An internet connection

## Quick start

### Windows

1. Install Python from https://www.python.org/downloads/. During install, tick **Add python.exe to PATH**.
2. Download this repository: click **Code > Download ZIP** and unzip it, or run:

   ```
   git clone https://github.com/Ohi1997/yt-hotspot-finder.git
   ```

3. Double-click `start.bat`.

On the first run, `start.bat` installs the dependencies. This takes about a minute. After that, the app opens with no console window.

### macOS and Linux

```
git clone https://github.com/Ohi1997/yt-hotspot-finder.git
cd yt-hotspot-finder
python3 -m pip install -r requirements.txt
python3 yt_hotspots.py
```

On Linux, if you get `No module named tkinter`, install it first:

```
sudo apt install python3-tk
```

## How to use

1. Paste a YouTube video URL into the box.
2. Change the options if you want:
   - **Clip length (s):** the length of each suggested range (default 30)
   - **Top results:** the number of ranges to list (default 8)
   - **Max comments:** the number of comments to load (default 800)
3. Click **Analyze** and wait. On busy videos, loading comments can take 10 to 30 seconds.
4. Read the stats and the ranked ranges. Double-click a row to open that moment on YouTube.
5. Use **Export CSV** or **Copy list** to save the results.

## Error logs

The app writes progress and errors to `yt_hotspots.log`, next to `yt_hotspots.py`. Each error includes its full traceback.

- Click **Error log** in the app to open the log.
- If the app does not open at all, open `yt_hotspots.log` in a text editor.
- When you report a bug, attach `yt_hotspots.log`.

The log is capped at about 1 MB. Older entries move to `yt_hotspots.log.1`.

## Running the tests

```
python test_yt_hotspots.py
```

The test checks the scoring logic offline and prints `OK`. It needs no internet and no dependencies. GitHub Actions runs it on every push.

## Troubleshooting

| Problem | Fix |
|---|---|
| `Python not found` from `start.bat` | Install Python and tick **Add python.exe to PATH**. Then run `start.bat` again |
| `Missing dependency` error | Run `python -m pip install -r requirements.txt` |
| Errors fetching the video, or no heatmap on videos that should have one | YouTube changed something. Run `python -m pip install -U yt-dlp` |
| "No transcript available" in the status bar | The video has no captions. The app still works with the other signals |
| Slow analysis | Lower **Max comments** |
| Results look generic | The video probably has no heatmap and few timestamp comments. Small videos give weaker estimates |
| `No module named tkinter` | See the Linux install command above |

## Project files

| File | Purpose |
|---|---|
| `yt_hotspots.py` | The app: scoring logic, data fetching and GUI |
| `start.bat` | One-click launcher for Windows |
| `test_yt_hotspots.py` | Offline test for the scoring logic |
| `requirements.txt` | Python dependencies |

## Limitations

- Without the heatmap, results are predictions, not real viewing data. Only the channel owner can see true audience retention, in YouTube Studio.
- Comment timestamps are rare on small or new videos.
- The transcript signal is a simple word-pattern check, not an AI model.
- The heatmap and comments come from YouTube's pages through `yt-dlp`, which can break when YouTube changes its site. Scraping can also go against YouTube's Terms of Service. Use this tool for personal analysis. For anything bigger, use the official YouTube Data API.

## License

[MIT](LICENSE)

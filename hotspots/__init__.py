"""
YouTube Hotspot Finder  (Tkinter GUI)

Paste a YouTube URL -> get video stats and a ranked list of time ranges that
are most likely to be the "best" moments, using:
  1. YouTube's "Most replayed" heatmap (real viewer data, when available)
  2. Timestamps mentioned in comments (weighted by comment likes)
  3. Transcript energy (speech density, questions, emphasis, hook words)
  4. Chapters and timestamps in the video description

Install:   pip install yt-dlp youtube-transcript-api
           ffmpeg on PATH, to save a range as an mp4 (Export clip)
Run:       python yt_hotspots.py
"""

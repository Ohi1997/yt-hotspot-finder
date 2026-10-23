# Feature ideas

Ideas for what to add to YouTube Hotspot Finder next. Items marked **(done)** are implemented.

## Highest value

1. **(done)** **Show why each moment scored.** Split the score into heatmap, comments, and transcript so a row can say which signal actually carried it. Right now the list is only a single number.
2. **(done)** **Quote the transcript for each range.** Put the spoken lines next to the timestamp so you can judge the moment without opening YouTube.
3. **(done)** **Use chapters and description timestamps as a fourth signal.** Many videos already mark the good parts in the description or as chapters. Those are unused today.
4. **(done)** **Let clip length follow the peak.** Keep the fixed 30-second window as an option, but also offer ranges that start and end where the score actually rises and falls, snapped to sentence boundaries.
5. **(done)** **Tunable weights.** Expose the current 45% / 35% / 20% blend as sliders, and remember the last setting.
6. **(done)** **Cancel and a real progress state.** Comment loading can take 10–30 seconds with no way to stop it, and the status line is the only feedback.

## Editing and export

7. **(done)** **Export as YouTube chapters.** A description block like `0:00 Intro` / `5:00 Best moment`, built from the ranked ranges and the transcript.
8. **(done)** **Editor markers.** CSV is there; also offer a format Premiere, DaVinci, or CapCut can import as markers.
9. **(done)** **One-click timestamp links.** Copy `https://youtu.be/ID?t=300` for the selected row, not only the whole list.
10. **(done)** **Suggested titles for each clip.** A short label from the transcript (or a local model later) so the list is usable as Shorts or highlight ideas.
11. **(done)** **Exclude intros, outros, and sponsor reads.** Drop the first and last few seconds by default, and optionally skip SponsorBlock segments so those do not rank as "best moments."

## Timeline

12. **(done)** **Hover and click on the graph.** Show the time and score under the cursor, and open YouTube at that second on click. The graph is display-only today.
13. **(done)** **Storyboard thumbnails.** YouTube already serves preview images along the video. Show one for each ranked range.
14. **(done)** **Chapter markers on the timeline.** Draw official chapters under the score line when the video has them.

## Workflow

15. **(done)** **Recent videos.** A short history of URLs and results so you can reopen an analysis without fetching comments again.
16. **(done)** **Batch mode.** Paste several URLs, or a playlist, and run them one after another into one CSV.
17. **(done)** **Cache the fetch.** Save video info, heatmap, comments, and transcript locally so changing clip length or top-N does not hit YouTube again.
18. **(done)** **A command-line mode.** `python yt_hotspots.py URL --clip 30 --top 8 --csv out.csv` for the same scoring without the GUI.

## Quality of the estimates

19. **Stronger transcript scoring.** **(done)** The current signal is word count, `?` / `!`, and a fixed hook-word list. Phrase repetition, topic shifts, and laughter or emphasis from captions would rank moments more reliably when there is no heatmap.
20. **Comment text, not only timestamps.** **(done)** A liked comment that names a moment without a `3:45` still gets ignored. Matching phrases from the transcript would catch those.
21. **Confidence label.** **(done)** When the heatmap is missing, the app already says the results are estimates. Put a confidence level on each row based on how many signals agreed.
22. **Minimum score filter.** **(done)** Hide weak ranges instead of always filling Top results even when the video is flat.

## Small UI fixes

23. **Engagement in the stats line.** **(done)** Likes per view and comments per view, next to the raw counts.
24. **Sort the table.** **(done)** By score, by time, or by length.
25. **Paste button and URL check.** **(done)** A clear error for a channel, playlist, or Shorts URL before the fetch starts. Live streams already fail on duration; say that up front.

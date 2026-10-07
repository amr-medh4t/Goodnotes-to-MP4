# Goodnotes Replay to MP4

This small Windows app turns a Goodnotes document's synchronized handwriting
and embedded recording into an MP4 without playing the recording in real time.

## Setup

Install Python 3.10 or newer for Windows, including Tkinter. On the first
conversion, the app checks for Pillow, lz4, FFmpeg, and Poppler. Missing Python
packages are installed into the current user's local application data folder.
If FFmpeg or Poppler is not on `PATH`, the app downloads a Windows build,
verifies the publisher's SHA-256 checksum, and keeps the portable files under
`%LOCALAPPDATA%\GoodnotesReplay\tools`.

These downloads do not require administrator access, but the first setup
requires an internet connection and can download over 100 MB. The app uses the
latest Windows FFmpeg GPL shared build (needed for its H.264 encoder) from
[BtbN/FFmpeg-Builds](https://github.com/BtbN/FFmpeg-Builds) and the latest
Windows Poppler package from
[oschwartz10612/poppler-windows](https://github.com/oschwartz10612/poppler-windows).
Those tools retain their respective upstream licenses. The FFmpeg build is
GPL-licensed; review the upstream license if you redistribute it.

Python itself and a Windows installation that includes Tkinter must already be
present; the launcher does not download or install the Python runtime.

## Use

- Double-click `Run Goodnotes Replay UI.bat`, or run
  `python goodnotes_replay_gui.py` in PowerShell.
- Choose a `.goodnotes` document or an extracted Goodnotes folder.
- Choose the output `.mp4` path and select **Create MP4**.

The folder is organized so the files in the main directory are the reusable
application. The supplied extracted document is in
`input\Goodnotes-Extracted`, and generated videos and temporary frame checks
are in `output`.

The CLI is also available:

```powershell
python make_goodnotes_replay.py --input "Lecture.goodnotes" "Lecture-Replay.mp4"
```

The renderer supports Goodnotes archives whose event journal contains the
audio/handwriting synchronization data. It reports an error if it cannot find
that data or cannot download/install the required tools. The video is rendered
at 1080x1528 resolution and 30 FPS, uses lossless PNG intermediate frames to
avoid pixelated handwriting, and displays the complete PDF page using its
native portrait aspect ratio. No white side margins or landscape stretching
are added, and a modest stroke-width enhancement is applied for clearer
handwriting. Zoom and camera tracking are intentionally disabled in this
stable full-page mode.

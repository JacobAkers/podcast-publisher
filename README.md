# Akers Brothers Podcast Publisher

A Python-based podcast publishing pipeline that turns a YouTube playlist into a hosted podcast. The script downloads new playlist audio, converts and normalizes it, maintains a local episode database, generates an RSS 2.0/iTunes feed and HTML episode page, and uploads the published files to Cloudflare R2.

**Published endpoints:**

- Website: <https://podcast.akers.cc>
- RSS feed: <https://podcast.akers.cc/feed.xml>
- Source playlist: <https://www.youtube.com/playlist?list=PLsY2uFzK-ch9K9jzvyzNYca55HiT0WtM->

## How it works

Each run follows this pipeline:

1. Fetch the configured YouTube playlist with `yt-dlp`.
2. Skip videos that already have an episode record and a non-empty MP3 file.
3. Download and convert new videos to stereo, 44.1 kHz, 128 kbps MP3 audio.
4. Remove silence and apply loudness normalization with FFmpeg.
5. Record episode metadata and file details in `work/episodes.json`.
6. Generate `out/feed.xml` and `out/index.html`.
7. Upload the episodes, cover image, RSS feed, and HTML page to the `r2:akers-podcast` R2 remote.

The feed includes iTunes metadata such as category, explicit status, owner, cover art, episode duration, enclosure size, and publication date.

## Project layout

```text
.
├── build_feed.py          # Main download, processing, feed, and upload pipeline
├── cover/
│   └── cover.jpg          # Podcast artwork
├── episodes/              # Published MP3 files, one file per YouTube video ID
├── work/
│   ├── episodes.json      # Persistent episode metadata cache
│   ├── cookies.txt        # Optional yt-dlp authentication cookies
│   ├── last_run.log       # Example output from a recent run
│   └── *.webm/*.json      # Temporary download artifacts when a run encounters them
└── out/
    ├── feed.xml           # Generated RSS/iTunes podcast feed
    └── index.html         # Generated podcast website
```

## Requirements

The pipeline requires:

- Python 3.10 or newer
- `yt-dlp`
- `ffmpeg` and `ffprobe`
- `rclone`, configured with an R2 remote named `r2`
- Network access to YouTube
- A writable installation directory

The Python standard library is sufficient; this script does not use third-party Python packages.

Example package installation on Debian or Ubuntu:

```bash
sudo apt update
sudo apt install ffmpeg python3
python3 -m pip install --upgrade yt-dlp
```

Install and configure `rclone` separately, then verify the configured remote:

```bash
rclone listremotes
rclone lsd r2:
```

## Installation and execution

The script currently uses `/opt/podcast` as its deployment root. A typical server layout is:

```text
/opt/podcast/
├── build_feed.py
├── cover/
├── episodes/
├── work/
└── out/
```

Place `build_feed.py` and its support directories under that root, then run:

```bash
cd /opt/podcast
python3 build_feed.py
```

When developing or testing from another location, update the `BASE_DIR` value in `build_feed.py`:

```python
BASE_DIR = Path("/opt/podcast")
```

No command-line arguments are required.

## Configuration

Main configuration values are at the top of `build_feed.py`:

| Setting | Purpose |
| --- | --- |
| `PLAYLIST_URL` | YouTube playlist used as the episode source |
| `BASE_URL` | Public website and media base URL |
| `R2_REMOTE` | Cloudflare R2 destination |
| `PODCAST_TITLE` | Podcast title |
| `PODCAST_DESCRIPTION` | Podcast and iTunes channel description |
| `PODCAST_AUTHOR` | iTunes episode/channel author |
| `PODCAST_OWNER_NAME` | iTunes podcast owner name |
| `PODCAST_OWNER_EMAIL` | iTunes podcast owner email |
| `PODCAST_CATEGORY` | Main iTunes category |
| `PODCAST_SUBCATEGORY` | iTunes subcategory |
| `NEWEST_FIRST` | RSS item ordering; currently feed episodes are in playlist order |

## Authentication

For YouTube videos that require authenticated access, place a Netscape-format cookie export in:

```text
work/cookies.txt
```

The script only passes the cookies file to `yt-dlp` when it exists and is non-empty. Keep this file private; it can contain account session credentials.

## Episode metadata

`work/episodes.json` is the persistent cache. Each key is a YouTube video ID and each record contains:

```json
{
  "id": "VIDEO_ID",
  "url": "https://www.youtube.com/watch?v=VIDEO_ID",
  "playlist_index": 1,
  "title": "Episode title",
  "description": "Episode description",
  "pub_date": "Sat, 27 Sep 2025 12:00:00 +0000",
  "duration_sec": 1971,
  "filename": "VIDEO_ID.mp3",
  "byte_length": 23543085
}
```

The script reuses cached metadata when the MP3 file already exists. Before generating output, it refreshes the recorded playlist index and MP3 byte size.

## Generated website

`out/index.html` is a standalone, responsive episode page. It:

- Uses `cover.jpg` as the podcast artwork.
- Links to the RSS subscription URL.
- Displays the five newest episodes by episode-number sorting.
- Provides native HTML5 audio controls for each episode.

The page is uploaded to R2 with an explicit `text/html; charset=utf-8` content type.

## Generated RSS feed

`out/feed.xml` is an RSS 2.0 feed with iTunes podcast extensions. Every available episode becomes an RSS item containing:

- Title and description
- RFC 2822 publication date
- Stable GUID based on the YouTube video ID
- MP3 enclosure URL, MIME type, and current file size
- Author and episode duration
- Explicit-content status

On a successful run, the script uploads `feed.xml` to `r2:akers-podcast/feed.xml`.

## Cover art

The script looks for cover artwork in this priority:

1. `cover/cover.png`
2. `cover/cover.jpg`
3. `cover/cover.jpeg`
4. The first matching PNG/JPEG file found in `cover/`

Use a square, high-resolution JPEG or PNG for podcast directory compatibility.

## Processing details

New downloads use a FFmpeg pipeline that:

- Removes the video stream.
- Produces two audio channels.
- Samples audio at 44.1 kHz.
- Encodes MP3 audio at 128 kbps.
- Removes detected leading/trailing silence.
- Applies loudness normalization to -16 LUFS.
- Uses a true peak target of -1.5 dBTP.
- Applies an 11 LU integrated-loudness range target.

The script writes temporary normalized files in `work/` and replaces the original episode MP3 only after successful encoding.

## Scheduling

Because the script is safe to rerun, it can be scheduled periodically with cron or another task scheduler. For example:

```cron
0 12 * * * cd /opt/podcast && python3 build_feed.py >> /var/log/podcast.log 2>&1
```

A rerun does not redownload videos that are already cached and have a valid MP3. It does fetch the playlist, refresh output, and resynchronize assets.

## Build status web interface

`status_server.py` provides a dependency-free web page and JSON API showing the current podcast build step. It reads `work/status.json`, which `build_feed.py` updates atomically throughout the run.

Start it on the container:

```bash
python3 /opt/podcast/status_server.py --host 0.0.0.0 --port 8765 \
  --status-file /opt/podcast/work/status.json \
  --log-file /opt/podcast/work/last_run.log
```

Open `http://192.168.0.235:8765/`. The status page refreshes every two seconds and reports:

- Overall build state and progress percentage
- Current numbered step
- The current episode being processed
- Current MP3 target and, when available, the downloaded source file, inferred from the latest download/encoding entry in `work/last_run.log`
- Live FFmpeg phase progress, elapsed media time, and expected episode duration
- An overall progress estimate that advances within the active episode while FFmpeg encodes or normalizes it
- Episode download/build position
- Processing failures
- The last status update time

The JSON endpoint is available at `http://192.168.0.235:8765/api/status`. FFmpeg reports progress through its machine-readable `-progress pipe:1` output; the build process writes the current media time, duration, and phase percentage to `work/status.json`. Playlist downloads and upload transfers are still represented as active steps rather than precise byte-level progress.

For a persistent service on Debian 12, create `/etc/systemd/system/podcast-status.service`:

```ini
[Unit]
Description=Akers Brothers Podcast build status
After=network.target

[Service]
Type=simple
WorkingDirectory=/opt/podcast
ExecStart=/usr/bin/python3 /opt/podcast/status_server.py --host 0.0.0.0 --port 8765 --status-file /opt/podcast/work/status.json --log-file /opt/podcast/work/last_run.log
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Enable and start it:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now podcast-status.service
sudo systemctl status podcast-status.service
```

For public access, expose port `8765` through your firewall or reverse proxy. The server now binds to `0.0.0.0` by default so it is reachable at `192.168.0.235:8765`; use `--host 127.0.0.1` if it should only be local. A proxy such as Caddy or Nginx can provide HTTPS; no authentication is included by this small status server.

If a build stops unexpectedly, the status page changes from `running` to `stale` when it has not received an update for thirty minutes.

## Troubleshooting

### `yt-dlp` cannot download a video

- Confirm the playlist URL is public or accessible with cookies.
- Refresh `work/cookies.txt` if YouTube authentication is required.
- Update `yt-dlp` and its remote JavaScript components.
- Check `last_run.log` or captured stdout/stderr for the failing video ID.

### Normalization fails

- Confirm that `ffmpeg` and `ffprobe` are installed and available on `PATH`.
- Check that the downloaded source file is a valid audio/video container.
- Inspect the FFmpeg `loudnorm` measurement in the script output.

### R2 upload fails

- Run `rclone listremotes` and confirm `r2` exists.
- Confirm the R2 remote grants access to the `akers-podcast` bucket or container.
- Test the destination directly:

```bash
rclone copyto out/feed.xml r2:akers-podcast/feed.xml
```

### Episode is missing from the feed

The script only adds an RSS item if it has a matching non-empty MP3 in `episodes/`. Confirm both the cache entry and file exist:

```text
work/episodes.json -> VIDEO_ID
episodes/VIDEO_ID.mp3
```

## Operational notes

- Keep `work/episodes.json` as persistent state if the directory is recreated by a deployment process.
- Preserve `cover/` and `episodes/` when replacing application files.
- The script hardcodes `/opt/podcast`; change `BASE_DIR` when deploying elsewhere.
- `cookies.txt` should never be committed or shared.
- The existing `NEWEST_FIRST = False` setting means RSS items are generated in playlist order rather than reversed.

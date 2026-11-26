#!/usr/bin/env python3
import datetime
import email.utils
import html
import json
import mimetypes
import os
import re
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from pathlib import Path

# --- Show Configuration ---
PLAYLIST_URL = "https://www.youtube.com/playlist?list=PLsY2uFzK-ch9K9jzvyzNYca55HiT0WtM-"
BASE_URL = "https://podcast.akers.cc"
R2_REMOTE = "r2:akers-podcast"

PODCAST_TITLE = "Akers Brothers Podcast"
PODCAST_DESCRIPTION = (
    "Pull up a chair with brothers Jerry and Jacob Akers as they sit down for honest, "
    "laid-back conversations about their biblical faith, Scripture, and the books they are "
    "currently reading. Whether breaking down parables, sharing personal testimonies, or "
    "welcoming guests to talk about Jesus and the Word, the Akers Brothers Podcast is all "
    "about real-life faith, fellowship, and growing closer to God. New episodes weekly!"
)
PODCAST_AUTHOR = "Jacob Akers"
PODCAST_OWNER_NAME = "Jacob Akers"
PODCAST_OWNER_EMAIL = "jacob.d.akers@gmail.com"
PODCAST_LANGUAGE = "en-us"
PODCAST_EXPLICIT = "false"
PODCAST_CATEGORY = "Religion & Spirituality"
PODCAST_SUBCATEGORY = "Christianity"

# Set to True if you want newest episodes listed at the top of the feed
NEWEST_FIRST = False

# --- Paths ---
BASE_DIR = Path("/opt/podcast")
WORK_DIR = BASE_DIR / "work"
EPISODES_DIR = BASE_DIR / "episodes"
COVER_DIR = BASE_DIR / "cover"
OUT_DIR = BASE_DIR / "out"
DB_PATH = WORK_DIR / "episodes.json"
FEED_PATH = OUT_DIR / "feed.xml"
INDEX_PATH = OUT_DIR / "index.html"
COOKIES_PATH = WORK_DIR / "cookies.txt"
STATUS_PATH = WORK_DIR / "status.json"

ITUNES_NS = "http://www.itunes.com/dtds/podcast-1.0.dtd"
ATOM_NS = "http://www.w3.org/2005/Atom"
ET.register_namespace("itunes", ITUNES_NS)
ET.register_namespace("atom", ATOM_NS)


def reset_status() -> None:
    """Initialize or replace the status exposed by the local web interface."""
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    status = {
        "status": "running",
        "step": "Starting",
        "step_number": 0,
        "total_steps": 6,
        "progress": 0,
        "detail": "Preparing podcast build",
        "episode": None,
        "failures": 0,
        "pid": os.getpid(),
        "started_at": now,
        "updated_at": now,
        "completed_at": None,
    }
    write_status(status)


def write_status(status: dict) -> None:
    """Atomically replace the status file so readers never see partial JSON."""
    STATUS_PATH.parent.mkdir(parents=True, exist_ok=True)
    status["updated_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
    tmp_path = STATUS_PATH.with_suffix(".json.tmp")
    tmp_path.write_text(json.dumps(status, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp_path, STATUS_PATH)


def update_status(**changes) -> dict:
    status = {}
    if STATUS_PATH.is_file():
        try:
            status = json.loads(STATUS_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            status = {}
    status.update(changes)
    write_status(status)
    return status


def ffmpeg_time_seconds(progress: dict) -> float | None:
    raw_microseconds = progress.get("out_time_us") or progress.get("out_time_ms")
    if raw_microseconds:
        try:
            return int(raw_microseconds) / 1_000_000
        except ValueError:
            pass

    raw_time = progress.get("out_time")
    if raw_time and raw_time != "N/A":
        try:
            hours, minutes, seconds = raw_time.split(":")
            return int(hours) * 3600 + int(minutes) * 60 + float(seconds)
        except ValueError:
            pass
    return None


def run_ffmpeg_with_progress(
    cmd: list[str],
    duration_sec: int | float | None = None,
    progress_start: float = 0,
    progress_end: float = 1,
    check: bool = True,
) -> subprocess.CompletedProcess:
    if not cmd or Path(cmd[0]).name.lower() != "ffmpeg":
        raise ValueError("run_ffmpeg_with_progress requires an ffmpeg command")
    if len(cmd) < 2:
        raise ValueError("ffmpeg command is missing its output target")

    progress_cmd = cmd[:-1] + ["-progress", "pipe:1", "-nostats", cmd[-1]]
    status = {}
    if STATUS_PATH.is_file():
        try:
            status = json.loads(STATUS_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            status = {}

    episode = status.get("episode") or {}
    episode_position = episode.get("position")
    episode_total = episode.get("total")

    with tempfile.TemporaryFile(mode="w+t", encoding="utf-8") as stderr_file:
        process = subprocess.Popen(
            progress_cmd,
            stdout=subprocess.PIPE,
            stderr=stderr_file,
            text=True,
            bufsize=1,
        )
        progress_record = {}
        captured_progress = []
        if process.stdout is not None:
            for line in process.stdout:
                key, separator, value = line.strip().partition("=")
                if not separator:
                    continue
                progress_record[key] = value
                captured_progress.append(line)
                if key != "progress":
                    continue

                elapsed_sec = ffmpeg_time_seconds(progress_record)
                if elapsed_sec is not None and duration_sec and duration_sec > 0:
                    file_progress = min(100.0, max(0.0, elapsed_sec / duration_sec * 100))
                    changes = {
                        "file_progress": round(file_progress, 1),
                        "file_elapsed_sec": round(elapsed_sec, 1),
                        "file_duration_sec": round(float(duration_sec), 1),
                        "detail": (
                            f"FFmpeg processed {format_duration(int(elapsed_sec))} "
                            f"of {format_duration(int(duration_sec))}"
                        ),
                    }
                    if episode_position and episode_total:
                        episode_fraction = progress_start + (
                            progress_end - progress_start
                        ) * file_progress / 100
                        overall_progress = 10 + (
                            (episode_position - 1 + episode_fraction) / episode_total * 50
                        )
                        changes["progress"] = round(overall_progress, 1)
                    update_status(**changes)
                progress_record = {}

        return_code = process.wait()
        stderr_file.seek(0)
        stderr = stderr_file.read()
        stdout = "".join(captured_progress)
        if return_code and check:
            raise subprocess.CalledProcessError(
                return_code,
                cmd,
                output=stdout,
                stderr=stderr,
            )
        return subprocess.CompletedProcess(cmd, return_code, stdout, stderr)


def run_cmd(
    cmd: list[str],
    check: bool = True,
    ffmpeg_duration_sec: int | float | None = None,
    progress_start: float = 0,
    progress_end: float = 1,
) -> subprocess.CompletedProcess:
    executable = Path(cmd[0]).name.lower() if cmd else ""
    if executable == "ffmpeg":
        if "-af" in cmd:
            update_status(
                step="Normalizing audio with FFmpeg",
                detail="Analyzing loudness or removing silence in the current MP3",
            )
        elif "-i" in cmd:
            update_status(
                step="Encoding audio with FFmpeg",
                detail="Converting downloaded media to MP3",
            )
    elif executable == "ffprobe":
        update_status(
            step="Probing audio duration",
            detail="Reading final MP3 duration with FFprobe",
        )
    elif executable == "yt-dlp":
        update_status(
            step="Downloading episode",
            detail="Fetching playlist or episode media with yt-dlp",
        )
    elif executable == "rclone":
        update_status(
            step="Uploading to Cloudflare R2",
            detail=f"Running rclone: {' '.join(cmd[1:3])}",
        )

    if executable == "ffmpeg":
        result = run_ffmpeg_with_progress(
            cmd,
            duration_sec=ffmpeg_duration_sec,
            progress_start=progress_start,
            progress_end=progress_end,
            check=check,
        )
    else:
        result = subprocess.run(cmd, check=check, text=True, capture_output=True)

    if executable == "ffmpeg":
        update_status(detail="FFmpeg completed this audio step")
    elif executable == "rclone":
        update_status(detail="R2 transfer step completed")
    return result


def slugify(text: str) -> str:
    text = text.lower().strip()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    return text.strip("-") or "episode"


def find_cover_image() -> Path | None:
    for name in ("cover.png", "cover.jpg", "cover.jpeg"):
        p = COVER_DIR / name
        if p.is_file() and p.stat().st_size > 0:
            return p
    for ext in ("*.png", "*.jpg", "*.jpeg"):
        matches = sorted(COVER_DIR.glob(ext))
        if matches and matches[0].stat().st_size > 0:
            return matches[0]
    return None


def format_rfc2822(dt: datetime.datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return email.utils.format_datetime(dt)


def format_itunes_duration(seconds: int | None) -> str:
    if not seconds or seconds < 0:
        return "00:00"
    hrs, rem = divmod(int(seconds), 3600)
    mins, secs = divmod(rem, 60)
    if hrs > 0:
        return f"{hrs:02d}:{mins:02d}:{secs:02d}"
    return f"{mins:02d}:{secs:02d}"


def probe_mp3_duration(mp3_path: Path) -> int:
    res = run_cmd([
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(mp3_path),
    ])
    try:
        return int(round(float(res.stdout.strip())))
    except ValueError:
        return 0


def load_db() -> dict:
    if DB_PATH.is_file():
        return json.loads(DB_PATH.read_text(encoding="utf-8"))
    return {}


def save_db(data: dict) -> None:
    DB_PATH.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def fetch_playlist_entries() -> list[dict]:
    print("Fetching playlist entries via yt-dlp...", flush=True)
    res = run_cmd([
        "yt-dlp",
        "--flat-playlist",
        "--dump-single-json",
        "--no-warnings",
        PLAYLIST_URL,
    ])
    payload = json.loads(res.stdout)
    entries = payload.get("entries") or []
    normalized = []
    for idx, entry in enumerate(entries, start=1):
        vid = entry.get("id")
        if not vid:
            continue
        url = entry.get("url") or f"https://www.youtube.com/watch?v={vid}"
        if not url.startswith("http"):
            url = f"https://www.youtube.com/watch?v={vid}"
        normalized.append({
            "playlist_index": entry.get("playlist_index") or idx,
            "id": vid,
            "url": url,
            "title": entry.get("title") or f"Episode {idx}",
        })
    return normalized



SILENCE_FILTER = (
    "silenceremove="
    "start_periods=1:start_duration=0.3:start_threshold=-50dB:"
    "stop_periods=-1:stop_duration=1.5:stop_threshold=-50dB"
)


def probe_mp3_duration(mp3_path: Path) -> int:
    res = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(mp3_path),
        ],
        capture_output=True, text=True, check=True,
    )
    return int(round(float(res.stdout.strip())))


def normalize_and_trim_mp3(mp3_path: Path) -> None:
    tmp_path = WORK_DIR / f"norm_{mp3_path.name}"
    duration_sec = probe_mp3_duration(mp3_path)

    update_status(
        step="Analyzing audio with FFmpeg",
        step_number=2,
        detail=f"Measuring loudness for {mp3_path.name}",
        current_file=str(mp3_path),
        source_file=str(mp3_path),
        file_stage="Analyzing loudness",
        file_progress=0,
        file_elapsed_sec=0,
        file_duration_sec=duration_sec,
    )
    pass1 = run_ffmpeg_with_progress(
        [
            "ffmpeg", "-hide_banner", "-nostats", "-i", str(mp3_path),
            "-af", f"{SILENCE_FILTER},loudnorm=I=-16:TP=-1.5:LRA=11:print_format=json",
            "-f", "null", "-",
        ],
        duration_sec=duration_sec,
        progress_start=0.6,
        progress_end=0.75,
    )
    match = re.search(r"\{[^{}]*\"input_i\"[^{}]*\}", pass1.stderr, flags=re.DOTALL)
    if not match:
        raise RuntimeError(f"Could not parse loudnorm measurement for {mp3_path.name}")
    import json
    stats = json.loads(match.group(0))

    loudnorm_pass2 = (
        f"loudnorm=I=-16:TP=-1.5:LRA=11:"
        f"measured_I={stats['input_i']}:"
        f"measured_TP={stats['input_tp']}:"
        f"measured_LRA={stats['input_lra']}:"
        f"measured_thresh={stats['input_thresh']}:"
        f"offset={stats['target_offset']}:linear=true"
    )

    update_status(
        step="Normalizing and trimming audio",
        step_number=2,
        detail=f"Applying silence removal and loudness normalization to {mp3_path.name}",
        current_file=str(tmp_path),
        source_file=str(mp3_path),
        file_stage="Trimming and normalizing",
        file_progress=0,
        file_elapsed_sec=0,
        file_duration_sec=duration_sec,
    )
    try:
        run_ffmpeg_with_progress(
            [
                "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                "-i", str(mp3_path),
                "-af", f"{SILENCE_FILTER},{loudnorm_pass2}",
                "-c:a", "libmp3lame", "-b:a", "128k",
                str(tmp_path),
            ],
            duration_sec=duration_sec,
            progress_start=0.75,
            progress_end=1,
        )
        tmp_path.replace(mp3_path)
        update_status(
            current_file=str(mp3_path),
            source_file=None,
            file_stage="Complete",
            file_progress=100,
            file_elapsed_sec=duration_sec,
        )
    finally:
        if tmp_path.exists():
            tmp_path.unlink()


def download_and_convert(vid: str, url: str, fallback_title: str, fallback_index: int) -> dict:
    raw_template = WORK_DIR / f"{vid}.%(ext)s"
    mp3_path = EPISODES_DIR / f"{vid}.mp3"

    print(f"[{fallback_index}] Downloading audio for {vid} ({fallback_title})...", flush=True)
    yt_cmd = [
        "yt-dlp",
        "--no-playlist",
        "--no-warnings",
        "--remote-components",
        "ejs:github",
    ]
    if COOKIES_PATH.is_file() and COOKIES_PATH.stat().st_size > 0:
        yt_cmd.extend(["--cookies", str(COOKIES_PATH)])
    yt_cmd.extend([
        "-f",
        "bestaudio/best",
        "-o",
        str(raw_template),
        "--write-info-json",
        "--no-clean-info-json",
        url,
    ])
    meta_res = run_cmd(yt_cmd)
    time.sleep(5)
    _ = meta_res

    info_json_path = WORK_DIR / f"{vid}.info.json"
    info = {}
    if info_json_path.is_file():
        info = json.loads(info_json_path.read_text(encoding="utf-8"))

    raw_files = [
        p for p in WORK_DIR.glob(f"{vid}.*")
        if not p.name.endswith(".info.json") and not p.name.endswith(".part")
    ]
    if not raw_files:
        raise RuntimeError(f"No downloaded media file found for {vid}")
    raw_file = raw_files[0]

    duration_sec = info.get("duration")
    if not isinstance(duration_sec, (int, float)) or duration_sec <= 0:
        duration_sec = probe_mp3_duration(raw_file)

    episode_status = {}
    if STATUS_PATH.is_file():
        try:
            episode_status = json.loads(STATUS_PATH.read_text(encoding="utf-8")).get("episode") or {}
        except (json.JSONDecodeError, OSError):
            episode_status = {}
    episode_status["title"] = info.get("title") or fallback_title
    update_status(
        step="Encoding audio with FFmpeg",
        step_number=2,
        detail=f"Converting {raw_file.name} to MP3",
        episode=episode_status,
        current_file=str(mp3_path),
        source_file=str(raw_file),
        file_stage="Encoding MP3",
        file_progress=0,
        file_elapsed_sec=0,
        file_duration_sec=round(float(duration_sec), 1),
    )
    print(f"[{fallback_index}] Encoding MP3 via ffmpeg for {vid}...", flush=True)
    run_cmd([
        "ffmpeg",
        "-y",
        "-i",
        str(raw_file),
        "-vn",
        "-ac",
        "2",
        "-ar",
        "44100",
        "-c:a",
        "libmp3lame",
        "-b:a",
        "128k",
        str(mp3_path),
    ], ffmpeg_duration_sec=duration_sec, progress_start=0, progress_end=0.6)

    for f in raw_files:
        f.unlink(missing_ok=True)
    info_json_path.unlink(missing_ok=True)

    if not mp3_path.is_file() or mp3_path.stat().st_size == 0:
        raise RuntimeError(f"MP3 conversion failed for {vid}")

    upload_date = info.get("upload_date")
    if upload_date and re.fullmatch(r"\d{8}", str(upload_date)):
        dt = datetime.datetime.strptime(str(upload_date), "%Y%m%d").replace(
            hour=12, minute=0, second=0, tzinfo=datetime.timezone.utc
        )
    else:
        # Fallback: space episodes 7 days apart from today so order stays deterministic
        dt = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(
            days=max(0, (61 - fallback_index) * 7)
        )

    title = info.get("title") or fallback_title
    description = info.get("description") or title
    mp3_filename = f"{vid}.mp3"

    target_mp3 = EPISODES_DIR / f"{vid}.mp3"
    if target_mp3.is_file():
        normalize_and_trim_mp3(target_mp3)
        final_duration_sec = probe_mp3_duration(target_mp3)
        final_byte_length = target_mp3.stat().st_size
    else:
        final_duration_sec = int(duration_sec)
        final_byte_length = mp3_path.stat().st_size if mp3_path.is_file() else 0

    return {
        "id": vid,
        "url": url,
        "playlist_index": fallback_index,
        "title": title,
        "description": description,
        "pub_date": format_rfc2822(dt),
        "duration_sec": final_duration_sec,
        "filename": mp3_filename,
        "byte_length": final_byte_length,
    }


def generate_rss(episodes: list[dict], cover_file: Path | None) -> None:
    rss = ET.Element("rss", {
        "version": "2.0",
        "xmlns:atom": ATOM_NS,
    })
    channel = ET.SubElement(rss, "channel")

    ET.SubElement(channel, "title").text = PODCAST_TITLE
    ET.SubElement(channel, "link").text = BASE_URL
    ET.SubElement(channel, "description").text = PODCAST_DESCRIPTION
    ET.SubElement(channel, "language").text = PODCAST_LANGUAGE
    ET.SubElement(channel, "lastBuildDate").text = format_rfc2822(
        datetime.datetime.now(datetime.timezone.utc)
    )
    ET.SubElement(channel, "atom:link", {
        "href": f"{BASE_URL}/feed.xml",
        "rel": "self",
        "type": "application/rss+xml",
    })

    ET.SubElement(channel, f"{{{ITUNES_NS}}}author").text = PODCAST_AUTHOR
    ET.SubElement(channel, f"{{{ITUNES_NS}}}summary").text = PODCAST_DESCRIPTION
    ET.SubElement(channel, f"{{{ITUNES_NS}}}explicit").text = PODCAST_EXPLICIT

    owner = ET.SubElement(channel, f"{{{ITUNES_NS}}}owner")
    ET.SubElement(owner, f"{{{ITUNES_NS}}}name").text = PODCAST_OWNER_NAME
    ET.SubElement(owner, f"{{{ITUNES_NS}}}email").text = PODCAST_OWNER_EMAIL

    cat = ET.SubElement(channel, f"{{{ITUNES_NS}}}category", {"text": PODCAST_CATEGORY})
    if PODCAST_SUBCATEGORY:
        ET.SubElement(cat, f"{{{ITUNES_NS}}}category", {"text": PODCAST_SUBCATEGORY})

    if cover_file:
        cover_url = f"{BASE_URL}/{cover_file.name}"
        ET.SubElement(channel, f"{{{ITUNES_NS}}}image", {"href": cover_url})
        img = ET.SubElement(channel, "image")
        ET.SubElement(img, "url").text = cover_url
        ET.SubElement(img, "title").text = PODCAST_TITLE
        ET.SubElement(img, "link").text = BASE_URL

    ordered = list(reversed(episodes)) if NEWEST_FIRST else list(episodes)
    for ep in ordered:
        mp3_path = EPISODES_DIR / ep["filename"]
        if not mp3_path.is_file():
            continue

        item = ET.SubElement(channel, "item")
        ET.SubElement(item, "title").text = ep["title"]
        ET.SubElement(item, "description").text = ep["description"]
        ET.SubElement(item, "pubDate").text = ep["pub_date"]
        ET.SubElement(item, "guid", {"isPermaLink": "false"}).text = f"akers-podcast-{ep['id']}"
        ET.SubElement(item, "enclosure", {
            "url": f"{BASE_URL}/{ep['filename']}",
            "length": str(mp3_path.stat().st_size),
            "type": "audio/mpeg",
        })
        ET.SubElement(item, f"{{{ITUNES_NS}}}author").text = PODCAST_AUTHOR
        ET.SubElement(item, f"{{{ITUNES_NS}}}summary").text = ep["description"]
        ET.SubElement(item, f"{{{ITUNES_NS}}}duration").text = format_itunes_duration(
            ep.get("duration_sec")
        )
        ET.SubElement(item, f"{{{ITUNES_NS}}}explicit").text = PODCAST_EXPLICIT

    tree = ET.ElementTree(rss)
    ET.indent(tree, space="  ")
    tree.write(FEED_PATH, encoding="utf-8", xml_declaration=True)
    print(f"Wrote {FEED_PATH} with {len(ordered)} episodes.", flush=True)



def format_duration(seconds: int) -> str:
    if not seconds or seconds <= 0:
        return ""
    h, rem = divmod(int(seconds), 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def generate_index_html(episodes: list[dict], cover_file=None) -> None:
    def episode_sort_key(ep: dict) -> tuple[int, int]:
        title = ep.get("title", "")
        m = re.search(r"\bep(?:isode)?\s*:?\s*(\d+)\b", title, flags=re.IGNORECASE)
        ep_num = int(m.group(1)) if m else -1
        pl_idx = int(ep.get("playlist_index") or 0)
        return (ep_num, pl_idx)

    sorted_eps = sorted(episodes, key=episode_sort_key, reverse=True)[:5]
    cover_name = cover_file.name if cover_file is not None else "cover.jpg"
    cover_url = f"{BASE_URL}/{cover_name}"

    items_html = []
    for ep in sorted_eps:
        title = html.escape(ep.get("title", "Untitled Episode"))
        pub_date = html.escape(ep.get("pub_date", ""))
        duration = format_duration(ep.get("duration", 0))
        meta_parts = [part for part in (pub_date, duration) if part]
        meta_line = " &middot; ".join(meta_parts)
        audio_url = f"{BASE_URL}/{ep['id']}.mp3"
        items_html.append(f"""
        <article class="episode">
          <div class="ep-header">
            <h2>{title}</h2>
            <p class="meta">{meta_line}</p>
          </div>
          <audio controls preload="none" src="{audio_url}"></audio>
        </article>""")

    episodes_block = "\n".join(items_html) if items_html else "<p>No episodes published yet.</p>"

    page = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Akers Brothers Podcast</title>
  <link rel="alternate" type="application/rss+xml" title="Akers Brothers Podcast RSS" href="{BASE_URL}/feed.xml" />
  <style>
    :root {{
      --bg: #0f1115;
      --card: #181b22;
      --border: #262b36;
      --text: #f3f4f6;
      --muted: #9ca3af;
      --accent: #3b82f6;
      --accent-hover: #2563eb;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      background: var(--bg);
      color: var(--text);
      line-height: 1.5;
    }}
    .wrap {{
      max-width: 760px;
      margin: 0 auto;
      padding: 2.5rem 1.25rem 4rem;
    }}
    header {{
      display: flex;
      gap: 1.5rem;
      align-items: center;
      margin-bottom: 2.25rem;
      flex-wrap: wrap;
    }}
    .cover {{
      width: 148px;
      height: 148px;
      border-radius: 14px;
      object-fit: cover;
      border: 1px solid var(--border);
      flex-shrink: 0;
    }}
    h1 {{
      margin: 0 0 0.35rem;
      font-size: 1.85rem;
      letter-spacing: -0.02em;
    }}
    .sub {{
      margin: 0 0 1rem;
      color: var(--muted);
    }}
    .rss-row {{
      display: flex;
      gap: 0.6rem;
      flex-wrap: wrap;
      align-items: center;
    }}
    .btn {{
      display: inline-block;
      background: var(--accent);
      color: #fff;
      text-decoration: none;
      font-weight: 600;
      font-size: 0.9rem;
      padding: 0.55rem 0.95rem;
      border-radius: 8px;
    }}
    .btn:hover {{ background: var(--accent-hover); }}
    .feed-url {{
      font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
      font-size: 0.85rem;
      color: var(--muted);
      background: var(--card);
      border: 1px solid var(--border);
      padding: 0.45rem 0.7rem;
      border-radius: 8px;
      user-select: all;
    }}
    .section-label {{
      font-size: 0.8rem;
      text-transform: uppercase;
      letter-spacing: 0.08em;
      color: var(--muted);
      margin: 0 0 0.9rem;
    }}
    .episode {{
      background: var(--card);
      border: 1px solid var(--border);
      border-radius: 12px;
      padding: 1rem 1.15rem;
      margin-bottom: 0.9rem;
    }}
    .episode h2 {{
      margin: 0 0 0.25rem;
      font-size: 1.05rem;
    }}
    .meta {{
      margin: 0 0 0.75rem;
      font-size: 0.85rem;
      color: var(--muted);
    }}
    audio {{
      width: 100%;
      height: 38px;
    }}
  
    .btn {{ display: inline-block; padding: 10px 16px; border-radius: 8px; text-decoration: none; font-weight: 600; margin-right: 8px; }}
    .spotify-btn {{ background: #1DB954; color: #000000; }}
  </style>
</head>
<body>
  <main class="wrap">
    <header>
      <img class="cover" src="{cover_url}" alt="Akers Brothers Podcast cover art" />
      <div>
        <h1>Akers Brothers Podcast</h1>
        <p class="sub">Hosted by Jacob Akers</p>
        <div class="rss-row">
          <a class="btn apple-btn" href="https://podcasts.apple.com/us/podcast/akers-brothers-podcast/id6839635993" target="_blank" rel="noopener noreferrer">Listen on Apple Podcasts</a>
          <a class="btn spotify-btn" href="https://open.spotify.com/show/4FVzABybYzhL2vdjGGW6CP" target="_blank" rel="noopener noreferrer">Listen on Spotify</a>
          <a class="btn" href="{BASE_URL}/feed.xml">Subscribe via RSS</a>          
          <span class="feed-url">{BASE_URL}/feed.xml</span>
        </div>
      </div>
    </header>
    <p class="section-label">Latest 5 Episodes</p>
    {episodes_block}
  </main>
</body>
</html>
"""
    INDEX_PATH.write_text(page, encoding="utf-8")
    print(f"Wrote {INDEX_PATH} with {len(sorted_eps)} latest episodes.")


def sync_to_r2(cover_file: Path | None) -> None:
    print(f"Syncing episodes to {R2_REMOTE}...", flush=True)
    run_cmd(["rclone", "copy", str(EPISODES_DIR), R2_REMOTE, "--transfers", "4"])

    if cover_file:
        print(f"Uploading {cover_file.name} to {R2_REMOTE}...", flush=True)
        run_cmd(["rclone", "copyto", str(cover_file), f"{R2_REMOTE}/{cover_file.name}"])

    print(f"Uploading feed.xml to {R2_REMOTE}...", flush=True)
    run_cmd(["rclone", "copyto", str(FEED_PATH), f"{R2_REMOTE}/feed.xml"])
    if INDEX_PATH.is_file():
        print("Uploading index.html to r2:akers-podcast...")
        run_cmd([
            "rclone",
            "copyto",
            str(INDEX_PATH),
            "r2:akers-podcast/index.html",
            "--header-upload",
            "Content-Type: text/html; charset=utf-8",
        ])
    print("R2 sync complete.", flush=True)


def main() -> int:
    for d in (WORK_DIR, EPISODES_DIR, COVER_DIR, OUT_DIR):
        d.mkdir(parents=True, exist_ok=True)

    reset_status()
    failures = 0

    try:
        cover_file = find_cover_image()
        if not cover_file:
            print(
                "Warning: No cover image found in /opt/podcast/cover/. "
                "Place cover.png or cover.jpg there for best Apple/Spotify results.",
                file=sys.stderr,
            )

        update_status(
            step="Fetching playlist",
            step_number=1,
            progress=5,
            detail="Contacting YouTube for the configured playlist",
            episode=None,
        )
        entries = fetch_playlist_entries()
        if not entries:
            raise RuntimeError("No entries returned from playlist")

        update_status(
            step="Loading episode database",
            step_number=2,
            progress=10,
            detail=f"Found {len(entries)} playlist entries",
            episode=None,
        )

        db = load_db()
        processed_episodes = []

        for position, entry in enumerate(entries, start=1):
            vid = entry["id"]
            mp3_path = EPISODES_DIR / f"{vid}.mp3"
            episode_progress = 10 + int((position - 1) / len(entries) * 50)
            update_status(
                step="Processing episodes",
                step_number=2,
                progress=episode_progress,
                detail=f"Checking episode {position} of {len(entries)}: {entry['title']}",
                current_file=f"work/{vid}.*",
                source_file=None,
                file_stage="",
                file_progress=0,
                file_elapsed_sec=0,
                file_duration_sec=None,
                episode={
                    "id": vid,
                    "title": entry["title"],
                    "position": position,
                    "total": len(entries),
                },
                failures=failures,
            )

            if vid in db and mp3_path.is_file() and mp3_path.stat().st_size > 0:
                ep_rec = db[vid]
                ep_rec["playlist_index"] = entry["playlist_index"]
                ep_rec["byte_length"] = mp3_path.stat().st_size
                processed_episodes.append(ep_rec)
                update_status(
                    progress=10 + int(position / len(entries) * 50),
                    detail=f"Episode {position} of {len(entries)} already processed",
                    current_file=str(mp3_path),
                    source_file=None,
                    file_stage="Already processed",
                    file_progress=100,
                )
                continue

            try:
                ep_rec = download_and_convert(
                    vid=vid,
                    url=entry["url"],
                    fallback_title=entry["title"],
                    fallback_index=entry["playlist_index"],
                )
                db[vid] = ep_rec
                save_db(db)
                processed_episodes.append(ep_rec)
                update_status(
                    progress=10 + int(position / len(entries) * 50),
                    detail=f"Finished episode {position} of {len(entries)}",
                    file_progress=100,
                )
            except Exception as exc:
                failures += 1
                print(f"Error processing {vid} ({entry['title']}): {exc}", file=sys.stderr)
                update_status(
                    failures=failures,
                    detail=f"Episode {position} failed: {exc}",
                )
                if vid in db and mp3_path.is_file():
                    processed_episodes.append(db[vid])

        processed_episodes.sort(key=lambda x: x.get("playlist_index", 999999))

        update_status(
            step="Generating RSS feed",
            step_number=3,
            progress=65,
            detail=f"Writing feed.xml for {len(processed_episodes)} episodes",
            episode=None,
            failures=failures,
        )
        generate_rss(processed_episodes, cover_file)

        update_status(
            step="Generating web page",
            step_number=4,
            progress=75,
            detail="Writing index.html",
            episode=None,
        )
        generate_index_html(processed_episodes, cover_file)

        update_status(
            step="Uploading to Cloudflare R2",
            step_number=5,
            progress=85,
            detail="Syncing MP3 files, cover art, feed.xml, and index.html",
            episode=None,
            failures=failures,
        )
        sync_to_r2(cover_file)

        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        update_status(
            status="complete_with_errors" if failures else "complete",
            step="Complete",
            step_number=6,
            progress=100,
            detail=(
                f"Published {len(processed_episodes)} episodes with {failures} processing errors"
                if failures
                else f"Published {len(processed_episodes)} episodes"
            ),
            episode=None,
            failures=failures,
            completed_at=now,
        )

        print(f"Done. Total episodes in feed: {len(processed_episodes)} | New/retry errors: {failures}")
        return 0 if processed_episodes else 1

    except Exception as exc:
        update_status(
            status="error",
            detail=str(exc),
            failures=failures,
            episode=None,
            completed_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),
        )
        print(f"Build failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

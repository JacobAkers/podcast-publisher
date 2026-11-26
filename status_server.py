#!/usr/bin/env python3
import argparse
import datetime
import json
import os
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


def detect_active_processes() -> dict | None:
    """Detect build tools directly so the portal works before status.json is available."""
    for proc_dir in Path("/proc").glob("[0-9]*"):
        try:
            cmdline = (proc_dir / "cmdline").read_bytes().replace(b"\0", b" ").decode("utf-8", "replace").strip()
        except (OSError, PermissionError):
            continue
        if not cmdline:
            continue

        executable = cmdline.split()[0].rsplit("/", 1)[-1].lower()
        if executable == "ffmpeg":
            return {
                "step": "Processing audio with FFmpeg",
                "detail": f"FFmpeg is actively running (PID {proc_dir.name})",
                "tool": "ffmpeg",
                "pid": int(proc_dir.name),
            }
        if executable == "yt-dlp":
            return {
                "step": "Downloading with yt-dlp",
                "detail": f"yt-dlp is actively running (PID {proc_dir.name})",
                "tool": "yt-dlp",
                "pid": int(proc_dir.name),
            }
        if executable == "rclone":
            return {
                "step": "Uploading with rclone",
                "detail": f"rclone is actively running (PID {proc_dir.name})",
                "tool": "rclone",
                "pid": int(proc_dir.name),
            }
    return None


def read_last_logged_file(log_path: Path) -> dict | None:
    if not log_path.is_file():
        return None

    try:
        with log_path.open("rb") as log_file:
            log_file.seek(0, os.SEEK_END)
            size = log_file.tell()
            log_file.seek(max(0, size - 65536))
            log_text = log_file.read().decode("utf-8", "replace")
    except OSError:
        return None

    pattern = re.compile(
        r"^\[(?P<position>\d+)\]\s+"
        r"(?P<action>Downloading audio|Encoding MP3 via ffmpeg)"
        r"\s+for\s+(?P<video_id>[A-Za-z0-9_-]+)"
        r"(?:\s+\((?P<title>.*)\))?"
    )
    latest = None
    for line in log_text.splitlines():
        match = pattern.search(line)
        if match:
            latest = match
    if latest is None:
        return None

    video_id = latest.group("video_id")
    action = latest.group("action")
    is_encoding = action.startswith("Encoding")
    current_file = f"episodes/{video_id}.mp3" if is_encoding else f"work/{video_id}.*"
    source_file = None
    if is_encoding:
        source_candidates = sorted(
            candidate
            for candidate in log_path.parent.glob(f"{video_id}.*")
            if not candidate.name.endswith((".info.json", ".part"))
        )
        if source_candidates:
            source_file = str(source_candidates[-1])

    return {
        "current_file": current_file,
        "source_file": source_file,
        "file_stage": "Encoding MP3" if is_encoding else "Downloading media",
        "log_episode": {
            "id": video_id,
            "title": latest.group("title") or video_id,
            "position": int(latest.group("position")),
        },
        "log_updated_at": datetime.datetime.fromtimestamp(
            log_path.stat().st_mtime, datetime.timezone.utc
        ).isoformat(),
    }


def load_status(path: Path, log_path: Path) -> dict:
    active_process = detect_active_processes()
    logged_file = read_last_logged_file(log_path)
    if not path.is_file():
        if active_process:
            status = {
                "status": "running",
                "step": active_process["step"],
                "step_number": 2,
                "total_steps": 6,
                "progress": 35,
                "detail": active_process["detail"],
                "episode": None,
                "failures": 0,
                "started_at": None,
                "updated_at": None,
                "completed_at": None,
                "active_tool": active_process["tool"],
                "active_pid": active_process["pid"],
            }
            if logged_file:
                status.update(logged_file)
            return status
        status = {
            "status": "idle",
            "step": "Waiting for build",
            "step_number": 0,
            "total_steps": 6,
            "progress": 0,
            "detail": "No build status file has been created yet",
            "episode": None,
            "failures": 0,
            "started_at": None,
            "updated_at": None,
            "completed_at": None,
        }
        if logged_file:
            status.update(logged_file)
        return status

    try:
        status = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        status = {
            "status": "error",
            "step": "Status unavailable",
            "step_number": 0,
            "total_steps": 6,
            "progress": 0,
            "detail": "The status file could not be read",
            "episode": None,
            "failures": 0,
        }

    if status.get("status") == "running" and status.get("updated_at"):
        try:
            updated = datetime.datetime.fromisoformat(status["updated_at"])
            age = datetime.datetime.now(datetime.timezone.utc) - updated
            if age.total_seconds() > 1800:
                status["status"] = "stale"
                status["detail"] = "The build has not reported progress for over 30 minutes"
        except (TypeError, ValueError):
            pass

    if active_process:
        if status.get("status") in ("running", "stale"):
            status["status"] = "running"
            status.setdefault("step", active_process["step"])
            status.setdefault("detail", active_process["detail"])
        status["active_tool"] = active_process["tool"]
        status["active_pid"] = active_process["pid"]

    if logged_file:
        for key, value in logged_file.items():
            status.setdefault(key, value)

    return status


PAGE = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Podcast Build Status</title>
  <style>
    :root { color-scheme: dark; --bg:#0f1115; --card:#181b22; --border:#303644; --text:#f3f4f6; --muted:#a5adba; --blue:#3b82f6; --green:#22c55e; --red:#ef4444; --amber:#f59e0b; }
    * { box-sizing: border-box; }
    body { margin:0; font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; background:var(--bg); color:var(--text); }
    main { max-width:760px; margin:auto; padding:3rem 1.25rem; }
    h1 { margin:0 0 .35rem; }
    #summary { color:var(--muted); margin:0 0 1.5rem; }
    .card { background:var(--card); border:1px solid var(--border); border-radius:12px; padding:1.25rem; }
    .status-row { display:flex; justify-content:space-between; align-items:center; gap:1rem; }
    .badge { border-radius:999px; padding:.32rem .65rem; font-size:.78rem; font-weight:700; text-transform:uppercase; }
    .running { background:#1d4ed8; } .complete { background:#15803d; } .complete_with_errors { background:#b45309; }
    .error { background:#b91c1c; } .stale { background:#b45309; } .idle { background:#475569; }
    .track { height:.72rem; margin:1.25rem 0 .55rem; background:#252a35; border-radius:999px; overflow:hidden; }
    #progress { height:100%; width:0; background:var(--blue); transition:width .25s ease; }
    .file-track { height:.42rem; margin:.6rem 0 .35rem; background:#252a35; border-radius:999px; overflow:hidden; }
    #fileProgress { height:100%; width:0; background:#22c55e; transition:width .25s ease; }
    #progress.complete { background:var(--green); } #progress.complete_with_errors,#progress.error,#progress.stale { background:var(--amber); }
    #progress.error { background:var(--red); }
    .meta { display:grid; grid-template-columns:130px 1fr; gap:.5rem; margin-top:1.25rem; }
    .label { color:var(--muted); } code { font-size:.86rem; overflow-wrap:anywhere; }
  </style>
</head>
<body>
<main>
  <h1>Podcast Build Status</h1>
  <p id="summary">Checking status…</p>
  <section class="card">
    <div class="status-row">
      <strong id="step">Waiting</strong>
      <span id="badge" class="badge idle">idle</span>
    </div>
    <div class="track"><div id="progress"></div></div>
    <div class="label"><span id="percent">0%</span> · <span id="stepNumber">Step 0 of 6</span></div>
    <div class="meta">
      <span class="label">Detail</span><code id="detail">No build running</code>
      <span class="label">Episode</span><code id="episode">—</code>
      <span class="label">Current file</span><code id="currentFile">—</code>
      <span class="label">Source file</span><code id="sourceFile">—</code>
      <span class="label">FFmpeg progress</span><div><div class="file-track"><div id="fileProgress"></div></div><code id="fileProgressText">—</code></div>
      <span class="label">Failures</span><code id="failures">0</code>
      <span class="label">Active tool</span><code id="activeTool">—</code>
      <span class="label">Updated</span><code id="updated">—</code>
    </div>
  </section>
</main>
<script>
async function refresh() {
  try {
    const response = await fetch('/api/status', {cache:'no-store'});
    if (!response.ok) throw new Error('Status request failed');
    const s = await response.json();
    const state = s.status || 'idle';
    const progress = Number(s.progress || 0);
    document.getElementById('summary').textContent = 'Akers Brothers Podcast publishing pipeline';
    document.getElementById('step').textContent = s.step || 'Waiting';
    const badge = document.getElementById('badge');
    badge.className = 'badge ' + state;
    badge.textContent = state.replaceAll('_', ' ');
    const bar = document.getElementById('progress');
    bar.style.width = Math.max(0, Math.min(100, progress)) + '%';
    bar.className = state;
    document.getElementById('percent').textContent = progress + '%';
    document.getElementById('stepNumber').textContent = 'Step ' + (s.step_number || 0) + ' of ' + (s.total_steps || 6);
    document.getElementById('detail').textContent = s.detail || '—';
    const ep = s.episode;
    document.getElementById('episode').textContent = ep ? ((ep.position || '?') + '/' + (ep.total || '?') + ' · ' + (ep.title || ep.id || '—')) : '—';
    const logEpisode = s.episode || s.log_episode;
    document.getElementById('currentFile').textContent = s.current_file ? (s.current_file + (logEpisode ? ' · ' + s.file_stage + ' · #' + logEpisode.position + ' ' + logEpisode.title : '')) : '—';
    document.getElementById('sourceFile').textContent = s.source_file || '—';
    const fileProgress = Number(s.file_progress);
    const hasFileProgress = typeof s.file_progress === 'number' && Boolean(s.file_stage);
    const fileBar = document.getElementById('fileProgress');
    fileBar.style.width = hasFileProgress && Number.isFinite(fileProgress) ? Math.max(0, Math.min(100, fileProgress)) + '%' : '0%';
    const elapsed = Number(s.file_elapsed_sec);
    const duration = Number(s.file_duration_sec);
    const formatTime = seconds => {
      if (!Number.isFinite(seconds)) return '—';
      const total = Math.max(0, Math.floor(seconds));
      const hours = Math.floor(total / 3600);
      const minutes = Math.floor((total % 3600) / 60);
      const secs = total % 60;
      return hours ? hours + ':' + String(minutes).padStart(2, '0') + ':' + String(secs).padStart(2, '0') : minutes + ':' + String(secs).padStart(2, '0');
    };
    document.getElementById('fileProgressText').textContent = hasFileProgress && Number.isFinite(fileProgress)
      ? ((s.file_stage || 'FFmpeg') + ': ' + fileProgress.toFixed(1) + '% · ' + formatTime(elapsed) + ' / ' + formatTime(duration))
      : 'Waiting for FFmpeg progress';
    document.getElementById('failures').textContent = s.failures || 0;
    document.getElementById('activeTool').textContent = s.active_tool ? (s.active_tool + (s.active_pid ? ' (PID ' + s.active_pid + ')' : '')) : '—';
    document.getElementById('updated').textContent = s.updated_at ? new Date(s.updated_at).toLocaleString() : '—';
  } catch (error) {
    document.getElementById('summary').textContent = 'Status service unavailable';
  }
}
refresh();
setInterval(refresh, 2000);
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    status_path = Path("status.json")
    log_path = Path("last_run.log")

    def send_bytes(self, status_code: int, content_type: str, body: bytes) -> None:
        self.send_response(status_code)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'unsafe-inline'; script-src 'unsafe-inline'")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path == "/api/status":
            body = json.dumps(load_status(self.status_path, self.log_path), ensure_ascii=False).encode("utf-8")
            self.send_bytes(200, "application/json; charset=utf-8", body)
        elif self.path in ("/", "/index.html"):
            self.send_bytes(200, "text/html; charset=utf-8", PAGE.encode("utf-8"))
        else:
            self.send_bytes(404, "text/plain; charset=utf-8", b"Not found")

    def log_message(self, format: str, *args) -> None:
        print("%s - %s" % (self.address_string(), format % args), flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="Serve Akers Brothers Podcast build status")
    parser.add_argument("--host", default=os.environ.get("STATUS_HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("STATUS_PORT", "8765")))
    parser.add_argument("--status-file", default=os.environ.get("STATUS_FILE", "/opt/podcast/work/status.json"))
    parser.add_argument("--log-file", default=os.environ.get("LOG_FILE", "/opt/podcast/work/last_run.log"))
    args = parser.parse_args()

    Handler.status_path = Path(args.status_file)
    Handler.log_path = Path(args.log_file)
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"Podcast status interface listening on http://{args.host}:{args.port} ({args.status_file}, {args.log_file})", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

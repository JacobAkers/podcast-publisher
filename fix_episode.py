#!/usr/bin/env python3
import argparse
from datetime import datetime
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, "/opt/podcast")
import build_feed  # noqa: E402


class TerminalProgress:
    def __init__(self, status_path: Path, label: str) -> None:
        self.status_path = status_path
        self.label = label
        self.started_at = time.monotonic()
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.is_tty = sys.stderr.isatty()
        self.use_color = self.is_tty and "NO_COLOR" not in os.environ
        self.frames = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")

    def __enter__(self) -> "TerminalProgress":
        print(f"{self.label}...", file=sys.stderr, flush=True)
        self.thread.start()
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.stop_event.set()
        self.thread.join()
        if self.is_tty:
            print(file=sys.stderr, flush=True)
        elif exc_type is None:
            print(f"Finished {self.label.lower()}.", file=sys.stderr, flush=True)

    def _read_status(self) -> dict:
        try:
            status = json.loads(self.status_path.read_text(encoding="utf-8"))
            return status if isinstance(status, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def _run(self) -> None:
        frame_index = 0
        last_log_at = 0.0
        while not self.stop_event.wait(0.15 if self.is_tty else 1):
            status = self._read_status()
            stage = status.get("file_stage") or status.get("step") or self.label
            detail = status.get("detail") or ""
            progress = status.get("file_progress")
            try:
                progress = float(progress)
            except (TypeError, ValueError):
                progress = None

            elapsed = int(time.monotonic() - self.started_at)
            freshness = ""
            try:
                updated_at = datetime.fromisoformat(status["updated_at"]).timestamp()
                freshness = f" · update {max(0, int(time.time() - updated_at))}s ago"
            except (KeyError, TypeError, ValueError, OSError):
                pass

            if self.is_tty:
                self._draw(frame_index, stage, detail, progress, elapsed, freshness)
                frame_index += 1
            elif time.monotonic() - last_log_at >= 5:
                percent = f" · {progress:.1f}%" if progress is not None else ""
                print(
                    f"{stage}{percent} · elapsed {self._format_time(elapsed)}{freshness}",
                    file=sys.stderr,
                    flush=True,
                )
                last_log_at = time.monotonic()

    def _draw(
        self,
        frame_index: int,
        stage: str,
        detail: str,
        progress: float | None,
        elapsed: int,
        freshness: str,
    ) -> None:
        spinner = self.frames[frame_index % len(self.frames)]
        if progress is None:
            bar = "░" * 24
            percent_text = "working"
        else:
            progress = min(100, max(0, progress))
            filled = round(progress / 100 * 24)
            bar = "█" * filled + "░" * (24 - filled)
            percent_text = f"{progress:5.1f}%"
        color_start, color_end = ("\033[36m", "\033[0m") if self.use_color else ("", "")
        message = (
            f"\r{color_start}{spinner} {stage:<25.25} "
            f"[{bar}] {percent_text:>7} · {self._format_time(elapsed)}{freshness}{color_end}"
        )
        if detail and not self.use_color:
            message += f" · {detail[:60]}"
        print(message, end="", file=sys.stderr, flush=True)

    @staticmethod
    def _format_time(seconds: int) -> str:
        minutes, seconds = divmod(seconds, 60)
        hours, minutes = divmod(minutes, 60)
        return f"{hours:02}:{minutes:02}:{seconds:02}" if hours else f"{minutes:02}:{seconds:02}"


def copy_mp3_with_progress(src_mp3: Path, dest_mp3: Path) -> None:
    if src_mp3.resolve() == dest_mp3.resolve():
        build_feed.update_status(
            step="Preparing replacement MP3",
            detail="Using the existing episode MP3 as the input",
            file_stage="Using existing MP3",
            file_progress=100,
        )
        return

    total_bytes = src_mp3.stat().st_size
    copied_bytes = 0
    last_update = 0.0
    build_feed.update_status(
        step="Copying replacement MP3",
        detail=f"Copying {src_mp3.name} to the episode directory",
        file_stage="Copying source MP3",
        file_progress=0,
        file_elapsed_sec=0,
        file_duration_sec=None,
    )
    with src_mp3.open("rb") as source, dest_mp3.open("wb") as destination:
        while chunk := source.read(1024 * 1024):
            destination.write(chunk)
            copied_bytes += len(chunk)
            now = time.monotonic()
            if now - last_update >= 0.25 or copied_bytes == total_bytes:
                progress = copied_bytes / total_bytes * 100 if total_bytes else 100
                build_feed.update_status(
                    detail=(
                        f"Copied {copied_bytes / (1024 * 1024):.1f} / "
                        f"{total_bytes / (1024 * 1024):.1f} MiB"
                    ),
                    file_progress=progress,
                )
                last_update = now


def find_state_file() -> Path:
    script_dir = Path(__file__).resolve().parent
    for candidate in (
        Path("/opt/podcast/work/episodes.json"),
        Path("/opt/podcast/episodes.json"),
        script_dir / "work" / "episodes.json",
        script_dir / "episodes.json",
    ):
        if candidate.is_file():
            return candidate
    raise FileNotFoundError(
        "Could not locate episodes.json in /opt/podcast or next to this script"
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Replace an episode MP3, trim/normalize audio, and update/sync the podcast feed."
    )
    target_group = parser.add_mutually_exclusive_group(required=True)
    target_group.add_argument("--vid", help="YouTube video ID of the episode to replace")
    target_group.add_argument(
        "--latest",
        action="store_true",
        help="Target the highest playlist_index episode in episodes.json",
    )
    parser.add_argument(
        "--mp3",
        required=True,
        help="Path to the newly rendered MP3 file",
    )
    parser.add_argument(
        "--no-publish",
        action="store_true",
        help="Update the MP3 and episodes.json only; skip running build_feed.py / R2 sync",
    )
    args = parser.parse_args()

    src_mp3 = Path(args.mp3)
    if not src_mp3.is_file():
        raise FileNotFoundError(f"MP3 not found: {src_mp3}")
    if src_mp3.suffix.lower() != ".mp3":
        raise ValueError(f"Expected an .mp3 file, got: {src_mp3}")

    state_path = find_state_file()
    podcast_root = state_path.parent.parent if state_path.parent.name == "work" else state_path.parent
    build_feed.BASE_DIR = podcast_root
    build_feed.WORK_DIR = podcast_root / "work"
    build_feed.EPISODES_DIR = podcast_root / "episodes"
    build_feed.STATUS_PATH = build_feed.WORK_DIR / "status.json"

    raw_episodes = json.loads(state_path.read_text(encoding="utf-8"))
    if not raw_episodes:
        raise RuntimeError(f"No episodes found in {state_path}")

    if isinstance(raw_episodes, dict):
        episodes = raw_episodes
        episode_items = []
        for key, val in episodes.items():
            if not isinstance(val, dict):
                raise RuntimeError(f"Invalid episode record for {key} in {state_path}")
            episode_items.append((key, val))
    elif isinstance(raw_episodes, list):
        episode_items = []
        episodes = {}
        for val in raw_episodes:
            if not isinstance(val, dict) or not val.get("id"):
                raise RuntimeError(f"Invalid episode record in {state_path}")
            vid = val["id"]
            episode_items.append((vid, val))
            episodes[vid] = val
    else:
        raise RuntimeError(f"Episodes state must be a JSON object or array: {state_path}")

    if args.latest:
        ep_key, ep = max(
            episode_items,
            key=lambda item: item[1].get("playlist_index")
            if isinstance(item[1].get("playlist_index"), (int, float))
            else 0,
        )
    else:
        matches = [(key, val) for key, val in episode_items if key == args.vid]
        if not matches:
            raise KeyError(f"Video ID not found in {state_path}: {args.vid}")
        ep_key, ep = matches[0]

    vid = ep_key
    dest_mp3 = build_feed.EPISODES_DIR / f"{vid}.mp3"
    dest_mp3.parent.mkdir(parents=True, exist_ok=True)

    with TerminalProgress(build_feed.STATUS_PATH, f"Replacing episode {vid}"):
        copy_mp3_with_progress(src_mp3, dest_mp3)
        build_feed.normalize_and_trim_mp3(dest_mp3)

    ep["id"] = vid
    ep["filename"] = dest_mp3.name
    ep["duration_sec"] = build_feed.probe_mp3_duration(dest_mp3)
    ep["byte_length"] = dest_mp3.stat().st_size

    state_path.write_text(json.dumps(episodes, indent=2), encoding="utf-8")
    print(
        f"Updated {dest_mp3} | title={ep.get('title')} | "
        f"duration_sec={ep['duration_sec']} | byte_length={ep['byte_length']}"
    )

    if not args.no_publish:
        build_script = podcast_root / "build_feed.py"
        if not build_script.is_file():
            raise FileNotFoundError(f"Build script not found: {build_script}")
        subprocess.run(
            [sys.executable, str(build_script)],
            cwd=str(podcast_root),
            check=True,
        )


if __name__ == "__main__":
    main()

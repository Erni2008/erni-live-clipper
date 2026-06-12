from __future__ import annotations

import json
import os
import platform
import queue
import re
import shutil
import signal
import ssl
import subprocess
import sys
import threading
import time
import urllib.request
import csv
import ctypes
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
import tkinter as tk


APP_TITLE = "ERNI Live Clipper"
APP_VERSION = "1.0.0"
QUALITY_SELECTOR = "bestvideo*+bestaudio/best"
CAPTURE_QUALITY_SELECTOR = (
    "bestvideo[height<=1080][vcodec^=avc1]+bestaudio[acodec^=mp4a]/"
    "bestvideo[height<=1080]+bestaudio/"
    "best[height<=1080]/best"
)
FORMAT_SORT = "res,fps,hdr:12,vcodec,br"
YOUTUBE_EXTRACTOR_ARGS = "youtube:player_client=default,web,ios"
EXPORT_MODES = ("Universal Editing MP4", "Original Fast")
DASH_FRAGMENT_SECONDS = 5
LIVE_EDGE_SAFETY_SECONDS = 0
QUICK_CLIPS = (
    ("30s", 30),
    ("60s", 60),
    ("3m", 180),
    ("5m", 300),
)
TAG_PRESETS = ("funny", "rage", "fail", "win", "shorts", "important")


def four_char_code(value: str) -> int:
    return int.from_bytes(value.encode("mac_roman"), "big")


class CarbonEventTypeSpec(ctypes.Structure):
    _fields_ = [("eventClass", ctypes.c_uint32), ("eventKind", ctypes.c_uint32)]


class CarbonEventHotKeyID(ctypes.Structure):
    _fields_ = [("signature", ctypes.c_uint32), ("id", ctypes.c_uint32)]

EXTRA_TOOL_DIRS = (
    "/opt/homebrew/bin",
    "/usr/local/bin",
    "/usr/bin",
    "/bin",
    "/opt/local/bin",
)


@dataclass
class MediaReport:
    path: Path
    has_video: bool
    has_audio: bool
    video_codec: str | None = None
    audio_codec: str | None = None
    width: int | None = None
    height: int | None = None
    fps: float | None = None
    duration: float | None = None
    compatible: bool = False
    message: str = ""


@dataclass
class ClipJob:
    source_type: str
    start: float
    end: float
    label: str
    tag: str
    comment: str
    mode: str
    root_dir: Path
    output_dir: Path
    url: str = ""
    source: Path | None = None

    @property
    def range_label(self) -> str:
        return f"{format_timecode(self.start)} - {format_timecode(self.end)}"


def ensure_tool_path() -> None:
    current = os.environ.get("PATH", "").split(os.pathsep)
    merged = current[:]
    for item in EXTRA_TOOL_DIRS:
        if item not in merged:
            merged.append(item)
    os.environ["PATH"] = os.pathsep.join(path for path in merged if path)


def find_executable(name: str) -> str | None:
    ensure_tool_path()
    base_dir = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    extensions = [""]
    if platform.system() == "Windows":
        extensions = [".exe", ".cmd", ".bat", ""]

    if platform.system() == "Windows":
        exe_dir = Path(sys.executable).parent
        for directory in (exe_dir / "tools", exe_dir, base_dir / "tools"):
            for extension in extensions:
                candidate = directory / f"{name}{extension}"
                if candidate.exists() and os.access(candidate, os.X_OK):
                    return str(candidate)

    for extension in extensions:
        bundled = base_dir / f"{name}{extension}"
        if bundled.exists() and os.access(bundled, os.X_OK):
            return str(bundled)

    found = shutil.which(name)
    if found:
        return found

    for directory in EXTRA_TOOL_DIRS:
        for extension in extensions:
            candidate = Path(directory) / f"{name}{extension}"
            if candidate.exists() and os.access(candidate, os.X_OK):
                return str(candidate)
    return None


def open_path(path: Path) -> None:
    if platform.system() == "Darwin":
        subprocess.Popen(["open", str(path)])
    elif platform.system() == "Windows":
        os.startfile(str(path))  # type: ignore[attr-defined]
    else:
        subprocess.Popen(["xdg-open", str(path)])


def windows_process_flags(*, new_process_group: bool = False) -> int:
    if os.name != "nt":
        return 0
    flags = int(getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if new_process_group:
        flags |= int(getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
    return flags


def parse_timecode(value: str) -> float:
    text = value.strip().replace(",", ".")
    if not text:
        raise ValueError("Пустой таймкод.")
    parts = text.split(":")
    if len(parts) > 3:
        raise ValueError("Формат таймкода: SS, MM:SS или HH:MM:SS.")
    try:
        numbers = [float(part) for part in parts]
    except ValueError as exc:
        raise ValueError("Таймкод должен состоять из чисел.") from exc
    if any(number < 0 for number in numbers):
        raise ValueError("Таймкод не может быть отрицательным.")
    if len(numbers) == 1:
        return numbers[0]
    if len(numbers) == 2:
        return numbers[0] * 60 + numbers[1]
    return numbers[0] * 3600 + numbers[1] * 60 + numbers[2]


def format_timecode(seconds: float | int) -> str:
    total = max(0, int(seconds))
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def safe_name(value: str, fallback: str = "clip") -> str:
    text = re.sub(r"[^\w\-а-яА-ЯёЁ]+", "_", value.strip(), flags=re.UNICODE)
    text = re.sub(r"_+", "_", text).strip("_")
    return text[:80] or fallback


def clip_day_folder(now: datetime | None = None) -> str:
    return (now or datetime.now()).strftime("%Y-%m-%d")


def unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    for index in range(1, 10_000):
        candidate = path.with_name(f"{path.stem}-{index}{path.suffix}")
        if not candidate.exists():
            return candidate
    raise RuntimeError(f"Не удалось создать уникальное имя для {path}")


def bundled_path(name: str) -> Path:
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).parent))
    return base / name


def run_logged(command: list[str], on_log, cancel_event: threading.Event | None = None) -> int:
    creationflags = windows_process_flags(new_process_group=True)
    popen_kwargs: dict[str, object] = {}
    if os.name != "nt":
        popen_kwargs["preexec_fn"] = os.setsid

    on_log("$ " + " ".join(f'"{item}"' if " " in item else item for item in command) + "\n")
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        creationflags=creationflags,
        **popen_kwargs,
    )

    assert process.stdout is not None
    for line in process.stdout:
        on_log(line)
        if cancel_event and cancel_event.is_set():
            terminate_process(process)
            break
    return process.wait()


def terminate_process(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    try:
        if os.name == "nt":
            process.terminate()
        else:
            os.killpg(os.getpgid(process.pid), signal.SIGTERM)
    except OSError:
        process.terminate()
    try:
        process.wait(timeout=6)
    except subprocess.TimeoutExpired:
        try:
            if os.name == "nt":
                process.kill()
            else:
                os.killpg(os.getpgid(process.pid), signal.SIGKILL)
        except OSError:
            process.kill()


def probe_media(path: Path) -> MediaReport:
    ffprobe = find_executable("ffprobe")
    if not ffprobe:
        return MediaReport(path, False, False, message="ffprobe не найден.")
    command = [
        ffprobe,
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        str(path),
    ]
    completed = subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if completed.returncode != 0:
        return MediaReport(path, False, False, message=completed.stderr.strip())
    try:
        data = json.loads(completed.stdout)
    except json.JSONDecodeError:
        return MediaReport(path, False, False, message="ffprobe вернул нечитаемый ответ.")

    video = None
    audio = None
    for stream in data.get("streams", []):
        if stream.get("codec_type") == "video" and video is None:
            video = stream
        elif stream.get("codec_type") == "audio" and audio is None:
            audio = stream

    report = MediaReport(
        path=path,
        has_video=video is not None,
        has_audio=audio is not None,
        video_codec=video.get("codec_name") if video else None,
        audio_codec=audio.get("codec_name") if audio else None,
        width=video.get("width") if video else None,
        height=video.get("height") if video else None,
        duration=_safe_float(data.get("format", {}).get("duration")),
    )
    if video:
        report.fps = _parse_fps(video.get("avg_frame_rate") or video.get("r_frame_rate"))
    issues: list[str] = []
    if not report.has_video:
        issues.append("нет видео")
    if not report.has_audio:
        issues.append("нет звука")
    if report.video_codec not in {None, "h264"}:
        issues.append(f"video codec {report.video_codec}")
    if report.audio_codec not in {None, "aac"}:
        issues.append(f"audio codec {report.audio_codec}")
    report.compatible = report.has_video and report.has_audio and not issues
    report.message = "Совместимый MP4: H.264 + AAC." if report.compatible else "Проверено: " + "; ".join(issues)
    return report


def media_duration(path: Path) -> float | None:
    ffprobe = find_executable("ffprobe")
    if not ffprobe or not path.exists() or path.stat().st_size == 0:
        return None
    command = [
        ffprobe,
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(path),
    ]
    completed = subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if completed.returncode != 0:
        return None
    return _safe_float(completed.stdout.strip())


def ffmpeg_input_options(headers: dict[str, object] | None) -> list[str]:
    if not headers:
        return []
    options: list[str] = []
    user_agent = headers.get("User-Agent") or headers.get("user-agent")
    if user_agent:
        options.extend(["-user_agent", str(user_agent)])
    header_lines = []
    for key, value in headers.items():
        if value is None or str(key).lower() == "user-agent":
            continue
        header_lines.append(f"{key}: {value}")
    if header_lines:
        options.extend(["-headers", "\r\n".join(header_lines) + "\r\n"])
    return options


def media_inputs_from_info(info: dict[str, object]) -> list[tuple[str, dict[str, object]]]:
    requested = info.get("requested_formats")
    if isinstance(requested, list) and requested:
        inputs: list[tuple[str, dict[str, object]]] = []
        for item in requested[:2]:
            if not isinstance(item, dict):
                continue
            media_url = item.get("url")
            if not isinstance(media_url, str) or not media_url:
                continue
            headers = item.get("http_headers")
            if not isinstance(headers, dict):
                headers = info.get("http_headers")
            inputs.append((media_url, headers if isinstance(headers, dict) else {}))
        if inputs:
            return inputs

    media_url = info.get("url")
    if isinstance(media_url, str) and media_url:
        headers = info.get("http_headers")
        return [(media_url, headers if isinstance(headers, dict) else {})]
    return []


def capture_inputs_from_info(info: dict[str, object]) -> tuple[
    list[tuple[str, dict[str, object]]],
    dict[str, object] | None,
    dict[str, object] | None,
]:
    requested = info.get("requested_formats")
    video_format: dict[str, object] | None = None
    audio_format: dict[str, object] | None = None
    combined_format: dict[str, object] | None = None

    if isinstance(requested, list):
        for item in requested:
            if not isinstance(item, dict):
                continue
            media_url = item.get("url")
            if not isinstance(media_url, str) or not media_url:
                continue
            has_video = item.get("vcodec") not in {None, "none"}
            has_audio = item.get("acodec") not in {None, "none"}
            if has_video and has_audio and combined_format is None:
                combined_format = item
            elif has_video and video_format is None:
                video_format = item
            elif has_audio and audio_format is None:
                audio_format = item

    if not video_format and not audio_format and combined_format:
        video_format = combined_format

    inputs: list[tuple[str, dict[str, object]]] = []
    for item in (video_format, audio_format):
        if not item:
            continue
        media_url = item.get("url")
        if not isinstance(media_url, str) or not media_url:
            continue
        headers = item.get("http_headers")
        if not isinstance(headers, dict):
            headers = info.get("http_headers")
        inputs.append((media_url, headers if isinstance(headers, dict) else {}))

    if inputs:
        return inputs, video_format, audio_format

    media_url = info.get("url")
    if isinstance(media_url, str) and media_url:
        headers = info.get("http_headers")
        return [(media_url, headers if isinstance(headers, dict) else {})], info, None
    return [], None, None


def dash_fragment_duration(format_info: dict[str, object]) -> float:
    base_url = str(format_info.get("fragment_base_url") or format_info.get("url") or "")
    match = re.search(r"/dur/(\d+(?:\.\d+)?)", base_url)
    if match:
        return max(0.1, float(match.group(1)))
    return DASH_FRAGMENT_SECONDS


def _format_float(format_info: dict[str, object], key: str) -> float:
    value = _safe_float(format_info.get(key))
    return value if value is not None else 0.0


def _format_int(format_info: dict[str, object], key: str) -> int:
    value = _safe_float(format_info.get(key))
    return int(value or 0)


def _format_height(format_info: dict[str, object]) -> int:
    height = _format_int(format_info, "height")
    if height:
        return height
    for key in ("resolution", "format_note", "format"):
        value = format_info.get(key)
        if not isinstance(value, str):
            continue
        matches = re.findall(r"(?<!\d)(\d{3,4})p(?:\d+)?(?!\d)|x(\d{3,4})(?!\d)", value)
        heights = [int(left or right) for left, right in matches if left or right]
        if heights:
            return max(heights)
    return 0


def _format_width(format_info: dict[str, object]) -> int:
    width = _format_int(format_info, "width")
    if width:
        return width
    resolution = format_info.get("resolution")
    if isinstance(resolution, str):
        match = re.search(r"(?<!\d)(\d{3,5})x\d{3,4}(?!\d)", resolution)
        if match:
            return int(match.group(1))
    return 0


def _has_dash_fragments(format_info: dict[str, object]) -> bool:
    return isinstance(format_info.get("fragment_base_url"), str) and bool(format_info.get("fragment_base_url"))


def _format_summary(format_info: dict[str, object]) -> str:
    width = _format_width(format_info)
    height = _format_height(format_info)
    fps = _format_float(format_info, "fps")
    tbr = _format_float(format_info, "tbr")
    format_id = format_info.get("format_id") or "unknown"
    ext = format_info.get("ext") or "?"
    codec = format_info.get("vcodec") if format_info.get("vcodec") != "none" else format_info.get("acodec")
    note = format_info.get("format_note") or format_info.get("resolution") or ""
    size = f"{width}x{height}" if width and height else f"{height}p" if height else "audio"
    fps_text = f" {fps:g}fps" if fps else ""
    br_text = f" {tbr:g}k" if tbr else ""
    note_text = f" note={note}" if note else ""
    return f"id={format_id} {ext} {size}{fps_text}{br_text} codec={codec}{note_text}"


def live_video_candidate_summaries(info: dict[str, object], limit: int = 8) -> list[str]:
    formats = info.get("formats")
    if not isinstance(formats, list):
        return []
    candidates = [
        item
        for item in formats
        if isinstance(item, dict)
        and item.get("vcodec") not in {None, "none"}
        and item.get("acodec") in {None, "none"}
    ]

    def score(item: dict[str, object]) -> tuple[float, float, float, float]:
        return (
            float(_format_height(item)),
            float(_format_width(item)),
            _format_float(item, "fps"),
            max(_format_float(item, "tbr"), _format_float(item, "vbr")),
        )

    return [_format_summary(item) for item in sorted(candidates, key=score, reverse=True)[:limit]]


def live_format_candidates(info: dict[str, object]) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    formats = info.get("formats")
    if not isinstance(formats, list):
        raise RuntimeError("yt-dlp не вернул список форматов live.")

    dict_formats = [item for item in formats if isinstance(item, dict)]
    video_candidates = [
        item
        for item in dict_formats
        if item.get("vcodec") not in {None, "none"}
        and item.get("acodec") in {None, "none"}
        and _has_dash_fragments(item)
    ]
    audio_candidates = [
        item
        for item in dict_formats
        if item.get("acodec") not in {None, "none"}
        and item.get("vcodec") in {None, "none"}
        and _has_dash_fragments(item)
    ]

    if not video_candidates:
        raise RuntimeError("Не нашел video-only DASH формат. YouTube мог не отдать DVR-фрагменты.")
    if not audio_candidates:
        audio_candidates = [
            item
            for item in dict_formats
            if item.get("acodec") not in {None, "none"} and _has_dash_fragments(item)
        ]
    if not audio_candidates:
        raise RuntimeError("Не нашел audio DASH формат. YouTube мог не отдать звук отдельными фрагментами.")

    def video_score(item: dict[str, object]) -> tuple[float, float, float, float, float]:
        dynamic_range = str(item.get("dynamic_range") or item.get("hdr") or "").upper()
        hdr_score = 1.0 if "HDR" in dynamic_range or dynamic_range not in {"", "SDR"} else 0.0
        codec = str(item.get("vcodec") or "")
        codec_score = 3.0 if codec.startswith("av01") else 2.0 if codec.startswith("vp9") else 1.0 if codec.startswith("h264") else 0.0
        return (
            float(_format_height(item)),
            float(_format_width(item)),
            _format_float(item, "fps"),
            hdr_score,
            max(_format_float(item, "tbr"), _format_float(item, "vbr")) + codec_score,
        )

    def audio_score(item: dict[str, object]) -> tuple[float, float, float]:
        return (
            max(_format_float(item, "abr"), _format_float(item, "tbr")),
            _format_float(item, "asr"),
            _format_float(item, "filesize") or _format_float(item, "filesize_approx"),
        )

    return (
        sorted(video_candidates, key=video_score, reverse=True),
        sorted(audio_candidates, key=audio_score, reverse=True),
    )


def select_best_live_formats(info: dict[str, object]) -> tuple[dict[str, object], dict[str, object]]:
    video_candidates, audio_candidates = live_format_candidates(info)
    return video_candidates[0], audio_candidates[0]


def dash_fragment_bytes(format_info: dict[str, object], seq: int) -> bytes:
    base_url = format_info.get("fragment_base_url")
    if not isinstance(base_url, str) or not base_url:
        raise RuntimeError("No DASH fragment base URL.")
    headers = format_info.get("http_headers")
    headers = headers if isinstance(headers, dict) else {}
    request_headers = {str(key): str(value) for key, value in headers.items() if value is not None}
    request_headers.setdefault("User-Agent", "Mozilla/5.0")
    request = urllib.request.Request(base_url + f"sq/{seq}", headers=request_headers)
    context = ssl._create_unverified_context()
    with urllib.request.urlopen(request, timeout=30, context=context) as response:
        data = response.read()
    if not data:
        raise RuntimeError(f"Empty DASH fragment {seq}.")
    return data


def probe_dash_fragment_start(format_info: dict[str, object], seq: int, temp_dir: Path) -> float:
    probe_path = temp_dir / f"probe_{seq}.mp4"
    probe_path.write_bytes(dash_fragment_bytes(format_info, seq))
    ffprobe = find_executable("ffprobe")
    if not ffprobe:
        raise RuntimeError("ffprobe не найден.")
    completed = subprocess.run(
        [
            ffprobe,
            "-v",
            "error",
            "-show_entries",
            "format=start_time",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(probe_path),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    probe_path.unlink(missing_ok=True)
    if completed.returncode != 0:
        raise RuntimeError(f"Cannot probe DASH fragment {seq}: {completed.stderr.strip()}")
    value = _safe_float(completed.stdout.strip())
    if value is None:
        raise RuntimeError(f"Cannot read start time for DASH fragment {seq}.")
    return value


def find_dash_sequence_for_time(
    format_info: dict[str, object],
    target_time: float,
    temp_dir: Path,
    on_log,
) -> tuple[int, float]:
    fragment_duration = dash_fragment_duration(format_info)
    expected = max(0, int(target_time // fragment_duration) - 1)
    offsets = [0]
    for offset in range(1, 121):
        offsets.extend([-offset, offset])
    last_error: Exception | None = None
    best_seq: int | None = None
    best_start: float | None = None

    for offset in offsets:
        seq = expected + offset
        if seq < 0:
            continue
        try:
            fragment_start = probe_dash_fragment_start(format_info, seq, temp_dir)
        except Exception as exc:
            last_error = exc
            continue
        if best_start is None or abs(fragment_start - target_time) < abs(best_start - target_time):
            best_seq = seq
            best_start = fragment_start
        if fragment_start <= target_time + fragment_duration:
            on_log(
                f"Fast DASH mode: start fragment {seq} starts at {format_timecode(fragment_start)} "
                f"for requested {format_timecode(target_time)}.\n"
            )
            return max(0, seq - 1), max(0.0, fragment_start - fragment_duration)

    if best_seq is not None and best_start is not None and abs(best_start - target_time) <= 60:
        return max(0, best_seq - 1), best_start
    detail = f" Last probe: fragment {best_seq} starts at {format_timecode(best_start or 0)}." if best_seq is not None else ""
    if last_error:
        detail += f" {last_error}"
    raise RuntimeError(
        f"Не нашел DASH-фрагмент для {format_timecode(target_time)}. "
        f"Возможно, этот таймкод еще недоступен в DVR.{detail}"
    )


def find_latest_dash_fragment_time(format_info: dict[str, object], temp_dir: Path) -> float:
    low = 0
    high = 1
    while high < 200_000:
        try:
            probe_dash_fragment_start(format_info, high, temp_dir)
            low = high
            high *= 2
        except Exception:
            break
    while low + 1 < high:
        mid = (low + high) // 2
        try:
            probe_dash_fragment_start(format_info, mid, temp_dir)
            low = mid
        except Exception:
            high = mid
    return probe_dash_fragment_start(format_info, low, temp_dir)


def download_dash_fragment_range(
    format_info: dict[str, object],
    target: Path,
    start: float,
    end: float,
    on_log,
) -> tuple[Path, float] | None:
    base_url = format_info.get("fragment_base_url")
    if not isinstance(base_url, str) or not base_url:
        return None

    target = unique_path(target)
    part_dir = target.with_suffix(target.suffix + ".fragments")
    if part_dir.exists():
        shutil.rmtree(part_dir)
    part_dir.mkdir(parents=True, exist_ok=True)
    fragment_duration = dash_fragment_duration(format_info)
    start_seq, first_fragment_start = find_dash_sequence_for_time(format_info, start, part_dir, on_log)
    needed_duration = max(fragment_duration, end - first_fragment_start)
    fragment_count = max(1, int((needed_duration + fragment_duration - 0.001) // fragment_duration))
    end_seq = start_seq + fragment_count
    sequence_numbers = list(range(start_seq, end_seq + 1))

    on_log(
        f"\nFast DASH mode: downloading fragments {start_seq}-{end_seq} "
        f"({len(sequence_numbers)} fragments, {fragment_duration:g}s each) for {target.name}.\n"
    )

    def fetch(seq: int) -> tuple[int, Path]:
        fragment_path = part_dir / f"{seq:08d}.mp4"
        fragment_path.write_bytes(dash_fragment_bytes(format_info, seq))
        return seq, fragment_path

    downloaded: dict[int, Path] = {}
    workers = min(16, max(1, len(sequence_numbers)))
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(fetch, seq): seq for seq in sequence_numbers}
        for future in as_completed(futures):
            seq, fragment_path = future.result()
            downloaded[seq] = fragment_path

    with target.open("wb") as output:
        for seq in sequence_numbers:
            fragment_path = downloaded[seq]
            output.write(fragment_path.read_bytes())

    shutil.rmtree(part_dir, ignore_errors=True)
    if not target.exists() or target.stat().st_size == 0:
        return None
    return target, first_fragment_start


def _safe_float(value: object) -> float | None:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _parse_fps(value: str | None) -> float | None:
    if not value or value == "0/0":
        return None
    if "/" in value:
        left, right = value.split("/", 1)
        try:
            denominator = float(right)
            if denominator == 0:
                return None
            return round(float(left) / denominator, 3)
        except ValueError:
            return None
    return _safe_float(value)


class StreamRecorder:
    def __init__(self, on_log, on_status) -> None:
        self.on_log = on_log
        self.on_status = on_status
        self.process: subprocess.Popen[str] | None = None
        self.thread: threading.Thread | None = None
        self.cancel_event = threading.Event()
        self.started_at: float | None = None
        self.stopped_elapsed = 0.0
        self.output_file: Path | None = None

    @property
    def is_running(self) -> bool:
        return self.process is not None and self.process.poll() is None

    @property
    def elapsed(self) -> float:
        if not self.started_at:
            return self.stopped_elapsed
        if self.cancel_event.is_set():
            return self.stopped_elapsed
        if self.process is None or self.is_running:
            return time.monotonic() - self.started_at
        return self.stopped_elapsed
        return time.monotonic() - self.started_at

    def start(self, url: str, output_dir: Path) -> Path:
        if self.is_running:
            raise RuntimeError("Запись уже идет.")
        yt_dlp = find_executable("yt-dlp")
        ffmpeg = find_executable("ffmpeg")
        if not yt_dlp:
            raise RuntimeError("yt-dlp не найден.")
        if not ffmpeg:
            raise RuntimeError("ffmpeg не найден.")

        output_dir.mkdir(parents=True, exist_ok=True)
        captures = output_dir / "captures"
        captures.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        self.output_file = unique_path(captures / f"live-master_{stamp}.mkv")
        self.cancel_event.clear()
        self.stopped_elapsed = 0.0

        self.thread = threading.Thread(
            target=self._run_capture,
            args=(yt_dlp, ffmpeg, url),
            name="stream-recorder",
            daemon=True,
        )
        self.started_at = time.monotonic()
        self.thread.start()
        return self.output_file

    def stop(self) -> None:
        self.cancel_event.set()
        if self.started_at:
            self.stopped_elapsed = time.monotonic() - self.started_at
        process = self.process
        if process:
            terminate_process(process)

    def _run_capture(self, yt_dlp: str, ffmpeg: str, url: str) -> None:
        self.on_status("Connecting")
        self.on_log("Resolving best live streams with yt-dlp.\n")

        resolve_command = [
            yt_dlp,
            "--no-playlist",
            "--no-color",
            "--no-warnings",
            "--retries",
            "10",
            "--fragment-retries",
            "10",
            "--extractor-args",
            YOUTUBE_EXTRACTOR_ARGS,
            "-f",
            CAPTURE_QUALITY_SELECTOR,
            "-J",
            url,
        ]
        self.on_log("$ " + " ".join(resolve_command) + "\n")
        resolved = subprocess.run(
            resolve_command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        if resolved.stderr.strip():
            self.on_log(resolved.stderr)
        if resolved.returncode != 0:
            self.on_status("Error")
            self.on_log("\nyt-dlp could not resolve this live stream.\n")
            return
        try:
            info = json.loads(resolved.stdout)
        except json.JSONDecodeError:
            self.on_status("Error")
            self.on_log("\nyt-dlp returned unreadable stream data.\n")
            return

        media_inputs, video_format, audio_format = capture_inputs_from_info(info)
        if not media_inputs:
            self.on_status("Error")
            self.on_log("\nyt-dlp did not return a playable media URL.\n")
            return

        if video_format:
            self.on_log(f"Capture video: {_format_summary(video_format)}\n")
        if audio_format:
            self.on_log(f"Capture audio: {_format_summary(audio_format)}\n")
        elif video_format and video_format.get("acodec") in {None, "none"}:
            self.on_log("Warning: yt-dlp did not return a separate audio stream for capture.\n")

        self.on_log(f"Resolved {len(media_inputs)} media stream(s). Starting ffmpeg capture.\n")
        ffmpeg_command = [
            ffmpeg,
            "-y",
            "-hide_banner",
            "-loglevel",
            "info",
            "-reconnect",
            "1",
            "-reconnect_streamed",
            "1",
            "-reconnect_delay_max",
            "8",
        ]
        for media_url, headers in media_inputs:
            ffmpeg_command.extend(ffmpeg_input_options(headers))
            ffmpeg_command.extend(["-i", media_url])
        if len(media_inputs) >= 2:
            ffmpeg_command.extend(["-map", "0:v:0?", "-map", "1:a:0?"])
        else:
            ffmpeg_command.extend(["-map", "0:v:0?", "-map", "0:a:0?"])
        ffmpeg_command.extend(["-c", "copy", "-f", "matroska", str(self.output_file)])
        self.on_log("$ ffmpeg ... " + str(self.output_file) + "\n\n")

        creationflags = windows_process_flags(new_process_group=True)
        popen_kwargs: dict[str, object] = {}
        if os.name != "nt":
            popen_kwargs["preexec_fn"] = os.setsid

        ffmpeg_proc = subprocess.Popen(
            ffmpeg_command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            creationflags=creationflags,
            **popen_kwargs,
        )
        self.process = ffmpeg_proc

        self.on_status("Recording")
        assert ffmpeg_proc.stdout is not None
        for line in ffmpeg_proc.stdout:
            self.on_log(line)
            if self.cancel_event.is_set():
                break

        if self.cancel_event.is_set():
            terminate_process(ffmpeg_proc)
            if self.started_at:
                self.stopped_elapsed = time.monotonic() - self.started_at
            self.on_status("Finalizing MP4")
            self._remux_capture_to_mp4(ffmpeg)
            self.on_status("Stopped")
            self.on_log("\nLive capture stopped.\n")
        else:
            code = ffmpeg_proc.wait()
            if self.started_at:
                self.stopped_elapsed = time.monotonic() - self.started_at
            if code == 0:
                self.on_status("Finished")
                self.on_log("\nLive capture finished.\n")
            else:
                self.on_status("Error")
                self.on_log(f"\nCapture stopped with ffmpeg code {code}.\n")
        self.process = None

    def _remux_capture_to_mp4(self, ffmpeg: str) -> None:
        source = self.output_file
        if not source or not source.exists() or source.stat().st_size == 0:
            return
        target = unique_path(source.with_suffix(".mp4"))
        command = [
            ffmpeg,
            "-y",
            "-hide_banner",
            "-fflags",
            "+genpts",
            "-i",
            str(source),
            "-map",
            "0:v:0?",
            "-map",
            "0:a:0?",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast" if platform.system() != "Windows" else "ultrafast",
            "-crf",
            "20",
            "-profile:v",
            "high",
            "-pix_fmt",
            "yuv420p",
            "-fps_mode",
            "cfr",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-ar",
            "48000",
            "-ac",
            "2",
            "-max_muxing_queue_size",
            "4096",
            "-movflags",
            "+faststart",
            str(target),
        ]
        self.on_log(f"\nFinalizing stopped capture to Universal MP4: {target.name}\n")
        code = run_logged(command, self.on_log, None)
        if code == 0 and target.exists() and target.stat().st_size > 0:
            self.output_file = target
            source.unlink(missing_ok=True)
            report = probe_media(target)
            self.on_log(f"MP4 master ready: {target}\n{report.message}\n")
            return
        target.unlink(missing_ok=True)
        self.on_log("Universal MP4 finalize failed; keeping MKV master as fallback.\n")


class ClipExporter:
    def __init__(self, on_log, on_finish) -> None:
        self.on_log = on_log
        self.on_finish = on_finish
        self.thread: threading.Thread | None = None
        self.cancel_event = threading.Event()

    @property
    def is_running(self) -> bool:
        return self.thread is not None and self.thread.is_alive()

    def cancel(self) -> None:
        self.cancel_event.set()

    def export(
        self,
        source: Path,
        output_dir: Path,
        start: float,
        end: float,
        label: str,
        mode: str,
    ) -> None:
        if self.is_running:
            raise RuntimeError("Экспорт уже идет.")
        if end <= start:
            raise RuntimeError("Конец клипа должен быть позже начала.")
        if not source.exists() or source.stat().st_size == 0:
            raise RuntimeError("Master-файл еще не готов или пустой.")
        self.cancel_event.clear()
        self.thread = threading.Thread(
            target=self._run,
            args=(source, output_dir, start, end, label, mode),
            name="clip-exporter",
            daemon=True,
        )
        self.thread.start()

    def export_direct(
        self,
        url: str,
        output_dir: Path,
        start: float,
        end: float,
        label: str,
        mode: str,
    ) -> None:
        if self.is_running:
            raise RuntimeError("Экспорт уже идет.")
        if end <= start:
            raise RuntimeError("Конец клипа должен быть позже начала.")
        if not url.strip():
            raise RuntimeError("Вставь ссылку на стрим.")
        self.cancel_event.clear()
        self.thread = threading.Thread(
            target=self._run_direct,
            args=(url.strip(), output_dir, start, end, label, mode),
            name="direct-clip-exporter",
            daemon=True,
        )
        self.thread.start()

    def export_vod(
        self,
        url: str,
        output_dir: Path,
        start: float,
        end: float,
        label: str,
        mode: str,
    ) -> None:
        if self.is_running:
            raise RuntimeError("Экспорт уже идет.")
        if end <= start:
            raise RuntimeError("Конец клипа должен быть позже начала.")
        if not url.strip():
            raise RuntimeError("Вставь ссылку на готовое видео или сохраненную трансляцию.")
        self.cancel_event.clear()
        self.thread = threading.Thread(
            target=self._run_vod,
            args=(url.strip(), output_dir, start, end, label, mode),
            name="vod-range-exporter",
            daemon=True,
        )
        self.thread.start()

    def _run(self, source: Path, output_dir: Path, start: float, end: float, label: str, mode: str) -> None:
        try:
            ffmpeg = find_executable("ffmpeg")
            if not ffmpeg:
                raise RuntimeError("ffmpeg не найден.")
            clips_dir = output_dir / "clips"
            clips_dir.mkdir(parents=True, exist_ok=True)
            duration = end - start
            stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
            clean_label = safe_name(label, "clip")
            suffix = ".mp4" if mode == "Universal Editing MP4" else ".mkv"
            target = unique_path(
                clips_dir
                / f"{stamp}_{clean_label}_{format_timecode(start).replace(':', '-')}_{format_timecode(end).replace(':', '-')}{suffix}"
            )

            if mode == "Universal Editing MP4":
                command = [
                    ffmpeg,
                    "-y",
                    "-ss",
                    format_timecode(start),
                    "-i",
                    str(source),
                    "-t",
                    format_timecode(duration),
                    "-map",
                    "0:v:0?",
                    "-map",
                    "0:a:0?",
                    "-c:v",
                    "libx264",
                    "-preset",
                    "veryfast" if platform.system() != "Windows" else "ultrafast",
                    "-crf",
                    "20",
                    "-profile:v",
                    "high",
                    "-pix_fmt",
                    "yuv420p",
                    "-fps_mode",
                    "cfr",
                    "-c:a",
                    "aac",
                    "-b:a",
                    "192k",
                    "-ar",
                    "48000",
                    "-ac",
                    "2",
                    "-af",
                    "apad",
                    "-shortest",
                    "-movflags",
                    "+faststart",
                    str(target),
                ]
            else:
                command = [
                    ffmpeg,
                    "-y",
                    "-ss",
                    format_timecode(start),
                    "-i",
                    str(source),
                    "-t",
                    format_timecode(duration),
                    "-map",
                    "0:v:0?",
                    "-map",
                    "0:a:0?",
                    "-c",
                    "copy",
                    str(target),
                ]

            self.on_log(f"\nExporting clip: {format_timecode(start)} - {format_timecode(end)} ({mode})\n")
            code = run_logged(command, self.on_log, self.cancel_event)
            if code != 0 or not target.exists() or target.stat().st_size == 0:
                target.unlink(missing_ok=True)
                raise RuntimeError("ffmpeg не смог создать клип. Проверь лог.")
            report = probe_media(target)
            self.on_finish(True, target, report.message)
        except Exception as exc:
            self.on_finish(False, None, str(exc))

    def _convert_downloaded_range(
        self,
        ffmpeg: str,
        source: Path,
        target: Path,
        duration: float,
        mode: str,
    ) -> None:
        if mode == "Original Fast":
            shutil.move(str(source), str(target))
            return
        command = [
            ffmpeg,
            "-y",
            "-hide_banner",
            "-i",
            str(source),
            "-t",
            format_timecode(duration),
            "-map",
            "0:v:0?",
            "-map",
            "0:a:0?",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast" if platform.system() != "Windows" else "ultrafast",
            "-crf",
            "20",
            "-profile:v",
            "high",
            "-pix_fmt",
            "yuv420p",
            "-fps_mode",
            "cfr",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-ar",
            "48000",
            "-ac",
            "2",
            "-movflags",
            "+faststart",
            str(target),
        ]
        self.on_log("\nConverting range to Universal Editing MP4.\n")
        code = run_logged(command, self.on_log, self.cancel_event)
        if code != 0 or not target.exists() or target.stat().st_size == 0:
            target.unlink(missing_ok=True)
            raise RuntimeError("ffmpeg не смог создать Universal MP4.")
        source.unlink(missing_ok=True)

    def _run_live_section_download(
        self,
        yt_dlp: str,
        ffmpeg: str,
        url: str,
        output_dir: Path,
        start: float,
        end: float,
        label: str,
        mode: str,
        expected_height: int,
    ) -> Path:
        clips_dir = output_dir / "clips"
        temp_dir = output_dir / "temp-live-section"
        clips_dir.mkdir(parents=True, exist_ok=True)
        temp_dir.mkdir(parents=True, exist_ok=True)

        stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        clean_label = safe_name(label, "clip")
        range_name = f"{format_timecode(start).replace(':', '-')}_{format_timecode(end).replace(':', '-')}"
        section = f"*{format_timecode(start)}-{format_timecode(end)}"
        temp_template = str(temp_dir / f"{stamp}_{clean_label}_{range_name}.%(ext)s")
        before = {path for path in temp_dir.glob(f"{stamp}_{clean_label}_{range_name}.*")}
        command = [
            yt_dlp,
            "--no-playlist",
            "--no-color",
            "--newline",
            "--retries",
            "10",
            "--fragment-retries",
            "10",
            "--extractor-args",
            YOUTUBE_EXTRACTOR_ARGS,
            "--live-from-start",
            "-f",
            QUALITY_SELECTOR,
            "-S",
            FORMAT_SORT,
            "--download-sections",
            section,
            "--force-keyframes-at-cuts",
            "--merge-output-format",
            "mkv",
            "-o",
            temp_template,
            url,
        ]
        self.on_log(
            f"\nLive max-quality section download: {format_timecode(start)} - {format_timecode(end)}\n"
        )
        self.on_log("$ " + " ".join(command) + "\n")
        code = run_logged(command, self.on_log, self.cancel_event)
        candidates = [
            path
            for path in temp_dir.glob(f"{stamp}_{clean_label}_{range_name}.*")
            if path not in before and path.is_file() and path.stat().st_size > 0 and not path.name.endswith(".part")
        ]
        if code != 0 or not candidates:
            raise RuntimeError("yt-dlp не смог скачать live-диапазон через section mode.")

        temp_source = max(candidates, key=lambda path: path.stat().st_mtime)
        raw_report = probe_media(temp_source)
        self.on_log(f"Downloaded live section check: {raw_report.message}\n")

        suffix = temp_source.suffix if mode == "Original Fast" else ".mp4"
        target = unique_path(clips_dir / f"{stamp}_{clean_label}_{range_name}{suffix}")
        self._convert_downloaded_range(ffmpeg, temp_source, target, end - start, mode)
        report = probe_media(target)
        if expected_height and (report.height or 0) < expected_height:
            self.on_log(
                f"Notice: final live clip is {report.width}x{report.height}; "
                f"max live-DVR format reported by yt-dlp was {expected_height}p.\n"
            )
        return target

    def _run_direct(
        self,
        url: str,
        output_dir: Path,
        start: float,
        end: float,
        label: str,
        mode: str,
    ) -> None:
        try:
            yt_dlp = find_executable("yt-dlp")
            ffmpeg = find_executable("ffmpeg")
            if not yt_dlp:
                raise RuntimeError("yt-dlp не найден.")
            if not ffmpeg:
                raise RuntimeError("ffmpeg не найден.")

            clips_dir = output_dir / "clips"
            clips_dir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
            clean_label = safe_name(label, "clip")
            range_name = f"{format_timecode(start).replace(':', '-')}_{format_timecode(end).replace(':', '-')}"
            exact_duration = end - start

            self.on_log(
                f"\nResolving live stream from the start: {format_timecode(start)} - {format_timecode(end)}\n"
            )
            resolve_command = [
                yt_dlp,
                "--no-playlist",
                "--no-color",
                "--no-warnings",
                "--retries",
                "10",
                "--fragment-retries",
                "10",
                "--extractor-args",
                YOUTUBE_EXTRACTOR_ARGS,
                "--live-from-start",
                "-S",
                FORMAT_SORT,
                "-J",
                url,
            ]
            self.on_log("$ " + " ".join(resolve_command) + "\n")
            resolved = subprocess.run(
                resolve_command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )
            if resolved.stderr.strip():
                self.on_log(resolved.stderr)
            if resolved.returncode != 0:
                raise RuntimeError("yt-dlp не смог получить live-поток от начала стрима.")
            try:
                info = json.loads(resolved.stdout)
            except json.JSONDecodeError as exc:
                raise RuntimeError("yt-dlp вернул нечитаемые данные о live-потоке.") from exc

            summaries = live_video_candidate_summaries(info)
            if summaries:
                self.on_log("Available live video formats:\n")
                for summary in summaries:
                    self.on_log(f"  {summary}\n")
            else:
                self.on_log("yt-dlp did not expose separate live video formats.\n")

            temp_dir = output_dir / "temp-direct"
            temp_dir.mkdir(parents=True, exist_ok=True)
            video_candidates, audio_candidates = live_format_candidates(info)
            max_live_height = _format_height(video_candidates[0])
            expected_live_height = max_live_height
            if max_live_height and max_live_height < 1440:
                self.on_log(
                    f"yt-dlp currently exposes only {max_live_height}p for live. "
                    "Using the best format YouTube exposes to the downloader.\n"
                )
            try:
                target = self._run_live_section_download(
                    yt_dlp,
                    ffmpeg,
                    url,
                    output_dir,
                    start,
                    end,
                    label,
                    mode,
                    expected_live_height,
                )
                report = probe_media(target)
                self.on_finish(True, target, report.message)
                return
            except Exception as exc:
                self.on_log(f"Live section mode failed, falling back to DASH fragments: {exc}\n")

            strict_video_candidates = [
                item for item in video_candidates if _format_height(item) == max_live_height
            ]
            if max_live_height:
                self.on_log(
                    f"Max live-DVR format reported by yt-dlp: {max_live_height}p. "
                    "Trying only that height in DASH fallback.\n"
                )
            local_seek = start
            video_source: Path | None = None
            audio_source: Path | None = None
            video_format: dict[str, object] | None = None
            audio_format: dict[str, object] | None = None
            last_fast_error: Exception | None = None

            for candidate_index, candidate_video in enumerate(strict_video_candidates[:6], start=1):
                for candidate_audio in audio_candidates[:3]:
                    video_ext = safe_name(str(candidate_video.get("ext") or "mp4"), "mp4")
                    audio_ext = safe_name(str(candidate_audio.get("ext") or "m4a"), "m4a")
                    video_target = temp_dir / f"{stamp}_{clean_label}_{range_name}_video_{candidate_index}.{video_ext}"
                    audio_target = temp_dir / f"{stamp}_{clean_label}_{range_name}_audio_{candidate_index}.{audio_ext}"
                    self.on_log(f"Trying live video: {_format_summary(candidate_video)}\n")
                    self.on_log(f"Trying live audio: {_format_summary(candidate_audio)}\n")
                    try:
                        self.on_log("\nFast DASH mode: downloading video and audio in parallel.\n")
                        with ThreadPoolExecutor(max_workers=2) as executor:
                            video_future = executor.submit(
                                download_dash_fragment_range,
                                candidate_video,
                                video_target,
                                start,
                                end,
                                self.on_log,
                            )
                            audio_future = executor.submit(
                                download_dash_fragment_range,
                                candidate_audio,
                                audio_target,
                                start,
                                end,
                                self.on_log,
                            )
                            video_result = video_future.result()
                            audio_result = audio_future.result()
                        if not video_result or not audio_result:
                            raise RuntimeError("Fast DASH mode did not create video/audio fragments.")
                        video_source, video_first_start = video_result
                        audio_source, audio_first_start = audio_result
                        video_format = candidate_video
                        audio_format = candidate_audio
                        local_seek = max(0, start - min(video_first_start, audio_first_start))
                        self.on_log(f"Selected max available live video: {_format_summary(video_format)}\n")
                        self.on_log(f"Selected best available live audio: {_format_summary(audio_format)}\n")
                        break
                    except Exception as exc:
                        last_fast_error = exc
                        self.on_log(f"Live format failed, trying next if available: {exc}\n")
                        video_target.unlink(missing_ok=True)
                        audio_target.unlink(missing_ok=True)
                if video_source and audio_source:
                    break

            if not video_source or not audio_source:
                raise RuntimeError(
                    "Быстрый режим не смог скачать DASH-фрагменты. "
                    "Section mode тоже не сработал.\n"
                    f"Причина: {last_fast_error}"
                ) from last_fast_error

            if mode == "Original Fast":
                target = unique_path(clips_dir / f"{stamp}_{clean_label}_{range_name}.mkv")
                command = [
                    ffmpeg,
                    "-y",
                    "-hide_banner",
                    "-ss",
                    format_timecode(local_seek),
                    "-i",
                    str(video_source),
                    "-ss",
                    format_timecode(local_seek),
                    "-i",
                    str(audio_source),
                    "-t",
                    format_timecode(exact_duration),
                    "-map",
                    "0:v:0?",
                    "-map",
                    "1:a:0?",
                    "-c",
                    "copy",
                    "-avoid_negative_ts",
                    "make_zero",
                    str(target),
                ]
                self.on_log("\nDownloading exact direct clip without saving the full stream.\n")
                code = run_logged(command, self.on_log, self.cancel_event)
                if code != 0 or not target.exists() or target.stat().st_size == 0:
                    target.unlink(missing_ok=True)
                    raise RuntimeError("ffmpeg не смог скачать прямой Original Fast клип.")
            else:
                target = unique_path(clips_dir / f"{stamp}_{clean_label}_{range_name}.mp4")
                command = [
                    ffmpeg,
                    "-y",
                    "-hide_banner",
                    "-ss",
                    format_timecode(local_seek),
                    "-i",
                    str(video_source),
                    "-ss",
                    format_timecode(local_seek),
                    "-i",
                    str(audio_source),
                    "-t",
                    format_timecode(exact_duration),
                    "-map",
                    "0:v:0?",
                    "-map",
                    "1:a:0?",
                    "-c:v",
                    "libx264",
                    "-preset",
                    "veryfast" if platform.system() != "Windows" else "ultrafast",
                    "-crf",
                    "20",
                    "-profile:v",
                    "high",
                    "-pix_fmt",
                    "yuv420p",
                    "-fps_mode",
                    "cfr",
                    "-c:a",
                    "aac",
                    "-b:a",
                    "192k",
                    "-ar",
                    "48000",
                    "-ac",
                    "2",
                    "-af",
                    "apad",
                    "-shortest",
                    "-movflags",
                    "+faststart",
                    str(target),
                ]
                self.on_log("\nDownloading direct clip as Universal Editing MP4 without saving the full stream.\n")
                code = run_logged(command, self.on_log, self.cancel_event)
                if code != 0 or not target.exists() or target.stat().st_size == 0:
                    target.unlink(missing_ok=True)
                    raise RuntimeError("ffmpeg не смог создать Universal MP4 напрямую.")

            video_source.unlink(missing_ok=True)
            audio_source.unlink(missing_ok=True)
            video_source.with_suffix(video_source.suffix + ".ytdl").unlink(missing_ok=True)
            audio_source.with_suffix(audio_source.suffix + ".ytdl").unlink(missing_ok=True)

            report = probe_media(target)
            if expected_live_height and (report.height or 0) < expected_live_height:
                self.on_log(
                    f"Notice: final live clip is {report.width}x{report.height}; "
                    f"max live-DVR format reported by yt-dlp was {expected_live_height}p.\n"
                )
            self.on_finish(True, target, report.message)
        except Exception as exc:
            self.on_finish(False, None, str(exc))

    def _run_vod(
        self,
        url: str,
        output_dir: Path,
        start: float,
        end: float,
        label: str,
        mode: str,
    ) -> None:
        temp_source: Path | None = None
        try:
            yt_dlp = find_executable("yt-dlp")
            ffmpeg = find_executable("ffmpeg")
            if not yt_dlp:
                raise RuntimeError("yt-dlp не найден.")
            if not ffmpeg:
                raise RuntimeError("ffmpeg не найден.")

            clips_dir = output_dir / "clips"
            temp_dir = output_dir / "temp-vod"
            clips_dir.mkdir(parents=True, exist_ok=True)
            temp_dir.mkdir(parents=True, exist_ok=True)

            stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
            clean_label = safe_name(label, "saved_video")
            range_name = f"{format_timecode(start).replace(':', '-')}_{format_timecode(end).replace(':', '-')}"
            duration = end - start

            resolve_command = [
                yt_dlp,
                "--no-playlist",
                "--no-color",
                "--no-warnings",
                "--retries",
                "10",
                "--fragment-retries",
                "10",
                "-f",
                QUALITY_SELECTOR,
                "-S",
                FORMAT_SORT,
                "-J",
                url,
            ]
            self.on_log(
                f"\nResolving saved video range: {format_timecode(start)} - {format_timecode(end)}\n"
            )
            self.on_log("$ " + " ".join(resolve_command) + "\n")
            resolved = subprocess.run(
                resolve_command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )
            if resolved.stderr.strip():
                self.on_log(resolved.stderr)
            if resolved.returncode != 0:
                raise RuntimeError("yt-dlp не смог получить ссылки на готовое видео.")
            try:
                info = json.loads(resolved.stdout)
            except json.JSONDecodeError as exc:
                raise RuntimeError("yt-dlp вернул нечитаемые данные о готовом видео.") from exc

            media_inputs, video_format, audio_format = capture_inputs_from_info(info)
            if not media_inputs:
                raise RuntimeError("yt-dlp не вернул playable-ссылки на готовое видео.")
            if video_format:
                self.on_log(f"Saved range video: {_format_summary(video_format)}\n")
            if audio_format:
                self.on_log(f"Saved range audio: {_format_summary(audio_format)}\n")

            if mode == "Original Fast":
                target = unique_path(clips_dir / f"{stamp}_{clean_label}_{range_name}.mkv")
                command = [
                    ffmpeg,
                    "-y",
                    "-hide_banner",
                ]
                for media_url, headers in media_inputs:
                    command.extend(ffmpeg_input_options(headers))
                    command.extend(["-ss", format_timecode(start), "-i", media_url])
                if len(media_inputs) >= 2:
                    command.extend(["-map", "0:v:0?", "-map", "1:a:0?"])
                else:
                    command.extend(["-map", "0:v:0?", "-map", "0:a:0?"])
                command.extend([
                    "-t",
                    format_timecode(duration),
                    "-c",
                    "copy",
                    "-avoid_negative_ts",
                    "make_zero",
                    str(target),
                ])
                self.on_log("\nCutting saved-video range directly from media URLs.\n")
                code = run_logged(command, self.on_log, self.cancel_event)
                if code != 0 or not target.exists() or target.stat().st_size == 0:
                    target.unlink(missing_ok=True)
                    raise RuntimeError("ffmpeg не смог скачать выбранный диапазон готового видео.")
            else:
                target = unique_path(clips_dir / f"{stamp}_{clean_label}_{range_name}.mp4")
                command = [
                    ffmpeg,
                    "-y",
                    "-hide_banner",
                ]
                for media_url, headers in media_inputs:
                    command.extend(ffmpeg_input_options(headers))
                    command.extend(["-ss", format_timecode(start), "-i", media_url])
                if len(media_inputs) >= 2:
                    command.extend(["-map", "0:v:0?", "-map", "1:a:0?"])
                else:
                    command.extend(["-map", "0:v:0?", "-map", "0:a:0?"])
                command.extend([
                    "-t",
                    format_timecode(duration),
                    "-c:v",
                    "libx264",
                    "-preset",
                    "veryfast" if platform.system() != "Windows" else "ultrafast",
                    "-crf",
                    "20",
                    "-profile:v",
                    "high",
                    "-pix_fmt",
                    "yuv420p",
                    "-fps_mode",
                    "cfr",
                    "-c:a",
                    "aac",
                    "-b:a",
                    "192k",
                    "-ar",
                    "48000",
                    "-ac",
                    "2",
                    "-max_muxing_queue_size",
                    "4096",
                    "-movflags",
                    "+faststart",
                    str(target),
                ])
                self.on_log("\nConverting saved-video range to Universal Editing MP4.\n")
                code = run_logged(command, self.on_log, self.cancel_event)
                if code != 0 or not target.exists() or target.stat().st_size == 0:
                    target.unlink(missing_ok=True)
                    raise RuntimeError("ffmpeg не смог создать Universal MP4 из готового видео.")

            report = probe_media(target)
            self.on_finish(True, target, report.message)
        except Exception as exc:
            if temp_source:
                temp_source.unlink(missing_ok=True)
            self.on_finish(False, None, str(exc))

    def _download_live_prefix(
        self,
        yt_dlp: str,
        url: str,
        format_id: str,
        target: Path,
        needed_duration: float,
        label: str,
    ) -> Path:
        target = unique_path(target)
        command = [
            yt_dlp,
            "--no-playlist",
            "--no-color",
            "--newline",
            "--live-from-start",
            "--no-part",
            "--retries",
            "10",
            "--fragment-retries",
            "10",
            "-f",
            format_id,
            "-o",
            str(target),
            url,
        ]
        self.on_log(f"\nDownloading {label} until {format_timecode(needed_duration)} from stream start.\n")
        self.on_log("$ " + " ".join(command) + "\n")
        creationflags = windows_process_flags(new_process_group=True)
        popen_kwargs: dict[str, object] = {}
        if os.name != "nt":
            popen_kwargs["preexec_fn"] = os.setsid
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            creationflags=creationflags,
            **popen_kwargs,
        )

        def read_log() -> None:
            assert process.stdout is not None
            for line in process.stdout:
                self.on_log(line)

        threading.Thread(target=read_log, name=f"{label}-prefix-log", daemon=True).start()
        started = time.monotonic()
        max_wait = max(90, int(needed_duration * 4 + 120))
        last_duration = 0.0
        while process.poll() is None:
            if self.cancel_event.is_set():
                terminate_process(process)
                raise RuntimeError("Скачивание клипа отменено.")
            duration = media_duration(target)
            if duration:
                last_duration = duration
                if duration >= needed_duration:
                    self.on_log(f"{label} reached {format_timecode(duration)}. Stopping prefix download.\n")
                    terminate_process(process)
                    break
            if time.monotonic() - started > max_wait:
                terminate_process(process)
                raise RuntimeError(
                    f"{label}: не удалось скачать нужную длительность. Получилось примерно {format_timecode(last_duration)}."
                )
            time.sleep(1)

        if not target.exists() or target.stat().st_size == 0:
            raise RuntimeError(f"{label}: yt-dlp не создал файл.")
        return target


class GlobalHotkeyManager:
    def __init__(self, on_action, on_log, on_status) -> None:
        self.on_action = on_action
        self.on_log = on_log
        self.on_status = on_status
        self.listener = None
        self.carbon = None
        self.carbon_refs: list[ctypes.c_void_p] = []
        self.carbon_actions: dict[int, str] = {}
        self.carbon_thread: threading.Thread | None = None
        self.carbon_stop_event = threading.Event()
        self.mac_event_monitors: list[object] = []

    def start(self) -> None:
        if platform.system() == "Darwin":
            self._start_macos_carbon()
            self._start_macos_nsevent()
            return

        self._start_pynput_global("Global hotkeys")

    def _start_pynput_global(self, label: str) -> None:
        if self.listener:
            return
        try:
            from pynput import keyboard  # type: ignore[import-not-found]
        except Exception as exc:
            self.on_log(f"{label} unavailable: install pynput. {exc}\n")
            if platform.system() != "Darwin":
                self.on_status("Global hotkeys: unavailable")
            return

        try:
            shortcuts = {
                "<f7>": lambda: self.on_action("marker"),
                "<f8>": lambda: self.on_action("last30"),
                "<f9>": lambda: self.on_action("last60"),
                "<f10>": lambda: self.on_action("last180"),
                "<f11>": lambda: self.on_action("mark"),
                "<ctrl>+<alt>+m": lambda: self.on_action("marker"),
                "<ctrl>+<alt>+7": lambda: self.on_action("marker"),
                "<ctrl>+<alt>+1": lambda: self.on_action("last30"),
                "<ctrl>+<alt>+2": lambda: self.on_action("last60"),
                "<ctrl>+<alt>+3": lambda: self.on_action("last180"),
                "<ctrl>+<alt>+4": lambda: self.on_action("exact"),
            }
            self.listener = keyboard.GlobalHotKeys(shortcuts)
            self.listener.start()
            self.on_log(
                f"{label} enabled: F7 marker, F8/F9/F10/F11 and Ctrl+Alt+M/7/1/2/3/4.\n"
            )
            if platform.system() != "Darwin":
                self.on_status("Global hotkeys: enabled")
        except Exception as exc:
            self.listener = None
            if platform.system() != "Darwin":
                self.on_status("Global hotkeys: needs permission")
            if platform.system() == "Windows":
                self.on_log(
                    f"{label} could not start on Windows. If a game runs as administrator, "
                    f"run ERNI Live Clipper as administrator too. Details: {exc}\n"
                )
            else:
                self.on_log(
                    f"{label} could not start. On macOS allow Accessibility for this app. "
                    f"Details: {exc}\n"
                )

    def _start_macos_nsevent(self) -> None:
        if self.mac_event_monitors:
            return
        try:
            from AppKit import (  # type: ignore[import-not-found]
                NSEvent,
                NSEventMaskKeyDown,
                NSEventModifierFlagCommand,
                NSEventModifierFlagControl,
                NSEventModifierFlagOption,
            )
        except Exception as exc:
            self.on_log(f"macOS NSEvent hotkeys unavailable: {exc}\n")
            self._start_macos_carbon()
            return

        key_actions = {
            98: "marker",   # F7
            18: "last30",   # 1
            19: "last60",   # 2
            20: "last180",  # 3
            21: "exact",    # 4
            26: "marker",   # 7
            46: "marker",   # M
            100: "last30",  # F8
            101: "last60",  # F9
            109: "last180", # F10
            103: "mark",    # F11
        }

        def action_for_event(event) -> str | None:
            key_code = int(event.keyCode())
            flags = int(event.modifierFlags())
            if key_code in {98, 100, 101, 109, 103}:
                return key_actions.get(key_code)
            has_command = bool(flags & int(NSEventModifierFlagCommand))
            has_control_option = bool(flags & int(NSEventModifierFlagControl)) and bool(flags & int(NSEventModifierFlagOption))
            if has_control_option and key_code in {26, 46}:
                return "marker"
            if has_command or has_control_option:
                return key_actions.get(key_code)
            return None

        def global_handler(event) -> None:
            action = action_for_event(event)
            if action:
                self.on_log(f"Global hotkey fired: {action}\n")
                self.on_action(action)

        def local_handler(event):
            action = action_for_event(event)
            if action:
                self.on_log(f"Local macOS hotkey fired: {action}\n")
                self.on_action(action)
                return None
            return event

        try:
            global_monitor = NSEvent.addGlobalMonitorForEventsMatchingMask_handler_(
                NSEventMaskKeyDown,
                global_handler,
            )
            local_monitor = NSEvent.addLocalMonitorForEventsMatchingMask_handler_(
                NSEventMaskKeyDown,
                local_handler,
            )
            if global_monitor:
                self.mac_event_monitors.append(global_monitor)
            if local_monitor:
                self.mac_event_monitors.append(local_monitor)
            if not self.mac_event_monitors:
                raise RuntimeError("NSEvent did not return monitor handles.")
            self.on_log(
                "macOS hotkeys enabled: F7 marker, Cmd+1/2/3/4, F8/F9/F10/F11, "
                "Ctrl+Option+M/7/1/2/3/4.\n"
            )
            self.on_status("macOS hotkeys: enabled")
        except Exception as exc:
            self.on_log(f"macOS NSEvent hotkeys could not start: {exc}\n")
            self._start_macos_carbon()

    def _start_macos_carbon(self) -> None:
        if self.carbon_refs:
            return

        try:
            carbon = ctypes.CDLL("/System/Library/Frameworks/Carbon.framework/Carbon")
            self.carbon = carbon

            carbon.GetApplicationEventTarget.restype = ctypes.c_void_p
            carbon.RegisterEventHotKey.restype = ctypes.c_int
            carbon.UnregisterEventHotKey.restype = ctypes.c_int
            carbon.GetEventParameter.restype = ctypes.c_int
            carbon.ReceiveNextEvent.restype = ctypes.c_int
            carbon.ReleaseEvent.restype = None

            k_event_class_keyboard = four_char_code("keyb")
            k_event_hot_key_pressed = 5
            signature = four_char_code("ERNI")
            no_err = 0

            cmd_key = 1 << 8
            control_key = 1 << 12
            option_key = 1 << 11
            hotkeys = [
                (13, 98, 0, "marker", "F7"),
                (1, 18, cmd_key, "last30", "Cmd+1"),
                (2, 19, cmd_key, "last60", "Cmd+2"),
                (3, 20, cmd_key, "last180", "Cmd+3"),
                (4, 21, cmd_key, "exact", "Cmd+4"),
                (5, 100, 0, "last30", "F8"),
                (6, 101, 0, "last60", "F9"),
                (7, 109, 0, "last180", "F10"),
                (8, 103, 0, "mark", "F11"),
                (9, 18, control_key | option_key, "last30", "Ctrl+Option+1"),
                (10, 19, control_key | option_key, "last60", "Ctrl+Option+2"),
                (11, 20, control_key | option_key, "last180", "Ctrl+Option+3"),
                (12, 21, control_key | option_key, "exact", "Ctrl+Option+4"),
                (14, 46, control_key | option_key, "marker", "Ctrl+Option+M"),
                (15, 26, control_key | option_key, "marker", "Ctrl+Option+7"),
            ]

            target = carbon.GetApplicationEventTarget()
            failed: list[str] = []
            for hotkey_id_value, key_code, modifiers, action, label in hotkeys:
                hotkey_id = CarbonEventHotKeyID(signature, hotkey_id_value)
                ref = ctypes.c_void_p()
                status = carbon.RegisterEventHotKey(
                    key_code,
                    modifiers,
                    hotkey_id,
                    target,
                    0,
                    ctypes.byref(ref),
                )
                if status == no_err:
                    self.carbon_refs.append(ref)
                    self.carbon_actions[hotkey_id_value] = action
                else:
                    failed.append(f"{label} ({status})")

            if self.carbon_refs:
                self.carbon_stop_event.clear()
                event_type = CarbonEventTypeSpec(k_event_class_keyboard, k_event_hot_key_pressed)
                self.carbon_thread = threading.Thread(
                    target=self._macos_carbon_loop,
                    args=(event_type,),
                    name="macos-global-hotkeys",
                    daemon=True,
                )
                self.carbon_thread.start()
                self.on_log(
                    "Global hotkeys enabled on macOS: F7 marker, Cmd+1/2/3/4, F8/F9/F10/F11, "
                    "Ctrl+Option+M/7/1/2/3/4.\n"
                )
                if failed:
                    self.on_log("Some global hotkeys were already taken: " + ", ".join(failed) + "\n")
                self.on_status("Global hotkeys: enabled")
            else:
                raise RuntimeError("macOS returned no registered hotkeys.")
        except Exception as exc:
            self._stop_macos_carbon()
            self.on_status("macOS hotkeys: app window only")
            self.on_log(
                "macOS global hotkeys could not start. Local hotkeys still work while the app is active. "
                f"Details: {exc}\n"
            )

    def _macos_carbon_loop(self, event_type: CarbonEventTypeSpec) -> None:
        carbon = self.carbon
        if not carbon:
            return
        k_event_param_direct_object = four_char_code("----")
        type_event_hot_key_id = four_char_code("hkid")
        no_err = 0
        while not self.carbon_stop_event.is_set():
            event_ref = ctypes.c_void_p()
            try:
                status = carbon.ReceiveNextEvent(
                    1,
                    ctypes.byref(event_type),
                    ctypes.c_double(0.25),
                    True,
                    ctypes.byref(event_ref),
                )
                if status != no_err or not event_ref:
                    continue
                hotkey_id = CarbonEventHotKeyID()
                param_status = carbon.GetEventParameter(
                    event_ref,
                    k_event_param_direct_object,
                    type_event_hot_key_id,
                    None,
                    ctypes.sizeof(hotkey_id),
                    None,
                    ctypes.byref(hotkey_id),
                )
                carbon.ReleaseEvent(event_ref)
                if param_status != no_err:
                    continue
                action = self.carbon_actions.get(int(hotkey_id.id))
                if action:
                    self.on_log(f"Global hotkey fired: {action}\n")
                    self.on_action(action)
            except Exception as exc:
                self.on_log(f"macOS global hotkey loop stopped: {exc}\n")
                break

    def _stop_macos_carbon(self) -> None:
        self.carbon_stop_event.set()
        carbon = self.carbon
        if carbon:
            for ref in self.carbon_refs:
                try:
                    carbon.UnregisterEventHotKey(ref)
                except Exception:
                    pass
        self.carbon_refs.clear()
        self.carbon_actions.clear()
        self.carbon_thread = None
        self.carbon = None

    def stop(self) -> None:
        if platform.system() == "Darwin" and self.mac_event_monitors:
            try:
                from AppKit import NSEvent  # type: ignore[import-not-found]
                for monitor in self.mac_event_monitors:
                    NSEvent.removeMonitor_(monitor)
            except Exception:
                pass
            self.mac_event_monitors.clear()
        self._stop_macos_carbon()
        if self.listener:
            try:
                self.listener.stop()
            except Exception:
                pass
            self.listener = None


class ToolTip:
    def __init__(self, widget: tk.Widget, text: str) -> None:
        self.widget = widget
        self.text = text
        self.window: tk.Toplevel | None = None
        widget.bind("<Enter>", self._show)
        widget.bind("<Leave>", self._hide)
        widget.bind("<FocusIn>", self._show)
        widget.bind("<FocusOut>", self._hide)

    def _show(self, _event: tk.Event | None = None) -> None:
        if self.window or not self.text:
            return
        x = self.widget.winfo_rootx() + 18
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 8
        self.window = tk.Toplevel(self.widget)
        self.window.wm_overrideredirect(True)
        self.window.wm_geometry(f"+{x}+{y}")
        label = tk.Label(
            self.window,
            text=self.text,
            bg="#F8FAFC",
            fg="#0F172A",
            padx=10,
            pady=7,
            relief="solid",
            borderwidth=1,
            font=("Arial", 10),
            justify="left",
        )
        label.pack()

    def _hide(self, _event: tk.Event | None = None) -> None:
        if self.window:
            self.window.destroy()
            self.window = None


class LiveClipperApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        ensure_tool_path()
        self.title(f"{APP_TITLE} {APP_VERSION}")
        self.geometry("1360x900")
        self.minsize(1120, 720)

        self.events: queue.Queue[tuple[str, object]] = queue.Queue()
        self.recorder = StreamRecorder(self._log_from_thread, self._status_from_thread)
        self.exporter = ClipExporter(self._log_from_thread, self._export_finished_from_thread)
        self.hotkeys = GlobalHotkeyManager(self._hotkey_from_thread, self._log_from_thread, self._hotkey_status_from_thread)
        self.export_queue: list[ClipJob] = []
        self.active_job: ClipJob | None = None
        self.last_hotkey_name = ""
        self.last_hotkey_at = 0.0
        self.last_missing_live_url_at = 0.0
        self.marker_thread: threading.Thread | None = None
        self.marker_tracking = False
        self.marker_release_timestamp: float | None = None
        self.marker_anchor_edge = 0.0
        self.marker_anchor_monotonic = 0.0
        self.marker_title = ""
        self.marker_url = ""
        self.marker_file_path: Path | None = None
        self.save_dir = Path.home() / "Movies" / "ERNI Live Clipper"
        self.save_dir.mkdir(parents=True, exist_ok=True)

        self.url_var = tk.StringVar()
        self.folder_var = tk.StringVar(value=str(self.save_dir))
        self.status_var = tk.StringVar(value="Ready")
        self.queue_var = tk.StringVar(value="Queue: 0")
        self.hotkey_status_var = tk.StringVar(value="Global hotkeys: starting")
        self.timer_var = tk.StringVar(value="00:00:00")
        self.marker_timer_var = tk.StringVar(value="00:00:00")
        self.marker_state_var = tk.StringVar(value="Not tracking")
        self.marker_file_var = tk.StringVar(value="Markers file: not created yet")
        self.marker_note_var = tk.StringVar(value="")
        self.marker_before_var = tk.IntVar(value=30)
        self.marker_after_var = tk.IntVar(value=10)
        self.tools_var = tk.StringVar(value=self._tools_status())
        self.export_mode_var = tk.StringVar(value=EXPORT_MODES[0])
        self.label_var = tk.StringVar(value="")
        self.comment_var = tk.StringVar(value="")
        self.start_var = tk.StringVar(value="00:00:00")
        self.end_var = tk.StringVar(value="00:01:00")
        self.range_from_var = tk.StringVar(value="00:25:00")
        self.range_to_var = tk.StringVar(value="00:25:15")
        self.vod_url_var = tk.StringVar()
        self.vod_from_var = tk.StringVar(value="01:44:18")
        self.vod_to_var = tk.StringVar(value="01:46:12")
        self.vod_label_var = tk.StringVar(value="")
        self.moment_var = tk.StringVar(value="00:00:00")
        self.preroll_var = tk.IntVar(value=30)
        self.after_var = tk.IntVar(value=10)
        self.master_file_var = tk.StringVar(value="Master: not recording")

        self._configure_style()
        self._build_ui()
        self._bind_shortcuts()
        self.after(300, self.hotkeys.start)
        self.after(250, self._poll_events)
        self.after(500, self._tick)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _configure_style(self) -> None:
        self.configure(bg="#0B0F17")
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure(".", font=("Arial", 12), background="#0B0F17", foreground="#F8FAFC")
        style.configure("TFrame", background="#0B0F17")
        style.configure("Panel.TFrame", background="#111827")
        style.configure("Soft.TFrame", background="#182033")
        style.configure("TLabel", background="#0B0F17", foreground="#F8FAFC")
        style.configure("Muted.TLabel", background="#0B0F17", foreground="#B6C2D4", font=("Arial", 10))
        style.configure("Panel.TLabel", background="#111827", foreground="#F8FAFC")
        style.configure("PanelMuted.TLabel", background="#111827", foreground="#B6C2D4", font=("Arial", 10))
        style.configure("Section.TLabel", background="#111827", foreground="#FFFFFF", font=("Arial", 15, "bold"))
        style.configure("Title.TLabel", background="#0B0F17", foreground="#FFFFFF", font=("Arial", 28, "bold"))
        style.configure("Subtitle.TLabel", background="#0B0F17", foreground="#B6C2D4", font=("Arial", 11))
        style.configure("Timer.TLabel", background="#182033", foreground="#FFFFFF", font=("Arial", 38, "bold"))
        style.configure("Status.TLabel", background="#182033", foreground="#8CF7CF", font=("Arial", 13, "bold"))
        style.configure("SmallStatus.TLabel", background="#182033", foreground="#B6C2D4", font=("Arial", 10, "bold"))
        style.configure("TButton", padding=(16, 11), font=("Arial", 11, "bold"), background="#263244", foreground="#FFFFFF", borderwidth=1, focusthickness=3, focuscolor="#93C5FD")
        style.map("TButton", background=[("active", "#334155"), ("focus", "#334155"), ("disabled", "#1E293B")], foreground=[("disabled", "#7B8798")])
        style.configure("Accent.TButton", background="#0A84FF", foreground="#FFFFFF")
        style.map("Accent.TButton", background=[("active", "#339BFF"), ("focus", "#339BFF")])
        style.configure("Success.TButton", background="#0F8B68", foreground="#FFFFFF")
        style.map("Success.TButton", background=[("active", "#12A77D"), ("focus", "#12A77D")])
        style.configure("Danger.TButton", background="#C2415A", foreground="#FFFFFF")
        style.map("Danger.TButton", background=[("active", "#E0526E"), ("focus", "#E0526E")])
        style.configure("Tag.TButton", padding=(10, 8), font=("Arial", 10, "bold"), background="#1E293B", foreground="#EAF2FF")
        style.map("Tag.TButton", background=[("active", "#334155"), ("focus", "#334155")])
        style.configure("TEntry", fieldbackground="#070A10", foreground="#FFFFFF", insertcolor="#FFFFFF", padding=9, bordercolor="#46546A", lightcolor="#46546A", darkcolor="#46546A")
        style.configure("TSpinbox", fieldbackground="#070A10", foreground="#FFFFFF", insertcolor="#FFFFFF", padding=8, arrowsize=14)
        style.configure("TCombobox", fieldbackground="#070A10", background="#070A10", foreground="#FFFFFF", padding=9, arrowcolor="#FFFFFF")
        style.configure("Treeview", background="#0B1220", fieldbackground="#0B1220", foreground="#F8FAFC", rowheight=34, bordercolor="#334155", borderwidth=1)
        style.map("Treeview", background=[("selected", "#1D4ED8")], foreground=[("selected", "#FFFFFF")])
        style.configure("Treeview.Heading", background="#1E293B", foreground="#FFFFFF", font=("Arial", 10, "bold"))
        style.configure("TSeparator", background="#334155")

    def _build_menu(self) -> None:
        menu = tk.Menu(self)
        self.configure(menu=menu)

        clip_menu = tk.Menu(menu, tearoff=False)
        menu.add_cascade(label="Clips", menu=clip_menu)
        clip_menu.add_command(
            label="Write stream marker",
            accelerator="F7 / Ctrl+Alt+7",
            command=self._shortcut_write_marker,
        )
        clip_menu.add_separator()
        clip_menu.add_command(
            label="Last 30 seconds",
            accelerator="Cmd+1" if platform.system() == "Darwin" else "Ctrl+Alt+1",
            command=lambda: self._shortcut_quick_clip(30),
        )
        clip_menu.add_command(
            label="Last 60 seconds",
            accelerator="Cmd+2" if platform.system() == "Darwin" else "Ctrl+Alt+2",
            command=lambda: self._shortcut_quick_clip(60),
        )
        clip_menu.add_command(
            label="Last 3 minutes",
            accelerator="Cmd+3" if platform.system() == "Darwin" else "Ctrl+Alt+3",
            command=lambda: self._shortcut_quick_clip(180),
        )
        clip_menu.add_command(
            label="Exact range",
            accelerator="Cmd+4" if platform.system() == "Darwin" else "Ctrl+Alt+4",
            command=self._shortcut_direct_clip,
        )
        clip_menu.add_separator()
        clip_menu.add_command(
            label="Mark moment",
            accelerator="F11",
            command=self._shortcut_mark_moment,
        )
        clip_menu.add_command(
            label="Open folder",
            accelerator="Cmd+O" if platform.system() == "Darwin" else "Ctrl+O",
            command=self._shortcut_open_folder,
        )

    def _build_ui(self) -> None:
        self._build_menu()
        root = ttk.Frame(self, padding=24)
        root.pack(fill="both", expand=True)

        header = ttk.Frame(root)
        header.pack(fill="x", pady=(0, 18))
        title_box = ttk.Frame(header)
        title_box.pack(side="left")
        ttk.Label(title_box, text="ERNI Live Clipper", style="Title.TLabel").pack(anchor="w")
        ttk.Label(title_box, text="Live moments, tags, comments, queue and edit-ready MP4.", style="Subtitle.TLabel").pack(anchor="w", pady=(2, 0))
        status_box = ttk.Frame(header)
        status_box.pack(side="right", pady=(4, 0))
        ttk.Label(status_box, textvariable=self.tools_var, style="Muted.TLabel").pack(anchor="e")
        ttk.Label(status_box, textvariable=self.hotkey_status_var, style="Muted.TLabel").pack(anchor="e", pady=(4, 0))

        body = ttk.Frame(root)
        body.pack(fill="both", expand=True)
        body.columnconfigure(0, weight=4)
        body.columnconfigure(1, weight=3)
        body.rowconfigure(0, weight=1)

        left = ttk.Frame(body, style="Panel.TFrame", padding=0)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 14))
        right = ttk.Frame(body, style="Panel.TFrame", padding=20)
        right.grid(row=0, column=1, sticky="nsew")

        self.notebook = ttk.Notebook(left)
        self.notebook.pack(fill="both", expand=True)
        self.marker_tab = ttk.Frame(self.notebook, style="Panel.TFrame", padding=20)
        self.live_tab = ttk.Frame(self.notebook, style="Panel.TFrame", padding=20)
        self.saved_tab = ttk.Frame(self.notebook, style="Panel.TFrame", padding=20)
        self.notebook.add(self.marker_tab, text="Stream Markers")
        self.notebook.add(self.live_tab, text="Live Clipper")
        self.notebook.add(self.saved_tab, text="Saved Video Range")

        self._build_marker_panel(self.marker_tab)
        self._build_capture_panel(self.live_tab)
        self._build_clip_panel(self.live_tab)
        self._build_saved_video_panel(self.saved_tab)
        self._build_log_panel(right)
        self._build_clips_table(right)

    def _build_marker_panel(self, parent: ttk.Frame) -> None:
        ttk.Label(parent, text="1. Stream Marker", style="Section.TLabel").pack(anchor="w")
        ttk.Label(
            parent,
            text="Вставь live-ссылку, нажми Start Tracking и ставь метки в TXT без скачивания видео.",
            style="PanelMuted.TLabel",
        ).pack(anchor="w", pady=(4, 12))

        url_row = ttk.Frame(parent, style="Panel.TFrame")
        url_row.pack(fill="x")
        self.marker_url_entry = ttk.Entry(url_row, textvariable=self.url_var)
        self.marker_url_entry.pack(side="left", fill="x", expand=True)
        paste_button = ttk.Button(url_row, text="Paste", command=lambda: self._paste_url(self.marker_url_entry))
        paste_button.pack(side="left", padx=(10, 0))
        ToolTip(paste_button, "Вставить ссылку на live из буфера обмена.")

        folder_row = ttk.Frame(parent, style="Panel.TFrame")
        folder_row.pack(fill="x", pady=(10, 0))
        ttk.Entry(folder_row, textvariable=self.folder_var).pack(side="left", fill="x", expand=True)
        folder_button = ttk.Button(folder_row, text="Folder", command=self._choose_folder)
        folder_button.pack(side="left", padx=(10, 0))
        ToolTip(folder_button, "Папка, где будет создан TXT с таймкодами.")

        status = ttk.Frame(parent, style="Soft.TFrame", padding=18)
        status.pack(fill="x", pady=(18, 14))
        ttk.Label(status, textvariable=self.marker_timer_var, style="Timer.TLabel").pack(side="left")
        status_right = ttk.Frame(status, style="Soft.TFrame")
        status_right.pack(side="right", fill="x", expand=True, padx=(18, 0))
        ttk.Label(status_right, textvariable=self.marker_state_var, style="Status.TLabel").pack(anchor="e")
        ttk.Label(
            status_right,
            textvariable=self.marker_file_var,
            background="#182033",
            foreground="#B6C2D4",
            font=("Arial", 10),
            wraplength=420,
            justify="right",
        ).pack(anchor="e", pady=(6, 0))

        controls = ttk.Frame(parent, style="Panel.TFrame")
        controls.pack(fill="x")
        start_button = ttk.Button(controls, text="Start Tracking", style="Success.TButton", command=self._start_marker_tracking)
        start_button.pack(side="left", fill="x", expand=True)
        mark_button = ttk.Button(controls, text="Mark Funny Moment", style="Accent.TButton", command=self._write_stream_marker)
        mark_button.pack(side="left", fill="x", expand=True, padx=(10, 0))
        open_button = ttk.Button(controls, text="Open TXT", command=self._open_marker_file)
        open_button.pack(side="left", padx=(10, 0))
        ToolTip(start_button, "Один раз определить текущий live-таймкод и дальше считать время локально.")
        ToolTip(mark_button, "Записать метку в TXT. Хоткей: F7 или Ctrl+Option+M / Ctrl+Alt+M.")
        ToolTip(open_button, "Открыть TXT с метками.")

        range_box = ttk.Frame(parent, style="Soft.TFrame", padding=14)
        range_box.pack(fill="x", pady=(18, 0))
        ttk.Label(range_box, text="Range written to TXT", background="#182033", foreground="#FFFFFF", font=("Arial", 13, "bold")).pack(anchor="w")
        ttk.Label(
            range_box,
            text="При метке в документ попадет текущий момент и готовый диапазон From -> To для вкладки Saved Video Range.",
            background="#182033",
            foreground="#B6C2D4",
            font=("Arial", 10),
            wraplength=700,
            justify="left",
        ).pack(anchor="w", pady=(3, 10))
        range_row = ttk.Frame(range_box, style="Soft.TFrame")
        range_row.pack(fill="x")
        ttk.Label(range_row, text="Before", background="#182033", foreground="#B6C2D4", font=("Arial", 10)).pack(side="left")
        ttk.Spinbox(range_row, from_=0, to=3600, textvariable=self.marker_before_var, width=7).pack(side="left", padx=(8, 14))
        ttk.Label(range_row, text="After", background="#182033", foreground="#B6C2D4", font=("Arial", 10)).pack(side="left")
        ttk.Spinbox(range_row, from_=0, to=3600, textvariable=self.marker_after_var, width=7).pack(side="left", padx=(8, 0))

        note_box = ttk.Frame(parent, style="Panel.TFrame")
        note_box.pack(fill="x", pady=(16, 0))
        ttk.Label(note_box, text="Marker note", style="PanelMuted.TLabel").pack(anchor="w")
        ttk.Entry(note_box, textvariable=self.marker_note_var).pack(fill="x", pady=(4, 0))

        help_box = ttk.Frame(parent, style="Soft.TFrame", padding=14)
        help_box.pack(fill="x", pady=(18, 0))
        ttk.Label(
            help_box,
            text="Сценарий: Start Tracking -> на смешном моменте F7 -> после стрима открыть TXT -> скопировать ссылку и From/To во вкладку Saved Video Range.",
            background="#182033",
            foreground="#DDE7F7",
            font=("Arial", 10),
            wraplength=720,
            justify="left",
        ).pack(anchor="w")

    def _build_capture_panel(self, parent: ttk.Frame) -> None:
        ttk.Label(parent, text="1. Live Source", style="Section.TLabel").pack(anchor="w")
        ttk.Label(parent, text="Вставь YouTube live. Основной режим скачивает только выбранные моменты.", style="PanelMuted.TLabel").pack(anchor="w", pady=(4, 12))

        url_row = ttk.Frame(parent, style="Panel.TFrame")
        url_row.pack(fill="x")
        self.url_entry = ttk.Entry(url_row, textvariable=self.url_var)
        self.url_entry.pack(side="left", fill="x", expand=True)
        paste_button = ttk.Button(url_row, text="Paste", command=self._paste_url)
        paste_button.pack(side="left", padx=(10, 0))
        ToolTip(paste_button, "Вставить ссылку на live из буфера обмена.")

        folder_row = ttk.Frame(parent, style="Panel.TFrame")
        folder_row.pack(fill="x", pady=(10, 0))
        ttk.Entry(folder_row, textvariable=self.folder_var).pack(side="left", fill="x", expand=True)
        folder_button = ttk.Button(folder_row, text="Folder", command=self._choose_folder)
        folder_button.pack(side="left", padx=(10, 0))
        ToolTip(folder_button, "Выбрать папку, куда сохранять клипы и CSV-лог.")

        status = ttk.Frame(parent, style="Soft.TFrame", padding=16)
        status.pack(fill="x", pady=(16, 14))
        ttk.Label(status, textvariable=self.timer_var, style="Timer.TLabel").pack(side="left")
        status_right = ttk.Frame(status, style="Soft.TFrame")
        status_right.pack(side="right", fill="x", expand=True, padx=(18, 0))
        ttk.Label(status_right, textvariable=self.status_var, style="Status.TLabel").pack(anchor="e")
        ttk.Label(status_right, textvariable=self.queue_var, style="SmallStatus.TLabel").pack(anchor="e", pady=(5, 0))
        ttk.Label(status_right, textvariable=self.master_file_var, background="#182033", foreground="#B6C2D4", font=("Arial", 10)).pack(anchor="e", pady=(5, 0))

        controls = ttk.Frame(parent, style="Panel.TFrame")
        controls.pack(fill="x")
        self.start_button = ttk.Button(controls, text="Start Capture", style="Success.TButton", command=self._start_capture)
        self.start_button.pack(side="left", fill="x", expand=True)
        self.stop_button = ttk.Button(controls, text="Stop", style="Danger.TButton", command=self._stop_capture, state="disabled")
        self.stop_button.pack(side="left", padx=(10, 0))
        open_button = ttk.Button(controls, text="Open", command=self._open_folder)
        open_button.pack(side="left", padx=(10, 0))
        hotkeys_button = ttk.Button(controls, text="Hotkeys", command=self._open_hotkeys_doc)
        hotkeys_button.pack(side="left", padx=(10, 0))
        ToolTip(self.start_button, "Fallback: записывать весь live локально, если прямое скачивание не подходит.")
        ToolTip(self.stop_button, "Остановить запись или отменить текущий экспорт.")
        ToolTip(open_button, "Открыть папку сохранения.")
        ToolTip(hotkeys_button, "Открыть документацию по горячим клавишам.")

    def _build_clip_panel(self, parent: ttk.Frame) -> None:
        sep = ttk.Separator(parent)
        sep.pack(fill="x", pady=18)
        ttk.Label(parent, text="2. Clip Exporter", style="Section.TLabel").pack(anchor="w")
        ttk.Label(parent, text="Быстрые кнопки берут последние секунды live. Хоткеи: F8=30s, F9=60s, F10=3m, F11=Mark.", style="PanelMuted.TLabel").pack(anchor="w", pady=(4, 12))

        quick = ttk.Frame(parent, style="Panel.TFrame")
        quick.pack(fill="x")
        for title, seconds in QUICK_CLIPS:
            button = ttk.Button(quick, text=f"Last {title}", style="Accent.TButton", command=lambda value=seconds: self._quick_clip(value))
            button.pack(side="left", padx=(0, 8))
            ToolTip(button, f"Скачать последние {title} live и поставить в очередь, если уже идет экспорт.")
        mark_button = ttk.Button(quick, text="Mark moment", command=self._mark_moment)
        mark_button.pack(side="left", padx=(8, 0))
        ToolTip(mark_button, "Создать клип вокруг текущего момента записи или значения To.")

        tags = ttk.Frame(parent, style="Panel.TFrame")
        tags.pack(fill="x", pady=(12, 0))
        ttk.Label(tags, text="Tags:", style="PanelMuted.TLabel").grid(row=0, column=0, sticky="w", padx=(0, 8), pady=(0, 6))
        for index, tag in enumerate(TAG_PRESETS):
            button = ttk.Button(tags, text=tag, style="Tag.TButton", command=lambda value=tag: self._set_tag(value))
            button.grid(row=index // 3, column=(index % 3) + 1, sticky="ew", padx=(0, 6), pady=(0, 6))
            tags.columnconfigure((index % 3) + 1, weight=1)
            ToolTip(button, f"Поставить тег {tag}. Клип сохранится в папку этого тега.")
        clear_tag = ttk.Button(tags, text="Clear", command=lambda: self.label_var.set(""))
        clear_tag.grid(row=1, column=0, sticky="ew", padx=(0, 8), pady=(0, 6))
        ToolTip(clear_tag, "Очистить тег. Клип попадет в папку untagged.")

        range_box = ttk.Frame(parent, style="Soft.TFrame", padding=12)
        range_box.pack(fill="x", pady=(14, 0))
        ttk.Label(range_box, text="Exact range from live stream", background="#182033", foreground="#FFFFFF", font=("Arial", 12, "bold")).pack(anchor="w")
        ttk.Label(
            range_box,
            text="Введи от и до, например 00:25:00 -> 00:25:15. Скачается ровно этот кусок live.",
            background="#182033",
            foreground="#B6C2D4",
            font=("Arial", 10),
        ).pack(anchor="w", pady=(2, 8))
        range_row = ttk.Frame(range_box, style="Soft.TFrame")
        range_row.pack(fill="x")
        ttk.Label(range_row, text="From", background="#182033", foreground="#B6C2D4", font=("Arial", 10)).pack(side="left")
        from_entry = ttk.Entry(range_row, textvariable=self.range_from_var, width=12)
        from_entry.pack(side="left", padx=(8, 12))
        ttk.Label(range_row, text="To", background="#182033", foreground="#B6C2D4", font=("Arial", 10)).pack(side="left")
        to_entry = ttk.Entry(range_row, textvariable=self.range_to_var, width=12)
        to_entry.pack(side="left", padx=(8, 12))
        range_button = ttk.Button(range_row, text="Download Exact Range", style="Accent.TButton", command=self._download_exact_range)
        range_button.pack(side="left", fill="x", expand=True)
        ToolTip(from_entry, "Начало клипа. Например: 00:25:00, 25:00 или 1500.")
        ToolTip(to_entry, "Конец клипа. Например: 00:25:15, 25:15 или 1515.")
        ToolTip(range_button, "Скачать ровно диапазон From-To из live-ссылки или из локальной записи.")

        form = ttk.Frame(parent, style="Panel.TFrame")
        form.pack(fill="x", pady=(14, 0))
        form.columnconfigure(1, weight=1)
        ttk.Label(form, text="Tag / label", style="PanelMuted.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Entry(form, textvariable=self.label_var, width=16).grid(row=1, column=0, sticky="ew", padx=(0, 10))
        ttk.Label(form, text="Mode", style="PanelMuted.TLabel").grid(row=0, column=1, sticky="w")
        ttk.Combobox(form, textvariable=self.export_mode_var, values=EXPORT_MODES, state="readonly").grid(row=1, column=1, sticky="ew")

        comment = ttk.Frame(parent, style="Panel.TFrame")
        comment.pack(fill="x", pady=(12, 0))
        ttk.Label(comment, text="Comment for montage log", style="PanelMuted.TLabel").pack(anchor="w")
        ttk.Entry(comment, textvariable=self.comment_var).pack(fill="x", pady=(4, 0))

        offsets = ttk.Frame(parent, style="Panel.TFrame")
        offsets.pack(fill="x", pady=(12, 0))
        ttk.Label(offsets, text="Moment clip:", style="PanelMuted.TLabel").pack(side="left")
        ttk.Label(offsets, text="before", style="PanelMuted.TLabel").pack(side="left", padx=(12, 4))
        ttk.Spinbox(offsets, from_=0, to=3600, textvariable=self.preroll_var, width=6).pack(side="left")
        ttk.Label(offsets, text="after", style="PanelMuted.TLabel").pack(side="left", padx=(12, 4))
        ttk.Spinbox(offsets, from_=0, to=3600, textvariable=self.after_var, width=6).pack(side="left")

    def _build_saved_video_panel(self, parent: ttk.Frame) -> None:
        ttk.Label(parent, text="Saved Video Range", style="Section.TLabel").pack(anchor="w")
        ttk.Label(
            parent,
            text="Для законченных стримов и обычных видео: скачивает только выбранный диапазон, не весь ролик.",
            style="PanelMuted.TLabel",
        ).pack(anchor="w", pady=(4, 14))

        url_box = ttk.Frame(parent, style="Panel.TFrame")
        url_box.pack(fill="x")
        ttk.Label(url_box, text="Finished stream / video URL", style="PanelMuted.TLabel").pack(anchor="w")
        url_row = ttk.Frame(url_box, style="Panel.TFrame")
        url_row.pack(fill="x", pady=(4, 0))
        self.vod_url_entry = ttk.Entry(url_row, textvariable=self.vod_url_var)
        self.vod_url_entry.pack(side="left", fill="x", expand=True)
        paste_button = ttk.Button(url_row, text="Paste", command=self._paste_vod_url)
        paste_button.pack(side="left", padx=(10, 0))
        ToolTip(paste_button, "Вставить ссылку на законченный стрим или видео из буфера.")

        range_box = ttk.Frame(parent, style="Soft.TFrame", padding=14)
        range_box.pack(fill="x", pady=(18, 0))
        ttk.Label(range_box, text="Range to download", background="#182033", foreground="#FFFFFF", font=("Arial", 13, "bold")).pack(anchor="w")
        ttk.Label(
            range_box,
            text="Пример: From 01:44:18 -> To 01:46:12. Поддерживает HH:MM:SS, MM:SS и секунды.",
            background="#182033",
            foreground="#B6C2D4",
            font=("Arial", 10),
        ).pack(anchor="w", pady=(3, 10))
        row = ttk.Frame(range_box, style="Soft.TFrame")
        row.pack(fill="x")
        ttk.Label(row, text="From", background="#182033", foreground="#B6C2D4", font=("Arial", 10)).pack(side="left")
        from_entry = ttk.Entry(row, textvariable=self.vod_from_var, width=13)
        from_entry.pack(side="left", padx=(8, 14))
        ttk.Label(row, text="To", background="#182033", foreground="#B6C2D4", font=("Arial", 10)).pack(side="left")
        to_entry = ttk.Entry(row, textvariable=self.vod_to_var, width=13)
        to_entry.pack(side="left", padx=(8, 0))
        ToolTip(from_entry, "Начало фрагмента. Например: 01:44:18.")
        ToolTip(to_entry, "Конец фрагмента. Например: 01:46:12.")

        options = ttk.Frame(parent, style="Panel.TFrame")
        options.pack(fill="x", pady=(16, 0))
        options.columnconfigure(0, weight=1)
        options.columnconfigure(1, weight=1)
        ttk.Label(options, text="Tag / label", style="PanelMuted.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Entry(options, textvariable=self.vod_label_var).grid(row=1, column=0, sticky="ew", padx=(0, 10))
        ttk.Label(options, text="Mode", style="PanelMuted.TLabel").grid(row=0, column=1, sticky="w")
        ttk.Combobox(options, textvariable=self.export_mode_var, values=EXPORT_MODES, state="readonly").grid(row=1, column=1, sticky="ew")

        buttons = ttk.Frame(parent, style="Panel.TFrame")
        buttons.pack(fill="x", pady=(18, 0))
        download_button = ttk.Button(
            buttons,
            text="Download Saved Video Range",
            style="Accent.TButton",
            command=self._download_vod_range,
        )
        download_button.pack(side="left", fill="x", expand=True)
        open_button = ttk.Button(buttons, text="Open Folder", command=self._open_folder)
        open_button.pack(side="left", padx=(10, 0))
        ToolTip(download_button, "Скачать только выбранный диапазон готового видео в максимальном доступном качестве.")
        ToolTip(open_button, "Открыть папку сохранения.")

        note = ttk.Frame(parent, style="Soft.TFrame", padding=14)
        note.pack(fill="x", pady=(18, 0))
        ttk.Label(
            note,
            text="Для монтажа оставь Universal Editing MP4. Для максимально быстрого сохранения без перекодирования выбери Original Fast.",
            background="#182033",
            foreground="#DDE7F7",
            font=("Arial", 10),
            wraplength=620,
            justify="left",
        ).pack(anchor="w")


    def _build_log_panel(self, parent: ttk.Frame) -> None:
        ttk.Label(parent, text="3. Live Log", style="Section.TLabel").pack(anchor="w")
        ttk.Label(parent, text="Команды, ошибки и прогресс загрузки.", style="PanelMuted.TLabel").pack(anchor="w", pady=(4, 0))
        log_frame = ttk.Frame(parent, style="Panel.TFrame")
        log_frame.pack(fill="both", expand=True, pady=(12, 14))
        self.log_text = tk.Text(
            log_frame,
            height=15,
            bg="#070A10",
            fg="#EAF2FF",
            insertbackground="#FFFFFF",
            selectbackground="#1D4ED8",
            selectforeground="#FFFFFF",
            relief="solid",
            borderwidth=1,
            wrap="word",
            font=("Menlo", 11) if platform.system() == "Darwin" else ("Consolas", 10),
        )
        scrollbar = ttk.Scrollbar(log_frame, orient="vertical", command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=scrollbar.set)
        self.log_text.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

    def _build_clips_table(self, parent: ttk.Frame) -> None:
        ttk.Label(parent, text="4. Created Clips", style="Section.TLabel").pack(anchor="w")
        ttk.Label(parent, text="Выбери клип и нажми Preview или Reveal file.", style="PanelMuted.TLabel").pack(anchor="w", pady=(4, 0))
        self.clips = ttk.Treeview(parent, columns=("range", "tag", "comment", "mode", "file"), show="headings", height=7)
        self.clips.heading("range", text="Range")
        self.clips.heading("tag", text="Tag")
        self.clips.heading("comment", text="Comment")
        self.clips.heading("mode", text="Mode")
        self.clips.heading("file", text="File")
        self.clips.column("range", width=120, anchor="w")
        self.clips.column("tag", width=70, anchor="w")
        self.clips.column("comment", width=140, anchor="w")
        self.clips.column("mode", width=130, anchor="w")
        self.clips.column("file", width=220, anchor="w")
        self.clips.pack(fill="x", pady=(12, 8))
        actions = ttk.Frame(parent, style="Panel.TFrame")
        actions.pack(fill="x")
        preview_button = ttk.Button(actions, text="Preview selected", style="Accent.TButton", command=self._open_selected_clip)
        preview_button.pack(side="left")
        reveal_button = ttk.Button(actions, text="Reveal file", command=self._reveal_selected_clip)
        reveal_button.pack(side="left", padx=(10, 0))
        clear_button = ttk.Button(actions, text="Clear log", command=lambda: self.log_text.delete("1.0", "end"))
        clear_button.pack(side="left", padx=(10, 0))
        ToolTip(preview_button, "Открыть выбранный клип в стандартном видеоплеере.")
        ToolTip(reveal_button, "Показать выбранный файл в Finder или Explorer.")
        ToolTip(clear_button, "Очистить только видимый лог. Файлы и CSV не удаляются.")

    def _bind_shortcuts(self) -> None:
        def bind(sequence: str, callback) -> None:
            try:
                self.bind_all(sequence, callback, add="+")
            except tk.TclError:
                pass

        bind("<Command-v>", self._paste_into_focused_widget)
        bind("<Command-V>", self._paste_into_focused_widget)
        bind("<Control-v>", self._paste_into_focused_widget)
        bind("<Control-V>", self._paste_into_focused_widget)

        for sequence in ("<F7>", "<KeyPress-F7>"):
            bind(sequence, lambda _event: self._shortcut_write_marker())
        for sequence in ("<F8>", "<KeyPress-F8>"):
            bind(sequence, lambda _event: self._shortcut_quick_clip(30))
        for sequence in ("<F9>", "<KeyPress-F9>"):
            bind(sequence, lambda _event: self._shortcut_quick_clip(60))
        for sequence in ("<F10>", "<KeyPress-F10>"):
            bind(sequence, lambda _event: self._shortcut_quick_clip(180))
        for sequence in ("<F11>", "<KeyPress-F11>"):
            bind(sequence, lambda _event: self._shortcut_mark_moment())

        if platform.system() == "Darwin":
            for sequence in (
                "<Command-KeyPress-1>", "<Command-KP_1>",
                "<Command-KeyPress-2>", "<Command-KP_2>",
                "<Command-KeyPress-3>", "<Command-KP_3>",
                "<Command-KeyPress-4>", "<Command-KP_4>",
            ):
                action = {
                    "1": lambda: self._shortcut_quick_clip(30),
                    "KP_1": lambda: self._shortcut_quick_clip(30),
                    "2": lambda: self._shortcut_quick_clip(60),
                    "KP_2": lambda: self._shortcut_quick_clip(60),
                    "3": lambda: self._shortcut_quick_clip(180),
                    "KP_3": lambda: self._shortcut_quick_clip(180),
                    "4": self._shortcut_direct_clip,
                    "KP_4": self._shortcut_direct_clip,
                }[sequence.rsplit("-", 1)[-1].rstrip(">")]
                bind(sequence, lambda _event, callback=action: callback())
            bind("<Command-KeyPress-Return>", lambda _event: self._shortcut_direct_clip())

        for prefix in ("Control-Alt", "Control-Mod1", "Control-Option"):
            bind(f"<{prefix}-KeyPress-1>", lambda _event: self._shortcut_quick_clip(30))
            bind(f"<{prefix}-KeyPress-2>", lambda _event: self._shortcut_quick_clip(60))
            bind(f"<{prefix}-KeyPress-3>", lambda _event: self._shortcut_quick_clip(180))
            bind(f"<{prefix}-KeyPress-4>", lambda _event: self._shortcut_direct_clip())
            bind(f"<{prefix}-KP_1>", lambda _event: self._shortcut_quick_clip(30))
            bind(f"<{prefix}-KP_2>", lambda _event: self._shortcut_quick_clip(60))
            bind(f"<{prefix}-KP_3>", lambda _event: self._shortcut_quick_clip(180))
            bind(f"<{prefix}-KP_4>", lambda _event: self._shortcut_direct_clip())

        bind("<Control-Alt-KeyPress-Return>", lambda _event: self._shortcut_direct_clip())
        bind("<Control-Mod1-KeyPress-Return>", lambda _event: self._shortcut_direct_clip())
        for sequence in (
            "<Control-Option-m>", "<Control-Option-M>",
            "<Control-Mod1-m>", "<Control-Mod1-M>",
            "<Control-Alt-m>", "<Control-Alt-M>",
            "<Control-Option-7>", "<Control-Mod1-7>", "<Control-Alt-7>",
        ):
            bind(sequence, lambda _event: self._shortcut_write_marker())
        bind("<Escape>", lambda _event: self._shortcut_cancel_export())
        bind("<Command-o>", lambda _event: self._shortcut_open_folder())
        bind("<Control-o>", lambda _event: self._shortcut_open_folder())
        bind("<Command-l>", lambda _event: self._shortcut_focus_url())
        bind("<Control-l>", lambda _event: self._shortcut_focus_url())
        bind("<KeyPress>", self._handle_keypress_shortcut)
        bind("<KeyRelease>", self._handle_keypress_shortcut)

    def _paste_url(self, entry: tk.Widget | None = None) -> None:
        try:
            text = self.clipboard_get().strip()
        except tk.TclError:
            text = ""
        if not text:
            messagebox.showwarning(APP_TITLE, "Буфер обмена пустой.")
            return
        self.url_var.set(text)
        target = entry or getattr(self, "url_entry", None) or getattr(self, "marker_url_entry", None)
        if target:
            target.focus_set()
            if hasattr(target, "icursor"):
                target.icursor("end")

    def _paste_vod_url(self) -> None:
        try:
            text = self.clipboard_get().strip()
        except tk.TclError:
            text = ""
        if not text:
            messagebox.showwarning(APP_TITLE, "Буфер обмена пустой.")
            return
        self.vod_url_var.set(text)
        self.vod_url_entry.focus_set()
        self.vod_url_entry.icursor("end")

    def _focus_live_url(self) -> None:
        try:
            self.notebook.select(self.live_tab)
        except Exception:
            pass
        if hasattr(self, "url_entry"):
            self.url_entry.focus_set()
            self.url_entry.selection_range(0, "end")

    def _live_url(self) -> str:
        return self.url_var.get().strip()

    def _warn_missing_live_url(
        self,
        message: str = "Вставь ссылку на live-стрим во вкладке Live Clipper.",
        *,
        focus_live_tab: bool = True,
    ) -> None:
        if focus_live_tab:
            self._focus_live_url()
        elif hasattr(self, "marker_url_entry"):
            try:
                self.notebook.select(self.marker_tab)
            except Exception:
                pass
            self.marker_url_entry.focus_set()
            self.marker_url_entry.selection_range(0, "end")
        self.status_var.set("Waiting for live URL")
        now = time.monotonic()
        if now - self.last_missing_live_url_at < 1.5:
            return
        self.last_missing_live_url_at = now
        messagebox.showwarning(APP_TITLE, message)

    def _require_live_url(self, message: str = "Вставь ссылку на live-стрим во вкладке Live Clipper.") -> str:
        url = self._live_url()
        if not url:
            self._warn_missing_live_url(message)
            raise RuntimeError(message)
        return url

    def _paste_into_focused_widget(self, event: tk.Event) -> str:
        try:
            text = self.clipboard_get()
        except tk.TclError:
            return "break"
        widget = self.focus_get()
        if isinstance(widget, (tk.Entry, ttk.Entry, ttk.Spinbox, tk.Text)):
            try:
                widget.event_generate("<<Paste>>")
            except tk.TclError:
                if isinstance(widget, tk.Text):
                    widget.insert("insert", text)
                else:
                    widget.insert("insert", text)
            return "break"
        self.url_var.set(text.strip())
        self.url_entry.focus_set()
        self.url_entry.icursor("end")
        return "break"

    def _choose_folder(self) -> None:
        selected = filedialog.askdirectory(initialdir=str(self.save_dir))
        if selected:
            self.save_dir = Path(selected)
            self.folder_var.set(str(self.save_dir))

    def _set_tag(self, tag: str) -> None:
        self.label_var.set(tag)

    def _marker_output_path(self) -> Path:
        root = Path(self.folder_var.get()).expanduser()
        markers_dir = root / "markers"
        markers_dir.mkdir(parents=True, exist_ok=True)
        return markers_dir / f"stream_markers_{datetime.now().strftime('%Y-%m-%d')}.txt"

    def _start_marker_tracking(self) -> None:
        url = self._live_url()
        if not url:
            self._warn_missing_live_url(
                "Вставь ссылку на live-стрим, потом нажми Start Tracking.",
                focus_live_tab=False,
            )
            return
        if self.marker_thread and self.marker_thread.is_alive():
            return
        self.save_dir = Path(self.folder_var.get()).expanduser()
        self.marker_state_var.set("Resolving live")
        self.status_var.set("Resolving marker stream")
        self._append_log("\nMarker tracker: resolving live time...\n")
        self.marker_thread = threading.Thread(
            target=self._marker_resolve_thread,
            args=(url,),
            name="stream-marker-resolver",
            daemon=True,
        )
        self.marker_thread.start()

    def _marker_resolve_thread(self, url: str) -> None:
        try:
            yt_dlp = find_executable("yt-dlp")
            if not yt_dlp:
                raise RuntimeError("yt-dlp не найден.")
            command = [
                yt_dlp,
                "--no-playlist",
                "--no-color",
                "--no-warnings",
                "--extractor-args",
                YOUTUBE_EXTRACTOR_ARGS,
                "--live-from-start",
                "-J",
                url,
            ]
            completed = subprocess.run(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )
            if completed.returncode != 0:
                detail = completed.stderr.strip() or "yt-dlp не смог получить live-поток."
                raise RuntimeError(detail)
            info = json.loads(completed.stdout)
            epoch = _safe_float(info.get("epoch"))
            release_timestamp = _safe_float(info.get("release_timestamp"))
            title = str(info.get("title") or "YouTube live")
            webpage_url = str(info.get("webpage_url") or url)
            if epoch is None or release_timestamp is None:
                raise RuntimeError("Не удалось определить текущий таймкод live.")
            edge = max(0.0, epoch - release_timestamp - LIVE_EDGE_SAFETY_SECONDS)
            self.events.put(("marker_resolved", (edge, release_timestamp, title, webpage_url)))
        except Exception as exc:
            self.events.put(("marker_error", str(exc)))

    def _apply_marker_resolved(self, payload: object) -> None:
        edge, release_timestamp, title, webpage_url = payload  # type: ignore[misc]
        self.marker_tracking = True
        self.marker_anchor_edge = float(edge)
        self.marker_anchor_monotonic = time.monotonic()
        self.marker_release_timestamp = float(release_timestamp)
        self.marker_title = str(title)
        self.marker_url = str(webpage_url)
        self.marker_file_path = self._marker_output_path()
        self.marker_timer_var.set(format_timecode(self._current_marker_time()))
        self.marker_state_var.set("Tracking live")
        self.marker_file_var.set(f"Markers file: {self.marker_file_path}")
        self.status_var.set("Marker tracking")
        self._append_log(
            f"Marker tracker started: {self.marker_title} at {format_timecode(self.marker_anchor_edge)}\n"
            f"Markers TXT: {self.marker_file_path}\n"
        )

    def _current_marker_time(self) -> float:
        if not self.marker_tracking:
            return 0.0
        return max(0.0, self.marker_anchor_edge + (time.monotonic() - self.marker_anchor_monotonic))

    def _write_stream_marker(self) -> None:
        if not self.marker_tracking:
            messagebox.showwarning(APP_TITLE, "Сначала нажми Start Tracking на вкладке Stream Markers.")
            return
        marker_time = self._current_marker_time()
        before = max(0, int(self.marker_before_var.get()))
        after = max(0, int(self.marker_after_var.get()))
        start = max(0.0, marker_time - before)
        end = marker_time + after
        path = self.marker_file_path or self._marker_output_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        is_new = not path.exists() or path.stat().st_size == 0
        now = datetime.now()
        note = self.marker_note_var.get().strip() or "funny moment"
        with path.open("a", encoding="utf-8") as handle:
            if is_new:
                handle.write("ERNI Live Clipper - stream markers\n")
                handle.write(f"Stream: {self.marker_title}\n")
                handle.write(f"URL: {self.marker_url or self.url_var.get().strip()}\n")
                handle.write(f"Date: {now.strftime('%Y-%m-%d')}\n")
                handle.write("-" * 72 + "\n")
            handle.write(
                f"{now.strftime('%H:%M:%S')} | marker {format_timecode(marker_time)} | "
                f"range {format_timecode(start)} -> {format_timecode(end)} | {note}\n"
            )
        self.marker_file_path = path
        self.marker_file_var.set(f"Markers file: {path}")
        self.marker_state_var.set(f"Marked {format_timecode(marker_time)}")
        self.status_var.set("Marker saved")
        self._append_log(
            f"\nMarker saved: {format_timecode(marker_time)} "
            f"range {format_timecode(start)} -> {format_timecode(end)} ({note})\n"
        )

    def _open_marker_file(self) -> None:
        path = self.marker_file_path or self._marker_output_path()
        if not path.exists():
            messagebox.showinfo(APP_TITLE, "TXT еще не создан. Поставь первую метку.")
            return
        open_path(path)

    def _start_capture(self) -> None:
        url = self.url_var.get().strip()
        if not url:
            messagebox.showwarning(APP_TITLE, "Вставь ссылку на live-стрим.")
            return
        try:
            self.save_dir = Path(self.folder_var.get()).expanduser()
            output = self.recorder.start(url, self.save_dir)
            self.master_file_var.set(f"Master: {output.name}")
            self.start_button.configure(state="disabled")
            self.stop_button.configure(state="normal")
            self._append_log(f"Master file: {output}\n")
        except Exception as exc:
            messagebox.showerror(APP_TITLE, str(exc))

    def _stop_capture(self) -> None:
        if self.exporter.is_running:
            self.exporter.cancel()
            self.status_var.set("Cancelling export")
            self._append_log("\nCancelling current clip export...\n")
            return
        self.recorder.stop()
        self.status_var.set("Stopping")
        self.start_button.configure(state="disabled")
        self.stop_button.configure(state="disabled")

    def _custom_clip(self) -> None:
        try:
            start = parse_timecode(self.start_var.get())
            end = parse_timecode(self.end_var.get())
            self._export_clip(start, end, self._clip_label("clip"), self.export_mode_var.get())
        except Exception as exc:
            messagebox.showerror(APP_TITLE, str(exc))

    def _direct_clip(self) -> None:
        try:
            start = parse_timecode(self.start_var.get())
            end = parse_timecode(self.end_var.get())
            self._download_direct_range(start, end, self._clip_label("clip"), self.export_mode_var.get())
        except Exception as exc:
            messagebox.showerror(APP_TITLE, str(exc))

    def _download_exact_range(self) -> None:
        try:
            if not self._live_url():
                self._warn_missing_live_url("Для Exact Range вставь live-ссылку во вкладке Live Clipper.")
                return
            start = parse_timecode(self.range_from_var.get())
            end = parse_timecode(self.range_to_var.get())
            if end <= start:
                raise RuntimeError("To должен быть позже From.")
            self.start_var.set(format_timecode(start))
            self.end_var.set(format_timecode(end))
            self._append_log(f"\nExact range: {format_timecode(start)} - {format_timecode(end)}\n")
            if self.recorder.is_running:
                self._export_clip(start, end, self._clip_label("clip"), self.export_mode_var.get())
            else:
                self._download_direct_range(start, end, self._clip_label("clip"), self.export_mode_var.get())
        except Exception as exc:
            messagebox.showerror(APP_TITLE, str(exc))

    def _download_moment_by_time(self) -> None:
        try:
            if not self._live_url():
                self._warn_missing_live_url("Для Manual Moment вставь live-ссылку во вкладке Live Clipper.")
                return
            moment = parse_timecode(self.moment_var.get())
            before = max(0, int(self.preroll_var.get()))
            after = max(0, int(self.after_var.get()))
            if before == 0 and after == 0:
                raise RuntimeError("Before и after не могут оба быть 0.")
            start = max(0, moment - before)
            end = moment + after
            self.start_var.set(format_timecode(start))
            self.end_var.set(format_timecode(end))
            label = self._clip_label("moment")
            self._append_log(
                f"\nManual moment: {format_timecode(moment)} -> {format_timecode(start)} - {format_timecode(end)}\n"
            )
            if self.recorder.is_running:
                self._export_clip(start, end, label, self.export_mode_var.get())
            else:
                self._download_direct_range(start, end, label, self.export_mode_var.get())
        except Exception as exc:
            messagebox.showerror(APP_TITLE, str(exc))

    def _download_vod_range(self) -> None:
        try:
            start = parse_timecode(self.vod_from_var.get())
            end = parse_timecode(self.vod_to_var.get())
            if end <= start:
                raise RuntimeError("To должен быть позже From.")
            self.save_dir = Path(self.folder_var.get()).expanduser()
            label = self.vod_label_var.get().strip() or "saved_video"
            tag = safe_name(label, "saved_video")
            job = ClipJob(
                source_type="vod",
                start=start,
                end=end,
                label=label,
                tag=tag,
                comment=self.comment_var.get().strip(),
                mode=self.export_mode_var.get(),
                root_dir=self.save_dir,
                output_dir=self._clip_output_dir(self.save_dir, tag),
                url=self.vod_url_var.get().strip(),
            )
            self._append_log(f"\nSaved video range: {format_timecode(start)} - {format_timecode(end)}\n")
            self._queue_or_start_export(job)
        except Exception as exc:
            self.status_var.set("Export error")
            messagebox.showerror(APP_TITLE, str(exc))

    def _quick_clip(self, seconds: int) -> None:
        if not self.recorder.is_running and not self._live_url():
            self._warn_missing_live_url(f"Для Last {seconds}s вставь live-ссылку во вкладке Live Clipper.")
            return
        if self.recorder.is_running:
            end = max(0, self.recorder.elapsed - 1)
        else:
            try:
                end = self._resolve_live_edge_time()
            except Exception as exc:
                messagebox.showerror(APP_TITLE, str(exc))
                return
            if end <= 0:
                messagebox.showwarning(APP_TITLE, "Не удалось определить текущий live edge.")
                return
        start = max(0, end - seconds)
        self.start_var.set(format_timecode(start))
        self.end_var.set(format_timecode(end))
        label = self._clip_label(f"last_{seconds}s")
        if self.recorder.is_running:
            self._export_clip(start, end, label, self.export_mode_var.get())
        else:
            self._download_direct_range(start, end, label, self.export_mode_var.get())

    def _shortcut_quick_clip(self, seconds: int) -> str:
        if not self._accept_hotkey(f"last{seconds}"):
            return "break"
        self._hotkey_feedback(f"Last {seconds}s")
        self._append_log(f"\nHotkey fired: Last {seconds}s\n")
        self._quick_clip(seconds)
        return "break"

    def _shortcut_write_marker(self) -> str:
        if not self._accept_hotkey("stream_marker"):
            return "break"
        self._hotkey_feedback("Stream marker")
        self._append_log("\nHotkey fired: Stream marker\n")
        self._write_stream_marker()
        return "break"

    def _shortcut_mark_moment(self) -> str:
        if not self._accept_hotkey("mark"):
            return "break"
        self._hotkey_feedback("Mark moment")
        self._append_log("\nHotkey fired: Mark moment\n")
        self._mark_moment()
        return "break"

    def _shortcut_download_moment(self) -> str:
        if not self._accept_hotkey("manual_moment"):
            return "break"
        self._hotkey_feedback("Manual moment")
        self._append_log("\nHotkey fired: Manual moment\n")
        self._download_moment_by_time()
        return "break"

    def _shortcut_direct_clip(self) -> str:
        if not self._accept_hotkey("exact_range"):
            return "break"
        self._hotkey_feedback("Exact range")
        self._append_log("\nHotkey fired: Exact range\n")
        self._download_exact_range()
        return "break"

    def _shortcut_cancel_export(self) -> str:
        if self.exporter.is_running:
            self._stop_capture()
        return "break"

    def _shortcut_open_folder(self) -> str:
        self._open_folder()
        return "break"

    def _shortcut_focus_url(self) -> str:
        self.url_entry.focus_set()
        self.url_entry.selection_range(0, "end")
        return "break"

    def _hotkey_feedback(self, label: str) -> None:
        self.status_var.set(f"Hotkey: {label}")
        self.hotkey_status_var.set(f"Last hotkey: {label} at {datetime.now().strftime('%H:%M:%S')}")

    def _accept_hotkey(self, name: str) -> bool:
        now = time.monotonic()
        if self.last_hotkey_name == name and now - self.last_hotkey_at < 0.65:
            return False
        self.last_hotkey_name = name
        self.last_hotkey_at = now
        return True

    def _handle_keypress_shortcut(self, event: tk.Event) -> str | None:
        keysym = str(getattr(event, "keysym", ""))
        state = int(getattr(event, "state", 0) or 0)
        if not self._has_shortcut_modifier(state):
            return None

        key = keysym.lower()
        self.hotkey_status_var.set(f"Key seen: {keysym} at {datetime.now().strftime('%H:%M:%S')}")
        digit_map = {
            "1": lambda: self._shortcut_quick_clip(30),
            "kp_1": lambda: self._shortcut_quick_clip(30),
            "2": lambda: self._shortcut_quick_clip(60),
            "kp_2": lambda: self._shortcut_quick_clip(60),
            "3": lambda: self._shortcut_quick_clip(180),
            "kp_3": lambda: self._shortcut_quick_clip(180),
            "4": self._shortcut_direct_clip,
            "kp_4": self._shortcut_direct_clip,
            "7": self._shortcut_write_marker,
            "kp_7": self._shortcut_write_marker,
        }
        if key in digit_map:
            return digit_map[key]()
        if key == "m" and state & 0x0004:
            return self._shortcut_write_marker()
        if key in {"return", "kp_enter"}:
            return self._shortcut_direct_clip()
        if key == "l":
            return self._shortcut_focus_url()
        if key == "o":
            return self._shortcut_open_folder()
        return None

    def _has_shortcut_modifier(self, state: int) -> bool:
        # Tk reports modifiers differently across macOS/Tk builds, so accept the
        # common Control/Command/Option/Mod masks instead of relying on one name.
        if platform.system() == "Windows":
            has_control = bool(state & 0x0004)
            has_alt = bool(state & 0x0008) or bool(state & 0x0010) or bool(state & 0x0080)
            return has_control and has_alt
        modifier_masks = (0x0004, 0x0008, 0x0010, 0x0080, 0x0100, 0x100000, 0x200000)
        return any(state & mask for mask in modifier_masks)

    def _resolve_live_edge_time(self) -> float:
        yt_dlp = find_executable("yt-dlp")
        if not yt_dlp:
            raise RuntimeError("yt-dlp не найден.")
        url = self._require_live_url()
        self.status_var.set("Finding live edge")
        self._append_log("\nFinding current live edge for quick clip...\n")
        command = [
            yt_dlp,
            "--no-playlist",
            "--no-color",
            "--no-warnings",
            "--extractor-args",
            YOUTUBE_EXTRACTOR_ARGS,
            "--live-from-start",
            "-J",
            url,
        ]
        completed = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        if completed.stderr.strip():
            self._append_log(completed.stderr)
        if completed.returncode != 0:
            raise RuntimeError("yt-dlp не смог получить live-поток.")
        info = json.loads(completed.stdout)
        epoch = _safe_float(info.get("epoch"))
        release_timestamp = _safe_float(info.get("release_timestamp"))
        if epoch is None or release_timestamp is None:
            raise RuntimeError("Не удалось определить текущий таймкод live.")
        raw_edge = max(0, epoch - release_timestamp)
        edge = max(0, raw_edge - LIVE_EDGE_SAFETY_SECONDS)
        self._append_log(f"Current live edge at click: {format_timecode(edge)}\n")
        return edge

    def _mark_moment(self) -> None:
        if self.recorder.is_running:
            elapsed = self.recorder.elapsed
        else:
            try:
                elapsed = parse_timecode(self.range_to_var.get())
            except Exception as exc:
                messagebox.showerror(APP_TITLE, str(exc))
                return
            if elapsed <= 0:
                messagebox.showwarning(APP_TITLE, "Введи To — момент на стриме, вокруг которого нужно скачать клип.")
                return
        start = max(0, elapsed - int(self.preroll_var.get()))
        end = elapsed + int(self.after_var.get())
        self.start_var.set(format_timecode(start))
        self.end_var.set(format_timecode(end))
        if self.recorder.is_running:
            delay_ms = max(0, int(self.after_var.get())) * 1000
            self.status_var.set("Moment marked")
            self._append_log(f"\nMoment marked: {format_timecode(start)} - {format_timecode(end)}. Export will start after buffer is recorded.\n")
            self.after(delay_ms, lambda: self._export_clip(start, end, self._clip_label("moment"), self.export_mode_var.get()))
        else:
            self._download_direct_range(start, end, self._clip_label("moment"), self.export_mode_var.get())

    def _download_direct_range(self, start: float, end: float, label: str, mode: str) -> None:
        try:
            self.save_dir = Path(self.folder_var.get()).expanduser()
            url = self._live_url()
            if not url:
                self._warn_missing_live_url("Для скачивания клипа вставь live-ссылку во вкладке Live Clipper.")
                return
            tag = self._current_tag()
            job = ClipJob(
                source_type="direct",
                start=start,
                end=end,
                label=label,
                tag=tag,
                comment=self.comment_var.get().strip(),
                mode=mode,
                root_dir=self.save_dir,
                output_dir=self._clip_output_dir(self.save_dir, tag),
                url=url,
            )
            self._queue_or_start_export(job)
        except Exception as exc:
            self.status_var.set("Export error")
            messagebox.showerror(APP_TITLE, str(exc))

    def _export_clip(self, start: float, end: float, label: str, mode: str) -> None:
        source = self.recorder.output_file
        if not source:
            raise RuntimeError("Master-файл не выбран. Нажми Start Capture.")
        self.save_dir = Path(self.folder_var.get()).expanduser()
        tag = self._current_tag()
        job = ClipJob(
            source_type="recording",
            start=start,
            end=end,
            label=label,
            tag=tag,
            comment=self.comment_var.get().strip(),
            mode=mode,
            root_dir=self.save_dir,
            output_dir=self._clip_output_dir(self.save_dir, tag),
            source=source,
        )
        self._queue_or_start_export(job)

    def _clip_label(self, fallback: str) -> str:
        return self.label_var.get().strip() or fallback

    def _current_tag(self) -> str:
        return safe_name(self.label_var.get(), "untagged")

    def _clip_output_dir(self, root_dir: Path, tag: str) -> Path:
        output_dir = root_dir / "clips" / clip_day_folder() / safe_name(tag, "untagged")
        output_dir.mkdir(parents=True, exist_ok=True)
        return output_dir

    def _queue_or_start_export(self, job: ClipJob) -> None:
        if job.end <= job.start:
            raise RuntimeError("Конец клипа должен быть позже начала.")
        if self.exporter.is_running or self.active_job is not None:
            self.export_queue.append(job)
            self._update_queue_status()
            self.status_var.set("Queued")
            self._append_log(f"\nQueued clip: {job.range_label} ({job.tag}, {job.mode})\n")
            return
        self._start_export_job(job)

    def _start_export_job(self, job: ClipJob) -> None:
        self.active_job = job
        self._update_queue_status()
        self.stop_button.configure(state="normal")
        try:
            if job.source_type == "direct":
                self.status_var.set("Downloading range")
                self.exporter.export_direct(
                    job.url,
                    job.output_dir,
                    job.start,
                    job.end,
                    job.label,
                    job.mode,
                )
            elif job.source_type == "vod":
                self.status_var.set("Downloading saved video range")
                self.exporter.export_vod(
                    job.url,
                    job.output_dir,
                    job.start,
                    job.end,
                    job.label,
                    job.mode,
                )
            else:
                if not job.source:
                    raise RuntimeError("Master-файл не выбран. Нажми Start Capture.")
                self.status_var.set("Exporting")
                self.exporter.export(job.source, job.output_dir, job.start, job.end, job.label, job.mode)
        except Exception:
            self.active_job = None
            self._update_queue_status()
            if not self.recorder.is_running:
                self.stop_button.configure(state="disabled")
            raise

    def _start_next_export_job(self) -> None:
        if self.exporter.is_running or self.active_job is not None:
            return
        if not self.export_queue:
            self._update_queue_status()
            if not self.recorder.is_running:
                self.stop_button.configure(state="disabled")
            return
        job = self.export_queue.pop(0)
        self._append_log(f"\nStarting queued clip: {job.range_label} ({job.tag}, {job.mode})\n")
        try:
            self._start_export_job(job)
        except Exception as exc:
            self.status_var.set("Export error")
            self._append_log(f"\nQueued export error: {exc}\n")
            messagebox.showerror(APP_TITLE, str(exc))
            self.active_job = None
            self.after(100, self._start_next_export_job)

    def _update_queue_status(self) -> None:
        active = 1 if self.active_job is not None else 0
        self.queue_var.set(f"Queue: {len(self.export_queue)} waiting, {active} active")

    def _open_folder(self) -> None:
        self.save_dir = Path(self.folder_var.get()).expanduser()
        self.save_dir.mkdir(parents=True, exist_ok=True)
        open_path(self.save_dir)

    def _open_hotkeys_doc(self) -> None:
        doc_path = bundled_path("HOTKEYS.md")
        if not doc_path.exists():
            doc_path = Path(__file__).with_name("HOTKEYS.md")
        if doc_path.exists():
            open_path(doc_path)
        else:
            messagebox.showinfo(
                APP_TITLE,
                "HOTKEYS.md не найден рядом с приложением. Основные хоткеи: F8=Last 30s, F9=Last 60s, F10=Last 3m, F11=Mark moment.",
            )

    def _open_selected_clip(self) -> None:
        selected = self.clips.selection()
        if not selected:
            return
        path = Path(self.clips.set(selected[0], "file"))
        if path.exists():
            open_path(path)

    def _reveal_selected_clip(self) -> None:
        selected = self.clips.selection()
        if not selected:
            return
        path = Path(self.clips.set(selected[0], "file"))
        if not path.exists():
            return
        if platform.system() == "Darwin":
            subprocess.Popen(["open", "-R", str(path)])
        elif platform.system() == "Windows":
            subprocess.Popen(["explorer", f"/select,{path}"])
        else:
            open_path(path.parent)

    def _tools_status(self) -> str:
        yt = "yt-dlp" if find_executable("yt-dlp") else "yt-dlp missing"
        ffmpeg = "ffmpeg" if find_executable("ffmpeg") else "ffmpeg missing"
        return f"{yt} + {ffmpeg}"

    def _status_from_thread(self, status: str) -> None:
        self.events.put(("status", status))

    def _log_from_thread(self, text: str) -> None:
        self.events.put(("log", text))

    def _export_finished_from_thread(self, success: bool, path: Path | None, message: str) -> None:
        self.events.put(("export_finished", (success, path, message)))

    def _hotkey_from_thread(self, action: str) -> None:
        self.events.put(("hotkey", action))

    def _hotkey_status_from_thread(self, status: str) -> None:
        self.events.put(("hotkey_status", status))

    def _handle_hotkey(self, action: str) -> None:
        if action == "marker":
            self._shortcut_write_marker()
        elif action == "last30":
            self._shortcut_quick_clip(30)
        elif action == "last60":
            self._shortcut_quick_clip(60)
        elif action == "last180":
            self._shortcut_quick_clip(180)
        elif action == "mark":
            self._shortcut_mark_moment()
        elif action == "exact":
            self._shortcut_direct_clip()
        elif action == "moment":
            self._shortcut_download_moment()

    def _append_clip_metadata(self, job: ClipJob, path: Path, report_message: str) -> None:
        metadata_path = job.root_dir / "clips" / "clips_index.csv"
        metadata_path.parent.mkdir(parents=True, exist_ok=True)
        is_new = not metadata_path.exists()
        with metadata_path.open("a", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(
                handle,
                fieldnames=[
                    "created_at",
                    "file",
                    "start",
                    "end",
                    "duration_seconds",
                    "tag",
                    "comment",
                    "mode",
                    "source_type",
                    "source_url",
                    "check",
                ],
            )
            if is_new:
                writer.writeheader()
            writer.writerow(
                {
                    "created_at": datetime.now().isoformat(timespec="seconds"),
                    "file": str(path),
                    "start": format_timecode(job.start),
                    "end": format_timecode(job.end),
                    "duration_seconds": int(job.end - job.start),
                    "tag": job.tag,
                    "comment": job.comment,
                    "mode": job.mode,
                    "source_type": job.source_type,
                    "source_url": job.url,
                    "check": report_message,
                }
            )

    def _poll_events(self) -> None:
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == "log":
                    self._append_log(str(payload))
                elif kind == "status":
                    self.status_var.set(str(payload))
                    if payload == "Finalizing MP4":
                        self.start_button.configure(state="disabled")
                        self.stop_button.configure(state="disabled")
                    if payload in {"Stopped", "Finished", "Error"}:
                        self.start_button.configure(state="normal")
                        self.stop_button.configure(state="disabled")
                        if self.recorder.output_file:
                            self.master_file_var.set(f"Master: {self.recorder.output_file.name}")
                elif kind == "hotkey":
                    self._handle_hotkey(str(payload))
                elif kind == "hotkey_status":
                    self.hotkey_status_var.set(str(payload))
                elif kind == "marker_resolved":
                    self._apply_marker_resolved(payload)
                elif kind == "marker_error":
                    self.marker_tracking = False
                    self.marker_state_var.set("Tracking error")
                    self.status_var.set("Marker error")
                    self._append_log(f"\nMarker tracker error: {payload}\n")
                    messagebox.showerror(APP_TITLE, str(payload))
                elif kind == "export_finished":
                    success, path, message = payload  # type: ignore[misc]
                    job = self.active_job
                    self.active_job = None
                    self._update_queue_status()
                    if success and path:
                        self.status_var.set("Clip ready")
                        self._append_log(f"\nClip ready: {path}\n{message}\n")
                        if job:
                            self._append_clip_metadata(job, path, message)
                            self.clips.insert("", "end", values=(job.range_label, job.tag, job.comment, job.mode, str(path)))
                        else:
                            self.clips.insert("", "end", values=("", "", "", "", str(path)))
                    else:
                        self.status_var.set("Export error")
                        self._append_log(f"\nExport error: {message}\n")
                        messagebox.showerror(APP_TITLE, message)
                    self._start_next_export_job()
        except queue.Empty:
            pass
        self.after(250, self._poll_events)

    def _tick(self) -> None:
        self.timer_var.set(format_timecode(self.recorder.elapsed))
        if self.marker_tracking:
            self.marker_timer_var.set(format_timecode(self._current_marker_time()))
        self.after(500, self._tick)

    def _append_log(self, text: str) -> None:
        self.log_text.insert("end", text)
        self.log_text.see("end")

    def _on_close(self) -> None:
        if self.recorder.is_running:
            if not messagebox.askyesno(APP_TITLE, "Запись еще идет. Остановить и закрыть?"):
                return
            self.recorder.stop()
        if self.exporter.is_running:
            self.exporter.cancel()
        self.hotkeys.stop()
        self.destroy()


def main() -> None:
    app = LiveClipperApp()
    app.mainloop()


if __name__ == "__main__":
    main()

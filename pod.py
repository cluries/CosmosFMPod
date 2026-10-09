#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Podcast task TUI. Settings are read from config.json beside this script.

Enter URL [trim seconds] [nomove]. Transfer tasks require a device setting
and retain local files. nomove keeps results local only.
/exit finishes tasks; Ctrl+C stops them. Requires textual, curl, ffmpeg,
ffprobe and cp. Default concurrency: 3.
"""
import argparse
import asyncio
import html as html_module
import json
import math
import os
import re
import shlex
import shutil
import sys
import tempfile
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta
from glob import glob
from pathlib import Path
from urllib.parse import urlsplit

from rich.text import Text
from textual import events
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.widgets import DataTable, Input, RichLog, Static

BASE = Path(__file__).resolve().parent
CONFIG = BASE / "config.json"
DIST = str(BASE / "dist")
TMP = str(BASE / "tmp")
HISTORY = str(BASE / "transfer_history.jsonl")
MAX_CONCURRENT = 3
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36"
NUM_RE = re.compile(r"^[0-9]+(\.[0-9]+)?$")
NETWORK_ATTEMPTS = 3
RETRY_CURL_CODES = {5, 6, 7, 18, 28, 52, 55, 56}
RETRY_HTTP_CODES = {408, 429, 500, 502, 503, 504}


class CommandError(RuntimeError):
    def __init__(self, program, returncode, stdout, stderr):
        self.returncode = returncode
        self.stdout = stdout
        super().__init__(f"{program} failed ({returncode}): {stderr.strip()[-1500:]}")


def err(message):
    raise OSError(message)

def now_iso():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def fmt_size(b):
    mb = b / (1024 * 1024)
    return f"{mb:.2f} MB" if mb >= 1 else f"{b} B"

def fmt_dur(s):
    return "Unknown" if s is None else f"{s:.3f}s"

def fmt_speed(sp):
    return "Unknown" if not sp else f"{sp / (1024 * 1024):.2f} MB/s"

def parse_title(html):
    """读取页面标题并去掉网站后缀。"""
    m = re.search(r"<title[^>]*>(.*?)</title>", html, re.IGNORECASE | re.DOTALL)
    if not m:
        return ""
    page_title = m.group(1).replace("\n", "").replace("\r", "")
    return strip_site_suffix(page_title)

def strip_site_suffix(title):
    return re.sub(r" *\| *小宇宙.*$", "", title)

def parse_audio_url(html):
    """解析 og:audio 的 content（属性顺序、引号、大小写均不限）。"""
    patterns = [
        r'<meta\b[^>]*\bproperty\s*=\s*["\']og:audio["\'][^>]*\bcontent\s*=\s*["\']([^"\']+)["\']',
        r'<meta\b[^>]*\bcontent\s*=\s*["\']([^"\']+)["\'][^>]*\bproperty\s*=\s*["\']og:audio["\']',
    ]
    for pat in patterns:
        m = re.search(pat, html, re.IGNORECASE | re.DOTALL)
        if m:
            return m.group(1)
    return None

def guess_ext(url):
    ext = url.split(".")[-1].split("?")[0].lower()
    if not ext or len(ext) > 4:
        ext = "mp3"
    return ext

def history_path():
    path = Path(HISTORY)
    legacy = path.with_name(".move_history.jsonl")
    if not path.exists() and legacy.is_file():
        legacy.rename(path)
    return path

def history_records():
    path = history_path()
    if not path.exists():
        return
    with path.open(encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(record, dict):
                yield record

def valid_history_number(value):
    if type(value) not in (int, float):
        return False
    try:
        return math.isfinite(value) and value >= 0
    except OverflowError:
        return False

def read_last_cumulative():
    totals = (0.0, 0.0)
    for record in history_records():
        values = (record.get("cum_bytes"), record.get("cum_duration"))
        if all(valid_history_number(value) for value in values):
            totals = tuple(float(value) for value in values)
    return totals

def history_speed(destination):
    recent = deque(maxlen=5)
    legacy = None
    for record in history_records():
        if record.get("destination") == str(destination):
            size, duration = record.get("size_bytes"), record.get("duration")
            if all(valid_history_number(value) and value > 0 for value in (size, duration)):
                recent.append((float(size), float(duration)))
        elif record.get("destination") is None:
            size, duration = record.get("cum_bytes"), record.get("cum_duration")
            if all(valid_history_number(value) for value in (size, duration)):
                legacy = avg_speed(float(size), float(duration))
    if recent:
        return avg_speed(sum(size for size, _ in recent), sum(duration for _, duration in recent))
    return legacy

def download_total(headers):
    if not headers.exists():
        return None
    blocks = headers.read_text(encoding="utf-8", errors="replace").split("\n\n")
    for block in reversed(blocks):
        lines = block.splitlines()
        if not lines or not lines[0].startswith("HTTP/"):
            continue
        status = lines[0].split()
        if len(status) < 2 or not status[1].isdigit() or not 200 <= int(status[1]) < 300:
            return None
        fields = dict(line.lower().split(":", 1) for line in lines[1:] if ":" in line)
        if "transfer-encoding" in fields:
            return None
        length = fields.get("content-length", "").strip()
        return int(length) if length.isdigit() and int(length) > 0 else None
    return None

def avg_speed(cum_bytes, cum_duration):
    return cum_bytes / cum_duration if cum_duration > 0 else None

def estimate_seconds(size_bytes, speed):
    return size_bytes / speed if speed and speed > 0 else None

def append_history(
    start_iso, end_iso, task, destination, size, duration, cum_bytes, cum_duration
):
    rec = {
        "schema_version": 2,
        "start_time": start_iso,
        "end_time": end_iso,
        "filename": task.audio.name,
        "title_filename": task.title_file.name,
        "url": task.url,
        "trim_seconds": task.trim,
        "destination": str(destination),
        "audio_path": str(task.audio.resolve()),
        "title_path": str(task.title_file.resolve()),
        "size_bytes": size,
        "duration": round(duration, 6),
        "cum_bytes": int(cum_bytes),
        "cum_duration": round(cum_duration, 6),
        "title": task.title,
    }
    path = history_path()
    # Keep a truncated final record separate from the next successful transfer.
    needs_newline = False
    if path.exists() and path.stat().st_size:
        with path.open("rb") as f:
            f.seek(-1, os.SEEK_END)
            needs_newline = f.read(1) != b"\n"
    with path.open("a", encoding="utf-8") as f:
        if needs_newline:
            f.write("\n")
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")

def update_device_index(input_dir):
    """静默：只写 LIST.md，不输出常规日志（目录缺失仍报错）。"""
    if not os.path.isdir(input_dir):
        err(f"Directory '{input_dir}' does not exist.")
        return
    output_file = os.path.join(input_dir, "LIST.md")
    buf = []
    count = 0
    for f in sorted(glob(os.path.join(input_dir, "*.txt"))):
        if not os.path.isfile(f):
            continue
        n = os.path.splitext(os.path.basename(f))[0]
        try:
            with open(f, encoding="utf-8", errors="replace") as fh:
                content = fh.read()
        except OSError:
            content = ""
        # Each title line contributes the field before its first pipe.
        first_fields = [line.split("|")[0] for line in content.splitlines()]
        field = "\n".join(first_fields)
        buf.append(f"{n} => {field}\n\n")
        count += 1

    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=input_dir,
                                         prefix=".LIST_", suffix=".part", delete=False) as f:
            temporary = f.name
            if count == 0:
                f.write(f"No .txt files found in {input_dir}\n")
            else:
                f.writelines(buf)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temporary, output_file)
    finally:
        if temporary is not None:
            Path(temporary).unlink(missing_ok=True)


def parse_args(argv):
    """Parse URL [trim seconds] [nomove]; transfer by default."""
    if argv.count("nomove") > 1:
        raise ValueError("nomove may only appear once")
    rest = [arg for arg in argv if arg != "nomove"]
    if not 1 <= len(rest) <= 2:
        raise ValueError("Usage: URL [trim seconds] [nomove]")
    url = rest[0]
    parsed = urlsplit(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError("Enter a valid http/https page URL")
    trim = None
    if len(rest) == 2:
        if not NUM_RE.fullmatch(rest[1]):
            raise ValueError("Trim seconds must be a non-negative number")
        trim = float(rest[1])
        if not math.isfinite(trim):
            raise ValueError("Trim seconds are out of range")
    return url, trim, "nomove" not in argv


@dataclass
class PodTask:
    number: int
    url: str
    trim: float | None
    move: bool
    stem: str
    title: str = ""
    state: str = "Waiting to process"
    detail: str = ""
    started: float = 0
    finished: float | None = None
    audio: Path | None = None
    title_file: Path | None = None
    copy_started: float = 0
    copy_bytes: int = 0
    copy_total: int = 0
    device_order: int = 0
    download_started: float = 0
    download_bytes: int = 0
    download_total: int | None = None
    download_attempt: int = 0


class CommandInput(Input):
    """输入历史独立于任务表的键盘操作。"""
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.history: list[str] = []
        self.position = 0
        self.draft = ""

    def remember(self, value):
        if not self.history or self.history[-1] != value:
            self.history.append(value)
        self.position = len(self.history)
        self.draft = ""

    def on_key(self, event: events.Key):
        if event.key not in ("up", "down"):
            return
        event.stop()
        event.prevent_default()
        if self.position == len(self.history):
            self.draft = self.value
        self.position = max(0, min(len(self.history), self.position + (-1 if event.key == "up" else 1)))
        self.value = self.history[self.position] if self.position < len(self.history) else self.draft
        self.cursor_position = len(self.value)


class TaskTable(DataTable):
    """Keep short columns fixed; share extra width between title and details."""
    COLUMN_LAYOUT = (("ID", 3), ("Status", 20), ("Title / URL", 11),
                     ("Trim", 6), ("Device", 5), ("Elapsed", 7), ("Details", 9))

    def fit_columns(self):
        if not self.columns or not self.scrollable_content_region.width:
            return
        widths = [width for _, width in self.COLUMN_LAYOUT]
        available = self.scrollable_content_region.width - 2 * self.cell_padding * len(widths)
        extra = max(0, available - sum(widths))
        widths[2] += (extra * 3 + 4) // 5
        widths[6] += extra - (extra * 3 + 4) // 5
        if all(column.width == width for column, width in zip(self.ordered_columns, widths)):
            return
        for column, width in zip(self.ordered_columns, widths):
            column.width = width
        # Textual 8.2 没有公开的列宽 setter；同时更新尺寸和渲染缓存。
        self._require_update_dimensions = True
        self._update_count += 1
        self.check_idle()
        self.refresh(layout=True)

    def on_resize(self, event: events.Resize):
        self.fit_columns()


class PodApp(App):
    TITLE = "Pod · Podcast tasks"
    CSS = """
    Screen { layout: vertical; }
    #summary { height: 1; padding: 0 1; background: $panel; }
    #command-footer { dock: bottom; height: 4; }
    #device-transfer { height: auto; border: round $primary-muted; border-title-color: $text; border-title-style: bold; padding: 0 1; }
    #device-active { height: 2; }
    #device-waiting { height: 3; }
    #device-queue { height: auto; }
    #tasks { height: 2fr; min-height: 4; border: round $primary-muted; border-title-color: $text; border-title-style: bold; }
    #tasks:focus { border: round $primary; }
    #tasks > .datatable--header { background: $primary-muted; color: $text; text-style: bold; }
    #tasks > .datatable--odd-row { background: $surface; }
    #tasks > .datatable--even-row { background: $panel; }
    #tasks > .datatable--cursor { background: $primary 25%; text-style: bold; }
    #task-detail-pane { height: 3; padding: 0 1; }
    #task-detail { height: auto; color: $text-muted; }
    #log { height: 1fr; min-height: 3; max-height: 8; border-top: solid $primary; }
    #help { height: 1; padding: 0 1; color: $text-muted; }
    #command { height: 3; }
    """
    BINDINGS = [Binding("ctrl+c", "stop", "Stop now", priority=True),
                Binding("ctrl+q", "stop", "Stop now", show=False, priority=True)]

    def __init__(self, dest=None, concurrency=MAX_CONCURRENT):
        super().__init__()
        self.dest = str(Path(dest).expanduser().resolve()) if dest else None
        self.concurrency = concurrency
        self.tasks: list[PodTask] = []
        self.prepare_queue = asyncio.Queue()
        self.device_queue = asyncio.Queue()
        self.runners: list[asyncio.Task] = []
        self.closing = False
        self.stopping = False
        self.stop_event = threading.Event()
        self.last_stamp: datetime | None = None
        self.columns = ()
        self.device_sequence = 0

    def device_mounted(self):
        return self.dest is not None and os.path.ismount(self.dest)

    def compose(self) -> ComposeResult:
        with Vertical(id="device-transfer"):
            yield Static(id="device-active", markup=False)
            with VerticalScroll(id="device-waiting"):
                yield Static(id="device-queue", markup=False)
        yield TaskTable(id="tasks", cursor_type="row", zebra_stripes=True,
                        fixed_columns=2, cursor_foreground_priority="renderable")
        with VerticalScroll(id="task-detail-pane"):
            yield Static("No tasks yet · Enter a URL below to start", id="task-detail", markup=False)
        yield RichLog(id="log", max_lines=300, wrap=True, markup=False)
        yield Static("URL [trim seconds] [nomove]  ·  ↑↓ History  ·  /exit Finish & exit  ·  Ctrl+C Stop", id="help")
        with Vertical(id="command-footer"):
            yield CommandInput(placeholder="Enter a podcast page URL and press Enter to add a task", id="command")
            yield Static(id="summary", markup=False)

    def on_mount(self):
        table = self.query_one(DataTable)
        table.border_title = "Tasks"
        self.query_one("#device-transfer").border_title = "Device transfer · Copy and keep local files"
        self.columns = tuple(table.add_column(Text(label, justify="right" if label in ("ID", "Trim", "Elapsed") else "left"), width=width)
                             for label, width in TaskTable.COLUMN_LAYOUT)
        table.fit_columns()
        self.query_one(CommandInput).focus()
        self.runners = [asyncio.create_task(self.prepare_worker()) for _ in range(self.concurrency)]
        self.runners.append(asyncio.create_task(self.device_worker()))
        self.set_interval(0.3, self.refresh_status)
        self.log_message(f"Concurrent processing: {self.concurrency}; device: {self.dest or 'not configured (use nomove)'}")
        self.refresh_status()

    def log_message(self, message):
        self.query_one(RichLog).write(Text(f"{datetime.now():%H:%M:%S}  {message}"))

    def set_state(self, task, state, detail=""):
        task.state, task.detail = state, detail
        self.log_message(f"#{task.number} {state}" + (f": {detail}" if detail else ""))
        self.refresh_status()

    def download_detail(self, task):
        speed = task.download_bytes / max(.001, time.monotonic() - task.download_started)
        text = f"Attempt {task.download_attempt}/{NETWORK_ATTEMPTS} · {fmt_size(task.download_bytes)}"
        if task.download_total:
            remaining = max(0, task.download_total - task.download_bytes)
            text += (f" / {fmt_size(task.download_total)}"
                     f" · {min(1, task.download_bytes / task.download_total):.0%}"
                     f" · {fmt_speed(speed)} · ETA {fmt_dur(estimate_seconds(remaining, speed))}")
        else:
            text += f" · {fmt_speed(speed)} · Total unknown"
        return text

    def refresh_status(self):
        table = self.query_one(TaskTable)
        table.fit_columns()
        for task in self.tasks:
            elapsed = (task.finished or time.monotonic()) - task.started
            detail = task.detail
            if task.state == "Downloading" and not task.detail:
                detail = self.download_detail(task)
            if task.state == "Copying" and task.copy_total:
                duration = time.monotonic() - task.copy_started
                speed = task.copy_bytes / duration if duration > 0 else None
                remaining = estimate_seconds(task.copy_total - task.copy_bytes, speed)
                detail = f"{task.copy_bytes / task.copy_total:.0%} · {fmt_speed(speed)} · ETA {fmt_dur(remaining)}"
            style = ("green" if task.state == "Done" else "bold red" if task.state == "Failed"
                     else "yellow" if task.state in ("Waiting for device", "Waiting to copy")
                     else "dim" if task.state in ("Waiting to process", "Stopped") else "bold cyan")
            symbol = "✓" if task.state == "Done" else "×" if task.state in ("Failed", "Stopped") else "·" if task.state.startswith("Waiting") else "›"
            values = (Text(str(task.number), style="dim", justify="right"),
                      Text(f"{symbol} {task.state}", style=style),
                      Text(task.title or task.url, no_wrap=True, overflow="ellipsis"),
                      Text("—" if task.trim is None else f"{task.trim:g}s", justify="right"),
                      Text("Copy" if task.move else "Local", style="cyan" if task.move else "dim"),
                      Text(f"{elapsed:.1f}s", justify="right"),
                      Text(detail, style="red" if task.state == "Failed" else "dim", no_wrap=True, overflow="ellipsis"))
            for key, value in zip(self.columns, values):
                table.update_cell(str(task.number), key, value)
        self.refresh_task_detail()
        self.refresh_device_transfer()
        pending = sum(t.state == "Waiting to process" for t in self.tasks)
        working = sum(t.state in ("Fetching page", "Downloading", "Converting", "Saving") for t in self.tasks)
        copying = sum(t.state == "Copying" for t in self.tasks)
        device = sum(t.state in ("Waiting to copy", "Waiting for device") for t in self.tasks)
        complete = sum(t.state == "Done" for t in self.tasks)
        failed = sum(t.state == "Failed" for t in self.tasks)
        mounted = "not configured" if self.dest is None else "online" if self.device_mounted() else "offline"
        prefix = "Stopping" if self.stopping else "Exiting" if self.closing else "Running"
        summary = Text(no_wrap=True, overflow="ellipsis")
        summary.append(f"{prefix}  ", style="bold cyan")
        summary.append(f"Work {working}/{self.concurrency} · Wait {pending}  |  ")
        summary.append(f"Done {complete}", style="green")
        summary.append(f" · Fail {failed}", style="bold red" if failed else "dim")
        summary.append(f"  |  Copy {copying} · Q {device} · Device {mounted}", style="dim")
        self.query_one("#summary", Static).update(summary)
        if self.closing and not self.stopping and all(t.finished is not None for t in self.tasks):
            self.exit()

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted):
        self.refresh_task_detail()

    def refresh_task_detail(self):
        table = self.query_one(TaskTable)
        if not self.tasks or not table.is_valid_row_index(table.cursor_row):
            return
        task = self.tasks[table.cursor_row]
        elapsed = (task.finished or time.monotonic()) - task.started
        trim = "None" if task.trim is None else f"{task.trim:g}s from start"
        mode = "Copy to device (keep local)" if task.move else "Local only (nomove)"
        text = Text(f"#{task.number}  {task.title or task.url}", style="bold")
        text.append(f"\nStatus: {task.state}", style="red" if task.state == "Failed" else "cyan")
        text.append(f"  |  Trim: {trim}  |  Elapsed: {elapsed:.1f}s  |  Device: {mode}")
        text.append(f"\nURL: {task.url}\nOutput name: {task.stem}")
        if task.state == "Downloading" and not task.detail:
            text.append(f"\nDownload: {self.download_detail(task)}")
        if task.audio:
            text.append(f"\nAudio: {task.audio}")
        if task.title_file:
            text.append(f"\nTitle file: {task.title_file}")
        if task.move:
            text.append(f"\nDestination: {self.dest}")
            if task.state == "Waiting to copy":
                queued = sorted((t for t in self.tasks if t.state == "Waiting to copy"),
                                key=lambda t: t.device_order)
                position = next(i for i, t in enumerate(queued, 1) if t is task)
                text.append(f"  |  Queue position: {position}")
        if task.state == "Copying" and task.copy_total:
            duration = max(.001, time.monotonic() - task.copy_started)
            speed = task.copy_bytes / duration
            remaining = estimate_seconds(task.copy_total - task.copy_bytes, speed)
            text.append(f"\nCopy: {task.copy_bytes / task.copy_total:.0%}"
                        f" · {fmt_size(task.copy_bytes)} / {fmt_size(task.copy_total)}"
                        f" · {fmt_speed(speed)} · ETA {fmt_dur(remaining)}")
        if task.detail:
            text.append(f"\nDetails: {task.detail}")
        pane = self.query_one("#task-detail-pane", VerticalScroll)
        if getattr(self, "_detail_task_number", None) != task.number:
            pane.scroll_home(animate=False)
            self._detail_task_number = task.number
        self.query_one("#task-detail", Static).update(text)

    def refresh_device_transfer(self):
        """Show device queue order independently of task table scrolling."""
        active = next((t for t in self.tasks if t.state in ("Copying", "Waiting for device")), None)
        queued = sorted((t for t in self.tasks if t.state == "Waiting to copy"),
                        key=lambda task: task.device_order)
        preparing = sum(t.move and t.finished is None and t.state in
                        ("Waiting to process", "Fetching page", "Downloading", "Converting", "Saving") for t in self.tasks)
        text = Text()
        if active:
            text.append("Copying  " if active.state == "Copying" else "Waiting for device  ",
                        style="bold cyan" if active.state == "Copying" else "bold yellow")
            text.append(f"#{active.number}  {active.title or active.url}")
            if active.state == "Copying" and active.copy_total:
                fraction = min(1, active.copy_bytes / active.copy_total)
                speed = active.copy_bytes / max(.001, time.monotonic() - active.copy_started)
                filled = int(fraction * 12)
                text.append("\n" + "━" * filled, style="cyan")
                text.append("─" * (12 - filled), style="dim")
                text.append(f" {fraction:.0%} · {fmt_size(active.copy_bytes)} / {fmt_size(active.copy_total)}"
                            f" · {fmt_speed(speed)} · ETA {fmt_dur(estimate_seconds(active.copy_total - active.copy_bytes, speed))}")
            else:
                text.append(f"\n{self.dest}" if active.state == "Waiting for device" else f"\n{active.detail}", style="dim")
        else:
            text.append("Device not configured · Use nomove" if self.dest is None else
                        "Device idle" if self.device_mounted() else "Device disconnected", style="dim")
            text.append(f" · Queued {len(queued)} · Preparing {preparing}")
        text.no_wrap = True
        text.overflow = "ellipsis"
        self.query_one("#device-active", Static).update(text)
        queue_text = Text(no_wrap=True, overflow="ellipsis")
        for position, task in enumerate(queued, 1):
            if position > 1:
                queue_text.append("\n")
            queue_text.append(f"Queued {position}  ", style="yellow")
            queue_text.append(f"#{task.number}  {task.title or task.url}")
        self.query_one("#device-queue", Static).update(queue_text)
        self.query_one("#device-waiting").display = bool(queued)
        self.query_one("#device-transfer").border_title = (
            f"Device transfer · Queued {len(queued)} · Preparing {preparing} · Local files kept")

    def on_input_submitted(self, event: Input.Submitted):
        command = event.value.strip()
        if not command or self.closing:
            return
        entry = self.query_one(CommandInput)
        entry.remember(command)
        entry.value = ""
        if command == "/exit":
            self.closing = True
            entry.disabled = True
            self.log_message("No longer accepting tasks. Finishing submitted tasks before exiting; waiting if the device is disconnected. Ctrl+C to stop.")
            self.refresh_status()
            return
        try:
            url, trim, move = parse_args(shlex.split(command))
            if move and self.dest is None:
                raise ValueError("No device configured. Add nomove for local output, or set dest in config.json and restart.")
        except ValueError as exc:
            self.log_message(f"Input error: {exc}")
            return
        # 毫秒命名；同一毫秒连续提交时递增 1ms，保持格式和唯一性。
        stamp = datetime.now()
        stamp = stamp.replace(microsecond=stamp.microsecond // 1000 * 1000)
        if self.last_stamp is not None and stamp <= self.last_stamp:
            stamp = self.last_stamp + timedelta(milliseconds=1)
        while True:
            stem = stamp.strftime("%m%d_%H%M%S_%f")[:-3]
            if not any((Path(folder) / f"{stem}{suffix}").exists()
                       for folder in (DIST, self.dest) if folder is not None
                       for suffix in (".mp3", ".txt")):
                break
            stamp += timedelta(milliseconds=1)
        self.last_stamp = stamp
        task = PodTask(len(self.tasks) + 1, url, trim, move, stem, started=time.monotonic())
        self.tasks.append(task)
        self.query_one(DataTable).add_row(*([""] * len(self.columns)), key=str(task.number))
        self.prepare_queue.put_nowait(task)
        self.set_state(task, "Waiting to process", url)

    async def command(self, *args):
        """捕获子进程输出；取消时先终止并回收子进程。"""
        process = await asyncio.create_subprocess_exec(*map(str, args), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            stdout, stderr = await process.communicate()
        except asyncio.CancelledError:
            if process.returncode is None:
                try:
                    process.terminate()
                except ProcessLookupError:
                    pass
                try:
                    await asyncio.wait_for(process.wait(), 2)
                except asyncio.TimeoutError:
                    process.kill()
                    await process.wait()
            raise
        if process.returncode:
            raise CommandError(args[0], process.returncode, stdout.decode("utf-8", "replace"),
                               stderr.decode("utf-8", "replace"))
        return stdout.decode("utf-8", "replace").strip()

    async def network_request(self, task, url, output=None):
        """Retry temporary network failures; observe curl output without blocking the UI."""
        with tempfile.TemporaryDirectory(prefix="request_", dir=TMP) as folder:
            headers = Path(folder) / "headers"
            for attempt in range(1, NETWORK_ATTEMPTS + 1):
                headers.unlink(missing_ok=True)
                if output is not None:
                    output.unlink(missing_ok=True)
                    task.download_started = time.monotonic()
                    task.download_bytes = 0
                    task.download_total = None
                    task.download_attempt = attempt
                state = "Downloading" if output is not None else "Fetching page"
                self.set_state(task, state, "" if output is not None else f"Attempt {attempt}/{NETWORK_ATTEMPTS}")
                args = ["curl", "-sS", "-L", "--fail", "--connect-timeout", "30",
                        "-D", headers, "--write-out", "\n%{http_code}", "-A", UA]
                if output is None:
                    args += ["--max-time", "120"]
                else:
                    args += ["--speed-limit", "1", "--speed-time", "60", "-o", output]
                request = asyncio.create_task(self.command(*args, url))
                try:
                    if output is not None:
                        while not request.done():
                            task.download_bytes = output.stat().st_size if output.exists() else 0
                            task.download_total = download_total(headers)
                            await asyncio.wait({request}, timeout=.2)
                    result = await request
                    if output is not None:
                        task.download_bytes = output.stat().st_size
                        task.download_total = download_total(headers)
                    # --write-out appends the response status after the page body.
                    return result.rsplit("\n", 1)[0]
                except CommandError as exc:
                    status = exc.stdout.strip().rsplit("\n", 1)[-1]
                    retryable = (exc.returncode in RETRY_CURL_CODES or
                                 exc.returncode == 22 and status.isdigit() and int(status) in RETRY_HTTP_CODES)
                    if not retryable or attempt == NETWORK_ATTEMPTS:
                        raise
                    delay = 2 ** (attempt - 1)
                    reason = f"HTTP {status}" if exc.returncode == 22 else f"Network error (curl {exc.returncode})"
                    self.set_state(task, state, f"{reason} · Retry {attempt + 1}/{NETWORK_ATTEMPTS} in {delay}s")
                    await asyncio.sleep(delay)
                finally:
                    if not request.done():
                        request.cancel()
                    await asyncio.gather(request, return_exceptions=True)

    async def prepare_worker(self):
        while True:
            task = await self.prepare_queue.get()
            try:
                await self.prepare(task)
                if task.move:
                    self.device_sequence += 1
                    task.device_order = self.device_sequence
                    self.set_state(task, "Waiting to copy", str(task.audio))
                    self.device_queue.put_nowait(task)
                else:
                    task.finished = time.monotonic()
                    self.set_state(task, "Done", str(task.audio))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                task.finished = time.monotonic()
                self.set_state(task, "Failed", str(exc))
            finally:
                self.prepare_queue.task_done()

    async def prepare(self, task):
        missing = [name for name in ("curl", "ffmpeg", "ffprobe", "cp") if not shutil.which(name)]
        if missing:
            raise RuntimeError("Missing commands: " + ", ".join(missing))
        Path(DIST).mkdir(parents=True, exist_ok=True)
        Path(TMP).mkdir(parents=True, exist_ok=True)
        page = await self.network_request(task, task.url)
        task.title = html_module.unescape(parse_title(page))
        audio_url = parse_audio_url(page)
        if not audio_url:
            raise ValueError("No og:audio found on the page")
        audio_url = html_module.unescape(audio_url)
        ext = guess_ext(audio_url)
        with tempfile.TemporaryDirectory(prefix="temp_pod_", dir=TMP) as folder:
            original = Path(folder) / f"original.{ext}"
            converted = Path(folder) / "converted.mp3"
            await self.network_request(task, audio_url, original)
            self.set_state(task, "Converting", f"Downloaded {fmt_size(original.stat().st_size)}")
            copy_original = False
            if ext == "mp3" and task.trim is None:
                bitrate = await self.command("ffprobe", "-v", "error", "-show_entries", "stream=bit_rate", "-of", "default=noprint_wrappers=1:nokey=1", original)
                bitrate = bitrate.splitlines()[0] if bitrate else ""
                copy_original = not (bitrate.isdigit() and int(bitrate) > 128000)
            if copy_original:
                # Use external cp so large local copies do not block input or cancellation.
                await self.command("cp", original, converted)
            else:
                args = ["ffmpeg", "-nostdin", "-v", "error"]
                if task.trim is not None:
                    args += ["-ss", str(task.trim)]
                args += ["-i", original, "-c:a", "libmp3lame", "-q:a", "7", converted, "-y"]
                await self.command(*args)
            if not converted.is_file() or not converted.stat().st_size:
                raise RuntimeError("Audio processing did not produce a valid file")
            self.set_state(task, "Saving")
            task.audio = Path(DIST) / f"{task.stem}.mp3"
            task.title_file = Path(DIST) / f"{task.stem}.txt"
            task.title_file.write_text(task.title, encoding="utf-8")
            converted.replace(task.audio)

    async def device_worker(self):
        while True:
            task = await self.device_queue.get()
            try:
                while not self.device_mounted():
                    if task.state != "Waiting for device":
                        self.set_state(task, "Waiting for device", self.dest)
                    await asyncio.sleep(1)
                speed = await asyncio.to_thread(history_speed, self.dest)
                estimate = estimate_seconds(task.audio.stat().st_size, speed)
                self.set_state(task, "Copying", f"Estimated from history: {fmt_dur(estimate)}")
                # One consumer serializes device copies, history and LIST.md updates.
                await asyncio.to_thread(self.copy_task, task)
                task.finished = time.monotonic()
                self.set_state(task, "Done", f"{self.dest}/{task.audio.name} (local files kept)")
            except asyncio.CancelledError:
                raise
            except InterruptedError:
                task.finished = time.monotonic()
                self.set_state(task, "Stopped", "Local files kept")
            except Exception as exc:
                task.finished = time.monotonic()
                self.set_state(task, "Failed", f"Device copy: {exc}; local files kept")
            finally:
                self.device_queue.task_done()

    def copy_task(self, task):
        destination = Path(self.dest)
        task.copy_total = task.audio.stat().st_size
        task.copy_bytes = 0
        task.copy_started = time.monotonic()
        start_iso = now_iso()
        for source in (task.audio, task.title_file):
            if self.stop_event.is_set():
                raise InterruptedError("Copy stopped")
            if not os.path.ismount(destination):
                raise OSError("Device disconnected")
            target = destination / source.name
            if target.exists():
                raise FileExistsError(f"Destination file already exists: {target}")
            temporary = destination / f".{source.name}.part"
            owned = False
            try:
                with source.open("rb") as src, temporary.open("xb") as out:
                    owned = True
                    while block := src.read(1024 * 1024):
                        if self.stop_event.is_set():
                            raise InterruptedError("Copy stopped")
                        out.write(block)
                        if source == task.audio:
                            task.copy_bytes += len(block)
                    out.flush()
                    os.fsync(out.fileno())
                if self.stop_event.is_set():
                    raise InterruptedError("Copy stopped")
                temporary.rename(target)
            finally:
                if owned:
                    temporary.unlink(missing_ok=True)
        duration = time.monotonic() - task.copy_started
        cb, cd = read_last_cumulative()
        append_history(start_iso, now_iso(), task, destination, task.copy_total, duration, cb + task.copy_total, cd + duration)
        if self.stop_event.is_set():
            raise InterruptedError("Copy stopped")
        update_device_index(self.dest)

    async def action_stop(self):
        if self.stopping:
            return
        self.stopping = self.closing = True
        self.query_one(CommandInput).disabled = True
        self.stop_event.set()
        self.log_message("Stopping downloads/conversions and waiting for copy cleanup…")
        # Cancel preparation immediately; let the copy thread clean up before exiting.
        for runner in self.runners[:-1]:
            runner.cancel()
        await asyncio.gather(*self.runners[:-1], return_exceptions=True)
        device = self.runners[-1]
        if not any(t.state == "Copying" for t in self.tasks):
            device.cancel()
        else:
            while any(t.state == "Copying" and t.finished is None for t in self.tasks):
                await asyncio.sleep(0.05)
            device.cancel()
        await asyncio.gather(device, return_exceptions=True)
        for task in self.tasks:
            if task.finished is None:
                task.state = "Stopped"
                task.finished = time.monotonic()
        self.exit()


def load_config():
    try:
        with CONFIG.open(encoding="utf-8") as file:
            config = json.load(file)
    except FileNotFoundError:
        config = {}
    except (OSError, ValueError) as exc:
        raise ValueError(f"Cannot read {CONFIG.name}: {exc}") from None
    if not isinstance(config, dict):
        raise ValueError("config.json must contain a JSON object")
    unknown = config.keys() - {"dest", "concurrency"}
    if unknown:
        raise ValueError("Unknown config.json settings: " + ", ".join(sorted(unknown)))
    dest = config.get("dest")
    if dest is not None:
        if not isinstance(dest, str) or not dest.strip():
            raise ValueError("config.json dest must be a non-empty path string or null")
        path = Path(dest).expanduser()
        dest = str((path if path.is_absolute() else BASE / path).resolve())
    concurrency = config.get("concurrency", MAX_CONCURRENT)
    if type(concurrency) is not int or concurrency < 1:
        raise ValueError("config.json concurrency must be a positive integer")
    return dest, concurrency


def main():
    parser = argparse.ArgumentParser(description="Podcast task TUI. Configure dest and concurrency in config.json beside pod.py.")
    parser.parse_args()
    try:
        dest, concurrency = load_config()
    except ValueError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 1
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        print("The TUI requires an interactive terminal. Run pod.py directly in a terminal.", file=sys.stderr)
        return 1
    PodApp(dest, concurrency).run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

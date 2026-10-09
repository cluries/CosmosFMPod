# Pod Podcast Task TUI

A terminal app that downloads podcast audio from episode pages, optionally trims the beginning, and copies the resulting MP3 and title file to a mounted audio device. Downloads run concurrently; device transfers run one at a time, with progress and a queue shown in the terminal.

## Requirements and support

- Python 3.10 or newer.
- `curl`, `ffmpeg` (with `ffprobe` and the `libmp3lame` encoder), and the Unix `cp` command on your PATH.
- An interactive terminal; redirected input/output is not supported.

Development and local validation have been performed on macOS. Linux may work with the same dependencies and an appropriate device mount point, but has not been verified. Windows is not currently supported.

The downloader reads an `og:audio` meta tag from the page HTML. Xiaoyuzhou (小宇宙) episode pages are the intended use case; their title suffix is removed. Other pages may work if they expose an accessible audio URL in this tag. RSS feeds, direct audio URLs, login-required pages, and pages that expose audio only through JavaScript are not supported. Current live website compatibility has not been verified by the automated tests.

## Quick start

On macOS, install the system dependencies using Homebrew if needed:

```sh
brew install curl ffmpeg
```

From the cloned repository directory, create a virtual environment and install the Python dependencies:

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python pod.py
```

Type a podcast episode page URL followed by `nomove` into the bottom input box, then press Enter:

```text
https://www.xiaoyuzhoufm.com/episode/<episode-id> nomove
```

Replace `<episode-id>` with the ID from an actual episode page. `nomove` keeps the output local. Files are saved in `dist/` beside `pod.py`.

To copy results to a device, configure its mount point once as described below, then submit the episode URL without `nomove`. A configured destination must be a mounted filesystem, not just an existing folder. If it is disconnected, transfer tasks wait while other downloads continue. Without a configured destination, transfer submissions are rejected with a configuration hint; local `nomove` tasks still work.

## Configuration

Settings are read from `config.json` in the project root, beside `pod.py`, regardless of the directory you launch from. Copy the example once and edit it. If `config.json` already exists, edit that file directly; the command below preserves existing settings:

```sh
cp -n config.example.json config.json
```

For example:

```json
{
  "dest": "/Volumes/Your Audio Player",
  "concurrency": 3
}
```

- `dest`: the device mount point, or `null` for local `nomove` tasks only. Absolute paths and `~` are supported; relative paths are resolved against the project root.
- `concurrency`: a positive JSON integer, defaulting to 3.

If the file is absent, the defaults are no device and concurrency 3. Missing settings also use these defaults. Invalid JSON, unknown settings, or invalid values produce a configuration error before the TUI starts. Changes take effect on the next launch.

After saving the file, start normally each time:

```sh
python pod.py
```

Your local `config.json` is excluded from Git so your device path stays local. `config.example.json` is included in the repository. Configuration does not use command-line settings or environment variables.

`dist/`, `tmp/`, and `transfer_history.jsonl` are created beside the script, regardless of the working directory. The script directory must be writable. These files are excluded from Git.

Transfer history uses one JSON object per line and records each successfully copied MP3/TXT pair. New records include `schema_version` (2), the original episode page `url`, `trim_seconds` (`null` when omitted, or a number including explicit `0`), `title`, MP3 `filename`, `title_filename`, local `audio_path` and `title_path`, device `destination`, transfer `start_time` and `end_time`, audio `size_bytes`, transfer `duration` in seconds, and cumulative `cum_bytes` and `cum_duration` for speed estimates. Local-only `nomove` tasks and failed copies are not recorded. History is written before the device's `LIST.md` is regenerated, so a later index-update failure can leave a history record for a task shown as failed.

Malformed JSON, non-object records, missing statistical fields, and invalid statistical values are skipped when reading history. Statistics must be finite, non-negative JSON numbers; booleans and numeric strings are not accepted. Cumulative totals come from the latest valid record. Transfer-time estimates use total bytes divided by total duration for the same device's latest five valid transfers. Transfers to other devices are excluded; if no valid transfers match, legacy cumulative records without a destination provide a fallback. With no usable history, the estimate is shown as unknown.

If `transfer_history.jsonl` is absent, an existing `.move_history.jsonl` is automatically renamed when history is first read or written. Existing records and cumulative statistics are preserved; missing metadata in old records is not reconstructed. If both files exist, the app uses `transfer_history.jsonl` and leaves the old file untouched.

## Commands and controls

Submit `URL [trim seconds] [nomove]` in the TUI:

```text
https://example.com/episode
https://example.com/episode 10
https://example.com/episode nomove
https://example.com/episode 0.5 nomove
https://example.com/episode nomove 10
```

These URLs illustrate the syntax; use a supported episode page when running the app.

- A non-negative number trims that many seconds from the beginning. Decimals require a leading digit, for example `0.5`.
- Omitting `nomove` copies the task's MP3 and title TXT to the configured device and keeps the local files.
- Up / Down recalls input history while the input has focus. Tab switches focus.
- `/exit` stops accepting tasks and exits after submitted tasks finish. It keeps waiting if a required device is disconnected.
- Ctrl+C (or Ctrl+Q) stops downloads and conversions, waits for the active copy to clean up its temporary file, and exits. Local results and device files already finalized are kept.

## Processing and output

Within each task, page fetch, download, and conversion or trimming run in sequence. By default, up to three tasks process concurrently. A failed task does not stop the others.

Page fetching and audio downloading each allow up to three attempts for temporary network failures, including connection failures, timeouts, interrupted transfers, and HTTP 408, 429, 500, 502, 503 and 504. Retry waits are one second and then two seconds, with the next attempt shown in the task details and log. Audio retries restart the download from the beginning. Other HTTP errors, missing `og:audio`, and conversion errors fail without automatic retry. Ctrl+C also cancels a request or retry wait.

During audio downloading, the task table and selected task details show the attempt number, downloaded size and average speed for the current attempt. When the server supplies a usable `Content-Length`, they also show total size, percentage and estimated remaining time. Otherwise they show `Total unknown` without a percentage or ETA.

The output is an MP3 plus a UTF-8 TXT file containing the page title:

- An MP3 with no trim request is copied unchanged unless `ffprobe` reports an audio bitrate above 128 kbps. If no numeric bitrate is reported, it is also copied unchanged.
- Other inputs, higher-bitrate MP3s, and all trim requests are re-encoded using FFmpeg's `libmp3lame` encoder with `-q:a 7`. This is variable-quality encoding, not a fixed output bitrate; trimming is not lossless.
- Specifying `0` seconds still re-encodes the audio. Omit the trim argument to allow eligible MP3 files to be kept unchanged.
- Names use `monthday_hourminsec_millisecond`. The app checks existing output names and increments the timestamp to avoid collisions within its task submission flow. Multiple app instances sharing an output directory or device are not supported.
- The app processes only files produced for the submitted task; it does not scan or transfer old files in `dist/`.

Device tasks run in the order their audio finishes processing and enters the transfer queue, one at a time. Each copied file is written through a temporary `.part` file. The MP3/TXT pair is not one atomic transaction: an interruption after the MP3 is finalized may leave only that file on the device. Local results are retained if a transfer fails.

After each successful pair is copied, the app updates transfer history and regenerates the device's `LIST.md` from all top-level `.txt` files on the device. An existing `LIST.md` is replaced. Each title line contributes the text before its first `|`.

The pinned Device Transfer section shows the active transfer or device wait, copy progress, speed, estimated remaining time, and queue order. The task table adapts to terminal width; narrow terminals scroll horizontally with ID and status fixed. The selected task's details and long queue lists scroll independently. The bottom status line shows task counts and device connection. UI labels and logs are in English; podcast titles retain their original text.

## Troubleshooting

- **No device configured:** use `nomove`, or set `dest` in `config.json` and restart.
- **Waiting for device:** check the actual mount point. `/exit` will wait; Ctrl+C stops the session.
- **Missing commands:** install the required system tools and ensure they are on PATH.
- **No og:audio found:** the page does not expose audio in the supported HTML format.
- **Download or conversion failed:** review the task details and log. Completed local results are kept if only the device transfer fails.
- **Retrying failed tasks:** temporary network errors are retried within the current task as described above. Once a task is marked as failed, it is not retried automatically. The app waits for a disconnected device before starting a copy, but a copy that fails after starting is marked as failed and does not resume when the device reconnects. After a transfer failure, reconnect the device and manually copy the retained MP3/TXT files, or submit the episode again to create a new task. For a download or conversion failure, submit the episode again after resolving the cause.

## Tests

With the virtual environment activated and the system dependencies installed:

```sh
python -m unittest -v test_pod.py
```

Tests use temporary directories, a local HTTP server, generated audio, and mocked device mounting. They cover task parsing, configuration, TUI layout, processing concurrency, transfer ordering, history migration and malformed records, per-device speed estimates, download progress with known and unknown sizes, transient HTTP errors and interrupted-download retries, real local download/conversion/trimming, and cancellation cleanup during requests and retry waits. They do not contact podcast websites or write to a real audio device.

GitHub Actions runs this suite on macOS with Python 3.10 and 3.14.

## License

MIT. See [LICENSE](LICENSE).

# reSpeaker XVF3800 USB audio diagnostics

Reproducible diagnostic suite for the 48 kHz click/discontinuity report in [`respeaker/reSpeaker_XVF3800_USB_4MIC_ARRAY#39`](https://github.com/respeaker/reSpeaker_XVF3800_USB_4MIC_ARRAY/issues/39). It flashes the official firmware images, records every documented signal-path checkpoint while an independent speaker plays a deterministic stimulus, and analyzes the captures. The same code runs on macOS, Linux and Windows.

The repository contains the exact test voice WAV, the runner, a full validation script, and a `results/` area for compact evidence from real hardware runs. No hardware results have been published yet.

## Quick start

Requirements: Python 3.10+, internet access on the first run, the XVF3800 on its XMOS USB-C port, and a separate playback speaker.

```bash
python diagnose.py --yes        # Windows: py diagnose.py --yes
```

The runner creates its own virtual environment, downloads the official reSpeaker repository at a pinned commit (`RESPEAKER_REPO_COMMIT` in `diagnose.py`), obtains `dfu-util`, and verifies the firmware hashes. It then flashes each image, reads the running version and sample rate back, sets each route, plays the stimulus, records, and writes a self-contained HTML report, JSON/CSV and a compact evidence bundle under `runs/`.

- The full matrix takes about 25 minutes; `--quick` skips the historical v2.1.0 image and the extra localization routes.
- Current Seeed firmware exposes its DFU interface while running, so flashing needs no manual step. Otherwise the runner asks you to hold MUTE while reconnecting the board. `--yes` skips the ENTER prompts.
- Configuration changes are runtime-only: the runner never calls `SAVE_CONFIGURATION` or `CLEAR_CONFIGURATION`.
- `--reanalyze runs/<run-id>` rebuilds the analysis and report from a run's recordings.
- `--simulate` exercises the whole pipeline on synthetic captures, without hardware.

## What a run compares

- v2.1.0 48 kHz (historical) and v2.1.1 48 kHz normal processed output: conference left, ASR right;
- v2.1.1 48 kHz raw microphones, pre-SHF, SHF input, direct processed beam, and an upsampling-off probe;
- v2.1.1 native 16 kHz normal output and raw microphones;
- a 20-second no-playback capture for each firmware;
- on macOS with `ffmpeg` installed, the `normal` route recorded at the same time through FFmpeg/AVFoundation, the capture path used in the original report (`--no-host-control` disables it).

## What the report measures

- **Periodic discontinuities.** |x[n]−x[n−1]| is folded modulo candidate periods (1 ms USB frames and multiples, powers of two up to 2048). A capture is flagged only for a sharp single-phase excess. The report also states how many of the 12 largest jumps sit on that phase, the way the original issue counted boundary-aligned jumps.
- **48 kHz signature.** Whether a 48 kHz capture is band-limited to the XVF's 16 kHz processing domain, zero-stuffed (full-strength images above 8 kHz) or genuinely full-band.
- **Host capture-path continuity.** Every 512-frame block of the FFmpeg recording is located in the direct capture of the same stream. Bit-exact blocks with whole blocks missing mean the discontinuities are created by the host capture path, not by the device.

[`docs/METHODOLOGY.md`](docs/METHODOLOGY.md) explains the thresholds and how they were calibrated.

## Full validation

`tools/validate.py` runs every check end to end. It writes `validation/<timestamp>/report.html` (statistics across trials; per trial the folded mod-512 profile of FFmpeg vs direct capture, a dropped-buffer timeline, a sample-level zoom of the frames FFmpeg lost, and spectra and spectrograms of both), plus `SUMMARY.md`, `results.json`, `log.txt` and all recordings. The matrix run's own report sits next to it.

| Stage | What it does | Audible |
|---|---|---|
| `unit` | hardware-free test suite | no |
| `upstream` | pinned reSpeaker repository: firmware hashes, `xvf_host.py` CLI contract | no |
| `readonly` | read-only checks against the connected board | no |
| `ffmpeg` | builds current FFmpeg master, which contains the AVFoundation fix (macOS) | no |
| `matrix` | the full diagnostic run above, including the FFmpeg control | yes, reflashes |
| `hostpath` | repeated FFmpeg vs direct-capture trials on the latest 48 kHz firmware with Homebrew FFmpeg and the master build, `ffmpeg -t` duration checks, the built-in microphone as a non-XVF control, and v2.1.1 16k | yes, reflashes |

```bash
python tools/validate.py --silent                 # only the silent stages
python tools/validate.py --volume 50              # everything, at 50 % output volume
python tools/validate.py --stages unit            # just the test suite
python tools/validate.py --stop                   # stop a running validation immediately
```

The board is returned to the firmware it was running (or the image given with `--final-firmware`), and the output volume to its previous level. `--rebuild-report validation/<timestamp>` recomputes a finished run from its recordings.

## Publishing a run

Runs are written under `runs/` and ignored by Git because they contain raw recordings, including whatever the room sounded like. Import the compact evidence with:

```bash
python tools/import_run.py runs/xvf3800-diagnostic-YYYYMMDD-HHMMSS
```

This copies `report.html`, `summary.csv`, `analysis.json`, `session.json`, `github_comment.md` and `github-share-bundle.zip` into `results/issue-39/<run-id>/`, with SHA-256 hashes in `manifest.json`. Files GitHub would reject belong on a GitHub Release; keep their hash and link in the result directory.

## Canonical test voice

```text
assets/voice/xvf3800-test-vo.wav
SHA-256: 2a94eeb70177de6c22db7a1253c5dacf41a7b6203d28dd5bf4027cbc6e61b15b
48,000 Hz · mono · PCM16 · 63.04 s
```

The file is a byte-for-byte copy of the ElevenLabs download: not trimmed, normalized, denoised, EQ'd or transcoded. In the stimulus it gets a fixed −6 dB gain and a 10 ms fade at each edge, so the insertion itself cannot create a click. [`script.txt`](assets/voice/script.txt), [`voice-design-prompt.txt`](assets/voice/voice-design-prompt.txt) and [`provenance.json`](assets/voice/provenance.json) record how it was made.

Verify it with `shasum -a 256` (macOS), `sha256sum` (Linux) or `certutil -hashfile <file> SHA256` (Windows). Do not replace it after publishing results; add a new versioned fixture instead.

The rest of the stimulus (silence, sync chirp, pink and white noise, stepped tones, multitone, log sweep, transient train) is generated deterministically by the runner.

## Tests

```bash
python tools/validate.py --stages unit
```

This runs the hardware-free suite inside the runner's virtual environment. The suite emulates the device end to end with a stand-in for Seeed's `xvf_host.py` that enforces its exact CLI, a fake `dfu-util`, a fake FFmpeg that drops buffers the way AVFoundation does, and a PortAudio stand-in that re-enumerates after each flash. With that it runs the full firmware matrix. `XVFDIAG_NETWORK_TESTS=1` and `XVFDIAG_HARDWARE_TESTS=1` enable the `upstream` and `readonly` groups.

## Platform notes

- **macOS**: `dfu-util` is installed with Homebrew if missing. The FFmpeg host-path control needs `ffmpeg`.
- **Linux**: missing `dfu-util`/PortAudio are installed with the system package manager via `sudo`. Flashing and the USB control interface need root or a udev rule for USB device `2886:001a`.
- **Windows**: `dfu-util` is downloaded automatically. DFU needs the WinUSB driver: per Seeed's [DFU guide](https://github.com/respeaker/reSpeaker_XVF3800_USB_4MIC_ARRAY/blob/master/xmos_firmwares/dfu_guide.md), install it with [Zadig](https://zadig.akeo.ie/) if `dfu-util -l` reports `LIBUSB_ERROR_NOT_SUPPORTED`.

## Repository layout

```text
diagnose.py              runner: flashing, capture, analysis, report
tools/validate.py        full end-to-end validation
tools/validation_report.py  its HTML report
tools/import_run.py      publishes a run's compact evidence into results/
assets/voice/            canonical speech fixture and its provenance
docs/METHODOLOGY.md      measurement rationale and thresholds
examples/                simulated report and summary
results/issue-39/        published evidence (none yet)
tests/                   pytest suite, with fakes for the device, dfu-util and PortAudio
```

## Licenses

Source code and documentation are MIT licensed. Binary audio fixtures and captured diagnostic evidence are treated separately; see [`ASSET_NOTICE.md`](ASSET_NOTICE.md).

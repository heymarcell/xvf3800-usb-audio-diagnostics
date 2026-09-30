# reSpeaker XVF3800 USB audio diagnostics

Reproducible cross-platform diagnostic suite for investigating the 48 kHz processed-output discontinuity/click issue reported in [`respeaker/reSpeaker_XVF3800_USB_4MIC_ARRAY#39`](https://github.com/respeaker/reSpeaker_XVF3800_USB_4MIC_ARRAY/issues/39).

The repository contains **the exact test voice WAV**, the deterministic diagnostic runner, and compact published evidence from real hardware runs so another person can repeat the same experiment on macOS, Linux, or Windows.

## Repository layout

```text
.
├── diagnose.py                     # complete one-command diagnostic pipeline
├── run_mac_linux.sh                # convenience launcher
├── run_windows.bat                 # convenience launcher
├── assets/
│   └── voice/
│       ├── xvf3800-test-vo.wav     # exact immutable 48 kHz PCM VO fixture
│       ├── script.txt              # exact ElevenLabs v4 tagged generation text
│       ├── voice-design-prompt.txt # Voice Design prompt
│       ├── provenance.json         # generator/model/voice/hash metadata
│       └── SHA256SUMS.txt
├── docs/
│   └── METHODOLOGY.md
├── results/
│   └── issue-39/
│       └── <run-id>/               # compact evidence imported from a real run
├── examples/
│   ├── simulated_report.html
│   └── simulated_summary.csv
├── tools/
│   └── import_run.py               # publishes a compact run into results/
└── .github/workflows/ci.yml        # hardware-free smoke test
```

## Canonical test voice

The repository already includes the exact immutable ElevenLabs WAV used for this suite:

```text
assets/voice/xvf3800-test-vo.wav
SHA-256: 2a94eeb70177de6c22db7a1253c5dacf41a7b6203d28dd5bf4027cbc6e61b15b
48,000 Hz · mono · PCM16 · 63.04 s
```

The file is a byte-for-byte copy of the original ElevenLabs download. It is not trimmed, normalized, denoised, EQ'd, or transcoded. The runner applies a fixed **-6 dB gain** only when constructing the playback stimulus, preserving the fixture's relative dynamics while creating headroom.

[`assets/voice/script.txt`](assets/voice/script.txt) contains the exact ElevenLabs v4 tagged script, [`voice-design-prompt.txt`](assets/voice/voice-design-prompt.txt) records the Voice Design prompt, and [`provenance.json`](assets/voice/provenance.json) records the source filename, audio properties, generation metadata, and hash.

Do **not** regenerate or replace this WAV after publishing results. Reproducibility depends on everyone using the identical binary fixture.

## One-command run

When the canonical VO is present, no `--voice` argument is required:

```bash
python diagnose.py
```

Windows:

```powershell
py diagnose.py
```

To use another voice file temporarily:

```bash
python diagnose.py --voice /path/to/voice.wav
```

The runner creates its own virtual environment, installs Python dependencies, downloads the official reSpeaker repository, obtains `dfu-util` where practical, verifies firmware hashes, generates the technical stimulus, flashes/configures the XVF3800, plays the stimulus from an independent speaker, records the device, analyzes the captures, and generates a self-contained HTML report plus a compact GitHub evidence bundle.

The only unavoidable manual operation is entering XVF3800 DFU mode when prompted.

The runner never calls `SAVE_CONFIGURATION` or `CLEAR_CONFIGURATION`.

## Publish a real diagnostic run

A real run is written under `runs/` and intentionally ignored by Git because it can contain large raw WAV files.

Import the compact evidence package into the repository with:

```bash
python tools/import_run.py runs/xvf3800-diagnostic-YYYYMMDD-HHMMSS
```

This creates:

```text
results/issue-39/xvf3800-diagnostic-YYYYMMDD-HHMMSS/
├── README.md
├── manifest.json
├── report.html
├── summary.csv
├── analysis.json
├── session.json
├── github_comment.md
└── github-share-bundle.zip
```

Every published file is SHA-256 hashed in `manifest.json`.

If a result package is too large for normal GitHub storage, attach it to a GitHub Release instead and keep its SHA-256 plus release link under the corresponding `results/issue-39/<run-id>/` directory.

## Test matrix

The full run compares:

- v2.1.0 48 kHz historical processed-output control;
- v2.1.1 48 kHz normal processed output;
- v2.1.1 48 kHz raw microphones;
- v2.1.1 48 kHz pre-SHF microphones;
- v2.1.1 48 kHz SHF input;
- v2.1.1 48 kHz direct processed/ASR output;
- v2.1.1 48 kHz processed output with Audio Manager upsampling disabled as a localization probe;
- v2.1.1 native 16 kHz normal output;
- v2.1.1 native 16 kHz raw microphones;
- 20-second no-playback controls.

See [`docs/METHODOLOGY.md`](docs/METHODOLOGY.md) for the measurement rationale and interpretation rules.

## Technical stimulus

The technical signal is generated deterministically by the runner and includes silence, a synchronization chirp, pink noise, white noise, stepped sine tones, multitone, logarithmic sweep, a transient train, the canonical English VO at a fixed -6 dB playback gain, and tail silence.

The generated technical WAV is an output of the experiment, not a manually maintained source asset. This prevents accidental drift between the code and the stimulus.

## Validate without hardware

```bash
python diagnose.py --simulate --no-open
```

This exercises the stimulus, synthetic captures, analysis, plots, HTML report, CSV/JSON and evidence bundle without flashing hardware.

## Cross-platform scope

The same Python implementation is used on macOS, Linux and Windows. Device control uses Seeed's portable `python_control/xvf_host.py` from the official repository downloaded at runtime.

Platform-specific limitations, especially Windows DFU driver requirements, are documented in the generated report and methodology.

## Licenses

Source code and documentation in this repository are MIT licensed. Binary audio fixtures and captured diagnostic evidence are treated separately; see [`ASSET_NOTICE.md`](ASSET_NOTICE.md).

# Diagnostic methodology

## Goal

Reproduce or falsify the v2.1.1 48-kHz processed-output click report while localizing the first signal-path checkpoint where the artifact becomes measurable.

## Controls

The runner uses three kinds of controls:

1. **same firmware, earlier signal-path checkpoints**: raw microphone (category 1), pre-SHF amplified microphone (category 11), SHF input (category 3);
2. **same firmware, processed checkpoints**: default/user-selected processed output (category 8), direct processed auto-select beam (category 6), ASR output (category 7), plus an explicit upsampling-off localization probe;
3. **different firmware/sample-rate control**: native-16-kHz v2.1.1, tested last so the board is left in the clean STT firmware state.

Deep/default mode also runs v2.1.0_48k2ch once to preserve the historical comparison.

## Playback independence

The stimulus is played through a separate host output device rather than the reSpeaker's own playback path. This avoids adding the XVF far-end/AEC playback chain to a test that is intended to isolate capture/DSP/export behavior.

## Why broadband noise does not directly count as a click

White/pink noise naturally contains large adjacent-sample changes. Therefore the report does **not** classify a recording as faulty just because it contains many `abs(x[n]-x[n-1]) > 0.2` events.

The defect signature is based on abnormal concentration of such events at the same sample phase modulo candidate periods (64, 128, 256, 512, 1024, 2048). This is why the report includes both raw event counts and phase enrichment.

## Why modulo 512 is not treated as root cause

The original capture showed strong concentration at sample index 511 modulo 512. The report preserves that observation but also scans neighboring powers of two and explicitly labels the value as **capture periodicity**, not an assumed internal block size.

## Technical stimulus

The synthetic sections serve different purposes:

- silence: spontaneous artifact/noise baseline;
- synchronization chirp: acoustic alignment across capture APIs and platforms;
- pink noise: broad spectral excitation with speech-like low-frequency weighting;
- white noise: broad spectral excitation;
- stepped sine tones: steady-state continuity and level behavior;
- multitone: simultaneous-frequency behavior;
- logarithmic sweep: transfer-function/spectral anomaly visibility;
- transient train: response to short legitimate acoustic transients;
- canonical natural-English VO: behavior on actual speech rather than laboratory signals. The committed source WAV is preserved byte-for-byte and inserted into the composite stimulus with a fixed -6 dB gain and a 10 ms edge fade (so the insertion cannot itself create a discontinuity); there is no loudness normalization, compression, EQ, or denoising.

## Reproducibility

The runner records:

- OS/Python/audio-device inventory;
- the pinned commit of the official reSpeaker repository that supplies the firmware images and `xvf_host.py`;
- exact firmware SHA-256;
- XVF configuration before and after every capture;
- exact canonical VO SHA-256 and fixed playback gain;
- exact stimulus and stimulus manifest;
- original PCM WAV recording;
- machine-readable analysis JSON and summary CSV;
- the exact diagnostic runner used for the run.

No configuration is persisted to XVF flash by the runner.

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

## Detecting periodic discontinuities

Loud legitimate content produces large adjacent-sample changes: broadband noise, AGC-boosted plosives, a tap on the desk. A fixed threshold such as `abs(x[n]-x[n-1]) > 0.2` counts every sample of such a burst, depends on level and sample rate (the same audio has larger steps at 16 kHz), and misses defects whose individual steps are smaller than the threshold. Raw threshold counts are therefore reported for reference only.

The classification instead folds `|x[n]-x[n-1]|` modulo each candidate period P and averages it per phase. A defect at a fixed position of every P-frame block raises one phase; content that is not locked to the device clock averages out. Candidate periods are 1 ms USB frames and multiples (48, 480, 720, 960 at 48 kHz; 16, 160, 240 at 16 kHz) and powers of two up to 2048.

- **Fundamental period.** Splices at block boundaries look as strong at every multiple of the block length (split over several phases) and weaker at divisors, so the report names the smallest period whose excess is at least 60 % of the largest.
- **Flagging.** A channel is flagged when the worst phase is ≥ 10 robust z-scores above the median phase, ≥ 1.25× the median, and sharp (≥ 1.2× its neighbouring phases). On clean real captures the maximum over all periods was z ≈ 3–6 and ≤ 1.2×; FFmpeg/AVFoundation buffer splices measured z = 32–54 and 2.3–2.6×. Frame-rate modulation of loud processed speech can exceed the z threshold but is broad, and is reported as modulation rather than a click.
- **Exclusions.** On playback captures the sync chirp, stepped tones, multitone and transient train are excluded (with 50 ms margins) because their own periods divide the candidate periods; for example the transients are spaced 0.32 s = 30 × 512 frames at 48 kHz.
- **Report form.** Besides z and ratio, the report states how many of the 12 largest jumps sit on the flagged phase, which is how the original issue counted boundary-aligned jumps.

Phases are the index of the later sample: a splice between frames 511 and 512 of a 512-frame block is phase 0.

## Alignment

Processed outputs (AGC, noise suppression, beam selection) can nearly erase the 0.35 s sync chirp; on one real ASR capture the chirp's best match was 0.8 s off. The playback start is therefore found on the 5 ms level envelope of the whole stimulus first and then refined on the chirp within ±30 ms. The report shows the chirp correlation and how closely the two channels agree.

## 48 kHz signature

The XVF3800 processes audio at 16 kHz. For each 48 kHz capture the report checks the RMS of the three sample phases modulo 3 and the energy above 8.4 kHz relative to 0.1–7.6 kHz (or against the 16-bit noise floor for quiet channels):

- *zero-stuffed 3× upsampling*: one phase carries the signal, the other two are ~0; the spectrum above 8 kHz mirrors the band below at full strength and produces large sample-to-sample steps on ordinary content;
- *band-limited to 8 kHz*: filtered upsampling of the 16 kHz path;
- *full-band*: genuine content above 8 kHz.

Raw jump counts are only comparable between captures with the same signature.

## Host capture-path control

Issue #39 was measured with FFmpeg's AVFoundation input on macOS. On macOS with `ffmpeg` installed, the runner records the `normal` route of each firmware through `ffmpeg -f avfoundation` at the same time as the direct PortAudio capture. Both see the same USB stream, which is 16-bit, so its samples are identical in both recordings wherever both received them.

`buffer_continuity` locates every 512-frame block of the FFmpeg recording in the direct capture. Contiguous blocks keep a constant offset; each missing block shifts it by exactly 512 frames. The report lists bit-exact blocks, splices and missing frames. If every FFmpeg block is bit-exact and the only differences are whole missing blocks, the boundary-aligned discontinuities in that recording are created by the host capture path, not by the device.

## Firmware verification

Each image is SHA-256 verified before flashing. After flashing, the runner reads `VERSION` back and waits for the audio input at the image's sample rate; the two v2.1.1 images share a version and are told apart by rate. On macOS `dfu-util` 0.11 sometimes exits non-zero when the device disappears during the final `-R` reset after a complete download; that exit is accepted only when the output shows the completed download and manifest, and the read-back decides.

On v2.1.1 `AUDIO_MGR_OP_UPSAMPLE 0 0` reads back as written but the 48 kHz output stays band-limited, so the `processed_no_upsample` probe does not isolate the upsampler on that firmware.

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
- the running firmware version read back after every flash;
- exact firmware SHA-256;
- XVF configuration before and after every capture;
- exact canonical VO SHA-256 and fixed playback gain;
- exact stimulus and stimulus manifest;
- original PCM WAV recording;
- machine-readable analysis JSON and summary CSV;
- the exact diagnostic runner used for the run.

No configuration is persisted to XVF flash by the runner.

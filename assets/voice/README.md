# Canonical natural-speech fixture

`xvf3800-test-vo.wav` is the immutable natural-English speech fixture used by this diagnostic suite.

- Voice: **Mara - Neutral Diagnostic Narrator**
- Generator: ElevenLabs
- Model: `eleven_v4`
- Format: 48 kHz, mono, 16-bit PCM WAV
- Duration: 63.04 s
- SHA-256: `2a94eeb70177de6c22db7a1253c5dacf41a7b6203d28dd5bf4027cbc6e61b15b`

The original downloaded WAV is committed **without editing, trimming, normalization, EQ, denoising, or transcoding**. `provenance.json` records the source filename and generation metadata. `script.txt` contains the generation text including the ElevenLabs v4 delivery tags, and `voice-design-prompt.txt` records the Voice Design prompt.

The diagnostic runner does not modify this file. When building the composite acoustic stimulus it reads this fixture and applies a deterministic fixed **-6 dB playback gain** to provide headroom while preserving the waveform's relative dynamics.

Do not replace this file after publishing diagnostic results. A future voice fixture should be added under a new versioned filename and referenced explicitly by the run metadata.

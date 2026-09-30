# Publishing this repository

Recommended public repository name:

`heymarcell/xvf3800-usb-audio-diagnostics`

## 1. Verify the canonical VO

The canonical fixture is already included at:

`assets/voice/xvf3800-test-vo.wav`

Before the first push, verify its checksum:

```bash
shasum -a 256 assets/voice/xvf3800-test-vo.wav       # macOS
sha256sum assets/voice/xvf3800-test-vo.wav           # Linux
certutil -hashfile assets\\voice\\xvf3800-test-vo.wav SHA256  # Windows
```

Expected SHA-256:

`2a94eeb70177de6c22db7a1253c5dacf41a7b6203d28dd5bf4027cbc6e61b15b`

Do not replace this WAV after publishing real results. If a future fixture is needed, add a new versioned file and reference it explicitly from the run metadata.

## 2. Create and push with GitHub CLI

```bash
git init -b main
git add .
git commit -m "Initial XVF3800 diagnostic suite"
gh repo create heymarcell/xvf3800-usb-audio-diagnostics \
  --public \
  --description "Cross-platform reproducible diagnostics for reSpeaker XVF3800 USB audio firmware" \
  --source=. \
  --remote=origin \
  --push
```

If `gh` is not authenticated, run `gh auth login` first.

## 3. Publish a diagnostic result

After a real hardware run:

```bash
python tools/import_run.py runs/<run-id>
git add results/issue-39/<run-id>
git commit -m "Add diagnostic evidence <run-id>"
git push
```

If `github-share-bundle.zip` becomes too large for normal Git history, create a GitHub Release and attach the bundle there instead. Keep the release URL and SHA-256 in the result directory.

## 4. Link issue #39

Use the generated `github_comment.md` from the diagnostic run as the basis for the response at:

https://github.com/respeaker/reSpeaker_XVF3800_USB_4MIC_ARRAY/issues/39

Link back to the exact repository commit and published result directory so the maintainer can reproduce the run from immutable inputs.

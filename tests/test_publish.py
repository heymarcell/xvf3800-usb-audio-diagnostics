"""tools/publish_validation.py: sanitized, hashed, README with figures, index."""
import hashlib
import importlib.util
import json
import sys

import numpy as np

import diagnose
from conftest import REPO_ROOT


def load(name):
    sys.path.insert(0, str(REPO_ROOT / "tools"))
    spec = importlib.util.spec_from_file_location(name, REPO_ROOT / "tools" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_publish_validation(tmp_path, rt):
    rep, val, pub = load("validation_report"), load("validate"), load("publish_validation")
    run = tmp_path / "validation" / "20260930-185836"
    rng = np.random.default_rng(1)
    b, a = rt.sig.butter(4, 0.1)
    ref = rt.sig.lfilter(b, a, rng.normal(0, 0.4, 512 * 600))
    keep = rng.random(600) > 0.15
    keep[:4] = True
    test = np.concatenate([ref[k * 512:(k + 1) * 512] for k in range(600) if keep[k]])
    tdir = run / "hostpath" / "xvf48k_homebrew_ab1"
    tdir.mkdir(parents=True)
    rt.sf.write(str(tdir / "direct.wav"), np.column_stack([ref, ref]), 48000, subtype="PCM_16")
    rt.sf.write(str(tdir / "ffmpeg.wav"), np.column_stack([test, test]), 48000, subtype="PCM_16")
    (tdir / "secret-room-audio.flac").write_bytes(b"not for publishing")
    cont = diagnose.buffer_continuity(tdir / "direct.wav", tdir / "ffmpeg.wav", rt.sf, np, rt.sig)
    cont["missing_pct"] = 100 * cont["frames_missing"] / (cont["test_frames"] + cont["frames_missing"])
    trials = [{"name": "xvf48k_homebrew_ab1", "kind": "ab", "dir": str(tdir), "continuity": cont,
               "ffmpeg": {"classification": diagnose.PERIODIC, "periodicity": ["a", "b"]},
               "direct": {"classification": diagnose.CLEAN, "periodicity": ["a", "b"]}},
              {"name": "xvf48k_homebrew_duration30", "kind": "duration", "dir": str(run / "hostpath" / "d"), "requested_s": 30.0,
               "held_s": 23.7, "missing_pct": 21.0, "ffmpeg": {"classification": diagnose.PERIODIC}}]
    results = {"started": "20260930-185836", "errors": [], "env": {"platform": "macOS", "system_ffmpeg": {"version": "ffmpeg version 9.0.2 Copyright x"}},
               "stages": {"ffmpeg": {"ok": True, "version": "ffmpeg version git-2026-09-30-e9dc8fd Copyright x", "binary": str(REPO_ROOT / ".xvfdiag-cache/ffmpeg")},
                          "hostpath": {"ok": True, "trials": trials, "playback_device": "Speakers"}}}
    rep.render(results, run)
    (run / "results.json").write_text(json.dumps(results))
    (run / "SUMMARY.md").write_text(val.render_summary(results) + f"\nSession: `{run}/matrix/x`\n", encoding="utf-8")
    (run / "log.txt").write_text("private log")

    dest = pub.publish(run, tmp_path / "published", with_matrix=True)
    names = {p.relative_to(dest).as_posix() for p in dest.rglob("*") if p.is_file()}
    assert {"README.md", "report.html", "SUMMARY.md", "results.json", "manifest.json", "figures/durations.png",
            "figures/xvf48k_homebrew_ab1_fold.png", "figures/xvf48k_homebrew_ab1_zoom.png"} <= names
    assert not any(n.endswith((".wav", ".flac", "log.txt")) for n in names)
    for f in ("README.md", "SUMMARY.md", "results.json", "report.html"):
        text = (dest / f).read_text(encoding="utf-8")
        assert str(run) not in text and str(REPO_ROOT) not in text, f
    readme = (dest / "README.md").read_text(encoding="utf-8")
    assert readme.startswith("# Validation 2026-09-30 18:58: Homebrew FFmpeg 9.0.2 and FFmpeg git-2026-09-30-e9dc8fd (source build)")
    for line in readme.splitlines():
        if line.startswith("!["):
            assert (dest / line.split("](")[1].rstrip(")")).is_file()
    for f in json.loads((dest / "manifest.json").read_text(encoding="utf-8"))["files"]:
        assert hashlib.sha256((dest / f["path"]).read_bytes()).hexdigest() == f["sha256"]
    assert "validation-20260930-185836/README.md" in (tmp_path / "published" / "README.md").read_text(encoding="utf-8")
    assert json.loads((dest / "results.json").read_text(encoding="utf-8"))["stages"]["hostpath"]["trials"][0]["dir"] == "hostpath/xvf48k_homebrew_ab1"


def test_sanitize_windows_and_posix_paths(tmp_path):
    pub = load("publish_validation")
    run = tmp_path / "run"
    assert pub.relativize({"a": [str(run / "hostpath" / "x")]}, run) == {"a": ["hostpath/x"]}
    assert pub.sanitize(r"see C:\Users\alice\data and /Users/alice/x", run) == r"see ~\data and ~/x"

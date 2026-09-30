#!/usr/bin/env python3
"""End-to-end validation of the diagnostic suite and of the issue #39 evidence.

Stages, in order (select with --stages, or --silent for the first four):
  unit      hardware-free test suite                                   silent
  upstream  pinned reSpeaker repository: firmware hashes, xvf_host CLI     silent, network
  readonly  read-only checks against the connected XVF3800                silent
  ffmpeg    build FFmpeg at the upstream AVFoundation fix (macOS)          silent, network
  matrix    full diagnostic matrix incl. FFmpeg host-path control         plays audio, reflashes, ~25 min
  hostpath  repeated FFmpeg vs direct-capture trials: Homebrew FFmpeg and
            the fixed build on v2.1.1 48k, a -t duration check, the
            built-in microphone as a non-XVF control, and v2.1.1 16k       plays audio, reflashes, ~15 min

Everything lands in validation/<timestamp>/ (log.txt, results.json, SUMMARY.md, recordings).
The board is returned to the firmware it was running, and the output volume to its level.

Stop immediately:  python tools/validate.py --stop
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import re
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
import diagnose as d  # noqa: E402

STAGES = ["unit", "upstream", "readonly", "ffmpeg", "matrix", "hostpath"]
SILENT_STAGES = STAGES[:4]
PIDFILE = REPO / "validation" / "running.pid"
# FFmpeg commit "avdevice/avfoundation: wait for frame consumption to avoid dropping A/V frames".
FFMPEG_FIX_COMMIT = "ddf8f40301af20ad985cf369d1eb6d114be0c8f0"
FFMPEG_CONFIGURE = [
    "--disable-everything", "--disable-doc", "--disable-network", "--disable-ffplay", "--disable-ffprobe",
    "--enable-indev=avfoundation", "--enable-muxer=wav", "--enable-encoder=pcm_s16le",
    "--enable-decoder=pcm_s16le,pcm_s16be,pcm_s24le,pcm_s32le,pcm_f32le,pcm_f32be",
    "--enable-protocol=file", "--enable-filter=aresample,aformat,anull",
]
ISSUE_ROUTE = d.ROUTES_48K[0]  # L 8 0, R 7 3, upsample 1 1: the routing in issue #39

ACTIVE_CAPTURES: list[d.HostPathCapture] = []


# --- process control --------------------------------------------------------------------------

def stop_running() -> int:
    if not PIDFILE.exists():
        print("No validation run is recorded as running.")
        return 0
    info = json.loads(PIDFILE.read_text())
    pgid = int(info["pgid"])
    for sig_ in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(pgid, sig_)
        except ProcessLookupError:
            break
        time.sleep(3)
    PIDFILE.unlink(missing_ok=True)
    print(f"Stopped validation process group {pgid}.")
    return 0


def on_sigterm(signum: int, frame: Any) -> None:
    raise KeyboardInterrupt


class Tee:
    def __init__(self, *streams: Any):
        self.streams = streams

    def write(self, text: str) -> int:
        for s in self.streams:
            s.write(text)
            s.flush()
        return len(text)

    def flush(self) -> None:
        for s in self.streams:
            s.flush()


def run_logged(cmd: list[str], env: dict[str, str] | None = None, cwd: Path = REPO) -> int:
    print(f"$ {' '.join(cmd)}", flush=True)
    proc = subprocess.Popen(cmd, cwd=cwd, env={**os.environ, **(env or {})}, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True, bufsize=1)
    try:
        for line in proc.stdout:
            print(line, end="", flush=True)
        return proc.wait()
    except BaseException:
        proc.kill()
        raise


def output_volume() -> int | None:
    if platform.system() != "Darwin":
        return None
    out = subprocess.run(["osascript", "-e", "output volume of (get volume settings)"], capture_output=True, text=True)
    return int(out.stdout.strip()) if out.stdout.strip().isdigit() else None


def set_output_volume(level: int) -> None:
    subprocess.run(["osascript", "-e", f"set volume output volume {int(level)}"], check=False)


# --- stages: tests ----------------------------------------------------------------------------

def pytest_stage(target: str, env: dict[str, str] | None = None) -> dict[str, Any]:
    log = []
    cmd = [sys.executable, "-m", "pytest", target, "-q", "-p", "no:cacheprovider", "-rs"]
    print(f"$ {' '.join(cmd)}", flush=True)
    proc = subprocess.Popen(cmd, cwd=REPO, env={**os.environ, **(env or {})}, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True, bufsize=1)
    for line in proc.stdout:
        print(line, end="", flush=True)
        log.append(line)
    rc = proc.wait()
    summary = next((ln.strip() for ln in reversed(log) if re.search(r"\d+ (passed|failed|error)", ln)), "")
    return {"ok": rc == 0, "returncode": rc, "summary": summary}


# --- stages: FFmpeg with the upstream fix -------------------------------------------------------

def build_fixed_ffmpeg() -> dict[str, Any]:
    if platform.system() != "Darwin":
        return {"ok": False, "skipped": "AVFoundation exists only on macOS"}
    src = REPO / d.CACHE_DIR_NAME / f"ffmpeg-{FFMPEG_FIX_COMMIT[:12]}"
    binary = src / "ffmpeg"
    if not binary.exists():
        src.mkdir(parents=True, exist_ok=True)
        steps = [
            ["git", "init", "-q"],
            ["git", "fetch", "-q", "--depth", "1", "https://github.com/FFmpeg/FFmpeg.git", FFMPEG_FIX_COMMIT],
            ["git", "checkout", "-q", "FETCH_HEAD"],
            ["./configure", *FFMPEG_CONFIGURE],
            ["make", f"-j{os.cpu_count() or 4}", "ffmpeg"],
        ]
        for cmd in steps:
            if run_logged(cmd, cwd=src) != 0:
                return {"ok": False, "error": f"build step failed: {' '.join(cmd)}"}
    src_text = (src / "libavdevice" / "avfoundation.m").read_text(encoding="utf-8")
    devices = subprocess.run([str(binary), "-hide_banner", "-devices"], capture_output=True, text=True).stdout
    version = subprocess.run([str(binary), "-version"], capture_output=True, text=True).stdout.splitlines()[0]
    ok = "avfoundation" in devices and "while ((_context->current_audio_frame != nil)" in src_text
    return {"ok": ok, "binary": str(binary), "commit": FFMPEG_FIX_COMMIT, "version": version}


def system_ffmpeg() -> dict[str, Any]:
    path = d.shutil.which("ffmpeg")
    if not path:
        return {"path": None}
    version = subprocess.run([path, "-version"], capture_output=True, text=True).stdout.splitlines()[0]
    return {"path": path, "version": version}


# --- stages: hardware ------------------------------------------------------------------------

class Board:
    """Firmware/route control shared by the hardware stages."""

    def __init__(self, sd: Any):
        self.sd = sd
        cache = REPO / d.CACHE_DIR_NAME
        cache.mkdir(exist_ok=True)
        self.repo = d.ensure_repo(cache)
        self.dfu = d.ensure_dfu_util(cache)
        self.host_cmd = d.locate_xvf_host(self.repo)
        self.images = {r[0]: r for r in d.firmware_paths(self.repo, deep=True)}

    def current(self) -> str | None:
        version = d.parse_version(d.wait_for_xvf(self.host_cmd, timeout_s=30))
        d.refresh_audio_devices(self.sd)
        rate = int(self.sd.query_devices(d.find_input_device(self.sd))["default_samplerate"])
        return next((label for label, meta in d.FIRMWARES.items() if tuple(meta["version"]) == version and meta["rate"] == rate), None)

    def ensure(self, label: str) -> int:
        if self.current() != label:
            _, path, rate, _ = self.images[label]
            d.flash_firmware(self.dfu, path, label, interactive=False)
            d.verify_firmware_version(self.host_cmd, label, d.FIRMWARES[label]["version"])
        return d.wait_for_audio_input(self.sd, d.FIRMWARES[label]["rate"])


def speech_stimulus(out: Path, seconds: float, np: Any, sf: Any) -> Path:
    voice, fs = sf.read(str(REPO / "assets" / "voice" / "xvf3800-test-vo.wav"), dtype="float64")
    x = voice[: int(seconds * fs)] * d.db_to_amp(d.VOICE_PLAYBACK_GAIN_DB)
    out.parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(out), d.fade(x, fs, 10, np), fs, subtype="PCM_16")
    return out


def channel_summary(wav: Path, np: Any, sf: Any) -> dict[str, Any]:
    x, fs = sf.read(str(wav), always_2d=True, dtype="float64")
    chans = [d.analyze_channel(x[:, c], int(fs), np) for c in range(x.shape[1])]
    return {"seconds": len(x) / fs, "rate": int(fs), "classification": d.classify_test({"analysis": {"channels": chans}}),
            "periodicity": [d.fold_label(ch) for ch in chans], "max_jump": [ch["top_jumps"][0]["jump"] if ch["top_jumps"] else 0 for ch in chans]}


def ab_trial(out: Path, ffmpeg: str, avf_index: str, input_idx: int, channels: int, rate: int, stim: Path,
             output_idx: int, rt: Any, min_corr: float = 0.9999) -> dict[str, Any]:
    """Record the same input through FFmpeg/AVFoundation and directly (PortAudio) at the same time."""
    np, sig, sd, sf = rt
    ff_wav, direct_wav = out / "ffmpeg.wav", out / "direct.wav"
    side = d.HostPathCapture(ffmpeg, avf_index, ff_wav)
    ACTIVE_CAPTURES.append(side)
    try:
        time.sleep(2.0)  # ffmpeg takes ~1.5 s to open the AVFoundation device
        meta = d.capture_and_play(sd, sf, np, stim, output_idx, rate, direct_wav, input_idx=input_idx, channels=channels)
        ff_meta = side.stop()
    finally:
        side.kill()
        ACTIVE_CAPTURES.remove(side)
    result = {"direct_statuses": meta["statuses"], "ffmpeg_returncode": ff_meta["returncode"], "ffmpeg_stderr": ff_meta["stderr"]}
    if not ff_wav.exists() or ff_wav.stat().st_size < 1024:
        return {**result, "error": "ffmpeg produced no recording"}
    cont = d.buffer_continuity(direct_wav, ff_wav, sf, np, sig, min_corr=min_corr)
    if cont.get("comparable"):
        cont["missing_pct"] = 100 * cont["frames_missing"] / (cont["test_frames"] + cont["frames_missing"])
    return {**result, "continuity": cont, "ffmpeg": channel_summary(ff_wav, np, sf), "direct": channel_summary(direct_wav, np, sf)}


def duration_trial(out: Path, ffmpeg: str, avf_index: str, seconds: float, stim: Path, output_idx: int, rt: Any) -> dict[str, Any]:
    """ffmpeg -t N alone, as in the original report: a capture without gaps holds exactly N s."""
    np, sig, sd, sf = rt
    ff_wav = out / "ffmpeg.wav"
    stim_audio, stim_fs = sf.read(str(stim), dtype="float32")
    side = d.HostPathCapture(ffmpeg, avf_index, ff_wav, duration_s=seconds)
    ACTIVE_CAPTURES.append(side)
    try:
        time.sleep(1.0)
        sd.play(stim_audio, samplerate=stim_fs, device=output_idx)
        meta = side.stop()
    finally:
        sd.stop()
        side.kill()
        ACTIVE_CAPTURES.remove(side)
    if not ff_wav.exists() or ff_wav.stat().st_size < 1024:
        return {"error": "ffmpeg produced no recording", "ffmpeg_stderr": meta.get("stderr", "")}
    held = sf.info(str(ff_wav)).frames / sf.info(str(ff_wav)).samplerate
    return {"requested_s": seconds, "held_s": held, "missing_pct": 100 * (1 - held / seconds), "ffmpeg": channel_summary(ff_wav, np, sf)}


def matrix_stage(out: Path, rt: Any) -> dict[str, Any]:
    np, sig, sd, sf = rt
    rc = run_logged([sys.executable, str(REPO / "diagnose.py"), "--yes", "--no-open", "--output", str(out / "matrix")])
    sessions = sorted((out / "matrix").glob("xvf3800-diagnostic-*"))
    if rc != 0 or not sessions or not (sessions[-1] / "analysis.json").exists():
        return {"ok": False, "returncode": rc, "session": str(sessions[-1]) if sessions else None}
    data = json.loads((sessions[-1] / "analysis.json").read_text())
    rows = []
    for t in data["tests"]:
        ch = t["analysis"]["channels"]
        hp = t.get("host_path_control") or {}
        rows.append({"firmware": t["firmware"], "route": t["route"], "result": d.classify_test(t),
                     "periodicity": [d.fold_label(c) for c in ch], "signature": d.upsampling_label(ch[0]) if ch else "-",
                     "host_path": d.host_path_label(hp) if hp else None,
                     "host_path_continuity": {k: v for k, v in (hp.get("continuity") or {}).items() if k != "first_splices"} or None})
    return {"ok": True, "session": str(sessions[-1]), "tests": rows}


def find_builtin_mic(sd: Any) -> int | None:
    devices = sd.query_devices()
    default_in = sd.default.device[0]
    if isinstance(default_in, int) and default_in >= 0 and not d.is_xvf_name(devices[default_in]["name"]) and devices[default_in]["max_input_channels"] > 0:
        return default_in
    return next((i for i, dev in enumerate(devices) if dev["max_input_channels"] > 0 and re.search(r"macbook|built-in", dev["name"], re.I)), None)


def hostpath_stage(out: Path, board: Board, fixed_ffmpeg: str | None, trials: int, seconds: float, rt: Any) -> dict[str, Any]:
    np, sig, sd, sf = rt
    res: dict[str, Any] = {"trials": []}
    if platform.system() != "Darwin" or not d.shutil.which("ffmpeg"):
        return {**res, "skipped": "needs macOS with ffmpeg (AVFoundation)"}
    stim = speech_stimulus(out / "hostpath" / "speech_48k.wav", seconds, np, sf)
    brew = d.shutil.which("ffmpeg")
    tools = [("homebrew", brew)] + ([("fixed", fixed_ffmpeg)] if fixed_ffmpeg else [])

    def record(name: str, kind: str, **kw: Any) -> None:
        print(f"\n[hostpath] {name}", flush=True)
        t0 = time.time()
        try:
            r = (ab_trial if kind == "ab" else duration_trial)(out / "hostpath" / name, **kw, rt=rt)
        except Exception as exc:
            r = {"error": f"{type(exc).__name__}: {exc}"}
        r.update(name=name, kind=kind, seconds=round(time.time() - t0, 1))
        res["trials"].append(r)
        cont = r.get("continuity", {})
        print(f"[hostpath] {name}: " + (f"{cont.get('splices')} splices, {cont.get('missing_pct', 0):.1f}% missing" if cont.get("comparable")
              else f"held {r['held_s']:.3f}/{r['requested_s']:.0f} s" if "held_s" in r else r.get("error") or cont.get("reason", "?")), flush=True)

    # v2.1.1 48 kHz with the issue's routing
    xvf = board.ensure("v2.1.1_48k2ch")
    d.set_route(board.host_cmd, ISSUE_ROUTE)
    res["route_48k"] = {k: v for k, v in d.snapshot_xvf(board.host_cmd).items() if k.startswith(("VERSION", "AUDIO_MGR_OP", "BLD_MSG"))}
    out_idx = d.choose_output_device(sd, None, xvf)
    res["playback_device"] = sd.query_devices(out_idx)["name"]
    for label, ffmpeg in tools:
        found = d.host_control_ffmpeg(ffmpeg)
        if not found:
            res[f"{label}_unavailable"] = True
            continue
        for i in range(trials):
            record(f"xvf48k_{label}_ab{i + 1}", "ab", ffmpeg=ffmpeg, avf_index=found[1], input_idx=d.find_input_device(sd), channels=2,
                   rate=48000, stim=stim, output_idx=out_idx)
        record(f"xvf48k_{label}_duration30", "duration", ffmpeg=ffmpeg, avf_index=found[1], seconds=30.0, stim=stim, output_idx=out_idx)

    # A non-XVF microphone through the same capture path
    mic = find_builtin_mic(sd)
    if mic is not None:
        mic_name = sd.query_devices(mic)["name"]
        res["builtin_mic"] = mic_name
        for label, ffmpeg in tools:
            found = d.host_control_ffmpeg(ffmpeg, match=lambda n: n.strip().lower() == mic_name.strip().lower())
            if not found:
                continue
            record(f"builtin_mic_{label}_ab", "ab", ffmpeg=ffmpeg, avf_index=found[1], input_idx=mic, channels=1, rate=48000,
                   stim=stim, output_idx=out_idx, min_corr=0.999)  # float source: the two paths may round differently
            record(f"builtin_mic_{label}_duration20", "duration", ffmpeg=ffmpeg, avf_index=found[1], seconds=20.0, stim=stim, output_idx=out_idx)

    # v2.1.1 native 16 kHz control
    xvf = board.ensure("v2.1.1_native16k")
    d.set_route(board.host_cmd, d.ROUTES_16K[0])
    out_idx = d.find_output_by_name(sd, res["playback_device"])
    found = d.host_control_ffmpeg(brew)
    if found:
        record("xvf16k_homebrew_ab", "ab", ffmpeg=brew, avf_index=found[1], input_idx=xvf, channels=2, rate=16000, stim=stim, output_idx=out_idx)
        record("xvf16k_homebrew_duration30", "duration", ffmpeg=brew, avf_index=found[1], seconds=30.0, stim=stim, output_idx=out_idx)
    return res


# --- summary ------------------------------------------------------------------------------

def render_summary(results: dict[str, Any]) -> str:
    L = [f"# Validation run {results['started']}", "", f"- Host: {results['env'].get('platform')}", f"- Python: {results['env'].get('python')}",
         f"- Homebrew FFmpeg: {results['env'].get('system_ffmpeg', {}).get('version')}", f"- Runner: xvfdiag {d.APP_VERSION}",
         f"- Board firmware before / after: {results.get('firmware_before') or 'n/a'} / {results.get('firmware_after') or 'n/a'}", ""]
    st = results["stages"]
    L += ["## Stages", "", "| Stage | Result |", "|---|---|"]
    for name in STAGES:
        if name in st:
            r = st[name]
            text = r.get("summary") or r.get("error") or r.get("skipped") or r.get("version") or ("ok" if r.get("ok") else "failed")
            L.append(f"| {name} | {'✅' if r.get('ok') else '❌'} {text} |")
    m = st.get("matrix", {})
    if m.get("tests"):
        L += ["", "## Diagnostic matrix (direct capture)", "", f"Session: `{m['session']}`", "",
              "| Firmware | Route | Result | ch0 periodicity | ch1 periodicity | 48 kHz signature | FFmpeg control |", "|---|---|---|---|---|---|---|"]
        for t in m["tests"]:
            p = t["periodicity"] + ["-"] * (2 - len(t["periodicity"]))
            L.append(f"| {t['firmware']} | {t['route']} | {t['result']} | {p[0]} | {p[1]} | {t['signature']} | {t['host_path'] or '-'} |")
    hp = st.get("hostpath", {})
    trials = hp.get("trials", [])
    ab = [t for t in trials if t["kind"] == "ab"]
    if ab:
        L += ["", "## Same input, captured through FFmpeg/AVFoundation and directly at the same time", "",
              "| Trial | Blocks bit-exact | Splices | Audio missing | Splice phase mod 512 | FFmpeg recording | Direct recording |", "|---|---|---|---|---|---|---|"]
        for t in ab:
            c = t.get("continuity", {})
            if c.get("comparable"):
                L.append(f"| {t['name']} | {c['bit_exact_blocks']}/{c['blocks']} | {c['splices']} | {c['missing_pct']:.1f}% | {c['splice_positions_mod_block']} | "
                         f"{t['ffmpeg']['classification']} | {t['direct']['classification']} |")
            else:
                L.append(f"| {t['name']} | – | – | – | – | {t.get('error') or c.get('reason', '?')} | |")
    dur = [t for t in trials if t["kind"] == "duration"]
    if dur:
        L += ["", "## `ffmpeg -t N` alone (the original report's method)", "", "| Trial | Requested | Audio held | Missing | Recording |", "|---|---|---|---|---|"]
        for t in dur:
            if "held_s" in t:
                L.append(f"| {t['name']} | {t['requested_s']:.0f} s | {t['held_s']:.3f} s | {t['missing_pct']:.1f}% | {t['ffmpeg']['classification']} |")
            else:
                L.append(f"| {t['name']} | – | – | – | {t.get('error')} |")
    if results.get("errors"):
        L += ["", "## Errors", ""] + [f"- {e}" for e in results["errors"]]
    return "\n".join(L) + "\n"


# --- main -------------------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--stages", default=",".join(STAGES), help=f"comma-separated subset of {','.join(STAGES)}")
    ap.add_argument("--silent", action="store_true", help=f"only {','.join(SILENT_STAGES)} (no audio, no flashing)")
    ap.add_argument("--volume", type=int, help="macOS output volume for the audible stages (restored afterwards)")
    ap.add_argument("--trials", type=int, default=3, help="concurrent FFmpeg/direct trials per FFmpeg build on 48 kHz")
    ap.add_argument("--trial-seconds", type=float, default=40.0)
    ap.add_argument("--stop", action="store_true", help="stop a running validation immediately")
    args = ap.parse_args()
    if args.stop:
        return stop_running()
    d.bootstrap_venv(Path(__file__).resolve(), root=REPO, extra_deps=("pytest>=8,<10",))

    stages = SILENT_STAGES if args.silent else [s.strip() for s in args.stages.split(",") if s.strip()]
    unknown = set(stages) - set(STAGES)
    if unknown:
        ap.error(f"unknown stages: {sorted(unknown)}")
    if not sys.stdin.isatty():
        os.setpgrp()  # own process group, so --stop reaches ffmpeg and the runner too
    signal.signal(signal.SIGTERM, on_sigterm)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    out = REPO / "validation" / stamp
    out.mkdir(parents=True)
    PIDFILE.write_text(json.dumps({"pid": os.getpid(), "pgid": os.getpgrp(), "out": str(out)}))
    log = open(out / "log.txt", "a", encoding="utf-8")
    sys.stdout = Tee(sys.__stdout__, log)
    sys.stderr = Tee(sys.__stderr__, log)

    np, sig, sd, sf = d.import_runtime()
    rt = (np, sig, sd, sf)
    results: dict[str, Any] = {"started": stamp, "stages": {}, "errors": [],
                               "env": {"platform": platform.platform(), "python": sys.version.split()[0], "system_ffmpeg": system_ffmpeg()}}
    audible = any(s in stages for s in ("matrix", "hostpath"))
    volume_before = output_volume() if audible else None
    board: Board | None = None
    try:
        for name, env, target in (("unit", None, "tests"), ("upstream", {"XVFDIAG_NETWORK_TESTS": "1"}, "tests/test_upstream.py"),
                                  ("readonly", {"XVFDIAG_HARDWARE_TESTS": "1"}, "tests/test_hardware_readonly.py")):
            if name in stages:
                print(f"\n=== {name} ===", flush=True)
                results["stages"][name] = pytest_stage(target, env)
        fixed = None
        if "ffmpeg" in stages or "hostpath" in stages:
            print("\n=== ffmpeg (build at the upstream AVFoundation fix) ===", flush=True)
            results["stages"]["ffmpeg"] = r = build_fixed_ffmpeg()
            fixed = r.get("binary") if r.get("ok") else None
        if audible:
            board = Board(sd)
            results["firmware_before"] = board.current()
            if args.volume is not None:
                set_output_volume(args.volume)
        if "matrix" in stages:
            print("\n=== matrix ===", flush=True)
            results["stages"]["matrix"] = matrix_stage(out, rt)
            d.refresh_audio_devices(sd)
        if "hostpath" in stages:
            print("\n=== hostpath ===", flush=True)
            r = hostpath_stage(out, board, fixed, args.trials, args.trial_seconds, rt)
            r["ok"] = "skipped" in r or (bool(r["trials"]) and not any("error" in t for t in r["trials"]))
            results["stages"]["hostpath"] = r
    except KeyboardInterrupt:
        results["errors"].append("interrupted")
        print("\nInterrupted.", flush=True)
    except Exception as exc:
        results["errors"].append(f"{type(exc).__name__}: {exc}")
        import traceback
        traceback.print_exc()
    finally:
        for cap in list(ACTIVE_CAPTURES):
            cap.kill()
        try:
            sd.stop()
        except Exception:
            pass
        if volume_before is not None:
            set_output_volume(volume_before)
        if board and results.get("firmware_before") and "interrupted" not in results["errors"]:
            try:
                board.ensure(results["firmware_before"])
                results["firmware_after"] = board.current()
            except Exception as exc:
                results["errors"].append(f"restoring firmware: {exc}")
        (out / "results.json").write_text(json.dumps(results, indent=2, default=str), encoding="utf-8")
        (out / "SUMMARY.md").write_text(render_summary(results), encoding="utf-8")
        PIDFILE.unlink(missing_ok=True)
        print(f"\nResults: {out / 'SUMMARY.md'}", flush=True)
    failed = [n for n, r in results["stages"].items() if not r.get("ok")]
    return 1 if failed or results["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())

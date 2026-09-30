#!/usr/bin/env python3
"""One-click cross-platform diagnostic runner for reSpeaker XVF3800 issue #39.

Flow:
  * bootstraps an isolated virtualenv and Python dependencies
  * downloads the official reSpeaker repository
  * locates/installs dfu-util where practical
  * generates a deterministic technical acoustic stimulus + the canonical English VO fixture
  * flashes v2.1.0 48k (deep mode), v2.1.1 48k, and v2.1.1 native 16k
  * configures documented XVF output checkpoints without saving configuration
  * plays stimulus through an independent output device while recording XVF input
  * analyzes discontinuities, periodicity, spectra, segment behavior, and timing
  * writes JSON/CSV plus a self-contained HTML report and a compact share bundle

The script never calls SAVE_CONFIGURATION or CLEAR_CONFIGURATION.
"""
from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import html
import io
import json
import math
import os
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import textwrap
import time
import urllib.request
import venv
import wave
import zipfile
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any, Iterable

APP_VERSION = "1.1.1"
VOICE_PLAYBACK_GAIN_DB = -6.0
# Pinned so every run executes the same xvf_host.py and reads the same firmware tree.
# Upstream changes the host script's CLI/output format; bump deliberately and re-verify.
RESPEAKER_REPO_COMMIT = "4b49bfd19977c63cf6e90dfe9e5827e74e20bf6d"
REPO_ZIP_URL = f"https://github.com/respeaker/reSpeaker_XVF3800_USB_4MIC_ARRAY/archive/{RESPEAKER_REPO_COMMIT}.zip"
DFU_BINARIES_URL = "https://dfu-util.sourceforge.net/releases/dfu-util-0.11-binaries.tar.xz"
XVF_VID_PID_DFU = "2886:001a"
CACHE_DIR_NAME = ".xvfdiag-cache"
VENV_DIR_NAME = ".xvfdiag-venv"

# SHA-256 values verified against the files used in the original investigation.
FIRMWARES = {
    "v2.1.0_48k2ch": {
        "rel": "xmos_firmwares/usb/old_firmwares/respeaker_xvf3800_usb_dfu_firmware_v2.1.0_48k2ch.bin",
        "rate": 48000,
        "sha256": "175b1cd16959d177e2c5605bac7ce31e320dc0243109cb1b102d960c92d2684b",
    },
    "v2.1.1_48k2ch": {
        "rel": "xmos_firmwares/usb/respeaker_xvf3800_usb_dfu_firmware_v2.1.1_48k2ch.bin",
        "rate": 48000,
        "sha256": "b031de3bc5e7e8437ff53ccf655ba0664fa537bd77545584f6954a2a73d5f8b4",
    },
    "v2.1.1_native16k": {
        "rel": "xmos_firmwares/usb/respeaker_xvf3800_usb_dfu_firmware_v2.1.1.bin",
        "rate": 16000,
        "sha256": "85e9a89d859f0a73c52df661190adf77a6fb655c58d8bf4162924d45849c5057",
    },
}

VOICE_SCRIPT = """Just before dawn, Mara opened the blue door and stepped into the cool morning air. A thin mist drifted over the river, while somewhere beyond the trees a blackbird called three times. On the table behind her lay a red book, a brass key, two silver coins, and a folded note.

[softly]
“Cross the old bridge after sunrise. Wait beneath the great oak tree.”

[pause]

She read the words twice and smiled. The message was strange, but not frightening; it gave her an unexpected sense of pleasure. Outside, a fresh breeze moved through the leaves, and the church bell rang six clear notes.

[whispering]
“Three bright ships drift beyond the shore.”

[pause]

She repeated it in her ordinary voice: “Three bright ships drift beyond the shore.”

At seven thirty, Mara closed the window, picked up the key, and walked toward the bridge.

“Can you hear the river from here?” she wondered.

Then she stepped onto the old wooden boards and continued toward the other side."""

# Routes correspond to Seeed/XMOS documented checkpoints.
ROUTES_48K = [
    {"name": "normal", "left": (8, 0), "right": (7, 3), "upsample": (1, 1), "description": "Default conference left + ASR right"},
    {"name": "raw", "left": (1, 0), "right": (1, 1), "upsample": (1, 1), "description": "Raw microphone 0/1 before amplification"},
    {"name": "pre_shf", "left": (11, 0), "right": (11, 1), "upsample": (1, 1), "description": "Amplified microphones before system delay"},
    {"name": "shf_input", "left": (3, 0), "right": (3, 1), "upsample": (1, 1), "description": "Amplified microphones with delay, passed to SHF"},
    {"name": "processed_direct", "left": (6, 3), "right": (7, 3), "upsample": (1, 1), "description": "Direct auto-selected processed beam + ASR"},
    {"name": "processed_no_upsample", "left": (6, 3), "right": (7, 3), "upsample": (0, 0), "description": "Diagnostic only: processed outputs with Audio Manager upsampling disabled"},
]

ROUTES_16K = [
    {"name": "normal", "left": (8, 0), "right": (7, 3), "upsample": (1, 1), "description": "Default conference left + ASR right"},
    {"name": "raw", "left": (1, 0), "right": (1, 1), "upsample": (1, 1), "description": "Raw microphone 0/1 before amplification"},
]

PY_DEPS = [
    "numpy>=2.0,<3",
    "scipy>=1.14,<2",
    "sounddevice>=0.5,<1",
    "soundfile>=0.13,<1",
    "matplotlib>=3.9,<4",
    "pyusb>=1.2,<2",
    "libusb-package>=1.0.26,<2",
]


def eprint(*args: Any) -> None:
    print(*args, file=sys.stderr, flush=True)


def run(cmd: list[str], *, cwd: Path | None = None, check: bool = True, capture: bool = True, timeout: float | None = None) -> subprocess.CompletedProcess[str]:
    kwargs: dict[str, Any] = {"cwd": str(cwd) if cwd else None, "text": True, "timeout": timeout}
    if capture:
        kwargs.update(stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    proc = subprocess.run(cmd, **kwargs)
    if check and proc.returncode != 0:
        raise RuntimeError(f"Command failed ({proc.returncode}): {' '.join(cmd)}\n{proc.stdout or ''}")
    return proc


def in_managed_venv(script_dir: Path) -> bool:
    marker = os.environ.get("XVFDIAG_VENV")
    return bool(marker and Path(marker).resolve() == (script_dir / VENV_DIR_NAME).resolve())


def bootstrap_venv(script_path: Path) -> None:
    script_dir = script_path.parent.resolve()
    if in_managed_venv(script_dir):
        return
    vdir = script_dir / VENV_DIR_NAME
    py = vdir / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if not py.exists():
        print("[bootstrap] Creating isolated Python environment...", flush=True)
        venv.EnvBuilder(with_pip=True, clear=False).create(vdir)
    stamp = vdir / ".deps"
    wanted = "\n".join(PY_DEPS) + "\n"
    if not stamp.exists() or stamp.read_text(encoding="utf-8") != wanted:
        print("[bootstrap] Installing diagnostic dependencies...", flush=True)
        run([str(py), "-m", "pip", "install", "--upgrade", "pip"], capture=False)
        run([str(py), "-m", "pip", "install", *PY_DEPS], capture=False)
        stamp.write_text(wanted, encoding="utf-8")
    env = os.environ.copy()
    env["XVFDIAG_VENV"] = str(vdir)
    argv = [str(py), str(script_path), *sys.argv[1:]]
    if os.name == "nt":
        # os.exec* on Windows starts a new process and exits this one, which detaches
        # the interactive DFU prompts from the console. Wait for the child instead.
        raise SystemExit(subprocess.call(argv, env=env))
    os.execve(str(py), argv, env)


def _install_linux_portaudio() -> None:
    if platform.system() != "Linux":
        return
    root = hasattr(os, "geteuid") and os.geteuid() == 0
    choices: list[list[str]] = []
    if shutil.which("apt-get"):
        choices.append((["apt-get", "install", "-y", "libportaudio2"] if root else ["sudo", "apt-get", "install", "-y", "libportaudio2"]))
    elif shutil.which("dnf"):
        choices.append((["dnf", "install", "-y", "portaudio"] if root else ["sudo", "dnf", "install", "-y", "portaudio"]))
    elif shutil.which("pacman"):
        choices.append((["pacman", "-S", "--noconfirm", "portaudio"] if root else ["sudo", "pacman", "-S", "--noconfirm", "portaudio"]))
    elif shutil.which("zypper"):
        choices.append((["zypper", "--non-interactive", "install", "portaudio"] if root else ["sudo", "zypper", "--non-interactive", "install", "portaudio"]))
    for cmd in choices:
        try:
            print(f"[bootstrap] Installing PortAudio: {' '.join(cmd)}", flush=True)
            run(cmd, capture=False)
            return
        except Exception as exc:
            eprint(f"[bootstrap] PortAudio install failed: {exc}")

def import_runtime() -> tuple[Any, Any, Any, Any, Any]:
    import numpy as np
    import scipy.signal as sig
    import scipy.io.wavfile as wavfile
    import soundfile as sf
    try:
        import sounddevice as sd
    except Exception as exc:
        if platform.system() == "Linux" and ("PortAudio" in str(exc) or "portaudio" in str(exc).lower()):
            _install_linux_portaudio()
            import importlib
            sd = importlib.import_module("sounddevice")
        else:
            raise
    return np, sig, wavfile, sd, sf


def download(url: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    req = urllib.request.Request(url, headers={"User-Agent": f"xvfdiag/{APP_VERSION}"})
    with urllib.request.urlopen(req, timeout=60) as r, open(tmp, "wb") as f:
        shutil.copyfileobj(r, f)
    tmp.replace(dest)


def ensure_repo(cache: Path) -> Path:
    short = RESPEAKER_REPO_COMMIT[:12]
    target = cache / f"respeaker-repo-{short}"
    marker = target / "python_control" / "xvf_host.py"
    if marker.exists():
        return target
    print(f"[setup] Downloading official reSpeaker repository at {short}...", flush=True)
    archive = cache / f"respeaker-{short}.zip"
    download(REPO_ZIP_URL, archive)
    unpack = cache / "repo-unpack"
    shutil.rmtree(unpack, ignore_errors=True)
    unpack.mkdir(parents=True)
    with zipfile.ZipFile(archive) as zf:
        # GitHub stores the archived commit SHA as the zip comment.
        commit = zf.comment.decode("ascii", "replace").strip()
        if commit and commit != RESPEAKER_REPO_COMMIT:
            raise RuntimeError(f"reSpeaker archive is commit {commit}, expected {RESPEAKER_REPO_COMMIT}")
        zf.extractall(unpack)
    roots = [p for p in unpack.iterdir() if p.is_dir()]
    if len(roots) != 1:
        raise RuntimeError("Unexpected repository archive structure")
    shutil.rmtree(target, ignore_errors=True)
    shutil.move(str(roots[0]), str(target))
    return target


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def locate_xvf_host(repo: Path) -> list[str]:
    # Seeed explicitly provides python_control/xvf_host.py as the all-platform host.
    # Using it here keeps macOS/Linux/Windows control behavior identical.
    portable = repo / "python_control/xvf_host.py"
    if not portable.exists():
        raise RuntimeError(f"Official portable xvf_host.py not found: {portable}")
    return [sys.executable, str(portable)]

def find_windows_dfu_util(tools: Path) -> Path | None:
    # The release tarball ships win32 and win64 builds; always pick the same one.
    found = sorted(tools.rglob("dfu-util.exe")) if tools.exists() else []
    return next((p for p in found if "win64" in str(p).lower()), found[0] if found else None)


def ensure_dfu_util(cache: Path) -> str:
    found = shutil.which("dfu-util")
    if found:
        return found
    sysname = platform.system()
    if sysname == "Windows":
        tools = cache / "dfu-util-win"
        exe = find_windows_dfu_util(tools)
        if not exe:
            print("[setup] Downloading official dfu-util Windows binary...", flush=True)
            archive = cache / "dfu-util-0.11-binaries.tar.xz"
            download(DFU_BINARIES_URL, archive)
            shutil.rmtree(tools, ignore_errors=True)
            tools.mkdir(parents=True)
            with tarfile.open(archive, "r:xz") as tf:
                if hasattr(tarfile, "data_filter"):
                    tf.extractall(tools, filter="data")
                else:
                    tf.extractall(tools)
            exe = find_windows_dfu_util(tools)
        if not exe:
            raise RuntimeError("Could not locate dfu-util.exe after download")
        return str(exe)

    # Prefer native package manager on POSIX because it provides the required libusb integration.
    installers: list[list[str]] = []
    if sysname == "Darwin" and shutil.which("brew"):
        installers.append(["brew", "install", "dfu-util"])
    elif sysname == "Linux":
        if shutil.which("apt-get"):
            installers.append((["apt-get", "install", "-y", "dfu-util"] if os.geteuid() == 0 else ["sudo", "apt-get", "install", "-y", "dfu-util"]))
        elif shutil.which("dnf"):
            installers.append((["dnf", "install", "-y", "dfu-util"] if os.geteuid() == 0 else ["sudo", "dnf", "install", "-y", "dfu-util"]))
        elif shutil.which("pacman"):
            installers.append((["pacman", "-S", "--noconfirm", "dfu-util"] if os.geteuid() == 0 else ["sudo", "pacman", "-S", "--noconfirm", "dfu-util"]))
        elif shutil.which("zypper"):
            installers.append((["zypper", "--non-interactive", "install", "dfu-util"] if os.geteuid() == 0 else ["sudo", "zypper", "--non-interactive", "install", "dfu-util"]))
    for cmd in installers:
        print(f"[setup] Installing dfu-util: {' '.join(cmd)}", flush=True)
        try:
            run(cmd, capture=False)
            found = shutil.which("dfu-util")
            if found:
                return found
        except Exception as exc:
            eprint(f"[setup] dfu-util installation failed: {exc}")

    if sysname == "Darwin":
        raise RuntimeError("dfu-util is missing and Homebrew was not found. Install Homebrew once, or install dfu-util manually, then rerun.")
    raise RuntimeError("dfu-util is missing and no supported package manager was found.")


def xvf_call(host_cmd: list[str], command: str, *values: Any, check: bool = True) -> str:
    # python_control/xvf_host.py reads with `COMMAND` and writes with `COMMAND --values V...`;
    # positional values are rejected by its argparse.
    cmd = [*host_cmd, command]
    if values:
        cmd += ["--values", *map(str, values)]
    proc = run(cmd, check=check, capture=True, timeout=15)
    return (proc.stdout or "").strip()


# python_control/xvf_host.py prints `VERSION: [2, 1, 1]`; compiled host_control prints `VERSION 2 1 1`.
VERSION_RE = re.compile(r"^VERSION:?\s*\[?\s*\d+[,\s]+\d+[,\s]+\d+", re.MULTILINE)


def parse_last_line_value(output: str, command: str) -> str:
    """Return the `COMMAND: [values]` (or legacy `COMMAND values`) line from xvf_host output."""
    lines = [ln.strip() for ln in output.splitlines() if ln.strip()]
    cu = command.upper()
    for ln in reversed(lines):
        head = ln.upper()
        if head.startswith(cu + ":") or head.startswith(cu + " ") or head == cu:
            return ln
    lines = [ln for ln in lines if ln != "Done!"]
    return lines[-1] if lines else ""


def snapshot_xvf(host_cmd: list[str]) -> dict[str, str]:
    commands = [
        "VERSION", "BLD_MSG", "BLD_REPO_HASH", "USB_BIT_DEPTH",
        "AUDIO_MGR_OP_PACKED", "AUDIO_MGR_OP_UPSAMPLE", "AUDIO_MGR_OP_L", "AUDIO_MGR_OP_R",
        "AUDIO_MGR_MIC_GAIN", "AUDIO_MGR_REF_GAIN", "AUDIO_MGR_SYS_DELAY",
        "AEC_ASROUTONOFF", "AEC_ASROUTGAIN", "AEC_HPFONOFF", "AEC_RT60",
        "PP_AGCDESIREDLEVEL", "PP_AGCMAXGAIN", "PP_LIMITONOFF",
        "AEC_AZIMUTH_VALUES", "AEC_SPENERGY_VALUES", "DOA_VALUE",
    ]
    snap: dict[str, str] = {}
    for cmd in commands:
        try:
            out = xvf_call(host_cmd, cmd)
            snap[cmd] = parse_last_line_value(out, cmd)
        except Exception as exc:
            snap[cmd] = f"ERROR: {exc}"
    return snap


def set_route(host_cmd: list[str], route: dict[str, Any]) -> None:
    # Runtime-only. Never persisted.
    xvf_call(host_cmd, "AUDIO_MGR_OP_PACKED", "0", "0")
    up_l, up_r = route["upsample"]
    xvf_call(host_cmd, "AUDIO_MGR_OP_UPSAMPLE", str(up_l), str(up_r))
    xvf_call(host_cmd, "AEC_ASROUTONOFF", "1")
    # Use Seeed's documented default for reproducibility with maintainer setup.
    xvf_call(host_cmd, "AEC_ASROUTGAIN", "1.0")
    lc, ls = route["left"]
    rc, rs = route["right"]
    xvf_call(host_cmd, "AUDIO_MGR_OP_L", str(lc), str(ls))
    xvf_call(host_cmd, "AUDIO_MGR_OP_R", str(rc), str(rs))
    time.sleep(0.15)


def wait_for_dfu(dfu: str, timeout_s: float = 120.0) -> str:
    deadline = time.monotonic() + timeout_s
    last = ""
    while time.monotonic() < deadline:
        try:
            last = run([dfu, "-l"], check=False, capture=True, timeout=10).stdout or ""
        except subprocess.TimeoutExpired as exc:
            last = f"dfu-util -l timed out: {exc}"
        if XVF_VID_PID_DFU.lower() in last.lower() and "alt=1" in last:
            return last
        time.sleep(1)
    raise RuntimeError(f"DFU device not found within {timeout_s:.0f}s. Last dfu-util output:\n{last}")


def flash_firmware(dfu: str, fw: Path, label: str) -> str:
    print("\n" + "=" * 78)
    print(f"FLASH: {label}")
    print("Unplug the reSpeaker. Hold MUTE. While holding MUTE, reconnect the XVF3800 USB-C port")
    print("next to the 3.5 mm jack. Release MUTE when the red LED flashes.")
    input("Press ENTER after the board is in DFU mode... ")
    listing = wait_for_dfu(dfu)
    print("[flash] DFU Upgrade alt=1 detected.")
    proc = run([dfu, "-R", "-e", "-a", "1", "-D", str(fw)], capture=True, timeout=120)
    print(proc.stdout or "")
    return listing


def wait_for_xvf(host_cmd: list[str], timeout_s: float = 60.0) -> str:
    deadline = time.monotonic() + timeout_s
    last = ""
    while time.monotonic() < deadline:
        try:
            out = xvf_call(host_cmd, "VERSION", check=False)
            last = out
            if VERSION_RE.search(out):
                return out
        except Exception as exc:
            last = str(exc)
        time.sleep(1)
    raise RuntimeError(f"XVF3800 control interface did not return after flash. Last output: {last}")


def db_to_amp(db: float) -> float:
    return 10.0 ** (db / 20.0)


def rms(x: Any, np: Any) -> float:
    x = np.asarray(x, dtype=np.float64)
    return float(np.sqrt(np.mean(x * x))) if x.size else 0.0


def normalize_rms(x: Any, target_db: float, np: Any, peak_limit_db: float = -6.0) -> Any:
    x = np.asarray(x, dtype=np.float64)
    cur = rms(x, np)
    if cur > 0:
        x = x * (db_to_amp(target_db) / cur)
    lim = db_to_amp(peak_limit_db)
    peak = float(np.max(np.abs(x))) if x.size else 0.0
    if peak > lim:
        x = x * (lim / peak)
    return x


def fade(x: Any, fs: int, ms: float, np: Any) -> Any:
    x = np.array(x, dtype=np.float64, copy=True)
    n = min(len(x) // 2, max(1, int(fs * ms / 1000.0)))
    if n > 0:
        ramp = np.linspace(0, 1, n, endpoint=True)
        x[:n] *= ramp
        x[-n:] *= ramp[::-1]
    return x


def pink_noise(n: int, np: Any, seed: int = 3800) -> Any:
    rng = np.random.default_rng(seed)
    white = rng.standard_normal(n)
    spec = np.fft.rfft(white)
    f = np.fft.rfftfreq(n)
    scale = np.ones_like(f)
    scale[1:] = 1.0 / np.sqrt(f[1:])
    y = np.fft.irfft(spec * scale, n=n)
    y -= np.mean(y)
    y /= max(np.std(y), 1e-12)
    return y


def load_voice(path: Path, target_fs: int, np: Any, sig: Any, sf: Any) -> Any:
    """Load the speech fixture without loudness normalization or dynamics processing.

    The canonical fixture is preserved byte-for-byte in the repository. For acoustic
    playback we apply one fixed gain value to create predictable headroom while
    preserving all relative level differences, including the soft and whispered
    passages. Alternate fixtures receive the same fixed gain.
    """
    data, fs = sf.read(str(path), always_2d=True, dtype="float64")
    mono = data.mean(axis=1)
    if fs != target_fs:
        g = math.gcd(int(fs), int(target_fs))
        mono = sig.resample_poly(mono, target_fs // g, fs // g)
    mono = mono * db_to_amp(VOICE_PLAYBACK_GAIN_DB)
    peak = float(np.max(np.abs(mono))) if mono.size else 0.0
    if peak > 0.95:
        mono = mono * (0.95 / peak)
    return fade(mono, target_fs, 10, np)


def voice_fixture_info(path: Path | None) -> dict[str, Any] | None:
    if path is None:
        return None
    try:
        # Keep shared evidence free of local absolute paths for the in-repo fixture.
        shown = path.resolve().relative_to(Path(__file__).resolve().parent).as_posix()
    except ValueError:
        shown = str(path)
    return {
        "path": shown,
        "filename": path.name,
        "sha256": sha256_file(path),
        "playback_gain_db": VOICE_PLAYBACK_GAIN_DB,
    }


def generate_stimulus(voice: Path | None, out_dir: Path, np: Any, sig: Any, sf: Any) -> tuple[Path, list[dict[str, Any]]]:
    fs = 48000
    segments: list[tuple[str, Any, str]] = []

    def silence(sec: float) -> Any:
        return np.zeros(int(round(sec * fs)), dtype=np.float64)

    def tone(freq: float, sec: float, level_db: float = -20.0) -> Any:
        t = np.arange(int(round(sec * fs))) / fs
        return fade(np.sin(2 * np.pi * freq * t) * db_to_amp(level_db), fs, 8, np)

    segments.append(("silence_start", silence(2.0), "Ambient/no-signal baseline"))
    t = np.arange(int(0.35 * fs)) / fs
    chirp = sig.chirp(t, f0=300, f1=7500, t1=t[-1], method="logarithmic") * db_to_amp(-14)
    segments.append(("sync_chirp", fade(chirp, fs, 5, np), "Broadband acoustic alignment marker"))
    segments.append(("silence_after_sync", silence(0.75), "Marker separation"))

    pn = normalize_rms(pink_noise(int(3 * fs), np), -24, np, peak_limit_db=-10)
    segments.append(("pink_noise", fade(pn, fs, 15, np), "Broadband 1/f spectral test"))
    wn = normalize_rms(np.random.default_rng(3801).standard_normal(int(3 * fs)), -24, np, peak_limit_db=-10)
    segments.append(("white_noise", fade(wn, fs, 15, np), "Broadband flat-noise test"))

    for f in [250, 500, 1000, 2000, 4000, 6000]:
        segments.append((f"tone_{f}Hz", tone(float(f), 0.8, -20), f"Steady {f} Hz sine"))
        segments.append((f"gap_after_{f}Hz", silence(0.12), "Short separation"))

    t = np.arange(int(3.0 * fs)) / fs
    freqs = [250, 500, 1000, 2000, 4000, 6000]
    mt = sum(np.sin(2 * np.pi * f * t) for f in freqs) / len(freqs)
    mt = normalize_rms(mt, -22, np, peak_limit_db=-9)
    segments.append(("multitone", fade(mt, fs, 15, np), "Simultaneous six-tone linearity/intermodulation probe"))

    t = np.arange(int(6.0 * fs)) / fs
    sw = sig.chirp(t, f0=80, f1=7500, t1=t[-1], method="logarithmic")
    sw = normalize_rms(sw, -20, np, peak_limit_db=-8)
    segments.append(("log_sweep_80_7500", fade(sw, fs, 20, np), "Logarithmic frequency sweep"))

    # Short band-limited transient train. Easier to localize than raw one-sample impulses acoustically.
    transient = np.zeros(int(3.0 * fs), dtype=np.float64)
    pulse_len = int(0.006 * fs)
    pulse_t = np.arange(pulse_len) / fs
    pulse = np.sin(2 * np.pi * 1800 * pulse_t) * np.hanning(pulse_len) * db_to_amp(-10)
    for k in range(8):
        s = int((0.25 + k * 0.32) * fs)
        transient[s:s + pulse_len] += pulse
    segments.append(("transient_train", transient, "Eight short band-limited acoustic transients"))

    segments.append(("silence_before_voice", silence(0.75), "Voice separation"))
    if voice:
        vo = load_voice(voice, fs, np, sig, sf)
        segments.append(("english_voice", vo, "General English natural-speech test"))
    else:
        segments.append(("english_voice_placeholder", silence(3.0), "VO omitted; provide --voice for complete run"))
    segments.append(("silence_end", silence(2.0), "Post-stimulus baseline"))

    manifest: list[dict[str, Any]] = []
    parts = []
    cursor = 0
    for name, audio, desc in segments:
        audio = np.asarray(audio, dtype=np.float64)
        start = cursor / fs
        cursor += len(audio)
        end = cursor / fs
        parts.append(audio)
        manifest.append({"name": name, "description": desc, "start_s": start, "end_s": end, "duration_s": end - start})
    full = np.concatenate(parts)
    full = np.clip(full, -0.95, 0.95).astype(np.float32)
    out_dir.mkdir(parents=True, exist_ok=True)
    wav_path = out_dir / "diagnostic_stimulus_48k.wav"
    sf.write(str(wav_path), full, fs, subtype="PCM_16")
    (out_dir / "stimulus_manifest.json").write_text(json.dumps({"sample_rate": fs, "segments": manifest}, indent=2), encoding="utf-8")
    return wav_path, manifest


def list_audio_devices(sd: Any) -> list[dict[str, Any]]:
    devices = sd.query_devices()
    hostapis = sd.query_hostapis()
    rows = []
    for i, d in enumerate(devices):
        rows.append({
            "index": i,
            "name": d["name"],
            "hostapi": hostapis[d["hostapi"]]["name"],
            "max_input_channels": d["max_input_channels"],
            "max_output_channels": d["max_output_channels"],
            "default_samplerate": d["default_samplerate"],
        })
    return rows


def refresh_audio_devices(sd: Any) -> None:
    # PortAudio snapshots the device list when it initializes. The XVF re-enumerates on
    # USB after every flash (with a different rate on the 16 kHz image), so rescan.
    try:
        sd._terminate()
        sd._initialize()
    except Exception as exc:
        eprint(f"[audio] Could not refresh PortAudio device list: {exc}")


def wait_for_audio_input(sd: Any, rate: int, timeout_s: float = 20.0) -> int:
    # The audio interface can appear a little after the control interface answers.
    deadline = time.monotonic() + timeout_s
    while True:
        refresh_audio_devices(sd)
        try:
            idx = find_input_device(sd)
            sd.check_input_settings(device=idx, channels=2, dtype="float32", samplerate=rate)
            return idx
        except Exception as exc:
            if time.monotonic() >= deadline:
                raise RuntimeError(f"XVF3800 audio input did not become available at {rate} Hz: {exc}") from exc
        time.sleep(1)


def find_output_by_name(sd: Any, name: str) -> int:
    for i, d in enumerate(sd.query_devices()):
        if d["name"] == name and d["max_output_channels"] > 0:
            return i
    raise RuntimeError(f"Playback device {name!r} is no longer available")


def find_input_device(sd: Any) -> int:
    matches = []
    for i, d in enumerate(sd.query_devices()):
        name = d["name"].lower()
        if d["max_input_channels"] >= 2 and ("respeaker" in name or "xvf3800" in name):
            matches.append(i)
    if not matches:
        raise RuntimeError("Could not find a 2-channel reSpeaker/XVF3800 input device.")
    return matches[0]


def choose_output_device(sd: Any, selector: str | None, input_idx: int) -> int:
    devices = sd.query_devices()
    if selector:
        if selector.isdigit():
            idx = int(selector)
            if idx < len(devices) and devices[idx]["max_output_channels"] > 0:
                return idx
        hits = [i for i, d in enumerate(devices) if d["max_output_channels"] > 0 and selector.lower() in d["name"].lower()]
        if len(hits) == 1:
            return hits[0]
        if len(hits) > 1:
            raise RuntimeError(f"Output selector {selector!r} is ambiguous: {hits}")
        raise RuntimeError(f"No output device matched {selector!r}")
    default_out = sd.default.device[1]
    if isinstance(default_out, int) and default_out >= 0 and default_out != input_idx:
        name = devices[default_out]["name"].lower()
        if "respeaker" not in name and "xvf3800" not in name:
            return int(default_out)
    # Prefer built-in speakers as an independent acoustic source.
    for i, d in enumerate(devices):
        n = d["name"].lower()
        if d["max_output_channels"] > 0 and ("speaker" in n or "built-in" in n) and "respeaker" not in n:
            return i
    raise RuntimeError("Could not choose an independent output device. Pass --output-device NAME_OR_INDEX.")


def capture_and_play(sd: Any, sf: Any, np: Any, stimulus_path: Path, output_idx: int, expected_input_rate: int, out_wav: Path) -> dict[str, Any]:
    input_idx = find_input_device(sd)
    in_dev = sd.query_devices(input_idx)
    if in_dev["max_input_channels"] < 2:
        raise RuntimeError("reSpeaker input does not expose two channels")
    # Fail instead of silently resampling at the host.
    sd.check_input_settings(device=input_idx, channels=2, dtype="float32", samplerate=expected_input_rate)

    stim, stim_fs = sf.read(str(stimulus_path), dtype="float32", always_2d=False)
    if getattr(stim, "ndim", 1) > 1:
        stim = stim.mean(axis=1)
    # Playback stays at reference 48 kHz on all firmware variants.
    sd.check_output_settings(device=output_idx, channels=1, dtype="float32", samplerate=stim_fs)

    blocks: list[Any] = []
    statuses: list[str] = []
    start_wall = time.monotonic()

    def callback(indata: Any, frames: int, time_info: Any, status: Any) -> None:
        if status:
            statuses.append(str(status))
        blocks.append(indata.copy())

    stream = sd.InputStream(device=input_idx, channels=2, samplerate=expected_input_rate, dtype="float32", blocksize=0, callback=callback)
    stream.start()
    time.sleep(0.75)
    play_start = time.monotonic()
    sd.play(stim, samplerate=stim_fs, device=output_idx, blocking=True)
    play_end = time.monotonic()
    time.sleep(0.75)
    stream.stop()
    stream.close()
    end_wall = time.monotonic()

    if not blocks:
        raise RuntimeError("No audio frames captured")
    audio = np.concatenate(blocks, axis=0)
    out_wav.parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(out_wav), audio, expected_input_rate, subtype="PCM_16")
    return {
        "input_device_index": input_idx,
        "input_device_name": in_dev["name"],
        "output_device_index": output_idx,
        "output_device_name": sd.query_devices(output_idx)["name"],
        "input_rate": expected_input_rate,
        "output_rate": int(stim_fs),
        "captured_frames": int(audio.shape[0]),
        "capture_wall_s": end_wall - start_wall,
        "play_wall_s": play_end - play_start,
        "statuses": statuses,
    }



def capture_only(sd: Any, sf: Any, np: Any, expected_input_rate: int, out_wav: Path, duration_s: float = 20.0) -> dict[str, Any]:
    input_idx = find_input_device(sd)
    in_dev = sd.query_devices(input_idx)
    sd.check_input_settings(device=input_idx, channels=2, dtype="float32", samplerate=expected_input_rate)
    blocks: list[Any] = []
    statuses: list[str] = []
    start_wall = time.monotonic()
    def callback(indata: Any, frames: int, time_info: Any, status: Any) -> None:
        if status:
            statuses.append(str(status))
        blocks.append(indata.copy())
    stream = sd.InputStream(device=input_idx, channels=2, samplerate=expected_input_rate, dtype="float32", blocksize=0, callback=callback)
    stream.start()
    time.sleep(duration_s)
    stream.stop(); stream.close()
    end_wall = time.monotonic()
    if not blocks:
        raise RuntimeError("No audio frames captured")
    audio = np.concatenate(blocks, axis=0)
    out_wav.parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(out_wav), audio, expected_input_rate, subtype="PCM_16")
    return {"input_device_index": input_idx, "input_device_name": in_dev["name"], "input_rate": expected_input_rate,
            "captured_frames": int(audio.shape[0]), "capture_wall_s": end_wall-start_wall, "statuses": statuses,
            "playback": False, "requested_duration_s": duration_s}

def dbfs(value: float) -> float:
    return 20 * math.log10(max(value, 1e-12))


def candidate_period_stats(event_idx: Any, periods: Iterable[int], np: Any) -> list[dict[str, Any]]:
    rows = []
    n = len(event_idx)
    for p in periods:
        if n == 0:
            rows.append({"period": p, "events": 0, "dominant_phase": None, "dominant_count": 0, "concentration": 0.0, "enrichment": 0.0})
            continue
        phases = event_idx % p
        counts = np.bincount(phases, minlength=p)
        phase = int(np.argmax(counts))
        dom = int(counts[phase])
        concentration = dom / n
        rows.append({"period": p, "events": int(n), "dominant_phase": phase, "dominant_count": dom, "concentration": concentration, "enrichment": concentration * p})
    return rows


def align_reference(recorded: Any, fs: int, stimulus: Any, stim_fs: int, manifest: list[dict[str, Any]], np: Any, sig: Any) -> tuple[float, float]:
    sync = next((s for s in manifest if s["name"] == "sync_chirp"), None)
    if not sync:
        return 0.75, 0.0
    a = int(sync["start_s"] * stim_fs)
    b = int(sync["end_s"] * stim_fs)
    ref = stimulus[a:b]
    if stim_fs != fs:
        g = math.gcd(int(stim_fs), int(fs))
        ref = sig.resample_poly(ref, fs // g, stim_fs // g)
    rec = recorded[: min(len(recorded), int(fs * 8.0))]
    if len(rec) <= len(ref):
        return 0.75, 0.0
    ref = ref - np.mean(ref)
    rec = rec - np.mean(rec)
    corr = sig.fftconvolve(rec, ref[::-1], mode="valid")
    idx = int(np.argmax(np.abs(corr)))
    denom = math.sqrt(float(np.sum(ref * ref)) * float(np.sum(rec[idx:idx + len(ref)] ** 2)) + 1e-24)
    score = float(abs(corr[idx]) / denom) if denom else 0.0
    reference_sync_start_at_rec_s = idx / fs
    playback_start_s = reference_sync_start_at_rec_s - float(sync["start_s"])
    return playback_start_s, score


def analyze_channel(x: Any, fs: int, np: Any) -> dict[str, Any]:
    x = np.asarray(x, dtype=np.float64)
    peak = float(np.max(np.abs(x))) if x.size else 0.0
    rr = rms(x, np)
    dif = np.abs(np.diff(x)) if x.size > 1 else np.array([], dtype=np.float64)
    thresholds = [0.05, 0.10, 0.20, 0.30]
    jumps = {f"gt_{t:.2f}": int(np.sum(dif > t)) for t in thresholds}
    idx02 = np.flatnonzero(dif > 0.20)
    idx01 = np.flatnonzero(dif > 0.10)
    top_k = np.argsort(dif)[-12:][::-1] if dif.size else np.array([], dtype=int)
    top = [{
        "sample": int(i + 1), "time_s": float((i + 1) / fs), "jump": float(dif[i]),
        "mod_256": int((i + 1) % 256), "mod_512": int((i + 1) % 512), "mod_1024": int((i + 1) % 1024),
    } for i in top_k]

    win = np.hanning(len(x)) if len(x) > 1 else np.ones_like(x)
    spec = np.abs(np.fft.rfft(x * win)) ** 2 if len(x) else np.array([0.0])
    freqs = np.fft.rfftfreq(len(x), 1 / fs) if len(x) else np.array([0.0])
    total = float(np.sum(spec)) + 1e-24
    bands = {}
    for lo, hi in [(0, 300), (300, 3400), (3400, 8000), (8000, 16000), (16000, 24000)]:
        if lo >= fs / 2:
            continue
        m = (freqs >= lo) & (freqs < min(hi, fs / 2 + 1e-9))
        bands[f"{lo}_{hi}"] = float(np.sum(spec[m]) / total)

    mod3 = []
    for phase in range(3):
        mod3.append(rms(x[phase::3], np))

    return {
        "peak": peak, "peak_dbfs": dbfs(peak), "rms": rr, "rms_dbfs": dbfs(rr),
        "dc": float(np.mean(x)) if x.size else 0.0,
        "clipped_samples": int(np.sum(np.abs(x) >= 0.999969)),
        "jumps": jumps,
        "periodicity_gt_0_10": candidate_period_stats(idx01 + 1, [64, 128, 256, 512, 1024, 2048], np),
        "periodicity_gt_0_20": candidate_period_stats(idx02 + 1, [64, 128, 256, 512, 1024, 2048], np),
        "top_jumps": top,
        "bands": bands,
        "mod3_rms": mod3,
    }


def analyze_recording(wav_path: Path, stimulus_path: Path, manifest: list[dict[str, Any]], sf: Any, np: Any, sig: Any) -> dict[str, Any]:
    data, fs = sf.read(str(wav_path), dtype="float64", always_2d=True)
    stim, stim_fs = sf.read(str(stimulus_path), dtype="float64", always_2d=False)
    if getattr(stim, "ndim", 1) > 1:
        stim = stim.mean(axis=1)
    # Use stronger of two channels for sync alignment when a played stimulus exists.
    alignments = []
    if manifest:
        for ch in range(min(2, data.shape[1])):
            off, score = align_reference(data[:, ch], int(fs), stim, int(stim_fs), manifest, np, sig)
            alignments.append((off, score))
    playback_start_s, align_score = max(alignments, key=lambda t: t[1]) if alignments else (0.0, 0.0)

    channels = [analyze_channel(data[:, ch], int(fs), np) for ch in range(data.shape[1])]
    seg_rows = []
    for segm in manifest:
        start = playback_start_s + segm["start_s"]
        end = playback_start_s + segm["end_s"]
        a = max(0, int(round(start * fs)))
        b = min(len(data), int(round(end * fs)))
        row = {"name": segm["name"], "start_s": start, "end_s": end, "channels": []}
        for ch in range(data.shape[1]):
            xx = data[a:b, ch]
            if len(xx):
                d = np.abs(np.diff(xx))
                row["channels"].append({
                    "rms_dbfs": dbfs(rms(xx, np)),
                    "peak_dbfs": dbfs(float(np.max(np.abs(xx)))),
                    "jumps_gt_0_10": int(np.sum(d > 0.10)),
                    "jumps_gt_0_20": int(np.sum(d > 0.20)),
                })
            else:
                row["channels"].append({})
        seg_rows.append(row)
    return {
        "sample_rate": int(fs), "frames": int(len(data)), "duration_s": float(len(data) / fs),
        "channels": channels, "playback_start_s": playback_start_s, "alignment_score": align_score,
        "segments": seg_rows,
    }


def fig_data_uri(fig: Any) -> str:
    bio = io.BytesIO()
    fig.savefig(bio, format="png", dpi=130, bbox_inches="tight")
    import matplotlib.pyplot as plt
    plt.close(fig)
    return "data:image/png;base64," + base64.b64encode(bio.getvalue()).decode("ascii")


def create_plots(wav_path: Path, analysis: dict[str, Any], sf: Any, np: Any) -> dict[str, str]:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from scipy import signal

    data, fs = sf.read(str(wav_path), dtype="float64", always_2d=True)
    plots: dict[str, str] = {}
    t = np.arange(len(data)) / fs

    fig, ax = plt.subplots(figsize=(11, 3.0))
    step = max(1, len(data) // 120000)
    for ch in range(data.shape[1]):
        ax.plot(t[::step], data[::step, ch], linewidth=0.55, label=f"ch{ch}")
    ax.set(title="Waveform", xlabel="Time (s)", ylabel="FS")
    ax.legend(loc="upper right")
    ax.grid(alpha=.2)
    plots["waveform"] = fig_data_uri(fig)

    fig, ax = plt.subplots(figsize=(11, 3.0))
    for ch in range(data.shape[1]):
        d = np.abs(np.diff(data[:, ch]))
        step = max(1, len(d) // 120000)
        ax.plot(np.arange(len(d))[::step] / fs, d[::step], linewidth=.55, label=f"ch{ch}")
    ax.axhline(.1, linestyle="--", linewidth=.8)
    ax.axhline(.2, linestyle="--", linewidth=.8)
    ax.set(title="Adjacent-sample discontinuity magnitude", xlabel="Time (s)", ylabel="|x[n]-x[n-1]|")
    ax.set_ylim(bottom=0)
    ax.legend(loc="upper right")
    ax.grid(alpha=.2)
    plots["jumps"] = fig_data_uri(fig)

    fig, ax = plt.subplots(figsize=(10, 3.2))
    width = 0.42
    phases = np.arange(512)
    for ch in range(data.shape[1]):
        d = np.abs(np.diff(data[:, ch]))
        idx = np.flatnonzero(d > .1) + 1
        counts = np.bincount(idx % 512, minlength=512)
        ax.plot(phases, counts, linewidth=.9, label=f"ch{ch}")
    ax.set(title="Phase of >0.10 FS discontinuities modulo 512", xlabel="sample index mod 512", ylabel="events")
    ax.legend(loc="upper right")
    ax.grid(alpha=.2)
    plots["phase512"] = fig_data_uri(fig)

    fig, ax = plt.subplots(figsize=(10, 3.3))
    for ch in range(data.shape[1]):
        f, pxx = signal.welch(data[:, ch], fs=fs, nperseg=min(8192, len(data)))
        ax.semilogy(f, pxx + 1e-20, linewidth=.8, label=f"ch{ch}")
    ax.set(title="Welch power spectrum", xlabel="Frequency (Hz)", ylabel="PSD")
    ax.set_xlim(0, fs / 2)
    ax.legend(loc="upper right")
    ax.grid(alpha=.2)
    plots["spectrum"] = fig_data_uri(fig)

    fig, ax = plt.subplots(figsize=(10, 3.5))
    f, tt, sxx = signal.spectrogram(data[:, min(1, data.shape[1]-1)], fs=fs, nperseg=min(1024, len(data)), noverlap=min(768, max(0, len(data)//2)))
    db = 10 * np.log10(sxx + 1e-20)
    ax.pcolormesh(tt, f, db, shading="auto")
    ax.set(title=f"Spectrogram (ch{min(1, data.shape[1]-1)})", xlabel="Time (s)", ylabel="Hz", ylim=(0, fs/2))
    plots["spectrogram"] = fig_data_uri(fig)

    # Zoom around strongest jump on each channel.
    fig, axes = plt.subplots(data.shape[1], 1, figsize=(10, 2.5 * data.shape[1]), squeeze=False)
    for ch in range(data.shape[1]):
        d = np.abs(np.diff(data[:, ch]))
        idx = int(np.argmax(d)) + 1 if len(d) else 0
        a = max(0, idx - int(.006 * fs)); b = min(len(data), idx + int(.006 * fs))
        axes[ch, 0].plot((np.arange(a, b) - idx) / fs * 1000, data[a:b, ch], marker=".", markersize=2, linewidth=.7)
        axes[ch, 0].axvline(0, linestyle="--", linewidth=.8)
        axes[ch, 0].set(title=f"ch{ch} strongest discontinuity: sample {idx}, mod512={idx % 512}", xlabel="Time around event (ms)", ylabel="FS")
        axes[ch, 0].grid(alpha=.2)
    fig.tight_layout()
    plots["click_zoom"] = fig_data_uri(fig)
    return plots


def classify_test(test: dict[str, Any]) -> str:
    a = test["analysis"]
    if not a["channels"]:
        return "unknown"
    # A test is flagged when any channel shows the signature.
    periodic = False
    for ch in a["channels"]:
        rows = {r["period"]: r for r in ch["periodicity_gt_0_20"]}
        r512 = rows.get(512, {})
        # Broadband/noise stimulus naturally creates large adjacent-sample changes.
        # A defect signature is therefore based on implausible phase concentration,
        # not the raw number of threshold crossings.
        if r512.get("dominant_count", 0) >= 4 and r512.get("enrichment", 0) >= 20.0:
            periodic = True
    if periodic:
        return "periodic discontinuity signature"
    return "no strong periodic-click signature"


def esc(v: Any) -> str:
    return html.escape(str(v))


def generate_html_report(session: dict[str, Any], out_path: Path) -> None:
    tests = session["tests"]
    rows = []
    for t in tests:
        a = t["analysis"]
        chs = a["channels"]
        ch0 = chs[0] if len(chs) > 0 else {}
        ch1 = chs[1] if len(chs) > 1 else {}
        def per(ch: dict[str, Any]) -> str:
            if not ch: return "-"
            r512 = next((r for r in ch["periodicity_gt_0_20"] if r["period"] == 512), {})
            return f"{ch['jumps']['gt_0.20']} jumps; phase {r512.get('dominant_phase')}; {100*r512.get('concentration',0):.0f}% at dominant phase"
        rows.append(f"<tr><td>{esc(t['firmware'])}</td><td>{esc(t['route'])}</td><td>{a['sample_rate']}</td><td>{esc(classify_test(t))}</td><td>{esc(per(ch0))}</td><td>{esc(per(ch1))}</td><td>{a['alignment_score']:.3f}</td></tr>")

    sections = []
    for i, t in enumerate(tests):
        a = t["analysis"]
        segment_rows = "".join(
            f"<tr><td>{esc(s['name'])}</td>" + "".join(
                f"<td>{c.get('rms_dbfs', float('nan')):.1f}</td><td>{c.get('jumps_gt_0_20','-')}</td>" if c else "<td>-</td><td>-</td>" for c in s["channels"]
            ) + "</tr>" for s in a["segments"]
        )
        chcards = []
        for ci, ch in enumerate(a["channels"]):
            p512 = next((r for r in ch["periodicity_gt_0_20"] if r["period"] == 512), {})
            top = ch["top_jumps"][:6]
            top_html = "".join(f"<tr><td>{x['sample']}</td><td>{x['time_s']:.6f}</td><td>{x['jump']:.4f}</td><td>{x['mod_256']}</td><td>{x['mod_512']}</td><td>{x['mod_1024']}</td></tr>" for x in top)
            chcards.append(f"""
            <div class='card'><h4>Channel {ci}</h4>
            <p>Peak <b>{ch['peak_dbfs']:.2f} dBFS</b> · RMS {ch['rms_dbfs']:.2f} dBFS · clipped {ch['clipped_samples']} · jumps &gt;0.20 FS <b>{ch['jumps']['gt_0.20']}</b></p>
            <p>512-phase evidence: dominant phase <b>{p512.get('dominant_phase')}</b>, {p512.get('dominant_count',0)}/{p512.get('events',0)} events ({100*p512.get('concentration',0):.1f}%), enrichment {p512.get('enrichment',0):.1f}x.</p>
            <p>Modulo-3 RMS: {', '.join(f'{v:.5f}' for v in ch['mod3_rms'])}</p>
            <table><thead><tr><th>sample</th><th>time s</th><th>jump</th><th>mod256</th><th>mod512</th><th>mod1024</th></tr></thead><tbody>{top_html}</tbody></table>
            </div>""")
        plots = t["plots"]
        sections.append(f"""
        <section id='test-{i}'><h2>{esc(t['firmware'])} / {esc(t['route'])}</h2>
        <p class='muted'>{esc(t['route_description'])}</p>
        <p><b>Classification:</b> {esc(classify_test(t))} · capture {a['sample_rate']} Hz · {a['duration_s']:.3f}s · acoustic alignment score {a['alignment_score']:.3f}</p>
        <details><summary>XVF configuration before capture</summary><pre>{esc(json.dumps(t['xvf_before'], indent=2))}</pre></details>
        <div class='grid2'>{''.join(chcards)}</div>
        <img src='{plots['waveform']}'><img src='{plots['jumps']}'><img src='{plots['phase512']}'><img src='{plots['spectrum']}'><img src='{plots['spectrogram']}'><img src='{plots['click_zoom']}'>
        <h3>Per-stimulus segment</h3><table><thead><tr><th>segment</th>{''.join(f'<th>ch{c} RMS dBFS</th><th>ch{c} jumps &gt;.20</th>' for c in range(len(a['channels'])))}</tr></thead><tbody>{segment_rows}</tbody></table>
        </section>""")

    html_doc = f"""<!doctype html><html><head><meta charset='utf-8'><title>XVF3800 Diagnostic Report</title>
<style>
body{{font-family:Inter,system-ui,-apple-system,Segoe UI,sans-serif;max-width:1280px;margin:32px auto;padding:0 24px;color:#17202a;background:#f6f8fa}}h1,h2,h3{{color:#111}}section,.card{{background:white;border:1px solid #d8dee4;border-radius:10px;padding:18px;margin:18px 0}}table{{border-collapse:collapse;width:100%;font-size:13px}}th,td{{border-bottom:1px solid #e5e7eb;padding:7px;text-align:left;vertical-align:top}}th{{background:#f3f4f6}}img{{max-width:100%;display:block;margin:14px auto;border:1px solid #e5e7eb;border-radius:6px}}pre{{overflow:auto;background:#0d1117;color:#c9d1d9;padding:12px;border-radius:6px}}.muted{{color:#57606a}}.grid2{{display:grid;grid-template-columns:repeat(auto-fit,minmax(420px,1fr));gap:12px}}.badge{{display:inline-block;padding:4px 8px;border-radius:999px;background:#ddf4ff}}code{{background:#eff1f3;padding:2px 4px;border-radius:4px}}
</style></head><body>
<h1>reSpeaker XVF3800 automated diagnostic report</h1>
<p><span class='badge'>xvfdiag {APP_VERSION}</span> Generated {esc(session['generated_at'])}</p>
<section><h2>Purpose</h2><p>Controlled cross-platform reproduction package for reSpeaker issue #39. The run compares documented signal-path checkpoints, affected 48 kHz firmware, and native-16-kHz control firmware using the same deterministic acoustic stimulus. Configuration changes are runtime-only; the runner never persists them.</p></section>
<section><h2>Environment</h2><pre>{esc(json.dumps(session['environment'], indent=2))}</pre></section>
<section><h2>Stimulus</h2><p>Technical stimulus SHA-256: <code>{esc(session['stimulus_sha256'])}</code></p><p>Natural-speech fixture:</p><pre>{esc(json.dumps(session.get('voice_fixture'), indent=2))}</pre><pre>{esc(json.dumps(session['stimulus_manifest'], indent=2))}</pre></section>
<section><h2>Summary matrix</h2><table><thead><tr><th>Firmware</th><th>Route</th><th>Hz</th><th>Result</th><th>ch0 &gt;.20</th><th>ch1 &gt;.20</th><th>align</th></tr></thead><tbody>{''.join(rows)}</tbody></table></section>
{''.join(sections)}
<section><h2>Interpretation guardrails</h2><ul><li>A concentration at sample index modulo 512 is an observed capture periodicity, not proof of the firmware's internal block size.</li><li>Raw/pre-SHF controls help localize the point at which an artifact appears.</li><li>The native-16-kHz control distinguishes a 48-kHz-path-specific failure from a general microphone/capture failure.</li><li>Audio is played by an independent host output; the XVF far-end/AEC playback path is intentionally not used.</li></ul></section>
</body></html>"""
    out_path.write_text(html_doc, encoding="utf-8")


def write_summary_csv(session: dict[str, Any], path: Path) -> None:
    fields = ["firmware", "route", "sample_rate", "classification", "ch0_jumps_gt_020", "ch1_jumps_gt_020", "ch0_512_phase", "ch1_512_phase", "ch0_512_concentration", "ch1_512_concentration", "alignment_score"]
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for t in session["tests"]:
            a=t["analysis"]; ch=a["channels"]
            def p(ci: int) -> dict[str, Any]:
                if ci >= len(ch): return {}
                return next((r for r in ch[ci]["periodicity_gt_0_20"] if r["period"]==512), {})
            w.writerow({
                "firmware":t["firmware"], "route":t["route"], "sample_rate":a["sample_rate"], "classification":classify_test(t),
                "ch0_jumps_gt_020": ch[0]["jumps"]["gt_0.20"] if len(ch)>0 else "", "ch1_jumps_gt_020": ch[1]["jumps"]["gt_0.20"] if len(ch)>1 else "",
                "ch0_512_phase":p(0).get("dominant_phase",""), "ch1_512_phase":p(1).get("dominant_phase",""),
                "ch0_512_concentration":p(0).get("concentration",""), "ch1_512_concentration":p(1).get("concentration",""), "alignment_score":a["alignment_score"],
            })


def make_share_bundle(session_dir: Path, session: dict[str, Any], sf: Any) -> Path:
    share = session_dir / "share"
    shutil.rmtree(share, ignore_errors=True)
    share.mkdir()
    for fn in ["report.html", "analysis.json", "summary.csv", "session.json", "stimulus/diagnostic_stimulus_48k.wav", "stimulus/stimulus_manifest.json"]:
        src = session_dir / fn
        if src.exists():
            dest = share / Path(fn).name
            if fn.endswith("diagnostic_stimulus_48k.wav"):
                # Technical reference can be reconstructed; don't bloat bundle with it.
                continue
            shutil.copy2(src, dest)
    # Convert the most useful recordings to FLAC for GitHub-sized attachments.
    priority = []
    for t in session["tests"]:
        if (t["firmware"], t["route"]) in {
            ("v2.1.1_48k2ch", "normal_ambient"), ("v2.1.1_48k2ch", "normal"), ("v2.1.1_48k2ch", "raw"), ("v2.1.1_48k2ch", "pre_shf"),
            ("v2.1.1_native16k", "normal_ambient"), ("v2.1.1_native16k", "normal")
        }:
            priority.append(t)
    for t in priority:
        src = Path(t["recording_path"])
        if not src.exists():
            continue
        data, fs = sf.read(str(src), always_2d=True, dtype="float32")
        name = f"{t['firmware']}__{t['route']}.flac"
        sf.write(str(share / name), data, fs, format="FLAC", subtype="PCM_16")
    comment = generate_github_comment(session)
    (share / "github_comment.md").write_text(comment, encoding="utf-8")
    # tools/import_run.py publishes the draft from the session root.
    (session_dir / "github_comment.md").write_text(comment, encoding="utf-8")
    # Include exact runner and VO text.
    shutil.copy2(Path(__file__), share / "diagnose.py")
    (share / "VOICE_SCRIPT.txt").write_text(VOICE_SCRIPT + "\n", encoding="utf-8")
    archive = session_dir / "github-share-bundle.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for p in share.rglob("*"):
            if p.is_file():
                zf.write(p, p.relative_to(share))
    return archive


def generate_github_comment(session: dict[str, Any]) -> str:
    lines = [
        "Thanks for trying to reproduce this. I prepared a deterministic cross-platform reproduction runner and attached a compact evidence bundle.",
        "",
        "The runner flashes the official firmware images, sets the documented output routes without saving configuration, plays the same acoustic stimulus through an independent speaker, records the XVF USB input, and generates the attached HTML/JSON analysis.",
        "",
        "### Results from this run",
        "",
        "| Firmware | Route | Result | ch0 jumps >0.20 | ch1 jumps >0.20 |",
        "|---|---|---|---:|---:|",
    ]
    for t in session["tests"]:
        a=t["analysis"]; ch=a["channels"]
        lines.append(f"| `{t['firmware']}` | `{t['route']}` | {classify_test(t)} | {ch[0]['jumps']['gt_0.20'] if len(ch)>0 else '-'} | {ch[1]['jumps']['gt_0.20'] if len(ch)>1 else '-'} |")
    lines += [
        "",
        "The bundle contains the self-contained HTML report, exact runner, JSON/CSV data, and FLAC recordings for the key affected/control routes.",
        "",
        "As in the original report, any modulo-512 concentration is reported only as an observed USB-capture periodicity; I am not treating 512 as a confirmed internal firmware block size.",
    ]
    return "\n".join(lines) + "\n"


def firmware_paths(repo: Path, deep: bool) -> list[tuple[str, Path, int, str]]:
    order = ["v2.1.0_48k2ch", "v2.1.1_48k2ch", "v2.1.1_native16k"] if deep else ["v2.1.1_48k2ch", "v2.1.1_native16k"]
    rows=[]
    for label in order:
        meta=FIRMWARES[label]
        p=repo/meta["rel"]
        if not p.exists(): raise RuntimeError(f"Firmware missing from downloaded repository: {p}")
        actual=sha256_file(p)
        expected=meta["sha256"]
        if actual.lower()!=expected.lower():
            raise RuntimeError(f"Firmware SHA-256 mismatch for {label}:\nexpected {expected}\nactual   {actual}\nRefusing to flash an unexpected image.")
        rows.append((label,p,int(meta["rate"]),actual))
    return rows


def environment_info(sd: Any, dfu: str, host_cmd: list[str], repo: Path) -> dict[str, Any]:
    try: dfu_ver=(run([dfu,"-V"],check=False).stdout or "").splitlines()[0]
    except Exception: dfu_ver="unknown"
    return {
        "app_version":APP_VERSION,"platform":platform.platform(),"system":platform.system(),"machine":platform.machine(),"python":sys.version,
        "dfu_util":dfu_ver,"xvf_host":" ".join(host_cmd),"respeaker_repo":str(repo),"respeaker_repo_commit":RESPEAKER_REPO_COMMIT,"audio_devices":list_audio_devices(sd),
    }


def routes_for(fw_label: str, rate: int, deep: bool) -> list[dict[str, Any]]:
    routes = ROUTES_48K if rate == 48000 else ROUTES_16K
    if fw_label == "v2.1.0_48k2ch":
        # Historical comparison: enough to reproduce its processed-output behavior.
        routes = [ROUTES_48K[0]]
    if not deep:
        routes = [r for r in routes if r["name"] in {"normal", "raw", "pre_shf", "shf_input"}]
        if rate == 16000:
            routes = [r for r in routes if r["name"] == "normal"]
    return routes


def simulate_run(out_root: Path, voice: Path | None, np: Any, sig: Any, sf: Any) -> Path:
    stamp=time.strftime("%Y%m%d-%H%M%S")
    sess=out_root/f"simulated-{stamp}"; (sess/"recordings").mkdir(parents=True); (sess/"stimulus").mkdir()
    stim_path, manifest=generate_stimulus(voice,sess/"stimulus",np,sig,sf)
    stim,fs=sf.read(str(stim_path),dtype="float64",always_2d=False)
    tests=[]
    variants=[("v2.1.1_48k2ch","normal",True,48000),("v2.1.1_48k2ch","raw",False,48000),("v2.1.1_native16k","normal",False,16000)]
    for fw,route,corrupt,rate in variants:
        if rate!=fs:
            g=math.gcd(int(fs),rate); base=sig.resample_poly(stim,rate//g,int(fs)//g)
        else: base=stim.copy()
        delay=np.zeros(int(.75*rate)); y=np.concatenate([delay,base,delay])
        y=np.column_stack([y*.7,y*.65])
        if corrupt:
            # Click at every 40th 512-frame boundary: one sharp edge decaying over ~20 samples.
            # Unlike a one-sample spike (two edges) it lands on exactly phase 511 mod 512.
            impulses=np.zeros(len(y)); impulses[511:len(y)-1:512*40]=1.0
            click=sig.lfilter([1.0],[1.0,-math.exp(-1/20)],impulses)
            y[:,1]=np.clip(y[:,1]+.42*click,-.98,.98)
            y[:,0]=np.clip(y[:,0]-.32*click,-.98,.98)
        p=sess/"recordings"/f"{fw}__{route}.wav"; sf.write(str(p),y,rate,subtype="PCM_16")
        a=analyze_recording(p,stim_path,manifest,sf,np,sig); plots=create_plots(p,a,sf,np)
        tests.append({"firmware":fw,"firmware_sha256":"simulation","route":route,"route_description":"synthetic validation","xvf_before":{},"capture_meta":{},"recording_path":str(p),"analysis":a,"plots":plots})
    session={"generated_at":time.strftime("%Y-%m-%d %H:%M:%S %z"),"environment":{"simulation":True},"voice_fixture":voice_fixture_info(voice),"stimulus_sha256":sha256_file(stim_path),"stimulus_manifest":manifest,"tests":tests}
    json_session = dict(session)
    json_session["tests"] = [{k:v for k,v in t.items() if k != "plots"} for t in tests]
    (sess/"analysis.json").write_text(json.dumps(json_session,indent=2),encoding="utf-8"); (sess/"session.json").write_text(json.dumps({k:v for k,v in session.items() if k!="tests"},indent=2),encoding="utf-8")
    generate_html_report(session,sess/"report.html"); write_summary_csv(session,sess/"summary.csv"); make_share_bundle(sess,session,sf)
    return sess


def real_run(args: argparse.Namespace, script_dir: Path, np: Any, sig: Any, sf: Any, sd: Any) -> Path:
    cache=script_dir/CACHE_DIR_NAME; cache.mkdir(exist_ok=True)
    repo=ensure_repo(cache); dfu=ensure_dfu_util(cache); host_cmd=locate_xvf_host(repo)
    out_root=Path(args.output).expanduser().resolve(); out_root.mkdir(parents=True,exist_ok=True)
    stamp=time.strftime("%Y%m%d-%H%M%S"); sess=out_root/f"xvf3800-diagnostic-{stamp}"; (sess/"recordings").mkdir(parents=True); (sess/"stimulus").mkdir()
    if args.voice:
        voice = Path(args.voice).expanduser().resolve()
    else:
        default_voice = script_dir / "assets" / "voice" / "xvf3800-test-vo.wav"
        voice = default_voice.resolve() if default_voice.exists() else None
        if voice:
            print(f"[setup] Using repository test VO: {voice}", flush=True)
    if voice and not voice.exists(): raise RuntimeError(f"Voice file not found: {voice}")
    stim_path,manifest=generate_stimulus(voice,sess/"stimulus",np,sig,sf)
    envinfo=environment_info(sd,dfu,host_cmd,repo)
    fwrows=firmware_paths(repo,args.deep)

    print("\nPRE-FLIGHT")
    print("Place the reSpeaker and playback speaker in fixed positions. Use a comfortable, moderate speaker level.")
    print("Do not move either device during the matrix. The runner will not poll XVF controls while audio is being captured.")
    if not voice:
        print("WARNING: --voice was not supplied; the English speech section will be silence.")
    print("\nAudio devices:")
    for d in envinfo["audio_devices"]:
        print(f"  [{d['index']}] {d['name']} | in={d['max_input_channels']} out={d['max_output_channels']} | {d['hostapi']}")
    input("\nPress ENTER to begin the firmware matrix... ")

    tests=[]
    output_name: str|None=None
    for fw_label,fw_path,rate,fwsha in fwrows:
        flash_firmware(dfu,fw_path,fw_label)
        wait_for_xvf(host_cmd)
        input_idx=wait_for_audio_input(sd,rate)
        if output_name is None:
            output_idx=choose_output_device(sd,args.output_device,input_idx)
            output_name=sd.query_devices(output_idx)["name"]
            print(f"[audio] Independent output: [{output_idx}] {output_name}")
        else:
            # Indices can shift after the XVF re-enumerates; keep the same physical speaker.
            output_idx=find_output_by_name(sd,output_name)
        print(f"[audio] XVF input: [{input_idx}] {sd.query_devices(input_idx)['name']}")

        # Exact no-playback control, close to the maintainer's 20 s ALSA capture reproduction.
        ambient_route = ROUTES_48K[0] if rate == 48000 else ROUTES_16K[0]
        print(f"\n[capture] {fw_label} / normal_ambient: 20 s no-playback control")
        set_route(host_cmd, ambient_route)
        amb_before = snapshot_xvf(host_cmd)
        amb_rec = sess/"recordings"/f"{fw_label}__normal_ambient.wav"
        amb_cap = capture_only(sd, sf, np, rate, amb_rec, duration_s=20.0)
        amb_after = snapshot_xvf(host_cmd)
        amb_analysis = analyze_recording(amb_rec, stim_path, [], sf, np, sig)
        amb_plots = create_plots(amb_rec, amb_analysis, sf, np)
        amb_test = {"firmware":fw_label,"firmware_sha256":fwsha,"route":"normal_ambient","route_description":"20-second no-playback control matching the maintainer's reproduction style","xvf_before":amb_before,"xvf_after":amb_after,"capture_meta":amb_cap,"recording_path":str(amb_rec),"analysis":amb_analysis,"plots":amb_plots}
        tests.append(amb_test)
        print(f"[capture] {classify_test(amb_test)}")

        for route in routes_for(fw_label,rate,args.deep):
            print(f"\n[capture] {fw_label} / {route['name']}: {route['description']}")
            set_route(host_cmd,route)
            before=snapshot_xvf(host_cmd)
            rec=sess/"recordings"/f"{fw_label}__{route['name']}.wav"
            cap=capture_and_play(sd,sf,np,stim_path,output_idx,rate,rec)
            after=snapshot_xvf(host_cmd)
            analysis=analyze_recording(rec,stim_path,manifest,sf,np,sig)
            plots=create_plots(rec,analysis,sf,np)
            test={"firmware":fw_label,"firmware_sha256":fwsha,"route":route["name"],"route_description":route["description"],"xvf_before":before,"xvf_after":after,"capture_meta":cap,"recording_path":str(rec),"analysis":analysis,"plots":plots}
            tests.append(test)
            print(f"[capture] {classify_test(test)}")

    session={"generated_at":time.strftime("%Y-%m-%d %H:%M:%S %z"),"environment":envinfo,"voice_fixture":voice_fixture_info(voice),"stimulus_sha256":sha256_file(stim_path),"stimulus_manifest":manifest,"tests":tests}
    json_session = dict(session)
    json_session["tests"] = [{k:v for k,v in t.items() if k != "plots"} for t in tests]
    (sess/"analysis.json").write_text(json.dumps(json_session,indent=2),encoding="utf-8")
    (sess/"session.json").write_text(json.dumps({"generated_at":session["generated_at"],"environment":envinfo,"voice_fixture":session["voice_fixture"],"stimulus_sha256":session["stimulus_sha256"],"firmware_order":[x[0] for x in fwrows]},indent=2),encoding="utf-8")
    generate_html_report(session,sess/"report.html"); write_summary_csv(session,sess/"summary.csv"); bundle=make_share_bundle(sess,session,sf)
    print(f"\nDONE\nHTML report: {sess/'report.html'}\nShare bundle: {bundle}\nRaw session: {sess}")
    return sess


def write_assets(script_dir: Path) -> None:
    voice_dir = script_dir / "assets" / "voice"
    voice_dir.mkdir(parents=True, exist_ok=True)
    p = voice_dir / "script.txt"
    if not p.exists():
        p.write_text(VOICE_SCRIPT + "\n", encoding="utf-8")


def main() -> int:
    script_path=Path(__file__).resolve(); script_dir=script_path.parent
    bootstrap_venv(script_path)
    np,sig,wavfile,sd,sf=import_runtime()
    write_assets(script_dir)

    ap=argparse.ArgumentParser(description="One-click XVF3800 firmware/audio diagnostic pipeline")
    ap.add_argument("--voice", help="English VO WAV. Defaults to assets/voice/xvf3800-test-vo.wav when present")
    ap.add_argument("--output",default=str(script_dir/"runs"),help="Output root directory")
    ap.add_argument("--output-device",help="Independent playback device name substring or index")
    ap.add_argument("--deep",action="store_true",help=argparse.SUPPRESS)
    ap.add_argument("--quick",action="store_true",help="Skip historical v2.1.0 and extra localization routes")
    ap.add_argument("--simulate",action="store_true",help="Generate a synthetic end-to-end report without hardware")
    ap.add_argument("--devices",action="store_true",help="List audio devices and exit")
    ap.add_argument("--make-stimulus",action="store_true",help="Only build the deterministic stimulus")
    ap.add_argument("--no-open",action="store_true",help="Do not open the HTML report automatically when finished")
    args=ap.parse_args()

    args.deep = bool(args.deep or not args.quick)
    if args.devices:
        for d in list_audio_devices(sd): print(json.dumps(d,ensure_ascii=False))
        return 0
    if args.voice:
        voice = Path(args.voice).expanduser().resolve()
    else:
        default_voice = script_dir / "assets" / "voice" / "xvf3800-test-vo.wav"
        voice = default_voice.resolve() if default_voice.exists() else None
        if voice:
            print(f"[setup] Using repository test VO: {voice}", flush=True)
    if args.make_stimulus:
        out=Path(args.output).expanduser().resolve()/"stimulus"; p,m=generate_stimulus(voice,out,np,sig,sf); print(p); return 0
    if args.simulate:
        sess=simulate_run(Path(args.output).expanduser().resolve(),voice,np,sig,sf); print(sess/"report.html"); return 0
    sess = real_run(args,script_dir,np,sig,sf,sd)
    if not args.no_open:
        try:
            import webbrowser
            webbrowser.open((sess / "report.html").resolve().as_uri())
        except Exception:
            pass
    return 0

if __name__=="__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        eprint("\nCancelled.")
        raise SystemExit(130)
    except Exception as exc:
        eprint(f"\nERROR: {exc}")
        if os.environ.get("XVFDIAG_DEBUG"):
            import traceback; traceback.print_exc()
        raise SystemExit(1)

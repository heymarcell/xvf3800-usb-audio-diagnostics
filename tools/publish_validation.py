#!/usr/bin/env python3
"""Publish a finished validation run into results/issue-39/validation-<timestamp>/.

Copies the self-contained HTML report (and, with --with-matrix, the matrix run's report), writes
SUMMARY.md and results.json without local paths, exports the key figures as PNG, writes a
README.md that GitHub renders with those figures, and hashes every file in manifest.json.
Recordings and logs are never copied: they contain whatever the room sounded like.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tools"))

# Figures embedded in the README: (trial, figure key, caption)
KEY_FIGURES = [
    ("xvf48k_homebrew_ab1", "fold", "Homebrew FFmpeg vs direct capture of the same moment: mean sample-to-sample step per phase modulo 512"),
    ("xvf48k_homebrew_ab1", "zoom", "The largest splice in the Homebrew FFmpeg recording, and the 512 frames it never delivered"),
    ("xvf48k_homebrew_ab1", "timeline", "Audio missing from the Homebrew FFmpeg recording over time, and the time between splices"),
    ("xvf48k_homebrew_ab1", "spectrogram", "Spectrograms: the splices show as vertical broadband lines in the FFmpeg recording only"),
    ("xvf48k_master_ab1", "fold", "FFmpeg master vs direct capture: no excess at any phase"),
    ("xvf48k_fixed_ab1", "fold", "FFmpeg at the fix commit vs direct capture: no excess at any phase"),
    ("builtin_mic_homebrew_ab", "fold", "The MacBook built-in microphone through Homebrew FFmpeg: the same splices without the XVF3800"),
]


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def pages_base() -> str | None:
    url = subprocess.run(["git", "remote", "get-url", "origin"], cwd=REPO, capture_output=True, text=True).stdout.strip()
    m = re.search(r"github\.com[:/]([^/]+)/([^/.]+)", url)
    return f"https://{m.group(1).lower()}.github.io/{m.group(2)}" if m else None


def sanitize(text: str, run_dir: Path) -> str:
    """Paths inside the run become run-relative, inside the checkout repo-relative, home dirs ~."""
    for root in (run_dir, REPO):
        text = text.replace(f"{root}/", "").replace(str(root), ".")
    return re.sub(r"/(?:Users|home)/[^/\s\"'`]+", "~", text)


def title_of(results: dict[str, Any]) -> str:
    s = results["started"]
    when = f"{s[:4]}-{s[4:6]}-{s[6:8]} {s[9:11]}:{s[11:13]}"
    brew = ((results.get("env") or {}).get("system_ffmpeg") or {}).get("version", "")
    built = (results.get("stages", {}).get("ffmpeg") or {}).get("version", "")
    short = lambda v: v.split(" Copyright")[0].replace("ffmpeg version ", "FFmpeg ")
    return f"Validation {when}: Homebrew {short(brew)} and {short(built)} (source build)" if built else f"Validation {when}: Homebrew {short(brew)}"


def write_index(dest_root: Path) -> None:
    rows = []
    for d in sorted(dest_root.glob("validation-*"), reverse=True):
        readme = d / "README.md"
        if readme.exists():
            head = readme.read_text(encoding="utf-8").splitlines()[0].lstrip("# ")
            rows.append(f"- [{head}]({d.name}/README.md)")
    (dest_root / "README.md").write_text(
        "# Issue #39 evidence\n\nPublished validation runs, newest first. Each has a README with the key figures, "
        "the full HTML report, a summary and the raw numbers, all hashed in its manifest.json.\n\n" + "\n".join(rows) + "\n", encoding="utf-8")


def publish(run_dir: Path, dest_root: Path, with_matrix: bool) -> Path:
    import validation_report as rep

    results = json.loads((run_dir / "results.json").read_text(encoding="utf-8"))
    dest = dest_root / f"validation-{results['started']}"
    if dest.exists():
        raise SystemExit(f"Already published: {dest}")
    (dest / "figures").mkdir(parents=True)

    html = (run_dir / "report.html").read_text(encoding="utf-8")
    matrix = results.get("stages", {}).get("matrix", {}).get("session")
    matrix_report = Path(matrix) / "report.html" if matrix else None
    link = re.search(r"<p>Full per-capture report[^<]*<a href='([^']+)'>[^<]*</a></p>", html)
    if link:
        if with_matrix and matrix_report and matrix_report.exists():
            shutil.copy2(matrix_report, dest / "matrix-report.html")
            html = html.replace(link.group(1), "matrix-report.html")
        else:
            html = html.replace(link.group(0), "")
    (dest / "report.html").write_text(sanitize(html, run_dir), encoding="utf-8")
    (dest / "SUMMARY.md").write_text(sanitize((run_dir / "SUMMARY.md").read_text(encoding="utf-8"), run_dir), encoding="utf-8")
    (dest / "results.json").write_text(sanitize(json.dumps(results, indent=2), run_dir) + "\n", encoding="utf-8")

    trials = {t["name"]: t for t in results.get("stages", {}).get("hostpath", {}).get("trials", [])}
    figures = []
    dur = rep.duration_figure(list(trials.values()))
    if dur:
        (dest / "figures" / "durations.png").write_bytes(dur)
        figures.append(("durations.png", "`ffmpeg -t N` alone: requested length vs audio actually in the file"))
    rendered: dict[str, dict[str, bytes]] = {}
    for name, key, caption in KEY_FIGURES:
        t = trials.get(name)
        if not t or not t.get("continuity", {}).get("comparable") or not Path(t["dir"]).exists():
            continue
        if name not in rendered:
            rendered[name] = dict(rep.trial_figures(t))
        if key in rendered[name]:
            fn = f"{name}_{key}.png"
            (dest / "figures" / fn).write_bytes(rendered[name][key])
            figures.append((fn, f"{caption} ({name})"))

    base = pages_base()
    rel = dest.relative_to(REPO).as_posix() if dest.is_relative_to(REPO) else None
    full = f"{base}/{rel}/report.html" if base and rel else "report.html"
    links = [f"**[Open the full report]({full})** (every trial, every diagram)"]
    if (dest / "matrix-report.html").exists():
        links.append(f"[matrix report]({f'{base}/{rel}/matrix-report.html' if base and rel else 'matrix-report.html'}) (every checkpoint: waveforms, spectra, spectrograms)")
    links += ["[summary](SUMMARY.md)", "[raw numbers](results.json)", "[hashes](manifest.json)"]
    summary = (dest / "SUMMARY.md").read_text(encoding="utf-8").split("\n", 1)[1]
    readme = [f"# {title_of(results)}", "", " · ".join(links), "", "## Key figures", ""]
    for fn, caption in figures:
        readme += [f"**{caption}**", "", f"![{caption}](figures/{fn})", ""]
    readme += ["## Summary", summary]
    (dest / "README.md").write_text("\n".join(readme), encoding="utf-8")

    files = sorted(p for p in dest.rglob("*") if p.is_file())
    manifest = {"run": results["started"], "files": [{"path": p.relative_to(dest).as_posix(), "bytes": p.stat().st_size, "sha256": sha256(p)} for p in files]}
    (dest / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    write_index(dest_root)
    return dest


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("run_dir", help="validation/<timestamp>")
    ap.add_argument("--with-matrix", action="store_true", help="also publish the matrix run's report (~19 MB)")
    ap.add_argument("--dest-root", default=str(REPO / "results" / "issue-39"))
    args = ap.parse_args()
    import diagnose
    diagnose.bootstrap_venv(Path(__file__).resolve(), root=REPO)
    print(publish(Path(args.run_dir).resolve(), Path(args.dest_root).resolve(), args.with_matrix))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

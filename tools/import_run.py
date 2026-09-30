#!/usr/bin/env python3
from __future__ import annotations
import argparse, hashlib, json, shutil
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

PREFERRED = [
    "report.html",
    "summary.csv",
    "analysis.json",
    "session.json",
    "github_comment.md",
    "github-share-bundle.zip",
]

# GitHub warns above 50 MB and rejects files above 100 MB.
GITHUB_WARN_BYTES = 50 * 1024 * 1024

def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()

def main() -> int:
    ap = argparse.ArgumentParser(description="Publish a compact diagnostic run into results/issue-39")
    ap.add_argument("run_dir")
    ap.add_argument("--dest-root", default=str(REPO_ROOT / "results" / "issue-39"))
    args = ap.parse_args()
    src = Path(args.run_dir).expanduser().resolve()
    if not src.is_dir():
        raise SystemExit(f"Run directory not found: {src}")
    found = [(name, src / name) for name in PREFERRED if (src / name).is_file()]
    if not found:
        raise SystemExit("No expected report artifacts found in the run directory")
    dest = Path(args.dest_root).expanduser().resolve() / src.name
    if dest.exists():
        raise SystemExit(f"Already imported: {dest}\nRemove it first to re-import this run.")
    dest.mkdir(parents=True)
    manifest = {"source_run": src.name, "files": []}
    for name, p in found:
        out = dest / name
        shutil.copy2(p, out)
        size = out.stat().st_size
        manifest["files"].append({"path": name, "bytes": size, "sha256": sha256(out)})
        if size > GITHUB_WARN_BYTES:
            print(f"WARNING: {name} is {size / 1e6:.1f} MB; attach it to a GitHub Release instead of committing it.")
    (dest / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    readme = [f"# Diagnostic run `{src.name}`", "", "Published evidence for reSpeaker XVF3800 issue #39.", "", "## Files", ""]
    for f in manifest["files"]:
        readme.append(f"- `{f['path']}` — {f['bytes']} bytes — SHA-256 `{f['sha256']}`")
    (dest / "README.md").write_text("\n".join(readme) + "\n", encoding="utf-8")
    print(dest)
    return 0

if __name__ == "__main__":
    raise SystemExit(main())

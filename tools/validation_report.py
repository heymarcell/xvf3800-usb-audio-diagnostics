"""Self-contained HTML report for tools/validate.py: statistics and diagrams for every
FFmpeg-vs-direct trial, the -t duration checks and the diagnostic matrix."""
from __future__ import annotations

import base64
import html
import io
import os
import re
from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import scipy.signal as sig  # noqa: E402
import soundfile as sf  # noqa: E402

import diagnose as d  # noqa: E402

GROUP_LABELS = {
    "xvf48k_homebrew": "XVF3800 v2.1.1 48 kHz · Homebrew FFmpeg",
    "xvf48k_fixed": "XVF3800 v2.1.1 48 kHz · FFmpeg at the upstream fix commit",
    "xvf48k_master": "XVF3800 v2.1.1 48 kHz · FFmpeg master",
    "builtin_mic_homebrew": "Built-in microphone 48 kHz · Homebrew FFmpeg",
    "builtin_mic_fixed": "Built-in microphone 48 kHz · FFmpeg at the upstream fix commit",
    "builtin_mic_master": "Built-in microphone 48 kHz · FFmpeg master",
    "xvf16k_homebrew": "XVF3800 v2.1.1 native 16 kHz · Homebrew FFmpeg",
}
FFMPEG_COLOR, DIRECT_COLOR = "#d1495b", "#00798c"


def esc(v: Any) -> str:
    return html.escape(str(v))


def group_of(name: str) -> str:
    return re.sub(r"_(ab\d*|duration\d+)$", "", name)


def label_of(group: str) -> str:
    return GROUP_LABELS.get(group, group)


def png_bytes(fig: Any) -> bytes:
    bio = io.BytesIO()
    fig.savefig(bio, format="png", dpi=100, bbox_inches="tight")
    plt.close(fig)
    return bio.getvalue()


def img(data: bytes) -> str:
    return f"<img alt='' src='data:image/png;base64,{base64.b64encode(data).decode('ascii')}'>"


def reference_offset(cont: dict[str, Any], frame: int) -> int:
    """reference_frame - test_frame at `frame` of the FFmpeg recording."""
    a = cont["anchor"]["test_frame"]
    off = cont["anchor"]["reference_frame"] - a
    for f, k in cont["splice_list"]:
        if a < f <= frame:
            off += k
        elif frame < f <= a:
            off -= k
    return off


def splice_stats(cont: dict[str, Any], ff: np.ndarray, rate: int) -> dict[str, Any]:
    sp = np.array(cont["splice_list"], dtype=np.int64).reshape(-1, 2)
    seconds = cont["test_frames"] / rate
    out: dict[str, Any] = {"splices_per_min": 60 * len(sp) / seconds if seconds else 0.0}
    if len(sp) > 1:
        iv = np.diff(sp[:, 0]) / rate * 1000
        out.update(interval_ms_median=float(np.median(iv)), interval_ms_p10=float(np.percentile(iv, 10)), interval_ms_p90=float(np.percentile(iv, 90)))
    if len(sp):
        runs = sp[:, 1] // cont["block"]
        out["run_lengths"] = {int(k): int(v) for k, v in zip(*np.unique(runs, return_counts=True))}
        dd = np.abs(np.diff(ff, axis=0)).max(axis=1)
        at = sp[:, 0][(sp[:, 0] > 0) & (sp[:, 0] < len(ff))] - 1
        typical = float(np.median(dd)) or 1e-12
        out["splice_jump_x_typical"] = float(np.median(dd[at]) / typical) if len(at) else 0.0
    return out


def fold_profile(x: np.ndarray, period: int = 512) -> np.ndarray:
    n = np.arange(1, len(x))
    prof = np.bincount(n % period, weights=np.abs(np.diff(x)), minlength=period) / np.maximum(np.bincount(n % period, minlength=period), 1)
    return prof / max(float(np.median(prof)), 1e-15)


def trial_figures(t: dict[str, Any]) -> list[tuple[str, bytes]]:
    """(key, PNG) for one FFmpeg-vs-direct trial: fold, timeline, zoom (when spliced), spectrum, spectrogram."""
    tdir = Path(t["dir"])
    ff, rate = sf.read(str(tdir / "ffmpeg.wav"), always_2d=True, dtype="float64")
    di, _ = sf.read(str(tdir / "direct.wav"), always_2d=True, dtype="float64")
    ch = ff.shape[1] - 1  # ASR channel on the XVF, the only channel on a mono microphone
    cont = t["continuity"]
    parts = []

    fig, ax = plt.subplots(figsize=(10, 2.8))
    ax.plot(fold_profile(ff[:, ch]), color=FFMPEG_COLOR, linewidth=.9, label="FFmpeg/AVFoundation")
    ax.plot(fold_profile(di[:, ch]), color=DIRECT_COLOR, linewidth=.9, label="direct (PortAudio/CoreAudio)")
    ax.set(title="Mean |x[n]-x[n-1]| per phase modulo 512 (1 = median phase)", xlabel="frame index mod 512", ylabel="x median")
    ax.legend(loc="upper right"); ax.grid(alpha=.2)
    parts.append(("fold", png_bytes(fig)))

    sp = np.array(cont["splice_list"], dtype=np.int64).reshape(-1, 2)
    fig, (a1, a2) = plt.subplots(1, 2, figsize=(10, 2.8), gridspec_kw={"width_ratios": [2, 1]})
    if len(sp):
        a1.step(np.r_[0, sp[:, 0]] / rate, np.r_[0, np.cumsum(sp[:, 1])] / rate * 1000, where="post", color=FFMPEG_COLOR)
        if len(sp) > 1:
            a2.hist(np.diff(sp[:, 0]) / rate * 1000, bins=30, color=FFMPEG_COLOR)
    a1.set(title="Audio missing from the FFmpeg recording", xlabel="FFmpeg recording time (s)", ylabel="cumulative missing (ms)"); a1.grid(alpha=.2)
    a2.set(title="Time between splices", xlabel="ms", ylabel="splices"); a2.grid(alpha=.2)
    fig.tight_layout()
    parts.append(("timeline", png_bytes(fig)))

    if len(sp):
        dd = np.abs(np.diff(ff[:, ch]))
        cand = sp[(sp[:, 0] > rate // 100) & (sp[:, 0] < len(ff) - rate // 100)]
        if len(cand):
            f, k = cand[np.argmax(dd[cand[:, 0] - 1])]
            w = int(0.004 * rate)
            off = reference_offset(cont, int(f) - 1)
            fig, (b1, b2) = plt.subplots(2, 1, figsize=(10, 4.2), sharey=True)
            b1.plot((np.arange(f - w, f + w) - f) / rate * 1000, ff[f - w:f + w, ch], color=FFMPEG_COLOR, marker=".", markersize=2, linewidth=.7)
            b1.axvline(0, color="k", linestyle="--", linewidth=.8)
            b1.set(title=f"FFmpeg recording around the splice at {f / rate:.3f} s: {k} frames missing here", ylabel="FS"); b1.grid(alpha=.2)
            r0 = f + off
            span = np.arange(r0 - w, r0 + k + w)
            span = span[(span >= 0) & (span < len(di))]
            b2.plot((span - r0) / rate * 1000, di[span, ch], color=DIRECT_COLOR, marker=".", markersize=2, linewidth=.7)
            b2.axvspan(0, k / rate * 1000, color=FFMPEG_COLOR, alpha=.15, label=f"{k} frames FFmpeg never delivered")
            b2.set(title="Direct capture of the same moment: continuous", xlabel="ms from the splice", ylabel="FS")
            b2.legend(loc="upper right"); b2.grid(alpha=.2)
            fig.tight_layout()
            parts.append(("zoom", png_bytes(fig)))

    fig, ax = plt.subplots(figsize=(10, 2.8))
    for x, color, label in ((ff, FFMPEG_COLOR, "FFmpeg/AVFoundation"), (di, DIRECT_COLOR, "direct")):
        fr, p = sig.welch(x[:, ch], fs=rate, nperseg=min(8192, len(x)))
        ax.semilogy(fr, p + 1e-20, color=color, linewidth=.8, label=label)
    ax.set(title="Welch power spectrum", xlabel="Hz", ylabel="PSD", xlim=(0, rate / 2)); ax.legend(loc="upper right"); ax.grid(alpha=.2)
    parts.append(("spectrum", png_bytes(fig)))

    n = min(len(ff), len(di), int(12 * rate))
    fig, axes = plt.subplots(1, 2, figsize=(10, 3.0), sharey=True)
    for axx, x, title in ((axes[0], ff, "FFmpeg/AVFoundation"), (axes[1], di, "direct")):
        fr, tt, sxx = sig.spectrogram(x[:n, ch], fs=rate, nperseg=512, noverlap=384)
        axx.pcolormesh(tt, fr, 10 * np.log10(sxx + 1e-20), shading="auto", vmin=-140, vmax=-40)
        axx.set(title=f"Spectrogram, first 12 s: {title}", xlabel="s")
    axes[0].set_ylabel("Hz")
    fig.tight_layout()
    parts.append(("spectrogram", png_bytes(fig)))
    return parts


def trial_plots(t: dict[str, Any]) -> str:
    return "".join(img(data) for _, data in trial_figures(t))


def mean_sd(values: list[float]) -> str:
    if not values:
        return "–"
    if len(values) == 1:
        return f"{values[0]:.1f}"
    return f"{np.mean(values):.1f} ± {np.std(values, ddof=1):.1f}"


def group_table(trials: list[dict[str, Any]]) -> str:
    groups: dict[str, list[dict[str, Any]]] = {}
    for t in trials:
        if t["kind"] == "ab":
            groups.setdefault(group_of(t["name"]), []).append(t)
    rows = []
    for g, ts in groups.items():
        ok = [t for t in ts if t.get("continuity", {}).get("comparable")]
        miss = [t["continuity"]["missing_pct"] for t in ok]
        spm = [t["stats"]["splices_per_min"] for t in ok if "stats" in t]
        ivl = [t["stats"]["interval_ms_median"] for t in ok if "interval_ms_median" in t.get("stats", {})]
        jx = [t["stats"]["splice_jump_x_typical"] for t in ok if "splice_jump_x_typical" in t.get("stats", {})]
        exact = sum(t["continuity"]["identical_blocks"] for t in ok), sum(t["continuity"]["located_blocks"] for t in ok)
        near = sum(t["continuity"]["within_1lsb_blocks"] for t in ok)
        phases = sorted({p for t in ok for p in t["continuity"]["splice_positions_mod_block"]})
        ff_flag = sum(t.get("ffmpeg", {}).get("classification") == d.PERIODIC for t in ts)
        di_flag = sum(t.get("direct", {}).get("classification") == d.PERIODIC for t in ts)
        rows.append(f"<tr><td>{esc(label_of(g))}</td><td>{len(ok)}/{len(ts)}</td><td>{mean_sd(miss)}</td><td>{mean_sd(spm)}</td><td>{mean_sd(ivl)}</td>"
                    f"<td>{mean_sd(jx)}</td><td>{exact[0]}/{exact[1]} ({near} within 1 LSB)</td><td>{esc(phases) if phases else '–'}</td><td>{ff_flag}/{len(ts)}</td><td>{di_flag}/{len(ts)}</td></tr>")
    if not rows:
        return ""
    return ("<table><thead><tr><th>Input · FFmpeg</th><th>Trials compared</th><th>Audio missing %</th><th>Splices / min</th><th>Median time between splices (ms)</th>"
            "<th>Jump at a splice ÷ typical step</th><th>Located blocks identical</th><th>Splice phases mod 512</th><th>FFmpeg flagged periodic</th><th>Direct flagged periodic</th></tr></thead>"
            f"<tbody>{''.join(rows)}</tbody></table><p class='muted'>Mean ± sample standard deviation across trials. Every 512-frame block of the FFmpeg recording "
            "that can be located in the direct capture is compared sample by sample; blocks FFmpeg recorded before the direct capture started or after it "
            "stopped cannot be located. The built-in microphone delivers float samples that the two paths round to 16 bits separately, so its blocks agree "
            "within 1 LSB rather than exactly.</p>")


def verdict(trials: list[dict[str, Any]]) -> str:
    lines = []
    groups: dict[str, list[dict[str, Any]]] = {}
    for t in trials:
        if t["kind"] == "ab" and t.get("continuity", {}).get("comparable"):
            groups.setdefault(group_of(t["name"]), []).append(t)
    for g, ts in groups.items():
        miss = [t["continuity"]["missing_pct"] for t in ts]
        phases = sorted({p for t in ts for p in t["continuity"]["splice_positions_mod_block"]})
        exact = sum(t["continuity"]["identical_blocks"] for t in ts), sum(t["continuity"]["located_blocks"] for t in ts)
        near = sum(t["continuity"]["within_1lsb_blocks"] for t in ts)
        di_flag = sum(t["direct"]["classification"] == d.PERIODIC for t in ts)
        match = f"{exact[0]}/{exact[1]} located blocks identical to the direct capture" if exact[0] == exact[1] or exact[0] else f"{near}/{exact[1]} located blocks within 1 LSB of the direct capture"
        if max(miss) == 0:
            lines.append(f"<li><b>{esc(label_of(g))}</b>: no audio missing in {len(ts)} trial(s); {match}.</li>")
        else:
            lines.append(f"<li><b>{esc(label_of(g))}</b>: {mean_sd(miss)} % of the audio missing across {len(ts)} trial(s), whole 512-frame buffers only "
                         f"(splice phases {esc(phases)}); {match}, which was flagged periodic in {di_flag}/{len(ts)}.</li>")
    return f"<ul>{''.join(lines)}</ul>" if lines else ""


def duration_figure(trials: list[dict[str, Any]]) -> bytes | None:
    dur = [t for t in trials if t["kind"] == "duration" and "held_s" in t]
    if not dur:
        return None
    fig, ax = plt.subplots(figsize=(10, 0.6 + 0.45 * len(dur)))
    names = [t["name"] for t in dur]
    ax.barh(names, [t["requested_s"] for t in dur], color="#d9d9d9", label="requested (-t)")
    ax.barh(names, [t["held_s"] for t in dur], color=DIRECT_COLOR, label="audio in the file")
    for i, t in enumerate(dur):
        ax.text(t["requested_s"], i, f"  {t['missing_pct']:.1f}% missing", va="center", fontsize=8)
    ax.invert_yaxis(); ax.set(xlabel="seconds", xlim=(0, max(t["requested_s"] for t in dur) * 1.18)); ax.grid(alpha=.2, axis="x")
    ax.legend(loc="upper center", bbox_to_anchor=(0.5, -0.35 if len(dur) < 4 else -0.18), ncol=2, frameon=False)
    return png_bytes(fig)


def duration_chart(trials: list[dict[str, Any]]) -> str:
    dur = [t for t in trials if t["kind"] == "duration" and "held_s" in t]
    if not dur:
        return ""
    rows = "".join(f"<tr><td>{esc(t['name'])}</td><td>{t['requested_s']:.0f} s</td><td>{t['held_s']:.3f} s</td><td>{t['missing_pct']:.1f}%</td>"
                   f"<td>{esc(t['ffmpeg']['classification'])}</td></tr>" for t in dur)
    return (img(duration_figure(trials)) + "<table><thead><tr><th>Trial</th><th>Requested</th><th>Audio held</th><th>Missing</th><th>Recording</th></tr></thead>"
            f"<tbody>{rows}</tbody></table>")


def render(results: dict[str, Any], out: Path) -> Path:
    stages = results.get("stages", {})
    trials = stages.get("hostpath", {}).get("trials", [])
    for t in trials:
        if t["kind"] == "ab" and t.get("continuity", {}).get("comparable") and "stats" not in t:
            ff, rate = sf.read(str(Path(t["dir"]) / "ffmpeg.wav"), always_2d=True, dtype="float64")
            t["stats"] = splice_stats(t["continuity"], ff, rate)

    stage_rows = "".join(
        f"<tr><td>{esc(n)}</td><td>{'✅' if r.get('ok') else '❌'}</td><td>{esc(r.get('summary') or r.get('error') or r.get('skipped') or r.get('version') or '')}</td></tr>"
        for n, r in stages.items())
    m = stages.get("matrix", {})
    matrix_html = ""
    if m.get("tests"):
        report = Path(m["session"]) / "report.html"
        link = os.path.relpath(report, out) if report.exists() else None
        rows = "".join(
            f"<tr><td>{esc(t['firmware'])}</td><td>{esc(t['route'])}</td><td>{esc(t['result'])}</td><td>{esc(' | '.join(t['periodicity']))}</td>"
            f"<td>{esc(t['signature'])}</td><td>{esc(t['host_path'] or '–')}</td></tr>" for t in m["tests"])
        matrix_html = ("<section><h2>Diagnostic matrix: direct capture of every checkpoint</h2>"
                       + (f"<p>Full per-capture report with waveforms, spectra, spectrograms, click zooms and per-segment levels: <a href='{esc(link)}'>{esc(link)}</a></p>" if link else "")
                       + "<table><thead><tr><th>Firmware</th><th>Route</th><th>Result</th><th>Periodicity (ch0 | ch1)</th><th>48 kHz signature</th><th>FFmpeg control</th></tr></thead>"
                       f"<tbody>{rows}</tbody></table></section>")

    trial_sections = []
    for t in trials:
        if t["kind"] != "ab":
            continue
        head = f"<h3>{esc(t['name'])} <span class='muted'>· {esc(label_of(group_of(t['name'])))}</span></h3>"
        cont = t.get("continuity", {})
        if not cont.get("comparable"):
            trial_sections.append(f"<section>{head}<p>{esc(t.get('error') or cont.get('reason', 'not comparable'))}</p></section>")
            continue
        st = t.get("stats", {})
        facts = (f"<p>{cont['identical_blocks']}/{cont['located_blocks']} located blocks identical ({cont['within_1lsb_blocks']} within 1 LSB, largest difference "
                 f"{cont['max_lsb_difference']} LSB; {cont['blocks'] - cont['located_blocks']} of {cont['blocks']} blocks recorded outside the direct capture) · "
                 f"{cont['splices']} splices · {cont['frames_missing']} frames "
                 f"({cont['missing_pct']:.2f}%) missing · splices/min {st.get('splices_per_min', 0):.0f} · run lengths (buffers: count) {esc(st.get('run_lengths', {}))} · "
                 f"jump at a splice {st.get('splice_jump_x_typical', 0):.1f}× the typical step</p>"
                 f"<p>FFmpeg recording: <b>{esc(t['ffmpeg']['classification'])}</b> ({esc(' | '.join(t['ffmpeg']['periodicity']))})<br>"
                 f"Direct capture: <b>{esc(t['direct']['classification'])}</b> ({esc(' | '.join(t['direct']['periodicity']))})"
                 + (f"<br>Direct-capture stream warnings: {esc(t['direct_statuses'])}" if t.get("direct_statuses") else "") + "</p>")
        try:
            plots = trial_plots(t)
        except Exception as exc:
            plots = f"<p>plots failed: {esc(exc)}</p>"
        trial_sections.append(f"<section>{head}{facts}{plots}</section>")

    env = results.get("env", {})
    doc = f"""<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width, initial-scale=1'>
<title>XVF3800 Validation Report</title><style>
body{{font-family:Inter,system-ui,-apple-system,Segoe UI,sans-serif;max-width:1180px;margin:32px auto;padding:0 16px;color:#17202a;background:#f6f8fa}}
section{{background:white;border:1px solid #d8dee4;border-radius:10px;padding:18px;margin:18px 0;overflow-x:auto}}
table{{border-collapse:collapse;width:100%;font-size:13px}}th,td{{border-bottom:1px solid #e5e7eb;padding:6px;text-align:left;vertical-align:top}}th{{background:#f3f4f6}}
img{{max-width:100%;display:block;margin:12px auto}}.muted{{color:#57606a;font-weight:normal}}code{{background:#eff1f3;padding:2px 4px;border-radius:4px}}
</style></head><body>
<h1>XVF3800 host capture path validation</h1>
<p class='muted'>Run {esc(results.get('started'))} · xvfdiag {esc(d.APP_VERSION)} · {esc(env.get('platform'))} · Python {esc(env.get('python'))}<br>
Homebrew FFmpeg: {esc((env.get('system_ffmpeg') or {}).get('version'))}<br>
FFmpeg built from source: {esc((stages.get('ffmpeg') or {}).get('version', '–'))} (commit <code>{esc((stages.get('ffmpeg') or {}).get('commit', '–'))}</code>)<br>
Board firmware before / after: {esc(results.get('firmware_before') or 'n/a')} / {esc(results.get('firmware_after') or 'n/a')} · playback: {esc(stages.get('hostpath', {}).get('playback_device', '–'))}</p>
<section><h2>Summary</h2>{verdict(trials) or '<p>No FFmpeg-vs-direct trials in this run.</p>'}
<table><thead><tr><th>Stage</th><th></th><th>Result</th></tr></thead><tbody>{stage_rows}</tbody></table></section>
<section><h2>FFmpeg vs direct capture: statistics across trials</h2><p>Each trial records the same input through FFmpeg/AVFoundation and directly through PortAudio/CoreAudio at the same time,
then locates every 512-frame FFmpeg block in the direct capture.</p>{group_table(trials) or '<p>–</p>'}</section>
<section><h2><code>ffmpeg -t N</code> alone: the original report's method</h2><p>A capture without gaps holds exactly N seconds of audio.</p>{duration_chart(trials) or '<p>–</p>'}</section>
{matrix_html}
<h2>Trials</h2>{''.join(trial_sections)}
{('<section><h2>Notes</h2><ul>' + ''.join(f'<li>{esc(n)}</li>' for n in results['notes']) + '</ul></section>') if results.get('notes') else ''}
{('<section><h2>Errors</h2><ul>' + ''.join(f'<li>{esc(e)}</li>' for e in results['errors']) + '</ul></section>') if results.get('errors') else ''}
</body></html>"""
    path = out / "report.html"
    path.write_text(doc, encoding="utf-8")
    return path

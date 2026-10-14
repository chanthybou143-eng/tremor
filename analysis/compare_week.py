"""Unit 1 vs AEMO 4 s frequency over a week, split old firmware / new firmware (fw-resilience, 2dc6afb).
Same method as compare_aemo.py (Sep 2026): Unit 1's 1 s readings are time-weighted onto each AEMO 4 s
interval (T-4, T] by GPS time (NEM = UTC+10); only intervals with >= 90 % Unit 1 coverage and AEMO quality
flag 1 are compared; readings outside 49.5-50.5 Hz or with amplitude < 0.1 V (plugpack out) are dropped.
Lag = shift applied to Unit 1 timestamps (+ = Unit 1 moved LATER) that maximises the correlation of the
high-passed (5-min mean removed) series, scanned over +-10 s in 0.25 s steps with parabolic refinement.

    python3 compare_week.py fpp [START END]      # reference: NEMweb FPP SA1 (aemo_fpp/)
    python3 compare_week.py weekly [START END]   # reference: AEMO Weekly Frequency Report xlsb (aemo/), pyxlsb
START/END are NEM dates (default 2026-10-05 2026-10-13). Old firmware = every boot that is not the new one,
a bench boot or a known-bad boot; new firmware = boot 597ea8ff32626c7b (its first 10 min skipped).
Read-only: inputs unit1_raw/unit1_*.json (UTC-day and NEM-day files, de-duplicated by boot+seq) + the
reference; outputs out/week_<ref>_<START>_<END>.json and unit1_minus_aemo_<ref>_<START>_<END>.png.
"""
import csv, glob, io, json, os, sys, zipfile
from datetime import datetime, timedelta, timezone
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

REF = sys.argv[1] if len(sys.argv) > 1 else "fpp"
START, END = (sys.argv[2], sys.argv[3]) if len(sys.argv) > 3 else ("2026-10-05", "2026-10-13")
NEM = timezone(timedelta(hours=10))
T0 = datetime.strptime(START, "%Y-%m-%d").replace(tzinfo=NEM).timestamp()
T1 = datetime.strptime(END, "%Y-%m-%d").replace(tzinfo=NEM).timestamp()
TAG = f"{REF}_{START}_{END}"
BENCH_BOOTS = {"00eaaddc5a9d13dd", "23eea7c98e340016", "7e54dfef72b7ec2b", "04c1aae33b75c717",
               "4637d32d1f6548b7", "a5cd48446f7198f4"}
EXCLUDE_BOOTS = {"398474c3bef237a1"}           # known-bad boot (26 Sep)
STEP, MIN_COVER, SANE, AMP_MIN = 4.0, 0.9, (49.5, 50.5), 0.1
OLD_BOOT, NEW_BOOT = "3bf8ae13fd84b5d0", "597ea8ff32626c7b"
os.makedirs("out", exist_ok=True)


# ---------------- reference ----------------
def load_fpp():
    pts = {}
    def eat(raw):
        for row in csv.reader(io.TextIOWrapper(io.BytesIO(raw), encoding="ascii")):
            if row[:3] == ["D", "FPP", "REGION_FREQ_MEASURE"] and row[6] == "SA1":
                t = datetime.strptime(row[5], "%Y/%m/%d %H:%M:%S").replace(tzinfo=NEM).timestamp()
                pts[t] = (50.0 + float(row[8]), row[9] == "1")
    for day in sorted(glob.glob("aemo_fpp/PUBLIC_FPP_RUN_2026????.zip")):
        with zipfile.ZipFile(day) as z:
            for inner in z.namelist():
                with zipfile.ZipFile(io.BytesIO(z.read(inner))) as zi:
                    for n in zi.namelist():
                        eat(zi.read(n))
    for f in sorted(glob.glob("aemo_fpp/current/PUBLIC_FPP_RUN_*.zip")):
        with zipfile.ZipFile(f) as zi:
            for n in zi.namelist():
                eat(zi.read(n))
    return pts, "AEMO FPP SA1 (FREQ_DEVIATION_HZ + 50, 4 s)"


def load_weekly():
    """FrequencyData sheet: row 3 = MEASUREMENTTIME (Excel serial, NEM), MEASUREMENT (Hz), Bad Data Quality
    (1 = excluded), then counters; data from row 4. Cached in out/weekly_<file>.npz (parsing is slow)."""
    import numpy as _np
    from pyxlsb import open_workbook
    pts = {}
    for f in sorted(glob.glob("aemo/*.xlsb")):
        cache = f"out/weekly_{os.path.basename(f)}.npz"
        if os.path.exists(cache) and os.path.getmtime(cache) > os.path.getmtime(f):
            z = _np.load(cache)
            ts, fs, qs = z["t"], z["f"], z["q"]
        else:
            ts, fs, qs = [], [], []
            with open_workbook(f) as wb, wb.get_sheet("FrequencyData") as sh:
                for row in sh.rows():
                    v = [c.v for c in row[:3]]
                    if not isinstance(v[0], float) or not isinstance(v[1], float):
                        continue                       # headers, blanks
                    sec = round((v[0] - 25569.0) * 86400.0)          # Excel serial -> seconds, NEM wall clock
                    ts.append(sec - 10 * 3600); fs.append(v[1]); qs.append(0 if v[2] in (None, 0, 0.0) else 1)
            ts, fs, qs = _np.array(ts, float), _np.array(fs), _np.array(qs)
            _np.savez(cache, t=ts, f=fs, q=qs)
            print(f"  parsed {f}: {len(ts)} points")
        for t, fr, q in zip(ts, fs, qs):
            pts[float(t)] = (float(fr), q == 0)
    return pts, "AEMO Weekly Frequency Report, mainland 4 s (NSW)"


pts, REF_LABEL = load_fpp() if REF == "fpp" else load_weekly()
at = np.array(sorted(t for t in pts if T0 < t <= T1))
af = np.array([pts[t][0] for t in at]); good_a = np.array([pts[t][1] for t in at])
steps = np.diff(at)
print(f"{REF_LABEL}: {len(at)} points {datetime.fromtimestamp(at[0], NEM)} .. {datetime.fromtimestamp(at[-1], NEM)} NEM, "
      f"quality-1 {good_a.mean():.4f}, steps {dict(zip(*np.unique(steps, return_counts=True)))}")

# ---------------- Unit 1 ----------------
rows, seen = [], set()
for f in sorted(glob.glob("unit1_raw/unit1_*.json")):
    for r in json.load(open(f)):
        k = (r["boot_id"], r["seq"], r["t"])
        if k not in seen and r["t"] is not None and T0 - 60 < r["t"] <= T1 + 60:
            seen.add(k); rows.append(r)
by_boot = {}
for r in rows:
    by_boot[r["boot_id"]] = by_boot.get(r["boot_id"], 0) + 1
rows = [r for r in rows if r["t"] is not None and r["gps_locked"]]
rows.sort(key=lambda r: r["t"])
ut = np.array([r["t"] for r in rows]); uf = np.array([r["freq_hz"] for r in rows])
ua = np.array([r["amplitude_v"] if r["amplitude_v"] is not None else 0.0 for r in rows])
boot = np.array([r["boot_id"] for r in rows])
bad = (uf < SANE[0]) | (uf > SANE[1]) | (ua < AMP_MIN)
prev = np.r_[-np.inf, ut[:-1]]
same_boot = np.r_[False, boot[1:] == boot[:-1]]
gap = ut - prev
us = np.where(same_boot & (gap > 0.5) & (gap < 1.5), prev, ut - 1.0)
old_mask = ~np.isin(boot, list(BENCH_BOOTS | EXCLUDE_BOOTS | {NEW_BOOT}))
FLASH_T = ut[boot == NEW_BOOT].min() if (boot == NEW_BOOT).any() else None
OLD_END = ut[old_mask].max() if old_mask.any() else None
print(f"Unit 1: {len(ut)} timed readings, dropped {bad.sum()} (glitch/unplugged); boots: "
      + ", ".join(f"{b}={n}" for b, n in sorted(by_boot.items(), key=lambda kv: -kv[1])))
print(f"  old firmware boots {sorted(set(boot[old_mask]))}, last reading "
      f"{datetime.fromtimestamp(OLD_END, NEM) if OLD_END else None} NEM; new firmware {NEW_BOOT} first reading "
      f"{datetime.fromtimestamp(FLASH_T, NEM) if FLASH_T else None} NEM")


def bin_unit(sel, shift=0.0):
    """Time-weighted mean of the selected Unit 1 readings over each AEMO interval (T-4, T]."""
    s, e, f = us[sel] + shift, ut[sel] + shift, uf[sel]
    num = np.zeros(len(at)); cov = np.zeros(len(at))
    k0 = np.searchsorted(at, s, side="left"); k1 = np.searchsorted(at, e, side="left")
    for kk_all, first in ((k0, True), (k1, False)):
        ok = (kk_all < len(at)) & (True if first else (k1 != k0))
        kk = kk_all[ok]
        lo = np.maximum(s[ok], at[kk] - STEP); hi = np.minimum(e[ok], at[kk])
        w = np.clip(hi - lo, 0, None)
        np.add.at(num, kk, w * f[ok]); np.add.at(cov, kk, w)
    with np.errstate(invalid="ignore", divide="ignore"):
        return num / cov, cov / STEP


def hp(x, idx, w=75):
    y = np.where(idx, x, np.nan); k = np.ones(w)
    return y - np.convolve(np.nan_to_num(y), k, "same") / np.convolve(idx.astype(float), k, "same")


def analyse(name, sel, window):
    lo, hi = window
    inwin = (at > lo) & (at <= hi)
    um, cov = bin_unit(sel)
    use = (cov >= MIN_COVER) & good_a & inwin
    d = (um - af)[use]
    lags = np.arange(-10, 10.001, 0.25); cs = []
    for L in lags:
        m, c = bin_unit(sel, L)
        ok = (c >= MIN_COVER) & good_a & inwin
        a_h, u_h = hp(af, ok), hp(m, ok); ok2 = ok & np.isfinite(a_h) & np.isfinite(u_h)
        cs.append(np.corrcoef(u_h[ok2], a_h[ok2])[0, 1])
    cs = np.array(cs); i = int(np.argmax(cs))
    lag = lags[i]
    if 0 < i < len(cs) - 1:
        y0, y1, y2 = cs[i-1:i+2]; lag += 0.5 * (y0 - y2) / (y0 - 2*y1 + y2) * 0.25
    mb, cb = bin_unit(sel, lag); ub = (cb >= MIN_COVER) & good_a & inwin; db = (mb - af)[ub]
    mad = lambda x: np.median(np.abs(x - np.median(x)))
    res = dict(window_nem=[str(datetime.fromtimestamp(lo, NEM)), str(datetime.fromtimestamp(hi, NEM))],
               aemo_intervals=int(inwin.sum()), matched=int(use.sum()),
               coverage_pct=round(100 * use.sum() / max(1, (inwin & good_a).sum()), 2),
               corr=round(float(np.corrcoef(um[use], af[use])[0, 1]), 4),
               mean_mHz=round(d.mean() * 1e3, 2), median_mHz=round(np.median(d) * 1e3, 2),
               std_mHz=round(d.std(ddof=1) * 1e3, 2), mad_mHz=round(mad(d) * 1e3, 2),
               lag_s=round(float(lag), 2), corr_hp_peak=round(float(cs[i]), 4),
               corr_at_lag=round(float(np.corrcoef(mb[ub], af[ub])[0, 1]), 4),
               mean_mHz_at_lag=round(db.mean() * 1e3, 2), std_mHz_at_lag=round(db.std(ddof=1) * 1e3, 2),
               mad_mHz_at_lag=round(mad(db) * 1e3, 2))
    print(f"\n[{name}] " + json.dumps(res, indent=1))
    return res, um, use


old_sel = old_mask & ~bad
new_sel = (boot == NEW_BOOT) & ~bad
segments = []
if old_sel.any():
    segments.append(("old firmware", old_sel, (T0, min(OLD_END, T1)), "#eb6834"))
if new_sel.any() and FLASH_T + 600 < T1:
    segments.append(("new firmware (2dc6afb)", new_sel, (FLASH_T + 600, T1), "#2a78d6"))
results, curves = {}, []
for name, sel, win, col in segments:
    res, um, use = analyse(name, sel, win)
    results[name] = res
    curves.append((um, use, col, name, res))
json.dump({"reference": REF_LABEL, "window_nem": [START, END], **results}, open(f"out/week_{TAG}.json", "w"), indent=1)

# ---------------- chart: difference over the week ----------------
C_OLD, C_NEW, INK, INK2, GRID = "#eb6834", "#2a78d6", "#0b0b0b", "#52514e", "#e4e3df"
plt.rcParams.update({"font.size": 10, "axes.edgecolor": INK2, "axes.labelcolor": INK2, "xtick.color": INK2,
                     "ytick.color": INK2, "axes.spines.top": False, "axes.spines.right": False,
                     "axes.grid": True, "grid.color": GRID, "grid.linewidth": 0.6})
fig, ax = plt.subplots(figsize=(13, 5))
days = (at - T0) / 86400
for um, use, col, lab, res in curves:
    dd = np.where(use, (um - af) * 1e3, np.nan)
    ax.plot(days, dd, ",", color=col, alpha=0.25)
    k = 225                                   # 15-min rolling mean
    num = np.convolve(np.nan_to_num(dd), np.ones(k), "same"); den = np.convolve(np.isfinite(dd).astype(float), np.ones(k), "same")
    with np.errstate(invalid="ignore", divide="ignore"):
        rm = np.where(den > k * 0.5, num / den, np.nan)
    ax.plot(days, rm, color=col, lw=1.6, label=f"{lab}: mean {res['mean_mHz']:+.1f} mHz, std {res['std_mHz']:.1f}, lag {res['lag_s']:+.2f} s")
if FLASH_T is not None and T0 < FLASH_T < T1:
    fx = (FLASH_T - T0) / 86400
    ax.axvline(fx, color=INK, lw=1, ls="--")
    ax.text(fx + 0.02, 12, "flash 2dc6afb\n" + datetime.fromtimestamp(FLASH_T, NEM).strftime("%d %b %H:%M NEM"),
            fontsize=9, color=INK)
ax.axhline(0, color=INK2, lw=0.8)
ax.set_ylim(-15, 15); ax.set_xlim(0, (T1 - T0) / 86400)
ND = int(round((T1 - T0) / 86400))
ax.set_xticks(range(ND + 1))
ax.set_xticklabels([(datetime.strptime(START, "%Y-%m-%d") + timedelta(days=i)).strftime("%a %d %b") for i in range(ND + 1)])
ax.set_ylabel("Unit 1 − AEMO (mHz), 4 s intervals as stamped")
ax.set_title(f"Unit 1 − {REF_LABEL}, {START} → {END} NEM (dots: 4 s; lines: 15-min mean)", loc="left", color=INK)
ax.legend(frameon=False, loc="lower left", fontsize=9)
fig.tight_layout(); out = f"unit1_minus_aemo_{TAG}.png"; fig.savefig(out, dpi=150); plt.close(fig)
print("chart:", os.path.abspath(out))

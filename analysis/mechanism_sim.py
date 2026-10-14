"""Does the OLD firmware's soft-timer sampling explain a ~-1.3 mHz frequency bias? Synthetic, deterministic.

A perfect 50.000 Hz sine goes through two sampling models and the REAL estimator (tremor-merge
chunk_summary.summarize_chunk); the mean reading error is the bias each model alone produces.

OLD (soft timer, batch close) -- MicroPython v1.27 rp2 facts:
  * the hardware alarm fires every 971 us (1030 Hz; alarm-pool period in whole us);
  * a SOFT machine.Timer callback is queued with mp_sched_schedule (scheduler depth 8 on rp2, py/scheduler.c:
    a full queue silently drops the call; duplicates allowed) and runs only between bytecodes;
  * while the main thread is inside a long C call (TLS crypto in a POST, gc.collect, json.dumps) no bytecode
    runs: the first 8 alarms of the stall are queued, later ones are DROPPED; when the VM resumes the queued
    callbacks run back to back (~15 us each), each stamping ticks_us() and reading the ADC AT RUN TIME;
  * outside stalls a callback runs 0-30 us late (bytecode granularity), stamped and sampled at run time;
  * the main loop drains <= 128 samples per pass and checks "chunk >= 1 s?" only after each batch; after a
    chunk closes, summarize_chunk keeps it busy ~150 ms (callbacks still run), so a backlog builds.
NEW (hard timer, per-sample close): stamped at the alarm (+0-5 us ISR entry), never dropped, and the real
  adc_chunker.ChunkBuilder closes each chunk at exactly 1 s.
Signal: 1.65 V + 0.75 V sine, 12-bit ADC scaled to u16, 1 mV rms noise; float32 buffers like the device.
Stall statistics on the old firmware were never measured, so they are swept, not assumed.
"""
import array, math, random, sys, time
import numpy as np

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))   # repo root
from chunk_summary import summarize_chunk                      # noqa: E402  the device's estimator
from adc_chunker import ChunkBuilder                           # noqa: E402  the new chunk logic

F0, PERIOD_US, DEPTH, CAP = 50.000, 971, 8, 1200
ADC_SCALE = 3.3 / 65535


def signal(t_us, rng, phase):
    v = 1.65 + 0.75 * np.sin(2 * np.pi * F0 * t_us * 1e-6 + phase) + rng.normal(0, 0.001, len(t_us))
    return (np.clip(np.round(v / 3.3 * 4095), 0, 4095) * 16).astype(np.int64)       # 12-bit -> u16


def stalls(total_us, post_stall_ms, gc_ms, gc_every_s, rng):
    """[(start, end)] main-thread stalls: one POST stall every 30 s (split into a few crypto pieces with VM
    gaps between, as a TLS handshake alternates computation and socket waits), plus periodic GC pauses."""
    out = []
    t = 5e6
    while t < total_us and post_stall_ms > 0:
        left, s = post_stall_ms * 1000, t
        while left > 0:
            piece = min(left, rng.uniform(100e3, 400e3))
            out.append((s, s + piece)); left -= piece; s += piece + rng.uniform(20e3, 200e3)
        t += 30e6
    t = rng.uniform(0, gc_every_s * 1e6)
    while t < total_us and gc_ms > 0:
        out.append((t, t + gc_ms * 1000)); t += gc_every_s * 1e6 * rng.uniform(0.7, 1.3)
    out.sort()
    return out


def old_samples(total_us, st, rng):
    """(stamp_us, run_us) of every sample the OLD soft timer actually takes."""
    alarms = np.arange(0, total_us, PERIOD_US, dtype=np.int64)
    run = alarms + rng.integers(0, 31, len(alarms))
    keep = np.ones(len(alarms), bool)
    for s, e in st:
        k0, k1 = np.searchsorted(alarms, s), np.searchsorted(alarms, e)
        if k1 <= k0:
            continue
        q = min(DEPTH, k1 - k0)
        run[k0:k0 + q] = int(e) + 15 * np.arange(1, q + 1)       # queued: run late, back to back
        keep[k0 + q:k1] = False                                   # queue full: dropped
    return run[keep]


def old_chunks(stamps, values, rng):
    """Batch-close chunking: drain <= 128 available samples per pass, check after each batch."""
    out, i, n, now = [], 0, len(stamps), stamps[0]
    while i < n:
        start = i
        while True:
            now += rng.uniform(2e3, 20e3)                         # one main-loop pass
            avail = np.searchsorted(stamps, now, side="right")
            j = min(avail, i + 128)
            if j <= i:
                continue
            i = j
            if stamps[i - 1] - stamps[start] >= 1_000_000 or i - start >= CAP:
                break
            if i >= n:
                return out
        out.append((start, i))
        now += 150e3                                               # summarize_chunk busy
    return out


def new_chunks(stamps):
    cb = ChunkBuilder(CAP, 1_000_000, "f", lambda a, b: a - b, int(stamps[0]))
    ring_t = array.array("q", stamps.tolist()); ring_v = array.array("H", [0] * len(stamps))
    out, r, n = [], 0, len(stamps)
    while r < n:
        start = r
        r = cb.drain(ring_t, ring_v, r, n, n + 1, 128)
        if cb.complete:
            out.append((r - cb.n, r)); cb.reset()
    return out


def bias(stamps, values, chunks):
    errs = []
    ts = array.array("f", [0.0] * CAP); vs = array.array("f", [0.0] * CAP); buf = array.array("f", [0.0] * CAP)
    for a, b in chunks:
        n = b - a
        if n < 50:
            continue
        t0 = stamps[a]
        for k in range(n):
            ts[k] = (stamps[a + k] - t0) * 1e-6
            vs[k] = values[a + k] * ADC_SCALE
        try:
            f, _amp = summarize_chunk(ts, vs, n=n, filtered_buf=buf)
        except ValueError:
            continue
        errs.append(f - F0)
    e = np.array(errs) * 1e3
    return dict(chunks=len(e), mean_mHz=round(e.mean(), 3), median_mHz=round(float(np.median(e)), 3),
                std_mHz=round(e.std(), 2), sem_mHz=round(e.std() / math.sqrt(len(e)), 3))


def ideal_stamps(total, seed):
    r = np.random.default_rng(seed)
    return np.arange(0, total, PERIOD_US, dtype=np.int64) + r.integers(0, 6, len(range(0, total, PERIOD_US)))


def run(label, minutes, post_ms, gc_ms, gc_every, seed=1, split=False):
    rng = random.Random(seed); nrng = np.random.default_rng(seed)
    total = int(minutes * 60e6)
    st = stalls(total, post_ms, gc_ms, gc_every, rng)
    stamps = old_samples(total, st, nrng)
    vals = signal(stamps, np.random.default_rng(seed + 100), 0.3)
    old = bias(stamps, vals, old_chunks(stamps, None, random.Random(seed + 7)))
    print(f"{label:48s} OLD  {old}", flush=True)
    if split:
        a = bias(stamps, vals, new_chunks(stamps))
        print(f"{'':48s}   old timer + per-sample close {a}", flush=True)
        nst = ideal_stamps(total, 999)
        nv = signal(nst, np.random.default_rng(1000), 0.3)
        b = bias(nst, nv, old_chunks(nst, None, random.Random(seed + 7)))
        print(f"{'':48s}   hard timer + batch close      {b}", flush=True)
    return old


if __name__ == "__main__":
    t0 = time.time()
    MIN = float(sys.argv[1]) if len(sys.argv) > 1 else 30
    print(f"{MIN:g} simulated minutes per scenario (~{int(MIN * 60)} chunks); OLD = soft timer + batch close\n")
    nst = ideal_stamps(int(MIN * 60e6), 999)                      # NEW: identical in every scenario
    new = bias(nst, signal(nst, np.random.default_rng(1000), 0.3), new_chunks(nst))
    print(f"{'NEW (hard timer + ChunkBuilder), any scenario':48s} NEW  {new}\n", flush=True)
    run("no stalls (soft-timer jitter + batch close)", MIN, 0, 0, 1, split=True)
    for post in (300, 600, 1000):
        run(f"POST stall {post} ms / 30 s", MIN, post, 0, 1)
    for gc, every in ((10, 3), (10, 1), (20, 1), (50, 1)):
        run(f"GC pause {gc} ms every ~{every} s", MIN, 0, gc, every)
    run("POST 600 ms/30 s + GC 20 ms every ~1 s", MIN, 600, 20, 1, split=True)
    run("POST 1000 ms/30 s + GC 50 ms every ~1 s (worst)", MIN, 1000, 50, 1)
    print(f"\n({time.time() - t0:.0f} s)")

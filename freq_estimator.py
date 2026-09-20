"""
TREMOR - Grid Frequency Estimator
===================================
Zero-crossing interpolation frequency estimator, validated against
synthetic 50 Hz mains data before being ported to MicroPython on the
Pico 2WH.

Why zero-crossing interpolation:
- Cheap enough to run on a Pico in real time (no FFT needed)
- Naturally gives sub-sample timing resolution via linear interpolation
- Well understood, standard technique for mains frequency/RoCoF estimation

This module is pure Python (no numpy) so it can be copy-pasted into
MicroPython later with minimal changes. Only this test harness uses
extra libraries.

The naive version (no hysteresis, no pre-filter) breaks under noise: a
single noisy sample near the zero crossing registers as its own spurious
crossing, which corrupts frequency estimates by orders of magnitude (see
the ablation in run_validation()). The fix is two-layered: a moving-average
pre-filter knocks down high-frequency noise before detection, and a
Schmitt-trigger-style hysteresis band stops a crossing from re-arming
until the signal has swung back past a margin on the other side of zero.
"""

import math
import random


# ---------------------------------------------------------------------------
# 1. Synthetic signal generator
# ---------------------------------------------------------------------------

def generate_synthetic_signal(freq_hz=50.0, fs_hz=4000.0, duration_s=2.0,
                               amplitude=1.0, dc_offset=0.0,
                               noise_std=0.0, freq_ramp_hz_per_s=0.0,
                               seed=None, rng=None):
    """
    Generate a synthetic sampled sine wave standing in for the divided-down
    mains signal at the Pico's ADC pin.

    freq_ramp_hz_per_s lets us simulate RoCoF events later (frequency
    drifting linearly over time) even though this module only estimates
    instantaneous frequency for now.

    rng: an optional random.Random instance to draw noise from. If not
    given, a private Random(seed) is created instead of touching the
    global random module -- calling this from multiple threads (e.g. one
    per simulated unit) concurrently would otherwise race on shared global
    state, letting one caller's random.seed() reset another's in-flight
    sequence out from under it. Pass your own long-lived rng if you want a
    continuous (not re-seeded) stream across repeated calls.

    Returns (timestamps, samples) as plain lists.
    """
    if rng is None:
        rng = random.Random(seed)

    n_samples = int(fs_hz * duration_s)
    dt = 1.0 / fs_hz

    timestamps = []
    samples = []
    phase = 0.0

    for i in range(n_samples):
        t = i * dt
        instantaneous_freq = freq_hz + freq_ramp_hz_per_s * t
        # integrate phase so ramped frequency is handled correctly
        if i == 0:
            phase = 0.0
        else:
            phase += 2 * math.pi * instantaneous_freq * dt

        value = amplitude * math.sin(phase) + dc_offset
        if noise_std > 0.0:
            value += rng.gauss(0.0, noise_std)

        timestamps.append(t)
        samples.append(value)

    return timestamps, samples


# ---------------------------------------------------------------------------
# 2. Simple moving-average pre-filter (noise reduction before crossing detect)
# ---------------------------------------------------------------------------

def moving_average(samples, window, n=None, out=None):
    """
    Simple boxcar low-pass filter. Cheap enough for MicroPython (just a
    running sum), and enough to knock down high-frequency ADC noise before
    zero-crossing detection. window=1 is a no-op (returns samples unchanged).

    window is a sample count, not a time duration -- see
    estimate_frequency()'s filter_window_s for why that matters once this
    runs at a sample rate other than the one it was tuned at.

    Note: a rectangular window has exact linear phase, so this delays the
    signal by a constant (window-1)/2 samples regardless of frequency
    content -- fine for frequency estimation, since a constant delay
    cancels out of the *difference* between two crossing times, but keep
    it in mind if timestamps ever need to line up with raw samples.

    n/out (both optional, default None): a caller that wants to avoid
    allocating a fresh ~chunk-length list every call (see
    chunk_summary.py's summarize_chunk and its own n/filtered_buf
    parameters) passes the valid length of `samples` as `n` (it may be a
    longer, oversized buffer with stale data past index n-1, never read
    here) and a reusable list at least `n` long as `out`, which is filled
    by index instead of grown via append and then returned. Default
    behaviour (n=None, out=None) is unchanged: n is taken from
    len(samples) and a fresh list is allocated and returned, exactly as
    before these parameters existed.
    """
    if n is None:
        n = len(samples)

    if window <= 1:
        if out is None:
            return list(samples)[:n] if n != len(samples) else list(samples)
        for i in range(n):
            out[i] = samples[i]
        return out

    acc = 0.0
    buf = []
    if out is None:
        out = []
        for i in range(n):
            x = samples[i]
            buf.append(x)
            acc += x
            if len(buf) > window:
                acc -= buf.pop(0)
            out.append(acc / len(buf))
        return out

    for i in range(n):
        x = samples[i]
        buf.append(x)
        acc += x
        if len(buf) > window:
            acc -= buf.pop(0)
        out[i] = acc / len(buf)
    return out


# ---------------------------------------------------------------------------
# 3. Zero-crossing detection with linear interpolation
# ---------------------------------------------------------------------------

def find_zero_crossings(timestamps, samples, dc_offset=0.0, rising_only=True,
                         hysteresis=0.0):
    """
    Find interpolated zero-crossing times (relative to dc_offset).

    Uses linear interpolation between the two samples that bracket each
    crossing to get sub-sample timing resolution:

        t_cross = t0 + (0 - y0) * (t1 - t0) / (y1 - y0)

    where y0/y1 are the samples already shifted by dc_offset.

    hysteresis (Schmitt-trigger style): the signal must first drop below
    -hysteresis before a rising crossing through 0 is armed again. Without
    this, noise sitting right at the zero point causes multiple spurious
    crossings per real cycle - this is what broke the naive version under
    heavy noise. hysteresis=0.0 reproduces the naive (unprotected) behaviour
    -- the re-arm threshold becomes "< 0", which any noise-induced dip below
    zero satisfies immediately, so it's not actually a special case in the
    code, just a degenerate hysteresis band.
    """
    crossings = []
    prev_t = timestamps[0]
    prev_y = samples[0] - dc_offset
    armed = True  # ready to detect a rising crossing

    for i in range(1, len(samples)):
        t = timestamps[i]
        y = samples[i] - dc_offset

        if y < -hysteresis:
            armed = True

        crossed_rising = armed and prev_y < 0.0 <= y
        crossed_falling = prev_y >= 0.0 > y and not rising_only

        if crossed_rising or crossed_falling:
            if y != prev_y:
                t_cross = prev_t + (0.0 - prev_y) * (t - prev_t) / (y - prev_y)
            else:
                t_cross = t
            crossings.append(t_cross)
            if crossed_rising:
                armed = False  # don't re-trigger until we dip back below -hysteresis

        prev_t, prev_y = t, y

    return crossings


# ---------------------------------------------------------------------------
# 4. Frequency from crossing intervals
# ---------------------------------------------------------------------------

def frequency_from_crossings(crossings):
    """
    Convert a list of (rising) zero-crossing times into per-cycle frequency
    estimates. Each consecutive pair of rising crossings = one mains cycle.

    Returns list of (t_mid, freq_hz) so later we can plot / average / feed
    into a RoCoF calc.
    """
    estimates = []
    for i in range(1, len(crossings)):
        period = crossings[i] - crossings[i - 1]
        if period <= 0:
            continue
        freq = 1.0 / period
        t_mid = 0.5 * (crossings[i] + crossings[i - 1])
        estimates.append((t_mid, freq))
    return estimates


def estimate_frequency(timestamps, samples, dc_offset=0.0, hysteresis=0.05,
                        filter_window_s=0.0):
    """
    Convenience wrapper: samples in -> single averaged frequency estimate
    out, plus the per-cycle estimates for inspection.

    hysteresis default of 0.05 (5% of a unit-amplitude signal) guards
    against noise-induced spurious crossings. Scale this to your real
    signal amplitude once hardware is wired up -- unlike filter_window_s,
    this is a voltage-domain threshold, not a time-domain one, so it does
    NOT need retuning if the sample rate changes.

    filter_window_s applies a moving-average pre-filter (see
    moving_average()) before crossing detection, expressed as a *duration*
    in seconds rather than a sample count: a fixed sample count represents
    a different real-world smoothing time at every sample rate (5 samples
    is 1.25 ms at 4 kHz but 12.5 ms -- more than half a mains half-cycle --
    at 400 Hz), which would silently over- or under-filter if this ever
    runs at a different ADC rate than it was tuned at. filter_window_s=0.0
    disables filtering. Sample rate is inferred from consecutive
    timestamps (assumed uniformly spaced).

    Both knobs will need re-tuning against real ADC data once hardware
    arrives; the values here are starting points, not final calibration.

    Raises ValueError if fewer than 2 crossings are found (e.g. hysteresis
    set larger than the signal amplitude) -- silently returning a None
    frequency is a trap for any caller that goes on to do arithmetic with
    it.
    """
    if filter_window_s > 0.0:
        sample_rate_hz = 1.0 / (timestamps[1] - timestamps[0])
        window_samples = max(1, round(filter_window_s * sample_rate_hz))
        filtered = moving_average(samples, window_samples)
    else:
        filtered = samples

    crossings = find_zero_crossings(timestamps, filtered, dc_offset=dc_offset,
                                     hysteresis=hysteresis)
    per_cycle = frequency_from_crossings(crossings)
    if not per_cycle:
        raise ValueError(
            "need at least 2 zero crossings to estimate frequency, "
            f"found {len(crossings)} (hysteresis/filter_window_s may be "
            "misconfigured for this signal's amplitude)"
        )
    mean_freq = sum(f for _, f in per_cycle) / len(per_cycle)
    return mean_freq, per_cycle


# ---------------------------------------------------------------------------
# 5. Validation harness
# ---------------------------------------------------------------------------

def _rmse(estimates, true_freq):
    errs = [(f - true_freq) ** 2 for _, f in estimates]
    return math.sqrt(sum(errs) / len(errs)) if errs else float("nan")


def run_validation():
    print("=" * 70)
    print("TREMOR frequency estimator — validation against synthetic data")
    print("=" * 70)

    # tolerance_mhz: 5 mHz for clean/realistic cases. The "heavy noise" case
    # (5% RMS noise on the signal) is deliberately worse than the real divider
    # circuit should ever produce - it's a stress test of the algorithm's
    # failure mode, not a target operating condition, so it gets a looser
    # 20 mHz bar. Real tolerance will be re-derived once we know actual
    # ADC noise floor from hardware.
    test_cases = [
        dict(label="Clean 50.000 Hz",              freq_hz=50.000, noise_std=0.0,
             tolerance_mhz=5.0),
        dict(label="Clean 49.800 Hz",              freq_hz=49.800, noise_std=0.0,
             tolerance_mhz=5.0),
        dict(label="Clean 50.200 Hz",              freq_hz=50.200, noise_std=0.0,
             tolerance_mhz=5.0),
        dict(label="50.000 Hz + light noise",      freq_hz=50.000, noise_std=0.01,
             tolerance_mhz=5.0),
        dict(label="50.000 Hz + heavy noise (stress test)", freq_hz=50.000, noise_std=0.05,
             hysteresis=0.2, filter_window_s=0.00125, tolerance_mhz=20.0),
        dict(label="50.000 Hz + DC offset (compensated)",  freq_hz=50.000, noise_std=0.0,
             dc_offset=0.1, tolerance_mhz=5.0),
        dict(label="50.000 Hz + DC offset (uncompensated)", freq_hz=50.000, noise_std=0.0,
             dc_offset=0.1, assumed_dc=0.0, tolerance_mhz=5.0),
    ]

    fs = 4000.0  # Hz, candidate ADC sample rate on the Pico
    duration = 2.0

    all_passed = True
    for case in test_cases:
        label = case.pop("label")
        dc_offset = case.get("dc_offset", 0.0)  # actual offset baked into the signal
        # what the estimator is told to compensate by -- defaults to the
        # true offset (correct compensation); a case can override this to
        # deliberately mismatch it (see "uncompensated" above).
        assumed_dc = case.pop("assumed_dc", dc_offset)
        hysteresis = case.pop("hysteresis", 0.05)
        filter_window_s = case.pop("filter_window_s", 0.0)
        tolerance_mhz = case.pop("tolerance_mhz")
        true_freq = case["freq_hz"]

        ts, ys = generate_synthetic_signal(fs_hz=fs, duration_s=duration,
                                            seed=42, **case)
        try:
            mean_freq, per_cycle = estimate_frequency(
                ts, ys, dc_offset=assumed_dc, hysteresis=hysteresis,
                filter_window_s=filter_window_s,
            )
        except ValueError as exc:
            print(f"\n{label}")
            print(f"  status:         FAIL ({exc})")
            all_passed = False
            continue

        rmse = _rmse(per_cycle, true_freq)
        error_mhz = (mean_freq - true_freq) * 1000  # milli-Hz

        status = "PASS" if abs(error_mhz) < tolerance_mhz else "FAIL"
        all_passed &= (status == "PASS")

        print(f"\n{label}")
        print(f"  true freq:      {true_freq:.3f} Hz")
        print(f"  estimated:      {mean_freq:.4f} Hz")
        print(f"  mean error:     {error_mhz:+.2f} mHz")
        print(f"  per-cycle RMSE: {rmse * 1000:.2f} mHz")
        print(f"  cycles used:    {len(per_cycle)}")
        print(f"  status:         {status}")

    print("\n" + "=" * 70)
    print("Naive vs. hysteresis+filter under increasing noise")
    print("=" * 70)
    print("(demonstrates the failure mode this module was fixed for: a")
    print(" single noisy sample near a crossing registers as its own")
    print(" spurious cycle, which can corrupt the mean by orders of")
    print(" magnitude -- not just a few mHz)")
    for noise_std in [0.02, 0.05, 0.1, 0.2, 0.3]:
        ts, ys = generate_synthetic_signal(fs_hz=fs, duration_s=duration,
                                            noise_std=noise_std, seed=42)
        try:
            naive_freq, naive_cycles = estimate_frequency(
                ts, ys, hysteresis=0.0, filter_window_s=0.0)
            naive_report = f"{naive_freq:9.3f} Hz ({len(naive_cycles)} cycles)"
        except ValueError as exc:
            naive_report = f"FAILED ({exc})"
        fixed_freq, fixed_cycles = estimate_frequency(
            ts, ys, hysteresis=0.2, filter_window_s=0.00125)
        print(f"  noise_std={noise_std:.2f}  naive: {naive_report:35s}  "
              f"hysteresis+filter: {fixed_freq:.4f} Hz ({len(fixed_cycles)} cycles)")

    print("\n" + "=" * 70)
    print("Sample rate sweep (50 Hz + light noise) — how low can fs go?")
    print("=" * 70)
    print("(filter_window_s/hysteresis held constant in *physical* units;")
    print(" a fixed *sample-count* window would silently smear over more")
    print(" than half a mains cycle at the lowest rates here)")
    # 4000/10000 Hz bracket the realistic Pico ADC range (see CLAUDE.md);
    # the lower rates characterise margin/failure, not a target operating
    # point.
    for fs_test in [200, 400, 800, 1000, 2000, 4000, 10000]:
        ts, ys = generate_synthetic_signal(freq_hz=50.0, fs_hz=fs_test,
                                            duration_s=duration, noise_std=0.02,
                                            seed=42)
        try:
            mean_freq, per_cycle = estimate_frequency(
                ts, ys, hysteresis=0.05, filter_window_s=0.00125)
            error_mhz = (mean_freq - 50.0) * 1000
            print(f"  fs={fs_test:>6.0f} Hz  ->  estimate {mean_freq:.4f} Hz "
                  f"(error {error_mhz:+.2f} mHz, {len(per_cycle)} cycles)")
        except ValueError as exc:
            print(f"  fs={fs_test:>6.0f} Hz  ->  FAILED ({exc})")

    print("\n" + "=" * 70)
    print("OVERALL:", "ALL TESTS PASSED" if all_passed else "SOME TESTS FAILED")
    print("=" * 70)
    return all_passed


if __name__ == "__main__":
    run_validation()

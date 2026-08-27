from __future__ import annotations

import numpy as np

from tremor.signal import generate_mains_signal


def test_sample_count_and_duration():
    sig = generate_mains_signal(duration_s=2.0, sample_rate_hz=4000.0)
    assert sig.samples.shape == (8000,)
    assert sig.t.shape == (8000,)
    assert np.isclose(sig.t[-1] - sig.t[0], (8000 - 1) / 4000.0)


def test_amplitude_scaling():
    sig = generate_mains_signal(
        duration_s=1.0, sample_rate_hz=10000.0, amplitude=3.3
    )
    assert np.isclose(np.max(np.abs(sig.samples)), 3.3, atol=1e-3)


def test_harmonic_content_appears_at_expected_frequency():
    fs = 10000.0
    duration = 2.0
    sig = generate_mains_signal(
        duration_s=duration,
        sample_rate_hz=fs,
        amplitude=1.0,
        harmonics={3: 0.1},
    )
    n = sig.samples.size
    spectrum = np.abs(np.fft.rfft(sig.samples))
    freqs = np.fft.rfftfreq(n, d=1.0 / fs)

    fundamental_bin = np.argmin(np.abs(freqs - 50.0))
    third_harmonic_bin = np.argmin(np.abs(freqs - 150.0))

    ratio = spectrum[third_harmonic_bin] / spectrum[fundamental_bin]
    assert np.isclose(ratio, 0.1, rtol=0.05)


def test_noise_increases_residual_std():
    kwargs = dict(duration_s=2.0, sample_rate_hz=8000.0, amplitude=1.0)
    rng = np.random.default_rng(42)

    clean = generate_mains_signal(**kwargs)
    noisy = generate_mains_signal(noise_std=0.05, rng=rng, **kwargs)

    residual = noisy.samples - clean.samples
    assert np.isclose(np.std(residual), 0.05, rtol=0.1)


def test_true_freq_reflects_constant_offset():
    sig = generate_mains_signal(
        duration_s=1.0, sample_rate_hz=4000.0, freq_offset_hz=0.3
    )
    assert np.allclose(sig.true_freq_hz, 50.3)


def test_true_freq_reflects_callable_offset():
    def ramp(t: np.ndarray) -> np.ndarray:
        return 0.2 * t  # 0.2 Hz/s ramp

    sig = generate_mains_signal(
        duration_s=2.0, sample_rate_hz=4000.0, freq_offset_hz=ramp
    )
    assert np.allclose(sig.true_freq_hz, 50.0 + 0.2 * sig.t)

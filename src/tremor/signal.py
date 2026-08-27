"""Synthetic mains (50 Hz) signal generation for offline testing.

Produces waveforms with a controllable instantaneous frequency (constant
offset or a function of time, so ramps/steps can be modelled later for RoCoF
work), configurable harmonics and additive white noise. Phase is obtained by
integrating instantaneous frequency rather than multiplying frequency by
time, so the signal stays phase-continuous even when frequency varies.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Dict, Optional, Union

import numpy as np


@dataclass
class SyntheticSignal:
    t: np.ndarray
    samples: np.ndarray
    true_freq_hz: np.ndarray


FreqOffset = Union[float, Callable[[np.ndarray], np.ndarray]]


def generate_mains_signal(
    duration_s: float,
    sample_rate_hz: float,
    nominal_freq_hz: float = 50.0,
    freq_offset_hz: FreqOffset = 0.0,
    amplitude: float = 1.0,
    harmonics: Optional[Dict[int, float]] = None,
    noise_std: float = 0.0,
    phase0: float = 0.0,
    rng: Optional[np.random.Generator] = None,
) -> SyntheticSignal:
    """Generate a synthetic mains waveform.

    Args:
        duration_s: length of the buffer in seconds.
        sample_rate_hz: sampling rate in Hz.
        nominal_freq_hz: nominal fundamental frequency (e.g. 50.0).
        freq_offset_hz: constant offset in Hz added to the nominal
            frequency, or a callable ``f(t) -> ndarray`` giving the offset
            at each timestamp (for ramps/steps).
        amplitude: fundamental peak amplitude.
        harmonics: mapping of harmonic order -> relative amplitude (relative
            to the fundamental), e.g. ``{3: 0.05, 5: 0.02}``.
        noise_std: standard deviation of additive white Gaussian noise.
        phase0: initial phase (radians) added to every component.
        rng: numpy random generator to use for noise; a fresh
            ``default_rng()`` is created if not given.
    """
    n = int(round(duration_s * sample_rate_hz))
    t = np.arange(n) / sample_rate_hz

    if callable(freq_offset_hz):
        offset = np.asarray(freq_offset_hz(t), dtype=float)
    else:
        offset = np.full(n, float(freq_offset_hz))
    true_freq_hz = nominal_freq_hz + offset

    dt = 1.0 / sample_rate_hz
    avg_freq = (true_freq_hz[:-1] + true_freq_hz[1:]) / 2.0
    phase_cum = np.concatenate(([0.0], np.cumsum(2.0 * np.pi * avg_freq * dt)))

    samples = amplitude * np.sin(phase_cum + phase0)
    if harmonics:
        for order, rel_amp in harmonics.items():
            samples = samples + amplitude * rel_amp * np.sin(
                order * phase_cum + phase0
            )

    if noise_std > 0.0:
        rng = rng if rng is not None else np.random.default_rng()
        samples = samples + rng.normal(0.0, noise_std, size=n)

    return SyntheticSignal(t=t, samples=samples, true_freq_hz=true_freq_hz)


class MainsSignalGenerator:
    """Stateful, one-sample-at-a-time counterpart to
    ``generate_mains_signal``.

    ``generate_mains_signal`` produces a whole fixed-length buffer up front,
    which doesn't fit a live source where a disturbance can be injected at
    an arbitrary moment while streaming is already under way. This class
    holds the oscillator's phase across calls and exposes ``set_offset`` so
    a caller (e.g. a background generation thread) can change the frequency
    offset between samples; phase stays continuous either way since it's
    accumulated incrementally rather than recomputed from ``t``.
    """

    def __init__(
        self,
        sample_rate_hz: float,
        nominal_freq_hz: float = 50.0,
        amplitude: float = 1.0,
        harmonics: Optional[Dict[int, float]] = None,
        noise_std: float = 0.0,
        rng: Optional[np.random.Generator] = None,
    ):
        self.sample_rate_hz = sample_rate_hz
        self.nominal_freq_hz = nominal_freq_hz
        self.amplitude = amplitude
        self.harmonics = harmonics or {}
        self.noise_std = noise_std
        self.rng = rng if rng is not None else np.random.default_rng()

        self._t = 0.0
        self._phase = 0.0
        self._offset_hz = 0.0

    def set_offset(self, offset_hz: float) -> None:
        self._offset_hz = offset_hz

    def next_sample(self):
        """Return ``(t, sample, instantaneous_freq_hz)`` for the next
        sample, advancing internal state by one sample period."""
        dt = 1.0 / self.sample_rate_hz
        freq_hz = self.nominal_freq_hz + self._offset_hz

        t = self._t
        value = self.amplitude * np.sin(self._phase)
        for order, rel_amp in self.harmonics.items():
            value += self.amplitude * rel_amp * np.sin(order * self._phase)
        if self.noise_std > 0.0:
            value += self.rng.normal(0.0, self.noise_std)

        self._phase += 2.0 * np.pi * freq_hz * dt
        self._t += dt

        return t, value, freq_hz

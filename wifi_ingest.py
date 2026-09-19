"""Buffers per-reading summaries and ships them to TREMOR's /api/ingest
endpoint in batches, retrying on failure instead of dropping data.

Deliberately isolated from network.WLAN/urequests (see wifi_unit_client.py
for that glue) so this buffering/eviction logic -- pure Python, zero
imports beyond the stdlib -- runs and is testable under desktop CPython,
same portability reasoning as freq_estimator.py. The actual HTTP POST is
injected as a callable rather than imported directly, so tests can swap in
a fake one without needing MicroPython's network stack.
"""


class IngestBuffer:
    """A bounded FIFO of pending (frequency_hz, amplitude_v, gps_utc_s)
    readings for one unit.

    append() during normal operation; flush() attempts to POST everything
    currently buffered. A failed POST (post_fn returns falsy or raises)
    leaves the buffer untouched for the next flush() call -- same
    "retry, don't silently drop" philosophy as adc_stream_gps.py's ADC
    ring buffer (which drops and *counts* an overflow rather than losing
    data with no trace). Only a genuinely full buffer drops anything here,
    and every drop is counted in dropped_count.

    max_readings default of 600 (~10 minutes at one reading/sec) is a
    starting point, not a measured ceiling -- see the WiFi client's design
    notes on Pico 2 W heap headroom under WiFi+TLS; retune once
    gc.mem_free() has actually been checked on real hardware.
    """

    def __init__(self, unit_id, post_fn, max_readings=600):
        self.unit_id = unit_id
        self._post_fn = post_fn
        self._max_readings = max_readings
        self._buf = []  # list of (frequency_hz, amplitude_v, gps_utc_s) tuples
        self.dropped_count = 0

    def __len__(self):
        return len(self._buf)

    def append(self, frequency_hz, amplitude_v, gps_utc_s=None):
        if len(self._buf) >= self._max_readings:
            self._buf.pop(0)  # drop oldest -- keep the buffer bounded, favor recent data
            self.dropped_count += 1
        self._buf.append((frequency_hz, amplitude_v, gps_utc_s))

    def flush(self):
        """Attempt to send everything currently buffered.

        Returns True if the batch was accepted (post_fn returned truthy)
        -- the buffer is cleared. Returns False if the POST failed for any
        reason, including post_fn raising -- the buffer is left exactly as
        it was, to retry whole on the next call. A no-op (returns True)
        when the buffer is empty, so callers can call this unconditionally
        on a timer without checking len() first.
        """
        if not self._buf:
            return True
        payload = {
            "unit_id": self.unit_id,
            "readings": [
                {"frequency_hz": f, "amplitude_v": a, "gps_utc_s": g}
                for f, a, g in self._buf
            ],
        }
        try:
            ok = self._post_fn(payload)
        except Exception:
            ok = False
        if ok:
            self._buf = []
        return ok

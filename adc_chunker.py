"""Turns the ADC ring buffer's (ticks_us, raw count) samples into ~1 s chunks for chunk_summary.

Pure Python (array only), no MicroPython imports, so it runs and is tested on the desktop:
tests/test_adc_chunker.py. wifi_unit_client.py calls drain() once per main-loop pass.

Why it exists (fw-resilience bench, 2026-10-08): the main loop used to drain up to 128 samples per pass
and only THEN check "has this chunk reached 1 s?", so a chunk could absorb up to one extra batch
(~124 ms) past 1 s before closing. On the bench 30 % (hard ADC timer) of readings were 1.11-1.12 s
apart and the unit produced ~570 readings per 10 min instead of ~600 -- no samples lost, just fewer,
longer chunks. drain() checks after EVERY sample and stops right after the one that completes the
chunk, leaving the rest in the ring for the next chunk, so every chunk spans 1 s to 1 s + one sample
interval whatever the batch size.

Timestamps are chunk-relative microseconds (small ints: no heap allocation per sample on MicroPython,
where ints >= 2**30 are heap objects) converted to seconds for summarize_chunk; the session-wide
elapsed time is updated once per drain() call, not once per sample.
"""

import array


class ChunkBuilder:
    def __init__(self, capacity, chunk_us, float_typecode, ticks_diff, first_ticks):
        self.ticks = array.array("I", [0] * capacity)              # raw ticks_us per sample
        self.ts_s = array.array(float_typecode, [0.0] * capacity)  # seconds since the chunk's first sample
        self.counts = array.array("H", [0] * capacity)             # raw u16 ADC counts
        self.capacity = capacity
        self.chunk_us = chunk_us
        self._diff = ticks_diff
        self._last_ticks = first_ticks
        self._rel_us = 0              # current sample's offset from the chunk's first sample
        self.n = 0                    # valid samples in the current chunk: indices [0, n)
        self.complete = False         # the chunk reached chunk_us (or capacity); take it, then reset()
        self.elapsed_us = 0           # sample time consumed since start (STATUS / logs)
        self.capacity_closes = 0      # chunks closed because they filled up before chunk_us

    def drain(self, ring_ticks, ring_raw, read_idx, write_idx, ring_capacity, max_n):
        """Move samples from the ring ([read_idx, write_idx), at most max_n) into the chunk, stopping
        right after the sample that completes it. Returns the new read_idx. Does nothing while a
        completed chunk has not been reset() yet."""
        if self.complete:
            return read_idx
        diff = self._diff
        ticks, ts, counts = self.ticks, self.ts_s, self.counts
        n, cap, chunk_us = self.n, self.capacity, self.chunk_us
        last, rel = self._last_ticks, self._rel_us
        consumed_us = 0
        drained = 0
        while read_idx != write_idx and drained < max_n:
            rt = ring_ticks[read_idx]
            rc = ring_raw[read_idx]
            read_idx = (read_idx + 1) % ring_capacity
            drained += 1
            d = diff(rt, last)
            last = rt
            consumed_us += d
            rel = 0 if n == 0 else rel + d
            ticks[n] = rt
            ts[n] = rel / 1e6
            counts[n] = rc
            n += 1
            if rel >= chunk_us:
                self.complete = True
                break
            if n == cap:
                self.complete = True
                self.capacity_closes += 1
                break
        self.n = n
        self._last_ticks = last
        self._rel_us = rel
        self.elapsed_us += consumed_us
        return read_idx

    def reset(self):
        """Start the next chunk (call after the completed one has been summarised)."""
        self.n = 0
        self.complete = False
        self._rel_us = 0

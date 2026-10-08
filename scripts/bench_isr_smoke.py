"""10-second on-device smoke test of the hard ADC handler ALONE (adc_chunker.make_adc_isr), run from RAM:

    .venv/bin/mpremote connect auto mount . run scripts/bench_isr_smoke.py

Runs the real handler as a hard 1030 Hz timer while the main thread deliberately churns the heap
(allocation, gc.collect) and keeps ADDING NEW GLOBALS -- the two things that killed earlier versions
inside the interrupt (NameError from a globals table being resized, MemoryError from a heap-allocating
closure call). No watchdog, no Wi-Fi, no wifi_config.py, nothing written to flash. PASS = the handler
kept sampling the whole time and served a temperature request.
"""
import array
import gc
import time

import micropython
from machine import ADC, Timer

from adc_chunker import make_adc_isr

micropython.alloc_emergency_exception_buf(100)

N = 4096
RUN_MS = 10000
rt = array.array("L", [0] * N)
rr = array.array("H", [0] * N)
st = array.array("i", [0, 0, 0, 0, -1, N])
adc = ADC(26)
tmp = ADC(ADC.CORE_TEMP)
isr = make_adc_isr(rt, rr, st, (time.ticks_us, adc.read_u16, tmp.read_u16))

t = Timer()
t.init(freq=1030, mode=Timer.PERIODIC, callback=isr, hard=True)
t0 = time.ticks_ms()
n = 0
junk = []
k = 0
requested = False
while time.ticks_diff(time.ticks_ms(), t0) < RUN_MS:
    while st[1] != st[0]:                              # drain like the main loop
        st[1] = (st[1] + 1) % N
        n += 1
    junk.append([k] * 40)                              # heap churn
    if len(junk) > 300:
        junk = []
        gc.collect()
    globals()["_smoke_global_%d" % k] = k              # grow the globals table under the interrupt
    k += 1
    if not requested and time.ticks_diff(time.ticks_ms(), t0) > RUN_MS // 2:
        st[3] = 1                                      # request one temperature sample, halfway
        requested = True
t.deinit()
elapsed = time.ticks_diff(time.ticks_ms(), t0)
expected = elapsed * 1.030
temp_c = None if st[4] < 0 else round(27.0 - (st[4] * 3.3 / 65535 - 0.706) / 0.001721, 1)
ok = n >= 0.98 * expected and st[2] == 0 and st[4] >= 0
print("# ISR_SMOKE samples={} expected~{} overflow={} new_globals={} temp_c={} -> {}".format(
    n, int(expected), st[2], k, temp_c, "PASS" if ok else "FAIL"))

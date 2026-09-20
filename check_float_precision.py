"""Run this ON-DEVICE (mpremote connect <port> run check_float_precision.py)
before flashing wifi_unit_client.py's array.array buffers -- it tells you
whether this MicroPython build's floats are single or double precision, so
FLOAT_TYPECODE in wifi_unit_client.py can be set correctly.

Not run as part of this fix -- per the task, this is offline work only,
no Pico/serial-port access. Do not run this until you're ready to touch
the device.

Why it matters: array.array('f', ...) always stores 4-byte single-precision
values, silently truncating anything wider. If this build's native floats
are actually double precision (MICROPY_FLOAT_IMPL_DOUBLE) and
FLOAT_TYPECODE is left at 'f', every value written to _chunk_ts_s/
_voltages/_filtered_buf loses precision it would otherwise have kept --
not a crash, just quietly worse numbers than today's plain-list code
produces. Getting this right costs nothing (double precision is 'd', 8
bytes/slot instead of 4 -- 12,000 more bytes total across the three float
buffers at CHUNK_CAPACITY=1200, negligible next to the tens of KB this fix
already recovers).

The classic test: 1.0 + 1e-9 is exactly representable in double precision
(64-bit) but rounds back down to 1.0 in single precision (32-bit), because
1e-9 is far smaller than a float32's ~7-significant-digit resolution at
magnitude 1.0.
"""

result = 1.0 + 1e-9
if result == 1.0:
    print("SINGLE precision floats (1.0 + 1e-9 == 1.0) -- use FLOAT_TYPECODE = 'f'")
else:
    print("DOUBLE precision floats (1.0 + 1e-9 == {!r}, != 1.0) -- use FLOAT_TYPECODE = 'd'".format(result))

from machine import UART, Pin
import time

uart = UART(0, baudrate=9600, tx=Pin(0), rx=Pin(1))

MAX_BUF_BYTES = 1024  # guard against unbounded growth if no newline ever arrives

buf = b""
fix_acquired = False


def check_fix(line):
    global fix_acquired
    if fix_acquired:
        return
    fields = line.split(",")
    if line.startswith("$GPGGA") or line.startswith("$GNGGA"):
        if len(fields) > 6 and fields[6] not in ("", "0"):
            fix_acquired = True
            print("*** FIX ACQUIRED (GGA fix quality = {}) ***".format(fields[6]))
    elif line.startswith("$GPRMC") or line.startswith("$GNRMC"):
        if len(fields) > 2 and fields[2] == "A":
            fix_acquired = True
            print("*** FIX ACQUIRED (RMC status = A) ***")


print("UART0 open: tx=GP0 rx=GP1 baud=9600 -- waiting for NMEA sentences...")

while True:
    chunk = uart.read()
    if chunk:
        buf += chunk
        while b"\n" in buf:
            line_bytes, buf = buf.split(b"\n", 1)
            try:
                line = line_bytes.decode("ascii").strip()
            except UnicodeError:
                continue  # garbled/partial bytes -- drop this line, keep going
            if not line:
                continue
            print(line)
            check_fix(line)
        if len(buf) > MAX_BUF_BYTES:
            buf = buf[-MAX_BUF_BYTES:]  # no newline for too long -- drop stale prefix
    else:
        time.sleep_ms(50)

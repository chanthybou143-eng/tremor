from machine import UART, Pin
import time

BAUD_RATES = [4800, 9600, 19200, 38400, 57600, 115200]
LISTEN_S = 5
SAMPLE_CAP = 64  # bytes of raw sample to keep for inspection, per baud rate

any_bytes_at_all = False

for baud in BAUD_RATES:
    print("=== {} baud ===".format(baud))
    uart = UART(0, baudrate=baud, tx=Pin(0), rx=Pin(1))

    total = 0
    sample = b""
    start = time.ticks_ms()
    while time.ticks_diff(time.ticks_ms(), start) < LISTEN_S * 1000:
        n = uart.any()
        if n:
            data = uart.read()
            if data:
                total += len(data)
                if len(sample) < SAMPLE_CAP:
                    sample += data[: SAMPLE_CAP - len(sample)]
        time.sleep_ms(20)

    if total:
        any_bytes_at_all = True
        print("  {} bytes received".format(total))
        hex_str = " ".join("{:02x}".format(b) for b in sample)
        printable = "".join(chr(b) if 32 <= b < 127 else "." for b in sample)
        print("  hex   :", hex_str)
        print("  ascii :", printable)
    else:
        print("  0 bytes received")

    try:
        uart.deinit()
    except Exception:
        pass
    time.sleep_ms(200)

print("=== sweep complete ===")
if not any_bytes_at_all:
    print("ZERO bytes at every baud rate tried -- not a baud mismatch.")
    print("Points to something outside baud config: module UART port disabled")
    print("or reconfigured (e.g. output moved to I2C/Qwiic only), needs checking")
    print("on the module config side (u-blox u-center or similar), not more")
    print("Pico-side script changes.")

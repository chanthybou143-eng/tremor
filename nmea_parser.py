"""Minimal NMEA parser for TREMOR's GPS time sync (see pps_time_sync.py).

Not a general NMEA library -- just enough to pull UTC time-of-day and date
out of a $--RMC sentence, with a validated checksum. RMC (not GGA) is used
because it carries both time and date in one sentence, which matters for
handling midnight UTC rollover in pps_time_sync.py.

Zero dependencies beyond the stdlib (no machine/time imports), so this is
plain-portable: it runs the same on the Pico under MicroPython and on a
desktop Python for testing.
"""


def _valid_checksum(sentence):
    """sentence is the full line including leading '$' and trailing
    '*hh' checksum, no CR/LF. Checksum is the XOR of all characters
    between '$' and '*'."""
    if not sentence.startswith("$") or "*" not in sentence:
        return False
    body, _, checksum_hex = sentence[1:].partition("*")
    if len(checksum_hex) < 2:
        return False
    checksum = 0
    for ch in body:
        checksum ^= ord(ch)
    try:
        expected = int(checksum_hex[:2], 16)
    except ValueError:
        return False
    return checksum == expected


def _rmc_fields(line):
    """Validated ($--RMC, checksum OK, status A) time and date fields as
    (time_field, date_field), or None. Shared by parse_rmc and parse_rmc_int so
    the checksum is only computed once per sentence."""
    line = line.strip()
    if not _valid_checksum(line):
        return None

    body = line.split("*", 1)[0]
    fields = body.split(",")
    if len(fields) < 10:
        return None

    sentence_id = fields[0]
    if not (sentence_id.startswith("$") and sentence_id.endswith("RMC")):
        return None

    time_field = fields[1]
    status = fields[2]
    date_field = fields[9]

    if status != "A":
        return None
    if len(time_field) < 6 or len(date_field) != 6:
        return None
    return time_field, date_field


def _parse_date(date_field):
    day = int(date_field[0:2])
    month = int(date_field[2:4])
    year = 2000 + int(date_field[4:6])
    return year, month, day


def _parse_time_int(time_field):
    """(second_of_day, microsecond) as plain ints -- no float anywhere, so
    nothing is rounded to single precision on a MicroPython build whose float
    is 32-bit. The fractional digits of "hhmmss.ss" are padded/truncated to
    exactly 6 digits."""
    hours = int(time_field[0:2])
    minutes = int(time_field[2:4])
    seconds = int(time_field[4:6])
    usec = 0
    if len(time_field) > 6:
        if time_field[6] != ".":
            raise ValueError("bad time field")
        frac = time_field[7:]
        if frac:
            if not frac.isdigit():
                raise ValueError("bad fraction")
            usec = int((frac + "000000")[0:6])
    return hours * 3600 + minutes * 60 + seconds, usec


def parse_rmc(line):
    """Parse a $--RMC sentence (any talker ID -- GP, GN, GL, GA, ... are
    all valid, so this checks the suffix, not a hardcoded prefix).

    Returns (utc_seconds_of_day: float, (year, month, day)) on success.
    Returns None for anything malformed, the wrong sentence type, a bad
    checksum, or a void (no-fix) status -- callers should treat None as
    "nothing usable here", not an error worth surfacing.
    """
    fields = _rmc_fields(line)
    if fields is None:
        return None
    time_field, date_field = fields
    try:
        hours = int(time_field[0:2])
        minutes = int(time_field[2:4])
        seconds = float(time_field[4:])
        date = _parse_date(date_field)
    except ValueError:
        return None

    utc_seconds_of_day = hours * 3600 + minutes * 60 + seconds
    return utc_seconds_of_day, date


def parse_rmc_int(line):
    """Integer-only variant of parse_rmc: returns
    (second_of_day: int, microsecond: int, (year, month, day)) or None."""
    fields = _rmc_fields(line)
    if fields is None:
        return None
    try:
        sod, usec = _parse_time_int(fields[0])
        date = _parse_date(fields[1])
    except ValueError:
        return None
    return sod, usec, date


def parse_rmc_both(line):
    """(parse_rmc result, parse_rmc_int result) from ONE checksum pass, or
    None if the sentence isn't a usable RMC fix."""
    fields = _rmc_fields(line)
    if fields is None:
        return None
    time_field, date_field = fields
    try:
        hours = int(time_field[0:2])
        minutes = int(time_field[2:4])
        seconds = float(time_field[4:])
        date = _parse_date(date_field)
        sod, usec = _parse_time_int(time_field)
    except ValueError:
        return None
    return (hours * 3600 + minutes * 60 + seconds, date), (sod, usec, date)

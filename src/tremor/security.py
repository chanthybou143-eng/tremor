"""Ingest tokens, export token and a small per-IP rate limiter.

Nothing here is a secret store: tokens come from the environment (set in the
PythonAnywhere WSGI file, never committed) and are compared in constant time.

Ingest authentication (``X-Tremor-Token`` header, one token per unit):
  * ``off``       -- no checks (local development; the default when no tokens are configured).
  * ``optional``  -- a request WITHOUT a token is accepted, logged and counted (so a legacy
                     Unit 1 that cannot send one keeps working); a request WITH a token that
                     is wrong for that unit is rejected (401).
  * ``required``  -- a missing or wrong token is rejected (401).
A 401 never says whether the token was missing or wrong, and no token is ever logged.

Environment:
  TREMOR_INGEST_TOKENS   "unit-1=<token>,unit-2=<token>"   (each token >= 16 characters)
  TREMOR_INGEST_AUTH     off | optional | required          (default: optional if tokens set, else off)
  TREMOR_EXPORT_TOKEN    token for /api/export; without it the endpoint is disabled
  TREMOR_HISTORY_RATE    "30/60" = 30 requests per 60 s per client for /api/history
  TREMOR_EXPORT_RATE     "10/60" likewise for /api/export
  TREMOR_CLIENT_IP_HEADER  e.g. "X-Real-IP", if the app sits behind a proxy (default: the socket address)

Misconfiguration raises ``ConfigError`` at startup on purpose: a typo that silently ran
without authentication would be worse than a loud failure at reload time.
"""

from __future__ import annotations

import hmac
import re
import threading
from collections import OrderedDict
from dataclasses import dataclass
from typing import Callable, Dict, Optional, Tuple

TOKEN_HEADER = "X-Tremor-Token"
MIN_TOKEN_LEN = 16
MODES = ("off", "optional", "required")
_UNIT_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
_RATE_RE = re.compile(r"^\s*(\d+)\s*/\s*(\d+(?:\.\d+)?)\s*$")


class ConfigError(ValueError):
    pass


def parse_tokens(spec: Optional[str]) -> Dict[str, str]:
    """``"unit-1=abc...,unit-2=def..."`` -> {unit: token}. Splits each entry on the FIRST '='."""
    out: Dict[str, str] = {}
    if not spec or not spec.strip():
        return out
    for entry in spec.split(","):
        entry = entry.strip()
        if not entry:
            continue
        unit, sep, token = entry.partition("=")
        unit, token = unit.strip(), token.strip()
        if not sep or not _UNIT_RE.match(unit):
            raise ConfigError("TREMOR_INGEST_TOKENS entries must look like unit-1=<token>")
        if len(token) < MIN_TOKEN_LEN:
            raise ConfigError(f"the token for {unit!r} is shorter than {MIN_TOKEN_LEN} characters")
        if unit in out:
            raise ConfigError(f"duplicate token entry for {unit!r}")
        out[unit] = token
    return out


def _same(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


@dataclass(frozen=True)
class AuthDecision:
    allowed: bool
    outcome: str            # authenticated | missing_accepted | rejected_missing | rejected_wrong | off


class IngestAuth:
    def __init__(self, tokens: Dict[str, str], mode: Optional[str] = None,
                 clock: Callable[[], float] = None, log=None, log_every_s: float = 600.0):
        if mode is None:
            mode = "optional" if tokens else "off"
        if mode not in MODES:
            raise ConfigError(f"TREMOR_INGEST_AUTH must be one of {', '.join(MODES)} (got {mode!r})")
        if mode != "off" and not tokens:
            raise ConfigError(f"TREMOR_INGEST_AUTH={mode} needs TREMOR_INGEST_TOKENS")
        for unit, tok in tokens.items():
            if len(tok) < MIN_TOKEN_LEN:
                raise ConfigError(f"the token for {unit!r} is shorter than {MIN_TOKEN_LEN} characters")
        self.tokens = dict(tokens)
        self.mode = mode
        self._clock = clock or (lambda: 0.0)
        self._log = log
        self._log_every = log_every_s
        self._last_logged: Dict[str, float] = {}
        self._lock = threading.Lock()
        self.counters = {"authenticated": 0, "missing_accepted": 0, "rejected_missing": 0, "rejected_wrong": 0}
        self.units_without_token: Dict[str, float] = {}       # unit -> last time it posted with no token

    @classmethod
    def from_env(cls, env, clock=None, log=None) -> "IngestAuth":
        return cls(parse_tokens(env.get("TREMOR_INGEST_TOKENS")), (env.get("TREMOR_INGEST_AUTH") or None),
                   clock=clock, log=log)

    def check(self, unit_id: str, presented: Optional[str]) -> AuthDecision:
        if self.mode == "off":
            return AuthDecision(True, "off")
        expected = self.tokens.get(unit_id)
        if presented:
            if expected is not None and _same(presented, expected):
                self._bump("authenticated")
                return AuthDecision(True, "authenticated")
            # present but wrong (or this unit has no token at all): rejected in every mode
            self._bump("rejected_wrong")
            self._warn(f"wrong:{unit_id}", "ingest for %s rejected: token does not match", unit_id)
            return AuthDecision(False, "rejected_wrong")
        if self.mode == "optional":
            now = self._clock()
            with self._lock:
                self.counters["missing_accepted"] += 1
                self.units_without_token[unit_id] = now
            self._warn(f"missing:{unit_id}", "ingest for %s accepted WITHOUT a token (auth is optional)", unit_id)
            return AuthDecision(True, "missing_accepted")
        self._bump("rejected_missing")
        self._warn(f"nomissing:{unit_id}", "ingest for %s rejected: no token (auth is required)", unit_id)
        return AuthDecision(False, "rejected_missing")

    def _bump(self, key: str) -> None:
        with self._lock:
            self.counters[key] += 1

    def _warn(self, key: str, msg: str, unit_id: str) -> None:
        if self._log is None:
            return
        now = self._clock()
        with self._lock:
            last = self._last_logged.get(key)
            if last is not None and now - last < self._log_every:
                return
            self._last_logged[key] = now
        self._log.warning(msg, unit_id)                       # the token itself is never passed to the logger

    def summary(self) -> dict:
        with self._lock:
            return dict(mode=self.mode, units_with_tokens=len(self.tokens), **self.counters,
                        units_seen_without_token=sorted(self.units_without_token))


class ExportAuth:
    """A single token for /api/export. Without one the endpoint is disabled."""

    def __init__(self, token: Optional[str]):
        if token is not None and len(token) < MIN_TOKEN_LEN:
            raise ConfigError(f"TREMOR_EXPORT_TOKEN is shorter than {MIN_TOKEN_LEN} characters")
        self.token = token or None

    @property
    def enabled(self) -> bool:
        return self.token is not None

    def check(self, presented: Optional[str]) -> bool:
        return self.enabled and bool(presented) and _same(presented, self.token)


def parse_rate(spec: Optional[str], default: Tuple[int, float]) -> Tuple[int, float]:
    if not spec or not spec.strip():
        return default
    m = _RATE_RE.match(spec)
    if not m or int(m.group(1)) < 1 or float(m.group(2)) <= 0:
        raise ConfigError(f"rate limit {spec!r} must look like 30/60 (requests/seconds)")
    return int(m.group(1)), float(m.group(2))


class RateLimiter:
    """Token bucket per key (a client address): ``limit`` requests per ``period_s``, refilled
    continuously. Memory is bounded (LRU, ``max_keys``). Uses the injected clock."""

    def __init__(self, limit: int, period_s: float, clock: Callable[[], float], max_keys: int = 5000):
        self.limit, self.period, self._clock, self._max = limit, period_s, clock, max_keys
        self._buckets: "OrderedDict[str, Tuple[float, float]]" = OrderedDict()
        self._lock = threading.Lock()

    def allow(self, key: str) -> Tuple[bool, float]:
        """(allowed, retry_after_seconds)."""
        now = self._clock()
        rate = self.limit / self.period
        with self._lock:
            tokens, last = self._buckets.get(key, (float(self.limit), now))
            tokens = min(float(self.limit), tokens + max(0.0, now - last) * rate)
            if tokens >= 1.0:
                self._buckets[key] = (tokens - 1.0, now)
                self._buckets.move_to_end(key)
                allowed, retry = True, 0.0
            else:
                self._buckets[key] = (tokens, now)
                self._buckets.move_to_end(key)
                allowed, retry = False, (1.0 - tokens) / rate
            while len(self._buckets) > self._max:
                self._buckets.popitem(last=False)
        return allowed, retry


def client_ip(request, header: Optional[str]) -> str:
    """The caller's address: the configured proxy header (first hop) if set and present,
    otherwise the socket address."""
    if header:
        v = request.headers.get(header)
        if v:
            return v.split(",")[0].strip() or (request.remote_addr or "?")
    return request.remote_addr or "?"

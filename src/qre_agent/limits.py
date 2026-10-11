"""Limits for the public demo (server.py): per-IP rate limits, the OpenRouter key's budget and the
client IP. The limits that must survive a restart (the daily cap, the stop level) are read from
OpenRouter's key endpoint, not counted here. See docs/demo-api.md."""

import ipaddress
import json
import threading
import time
import urllib.error
import urllib.request
from collections import Counter, defaultdict, deque

KEY_URL = "https://openrouter.ai/api/v1/key"  # https://openrouter.ai/docs/api-reference/limits

RUN_S = 240  # one run, both legs
MAX_RUNNING, MAX_QUEUED, QUEUE_WAIT_S = 2, 6, 60
IP_CONCURRENT, IP_HOUR, IP_DAY = 1, 4, 10
RUN_CAP_USD = 0.35  # per run; the most expensive of 40 routed eval tasks cost $0.31
DAILY_CAP_USD = 0.75  # stop accepting runs for the day when the key's usage_daily reaches this
STOP_BELOW_USD = 0.40  # stop for good when the key has less than this left
KEY_CHECK_TTL_S = 30
HOUR_S, DAY_S = 3600, 86400
MAX_TRACKED = 5000  # addresses kept in memory before the idle ones are forgotten


class Rejected(Exception):
    """A request the server refuses, with its HTTP status, error code and Retry-After seconds."""

    def __init__(self, status, code, message, retry_after=None):
        super().__init__(message)
        self.status, self.code, self.message, self.retry_after = status, code, message, retry_after


class IpLimiter:
    """Per IP: at most `concurrent` runs at once, `hour` starts in the last hour and `day` in the
    last 24 hours. In memory, so a restart resets it; the key's budget does not reset. Not
    thread-safe: the server calls it from its event loop only."""

    def __init__(self, concurrent=IP_CONCURRENT, hour=IP_HOUR, day=IP_DAY, clock=time.time):
        self.concurrent, self.hour, self.day, self.clock = concurrent, hour, day, clock
        self.active, self.starts = Counter(), defaultdict(deque)

    def acquire(self, ip):
        """Counts a run for `ip`, or raises Rejected(429)."""
        now = self.clock()
        if len(self.starts) > MAX_TRACKED:  # forget addresses with nothing left to count
            keep = {
                k: v
                for k, v in self.starts.items()
                if self.active[k] or (v and now - v[-1] < DAY_S)
            }
            self.starts = defaultdict(deque, keep)
        starts = self.starts[ip]
        while starts and now - starts[0] >= DAY_S:
            starts.popleft()
        if self.active[ip] >= self.concurrent:
            raise Rejected(429, "ip_busy", "You already have a run in progress.", 10)
        last_hour = [t for t in starts if now - t < HOUR_S]
        if len(last_hour) >= self.hour:
            wait = last_hour[0] + HOUR_S - now
            raise Rejected(429, "ip_hourly_limit", f"At most {self.hour} runs an hour.", wait)
        if len(starts) >= self.day:
            wait = starts[0] + DAY_S - now
            raise Rejected(429, "ip_daily_limit", f"At most {self.day} runs a day.", wait)
        starts.append(now)
        self.active[ip] += 1

    def remaining(self, ip):
        """{"hour": runs left this hour, "day": runs left today} for `ip`."""
        now, starts = self.clock(), self.starts.get(ip, ())
        hour = sum(now - t < HOUR_S for t in starts)
        day = sum(now - t < DAY_S for t in starts)
        return {"hour": max(0, self.hour - hour), "day": max(0, self.day - day)}

    def release(self, ip):
        self.active[ip] -= 1
        if self.active[ip] <= 0:
            del self.active[ip]


def fetch_key_info(key, url=KEY_URL, timeout=10):
    """`data` of OpenRouter's key endpoint. Errors name the status or the exception type, never
    the reply (its `label` can hold part of the key)."""
    request = urllib.request.Request(url, headers={"Authorization": f"Bearer {key}"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as r:
            return json.load(r)["data"]
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"HTTP {e.code}") from None
    except (OSError, ValueError, KeyError, TypeError) as e:
        raise RuntimeError(type(e).__name__) from None


def key_remaining(data):
    """Dollars left on the key (its own limit or the effective budget, whichever is lower), or
    None when it has no limit."""
    left = [data.get("limit_remaining")]
    if budget := data.get("effective_budget"):
        left.append(budget.get("remaining_usd"))
    left = [x for x in left if isinstance(x, (int, float))]
    return min(left) if left else None


class KeyBudget:
    """Whether the demo may start a run, from OpenRouter's key endpoint (`fetch() -> data`),
    asked at most once per `ttl` seconds. Running out is sticky until the process restarts. A
    key that can't be checked, or has no limit, refuses runs: the $5 limit is the point."""

    def __init__(self, fetch, ttl=KEY_CHECK_TTL_S, clock=time.monotonic, utcnow=time.time):
        self.fetch, self.ttl, self.clock, self.utcnow = fetch, ttl, clock, utcnow
        self.exhausted = False
        self.checked = None  # (time, error to raise or None)
        self.lock = threading.Lock()  # one check at a time; the others wait for its answer

    def mark_exhausted(self):
        """A call came back out of credit."""
        self.exhausted = True

    def check(self):
        """Raises Rejected(503) when no run may start."""
        if self.exhausted:
            raise self.out_of_budget()
        with self.lock:
            if self.checked is None or self.clock() - self.checked[0] >= self.ttl:
                self.checked = (self.clock(), self.look())
            error = self.checked[1]
        if error:
            raise error

    def look(self):
        try:
            data = self.fetch()
        except RuntimeError as e:
            return Rejected(503, "budget_unknown", f"The key's budget can't be checked ({e}).", 30)
        left = key_remaining(data) if isinstance(data, dict) else None
        if left is None:
            return Rejected(503, "budget_unknown", "The key has no credit limit.", 30)
        if left < STOP_BELOW_USD:
            self.exhausted = True
            return self.out_of_budget()
        if (data.get("usage_daily") or 0) >= DAILY_CAP_USD:
            tomorrow = (self.utcnow() // DAY_S + 1) * DAY_S  # OpenRouter's day is UTC
            return Rejected(
                503, "daily_cap", "Today's budget is used up; try again tomorrow (UTC).",
                tomorrow - self.utcnow(),
            )  # fmt: skip
        return None

    @staticmethod
    def out_of_budget():
        return Rejected(503, "budget_exhausted", "The demo's budget is used up.")


def client_ip(forwarded_for, peer, hops=1):
    """The address a request is limited by: the `hops`-th entry from the right of X-Forwarded-For,
    the one our own proxy appended. Never the leftmost, which the client writes. With fewer
    entries than `hops`, or none, the socket peer. IPv6 is grouped by /64."""
    parts = [p.strip() for p in (forwarded_for or "").split(",") if p.strip()]
    raw = parts[-hops] if len(parts) >= hops else peer
    try:
        ip = ipaddress.ip_address(raw)
    except ValueError:
        return str(peer or "unknown")
    if ip.version == 6:
        return str(ipaddress.ip_network(f"{ip}/64", strict=False))
    return str(ip)

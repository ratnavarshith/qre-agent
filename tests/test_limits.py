import io
import urllib.error

import pytest

from qre_agent import limits
from qre_agent.limits import IpLimiter, KeyBudget, Rejected, client_ip, fetch_key_info


class Clock:
    def __init__(self, now=1_000_000.0):
        self.now = now

    def __call__(self):
        return self.now


def test_an_address_runs_one_at_a_time():
    ips = IpLimiter()
    ips.acquire("1.1.1.1")
    with pytest.raises(Rejected) as e:
        ips.acquire("1.1.1.1")
    assert (e.value.status, e.value.code) == (429, "ip_busy")
    ips.acquire("2.2.2.2")  # another address is not affected
    ips.release("1.1.1.1")
    ips.acquire("1.1.1.1")


def test_four_runs_an_hour_then_the_wait_until_the_oldest_one_ages_out():
    clock = Clock()
    ips = IpLimiter(clock=clock)
    for _ in range(4):
        ips.acquire("1.1.1.1")
        ips.release("1.1.1.1")
        clock.now += 60
    with pytest.raises(Rejected) as e:
        ips.acquire("1.1.1.1")
    assert e.value.code == "ip_hourly_limit"
    assert e.value.retry_after == pytest.approx(3600 - 240)  # the first run was 240 s ago
    clock.now += 3600 - 240
    ips.acquire("1.1.1.1")


def test_ten_runs_a_day():
    clock = Clock()
    ips = IpLimiter(clock=clock)
    for _ in range(10):
        ips.acquire("1.1.1.1")
        ips.release("1.1.1.1")
        clock.now += 1900  # under 4 an hour, over the day's 10
    with pytest.raises(Rejected) as e:
        ips.acquire("1.1.1.1")
    assert e.value.code == "ip_daily_limit"
    assert e.value.retry_after == pytest.approx(86400 - 9 * 1900 - 1900)
    clock.now += e.value.retry_after
    ips.acquire("1.1.1.1")


def test_idle_addresses_are_forgotten_but_busy_and_recent_ones_are_kept(monkeypatch):
    monkeypatch.setattr(limits, "MAX_TRACKED", 2)
    clock = Clock()
    ips = IpLimiter(clock=clock)
    ips.acquire("old")
    ips.release("old")
    ips.acquire("busy")
    with pytest.raises(Rejected):
        ips.acquire("busy")  # leaves nothing new, but is counted as seen
    clock.now += 86400 + 1
    ips.acquire("new")
    ips.release("new")
    ips.acquire("fresh")  # three tracked: the sweep runs
    assert set(ips.starts) == {"busy", "new", "fresh"} and "old" not in ips.starts


def test_remaining_runs_for_an_address():
    ips = IpLimiter()
    assert ips.remaining("1.1.1.1") == {"hour": 4, "day": 10}
    ips.acquire("1.1.1.1")
    assert ips.remaining("1.1.1.1") == {"hour": 3, "day": 9}


@pytest.mark.parametrize(
    ("forwarded", "hops", "peer", "expected"),
    [
        ("9.9.9.9", 1, "10.0.0.1", "9.9.9.9"),
        ("6.6.6.6, 9.9.9.9", 1, "10.0.0.1", "9.9.9.9"),  # the client wrote the first entry
        ("6.6.6.6, 9.9.9.9, 35.1.1.1", 2, "10.0.0.1", "9.9.9.9"),  # behind a load balancer
        ("9.9.9.9", 2, "10.0.0.1", "10.0.0.1"),  # fewer entries than proxies: the peer
        (None, 1, "10.0.0.1", "10.0.0.1"),
        ("not an ip", 1, "10.0.0.1", "10.0.0.1"),
        ("2001:db8:1:2:aaaa::1", 1, "10.0.0.1", "2001:db8:1:2::/64"),
        ("2001:db8:1:2:bbbb::7", 1, "10.0.0.1", "2001:db8:1:2::/64"),  # the same /64
        (None, 1, None, "unknown"),
    ],
)
def test_the_client_address(forwarded, hops, peer, expected):
    assert client_ip(forwarded, peer, hops) == expected


def key(left=4.0, daily=0.0, **more):
    return {"limit": 5, "limit_remaining": left, "usage_daily": daily, **more}


def budget(data, **kw):
    calls = []

    def fetch():
        calls.append(1)
        if isinstance(data, Exception):
            raise data
        return data

    return KeyBudget(fetch, **kw), calls


def test_a_key_with_room_lets_runs_start_and_is_asked_once_per_ttl():
    clock = Clock(0)
    b, calls = budget(key(), clock=clock, ttl=30)
    b.check()
    b.check()
    assert len(calls) == 1
    clock.now = 31
    b.check()
    assert len(calls) == 2


def test_below_forty_cents_left_stops_for_good():
    clock = Clock(0)
    data = key(left=0.39)
    b, calls = budget(data, clock=clock)
    with pytest.raises(Rejected) as e:
        b.check()
    assert (e.value.status, e.value.code) == (503, "budget_exhausted")
    data["limit_remaining"] = 4.0  # topped up: still stopped until a restart
    clock.now += 100
    with pytest.raises(Rejected) as e:
        b.check()
    assert e.value.code == "budget_exhausted" and len(calls) == 1


def test_exactly_forty_cents_left_still_runs():
    b, _ = budget(key(left=0.40))
    b.check()


def test_the_daily_cap_waits_for_utc_midnight_and_lifts_by_itself():
    clock, now = Clock(0), Clock(86400 * 5 + 3600 * 20)  # 20:00 UTC
    data = key(daily=0.75)
    b, _ = budget(data, clock=clock, utcnow=now)
    with pytest.raises(Rejected) as e:
        b.check()
    assert (e.value.status, e.value.code) == (503, "daily_cap")
    assert e.value.retry_after == 4 * 3600
    data["usage_daily"] = 0.0  # a new day
    clock.now += 31
    b.check()


def test_the_lower_of_the_key_limit_and_the_effective_budget_counts():
    b, _ = budget(key(left=4.0, effective_budget={"remaining_usd": 0.1}))
    with pytest.raises(Rejected) as e:
        b.check()
    assert e.value.code == "budget_exhausted"


@pytest.mark.parametrize("data", [RuntimeError("HTTP 500"), {"usage_daily": 0}, None])
def test_a_key_that_cannot_be_checked_or_has_no_limit_refuses_runs(data):
    b, _ = budget(data)
    with pytest.raises(Rejected) as e:
        b.check()
    assert (e.value.status, e.value.code, e.value.retry_after) == (503, "budget_unknown", 30)
    assert not b.exhausted  # transient: the next check may pass


def test_a_call_that_came_back_out_of_credit_stops_runs_without_asking_again():
    b, calls = budget(key())
    b.mark_exhausted()
    with pytest.raises(Rejected) as e:
        b.check()
    assert e.value.code == "budget_exhausted" and calls == []


def test_the_key_check_names_the_status_or_exception_never_the_reply(monkeypatch):
    def raising(error):
        def urlopen(request, timeout):
            raise error

        return urlopen

    secret = b'{"data": {"label": "sk-or-v1-abc...xyz"}}'
    cases = [
        (urllib.error.HTTPError("u", 401, "", {}, io.BytesIO(secret)), "HTTP 401"),
        (TimeoutError("sk-or-v1-leak"), "TimeoutError"),
        (urllib.error.URLError("sk-or-v1-leak"), "URLError"),
    ]
    for error, expected in cases:
        monkeypatch.setattr("urllib.request.urlopen", raising(error))
        with pytest.raises(RuntimeError) as e:
            fetch_key_info("sk-or-v1-key")
        assert str(e.value) == expected


def test_the_limits_are_the_agreed_numbers():
    assert (limits.RUN_S, limits.MAX_RUNNING, limits.MAX_QUEUED) == (240, 2, 6)
    assert (limits.IP_CONCURRENT, limits.IP_HOUR, limits.IP_DAY) == (1, 4, 10)
    assert (limits.RUN_CAP_USD, limits.DAILY_CAP_USD, limits.STOP_BELOW_USD) == (0.35, 0.75, 0.40)

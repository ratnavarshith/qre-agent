"""The demo page (src/qre_agent/static/index.html), served at GET /. The page is checked in a real
browser by hand; these tests hold what a browser would not tell us: nothing is loaded from outside,
the Content-Security-Policy matches the page, and the page knows every error code the server sends."""

import base64
import hashlib
import json
import re
from html.parser import HTMLParser
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from test_demo import ANSWER, BENCH, FakeClient, make, reply
from test_server import FakeDemo, open_budget

from qre_agent.limits import IpLimiter, KeyBudget
from qre_agent.server import MAX_PROBLEM, PAGE, Problem, create_app, load_page

SRC = Path(__file__).resolve().parents[1] / "src" / "qre_agent"
REPO = "https://github.com/ratnavarshith/qre-agent"
PAGE_ONLY_CODES = {"network", "cut_off"}  # made by the page itself, not sent by the server


@pytest.fixture
def html():
    return PAGE.read_text(encoding="utf-8")


def inline(html, tag):
    found = re.findall(rf"<{tag}>(.*?)</{tag}>", html, re.DOTALL)
    assert len(found) == 1
    return found[0]


class Tags(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tags, self.attrs = [], []

    def handle_starttag(self, tag, attrs):
        self.tags.append(tag)
        self.attrs += [(tag, k, v) for k, v in attrs]


def test_the_root_serves_the_page_with_safe_headers():
    client = TestClient(create_app(FakeDemo(), open_budget()))
    r = client.get("/")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/html")
    assert "<title>Quantum resource estimates</title>" in r.text
    assert r.text == PAGE.read_text(encoding="utf-8")
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["referrer-policy"] == "no-referrer"
    assert r.headers["cache-control"] == "no-cache"


def test_the_policy_allows_exactly_the_pages_own_script_and_style(html):
    _, headers = load_page()
    policy = headers["Content-Security-Policy"]

    def digest(text):
        return "'sha256-" + base64.b64encode(hashlib.sha256(text.encode()).digest()).decode() + "'"

    assert f"script-src {digest(inline(html, 'script'))};" in policy
    assert f"style-src {digest(inline(html, 'style'))};" in policy
    assert "default-src 'none'" in policy and "connect-src 'self'" in policy
    assert "frame-ancestors 'none'" in policy and "form-action 'none'" in policy
    assert "unsafe" not in policy and "http" not in policy


def test_a_page_with_two_scripts_is_refused(tmp_path):
    path = tmp_path / "p.html"
    path.write_text("<script>1</script><script>2</script><style>a{}</style>", encoding="utf-8")
    with pytest.raises(RuntimeError, match="exactly one inline <script>, found 2"):
        load_page(path)


def test_serving_the_page_costs_nothing(html):
    asked = []
    budget = KeyBudget(lambda: asked.append(1) or {"limit_remaining": 4, "usage_daily": 0})
    limiter = IpLimiter()
    client = TestClient(create_app(FakeDemo(), budget, limiter=limiter))
    for _ in range(20):
        assert client.get("/").status_code == 200
    assert asked == [] and limiter.remaining("testclient") == {"hour": 4, "day": 10}


def test_nothing_is_loaded_from_outside(html):
    parser = Tags()
    parser.feed(html)
    assert set(parser.tags) <= {
        "html", "head", "meta", "title", "style", "body", "main", "h1", "h2", "p", "a", "div",
        "label", "textarea", "span", "button", "section", "ol", "table", "ul", "details",
        "summary", "dl", "footer", "script",
    }  # fmt: skip
    for tag, name, value in parser.attrs:
        assert name != "src" and name != "srcset" and name != "style", (
            tag,
            name,
        )  # no style attribute: the policy blocks it
        assert not name.startswith("on"), (tag, name)  # no inline handlers: the policy blocks them
        if name == "href":
            assert value == REPO, value
    assert "http-equiv" not in html
    css = inline(html, "style")
    assert "@import" not in css and "url(" not in css and "@font-face" not in css
    urls = set(re.findall(r"https?://[^\s\"'<>)]+", html))
    assert urls == {REPO}, urls
    assert not re.search(r"""["']//[a-z]""", html)  # no protocol-relative URL


def test_the_script_never_builds_html_or_runs_text(html):
    js = inline(html, "script")
    for banned in (
        "innerHTML",
        "outerHTML",
        "insertAdjacentHTML",
        "document.write",
        "eval(",
        "new Function",
        "srcdoc",
        'setTimeout("',
        "createContextualFragment",
    ):
        assert banned not in js, banned
    assert not re.search(r"""setAttribute\(\s*["'](style|on)""", js)
    assert set(re.findall(r"""fetch\(\s*"([^"]+)"?""", js)) == {"/limits", "/estimate"}


def test_the_page_has_a_reason_for_every_error_code_the_server_sends(html):
    sources = "".join(
        (SRC / f).read_text(encoding="utf-8") for f in ("limits.py", "server.py", "demo.py")
    )
    sent = set(re.findall(r"Rejected\(\s*\d+,\s*\"(\w+)\"", sources))
    sent |= set(re.findall(r"error_event\(\s*\"(\w+)\"", sources))
    sent |= set(re.findall(r"return \"(\w+)\"", (SRC / "demo.py").read_text(encoding="utf-8")))
    sent |= set(re.findall(r"\"code\": \"(\w+)\"", (SRC / "demo.py").read_text(encoding="utf-8")))
    assert {
        "ip_busy",
        "daily_cap",
        "budget_exhausted",
        "run_cap",
        "provider_error",
        "timeout",
    } <= sent
    block = re.search(r"const MESSAGES = \{(.*?)\n\};", inline(html, "script"), re.DOTALL)[1]
    known = set(re.findall(r"^\s+(\w+): ", block, re.MULTILINE))
    assert sent <= known, sent - known
    assert known - sent == PAGE_ONLY_CODES  # and nothing stale is left in the page


def test_the_example_is_a_valid_request(html):
    example = re.search(r'const EXAMPLE = "(.*?)";', inline(html, "script"))[1]
    assert Problem.model_validate({"problem": example}).problem == example
    assert 0 < len(example) <= MAX_PROBLEM
    assert re.search(r"maxlength=\"(\d+)\"", html)[1] == str(MAX_PROBLEM)
    assert f"const MAX = {MAX_PROBLEM};" in html and f"0 / {MAX_PROBLEM}" in html


def test_the_page_describes_the_project_and_links_the_repository(html):
    assert f'<a href="{REPO}">' in html
    blurb = re.search(r"<h1>.*?</h1>\s*<p>(.*?)</p>", html, re.DOTALL)[1]
    assert 2 <= len(re.findall(r"\.(?:\s|$)", blurb)) <= 3  # two or three sentences


def test_the_limits_endpoint_has_what_the_page_reads():
    client = TestClient(create_app(FakeDemo(), open_budget()))
    body = client.get("/limits").json()
    assert body["accepting_runs"] is True and body["reason"] is None
    assert set(body["runs_left"]) == {"hour", "day"}


def test_a_result_has_what_the_page_reads(tmp_path):
    import yaml
    from test_demo import INFO, PRIMARY, STRONG

    budgets = tmp_path / "budgets.yaml"
    price = {"input": 1.0, "cache_read": 0.1, "output": 2.0}
    budgets.write_text(
        yaml.safe_dump({"caps": {"demo": 5}, "prices": {PRIMARY: price, STRONG: price}})
    )
    event = make(budgets, FakeClient(*BENCH, reply(ANSWER))).run(
        "task", "d-1", __import__("test_demo").Quiet()
    )
    for key in ("run_id", "answer", "table", "verification", "verify_passed_first_try", "routing",
                "assumptions", "steps", "cost_usd", "latency_s", "versions", "measurement_tables"):  # fmt: skip
        assert key in event, key
    assert {
        "escalated",
        "reason",
        "answered_by",
        "primary_model",
        "strong_model",
        "cache_hits",
        "cache_misses",
    } <= set(event["routing"])
    assert {"error_budget", "physical_error_rate"} <= set(event["assumptions"])
    assert event["versions"] == INFO["versions"]


@pytest.mark.compiler
def test_real_estimates_have_the_fields_of_a_table_row():
    from qre_agent.tools import Toolbox

    t = Toolbox(timeout=None)
    circuit = json.loads(t.call("build_benchmark", {"family": "qft", "n": 4}))["circuit_id"]
    rows = [
        json.loads(t.call("estimate_surface", {"circuit_id": circuit})),
        json.loads(t.call("estimate_bicycle", {"circuit_id": circuit, "code": "gross"})),
    ]
    for row in rows:
        for key in ("architecture", "physical_qubits", "runtime_ns", "physical_qubit_seconds",
                    "logical_qubits", "t_count", "rotation_count", "error", "passes"):  # fmt: skip
            assert key in row, (row["architecture"], key)
        assert {"error_budget", "physical_error_rate"} <= set(row["assumptions"])
    assert rows[1]["code"] == "gross"


def test_the_root_answers_head_with_the_same_headers_and_no_body():
    """Uptime monitors and `curl -I` send HEAD; it got 405 on the live service."""
    client = TestClient(create_app(FakeDemo(), open_budget()))
    get, head = client.get("/"), client.head("/")
    assert head.status_code == 200 and head.content == b""
    for name in ("content-type", "content-security-policy", "x-content-type-options"):
        assert head.headers[name] == get.headers[name], name

import pytest
from test_agent import ANSWER, ESTIMATE, FakeClient, call, go, guard, reply, trace  # noqa: F401

from qre_agent.tracing import read_spans


def spans_of(tmp_path, run_id="t1"):
    return read_spans(tmp_path / "otel" / f"{run_id}.jsonl")


def test_one_trace_per_run_with_a_span_per_llm_and_tool_call(guard, tmp_path):  # noqa: F811
    client = FakeClient(reply("", call("build_circuit", {"code": "bad"})), *ESTIMATE, reply(ANSWER))
    result = go(client, guard, tmp_path)
    spans = spans_of(tmp_path)
    names = sorted(s["name"] for s in spans)
    assert names == ["agent_run"] + ["llm_call"] * 4 + ["tool_call"] * 4
    assert len({s["context"]["trace_id"] for s in spans}) == 1
    root = next(s for s in spans if s["name"] == "agent_run")
    assert root["parent_id"] is None
    assert all(s["parent_id"] == root["context"]["span_id"] for s in spans if s is not root)
    assert root["attributes"]["stop_reason"] == "answered" and root["attributes"]["steps"] == 4

    llm = [s["attributes"] for s in spans if s["name"] == "llm_call"]
    assert [a["step"] for a in llm] == [1, 2, 3, 4]
    assert llm[0]["gen_ai.request.model"] == "fake"
    assert (llm[0]["gen_ai.usage.input_tokens"], llm[0]["gen_ai.usage.cached_tokens"],
            llm[0]["gen_ai.usage.output_tokens"]) == (1000, 400, 100)  # fmt: skip
    assert sum(a["cost_usd"] for a in llm) == pytest.approx(result.totals["cost_usd"])
    assert all(a["latency_s"] >= 0 for a in llm)

    tools = [s["attributes"] for s in spans if s["name"] == "tool_call"]
    assert tools[0]["tool.name"] == "build_circuit" and tools[0]["error.type"] == "SandboxError"
    assert all("error.type" not in a for a in tools[1:])


def test_the_jsonl_trace_is_unchanged_by_tracing(guard, tmp_path):  # noqa: F811
    on = go(FakeClient(*ESTIMATE, reply(ANSWER)), guard, tmp_path / "on")
    off = go(FakeClient(*ESTIMATE, reply(ANSWER)), guard, tmp_path / "off", otel=False)
    kinds = [[r["type"] for r in trace(r.trace_path)] for r in (on, off)]
    assert kinds[0] == kinds[1] and kinds[0][-1] == "final"
    assert not (tmp_path / "off" / "otel").exists()


def test_invalid_tool_arguments_are_a_json_error_on_the_span(guard, tmp_path):  # noqa: F811
    client = FakeClient(reply("", call("build_circuit", "{not json")), *ESTIMATE, reply(ANSWER))
    go(client, guard, tmp_path)
    first = next(s for s in spans_of(tmp_path) if s["name"] == "tool_call")
    assert first["attributes"]["error.type"] == "JSONDecodeError"


class Failing:
    def complete(self, model, messages, max_tokens):
        raise TimeoutError("read timed out")


def test_a_failed_llm_call_is_an_error_span_and_the_trace_is_closed(guard, tmp_path):  # noqa: F811
    with pytest.raises(TimeoutError):
        go(Failing(), guard, tmp_path)
    spans = {s["name"]: s for s in spans_of(tmp_path)}
    llm = spans["llm_call"]
    assert llm["status"]["status_code"] == "ERROR"
    assert llm["attributes"]["error.type"] == "TimeoutError"
    assert spans["agent_run"]["attributes"]["stop_reason"].startswith("error: TimeoutError")
    assert trace(tmp_path / "t1.jsonl")[-1]["type"] == "final"

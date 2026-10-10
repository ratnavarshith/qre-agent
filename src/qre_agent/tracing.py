"""OpenTelemetry tracing with a local exporter: one trace per agent run, one JSON line per span in
runs/otel/<run_id>.jsonl. No collector or hosted service. See docs/agent-design.md."""

import json
from pathlib import Path

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor, SpanExporter, SpanExportResult

RESOURCE = Resource({"service.name": "qre-agent"})


class JsonLinesExporter(SpanExporter):
    """Appends each finished span to `path` as one line of OpenTelemetry's JSON."""

    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def export(self, spans):
        with self.path.open("a", encoding="utf-8") as f:
            for span in spans:
                f.write(span.to_json(indent=None) + "\n")
        return SpanExportResult.SUCCESS

    def shutdown(self):
        pass


def provider(path):
    """A tracer provider for one run, exporting to `path`. Not the global provider, so each run
    has its own file; call shutdown() when the run ends."""
    p = TracerProvider(resource=RESOURCE)
    p.add_span_processor(SimpleSpanProcessor(JsonLinesExporter(path)))
    return p


def read_spans(path):
    return [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines()]


NO_OP = trace.NoOpTracer()

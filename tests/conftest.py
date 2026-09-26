import pytest
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from app.telemetry import setup_telemetry

# Install the global providers before app.main is imported, so tests can inspect the signals.
_metric_reader = InMemoryMetricReader()
_span_exporter = InMemorySpanExporter()
setup_telemetry(extra_metric_readers=[_metric_reader], extra_span_exporters=[_span_exporter])


@pytest.fixture
def metric_reader():
    return _metric_reader


@pytest.fixture
def span_exporter():
    return _span_exporter

import logging
import sys

from opentelemetry import metrics, trace
from opentelemetry._logs import set_logger_provider
from opentelemetry.instrumentation.logging.handler import LoggingHandler
from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.sdk._logs.export import ConsoleLogRecordExporter, SimpleLogRecordProcessor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import ConsoleMetricExporter, PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import ConsoleSpanExporter, SimpleSpanProcessor


SERVICE_NAME = "order-tracker"

_configured = False


class _Stdout:
    """Resolve sys.stdout on every write, so redirected or captured streams don't go stale."""

    def write(self, text):
        return sys.stdout.write(text)

    def flush(self):
        sys.stdout.flush()


def setup_telemetry(extra_metric_readers=(), extra_span_exporters=()):
    """Send traces, metrics, and logs to the console. Later calls are no-ops.

    The metric export interval comes from OTEL_METRIC_EXPORT_INTERVAL (milliseconds).
    """
    global _configured
    if _configured:
        return
    _configured = True

    resource = Resource.create({"service.name": SERVICE_NAME})

    tracer_provider = TracerProvider(resource=resource)
    for exporter in (ConsoleSpanExporter(out=_Stdout()), *extra_span_exporters):
        tracer_provider.add_span_processor(SimpleSpanProcessor(exporter))
    trace.set_tracer_provider(tracer_provider)

    metric_readers = [PeriodicExportingMetricReader(ConsoleMetricExporter(out=_Stdout())), *extra_metric_readers]
    metrics.set_meter_provider(MeterProvider(resource=resource, metric_readers=metric_readers))

    logger_provider = LoggerProvider(resource=resource)
    logger_provider.add_log_record_processor(SimpleLogRecordProcessor(ConsoleLogRecordExporter(out=_Stdout())))
    set_logger_provider(logger_provider)
    app_logger = logging.getLogger("app")
    app_logger.setLevel(logging.INFO)
    app_logger.addHandler(LoggingHandler(level=logging.INFO, logger_provider=logger_provider))

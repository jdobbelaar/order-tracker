import logging
import os
import sys

from opentelemetry import metrics, trace
from opentelemetry._logs import set_logger_provider
from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.logging.handler import LoggingHandler
from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.sdk._logs.export import (
    BatchLogRecordProcessor,
    ConsoleLogRecordExporter,
    SimpleLogRecordProcessor,
)
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import ConsoleMetricExporter, PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter, SimpleSpanProcessor


SERVICE_NAME = "order-tracker"

_configured = False


class _Stdout:
    """Resolve sys.stdout on every write, so redirected or captured streams don't go stale."""

    def write(self, text):
        return sys.stdout.write(text)

    def flush(self):
        sys.stdout.flush()


def setup_telemetry(extra_metric_readers=(), extra_span_exporters=()):
    """Send traces, metrics, and logs to the console, and to an OTLP endpoint when configured.

    Set OTEL_EXPORTER_OTLP_ENDPOINT (for example http://otel-collector:4318) to also export over
    OTLP/HTTP. The metric export interval comes from OTEL_METRIC_EXPORT_INTERVAL (milliseconds).
    Later calls are no-ops.
    """
    global _configured
    if _configured:
        return
    _configured = True

    use_otlp = bool(os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT"))
    resource = Resource.create({"service.name": SERVICE_NAME})

    tracer_provider = TracerProvider(resource=resource)
    for exporter in (ConsoleSpanExporter(out=_Stdout()), *extra_span_exporters):
        tracer_provider.add_span_processor(SimpleSpanProcessor(exporter))
    if use_otlp:
        tracer_provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    trace.set_tracer_provider(tracer_provider)

    metric_readers = [
        PeriodicExportingMetricReader(ConsoleMetricExporter(out=_Stdout())),
        *extra_metric_readers,
    ]
    if use_otlp:
        metric_readers.append(PeriodicExportingMetricReader(OTLPMetricExporter()))
    metrics.set_meter_provider(MeterProvider(resource=resource, metric_readers=metric_readers))

    logger_provider = LoggerProvider(resource=resource)
    logger_provider.add_log_record_processor(
        SimpleLogRecordProcessor(ConsoleLogRecordExporter(out=_Stdout()))
    )
    if use_otlp:
        logger_provider.add_log_record_processor(BatchLogRecordProcessor(OTLPLogExporter()))
    set_logger_provider(logger_provider)
    app_logger = logging.getLogger("app")
    app_logger.setLevel(logging.INFO)
    app_logger.addHandler(LoggingHandler(level=logging.INFO, logger_provider=logger_provider))
    # Unhandled exceptions (the tracebacks behind a 500) are logged here by uvicorn.
    logging.getLogger("uvicorn.error").addHandler(
        LoggingHandler(level=logging.ERROR, logger_provider=logger_provider)
    )

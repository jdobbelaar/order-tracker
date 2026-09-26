# Order Tracker

A small order tracking app for the AI Dev Tools Zoomcamp observability homework. It includes a web page, API, tests, and a Docker Compose setup. You add telemetry, alerts, and an incident responder in Homework 4.

The main user flow is creating an order and checking its status. Three sample orders are created on first startup.

## Run it

You need Docker with Compose. To run the tests, you also need Python 3.11+ and `uv`.

```bash
docker compose up --build -d --wait
```

Open <http://127.0.0.1:8000>. The API is at `/api/orders`, and the health check is at `/healthz`. Data is stored in a Docker volume and survives container recreation.

If port 8000 is occupied, set `ORDER_TRACKER_PORT`, for example:

```bash
ORDER_TRACKER_PORT=18080 docker compose up --build -d --wait
```

Run tests with `uv run --frozen pytest -q`. Stop the app with `docker compose down`. Add `-v` only if you also want to delete the order data.

## Telemetry

The app emits OpenTelemetry traces, metrics, and logs. It always prints them to the console (`docker compose logs app`), and exports them over OTLP to an OpenTelemetry Collector, which fans them out to Prometheus (metrics), Loki (logs), and Tempo (traces). Grafana at <http://127.0.0.1:3000> opens on the **Order Tracker: requests and errors** dashboard, with no login because it only listens on loopback. Prometheus is at <http://127.0.0.1:9090>. Set `GRAFANA_PORT` or `PROMETHEUS_PORT` if those ports are taken.

Each order lookup produces an `order.lookup` span and an INFO or WARN log record that share a trace ID, so you can jump from a log line to its trace. The `order_tracker.http.requests` counter carries `http.route` and `http.response.status_code` attributes (Prometheus name `order_tracker_http_requests_total`). `/healthz` is excluded.

All configuration lives in `observability/`: the Collector pipeline, Prometheus scrape config, Loki and Tempo settings, and Grafana's provisioned data sources and dashboard. Edit the dashboard JSON there, not in the Grafana UI, or the change is lost when the container is recreated. Telemetry data is kept in Docker volumes, and `docker compose down -v` deletes it along with the orders.

## API

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/` | Web page |
| GET | `/healthz` | Database health check |
| GET | `/api/orders` | List orders |
| POST | `/api/orders` | Create an order |
| GET | `/api/orders/{id}` | Check an order |
| PATCH | `/api/orders/{id}` | Change an order status |

The app uses SQLite to keep setup small. Run one app container at a time. The course exercise is about detecting and handling an incident, not scaling the database.

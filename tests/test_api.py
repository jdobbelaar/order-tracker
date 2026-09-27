import pytest
from fastapi.testclient import TestClient

from app import main


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "DB_PATH", tmp_path / "orders.db")
    with TestClient(main.app) as test_client:
        yield test_client


def test_health_and_seeded_orders(client):
    assert client.get("/healthz").json() == {"status": "ok"}
    orders = client.get("/api/orders").json()
    assert len(orders) == 3
    assert {order["priority"] for order in orders} == {"standard", "express"}


def test_create_and_update_order(client):
    response = client.post(
        "/api/orders",
        json={"customer": "Taylor", "item": "Mug", "priority": "standard"},
    )
    assert response.status_code == 201
    order_id = response.json()["id"]
    assert client.get(f"/api/orders/{order_id}").json()["status"] == "received"
    updated = client.patch(f"/api/orders/{order_id}", json={"status": "shipped"})
    assert updated.status_code == 200
    assert updated.json()["status"] == "shipped"


def test_missing_order(client):
    assert client.get("/api/orders/missing").status_code == 404


def request_count(metric_reader, route, status_code):
    data = metric_reader.get_metrics_data()
    total = 0
    for resource_metrics in data.resource_metrics if data else []:
        for scope_metrics in resource_metrics.scope_metrics:
            for metric in scope_metrics.metrics:
                if metric.name != "order_tracker.http.requests":
                    continue
                for point in metric.data.data_points:
                    attributes = point.attributes
                    if (attributes["http.route"] == route
                            and attributes["http.response.status_code"] == status_code):
                        total += point.value
    return total


def test_lookup_metric_has_route_and_status_code(client, metric_reader):
    found_before = request_count(metric_reader, "/api/orders/{order_id}", 200)
    missing_before = request_count(metric_reader, "/api/orders/{order_id}", 404)
    order_id = client.get("/api/orders").json()[0]["id"]
    client.get(f"/api/orders/{order_id}")
    client.get("/api/orders/missing")
    assert request_count(metric_reader, "/api/orders/{order_id}", 200) == found_before + 1
    assert request_count(metric_reader, "/api/orders/{order_id}", 404) == missing_before + 1


def test_lookup_emits_span(client, span_exporter):
    span_exporter.clear()
    client.get("/api/orders/missing")
    lookup = [s for s in span_exporter.get_finished_spans() if s.name == "order.lookup"]
    assert len(lookup) == 1
    assert lookup[0].attributes["order.id"] == "missing"
    assert lookup[0].attributes["order.found"] is False


def test_express_order_placed_at_month_end(client):
    response = client.get("/api/orders")
    orders = response.json()
    express_orders = [o for o in orders if o["priority"] == "express"]
    assert len(express_orders) > 0
    for order in express_orders:
        response = client.get(f"/api/orders/{order['id']}")
        assert response.status_code == 200
        order_data = response.json()
        assert "estimated_delivery" in order_data

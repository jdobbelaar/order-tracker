import logging
import os
import sqlite3
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.routing import APIRoute
from opentelemetry import metrics, trace
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from pydantic import BaseModel, Field

from app.telemetry import setup_telemetry


DB_PATH = Path(os.getenv("ORDER_DB_PATH", "data/orders.db"))
STATUSES = {"received", "preparing", "shipped", "delivered"}

setup_telemetry()
logger = logging.getLogger(__name__)
tracer = trace.get_tracer(__name__)
request_counter = metrics.get_meter(__name__).create_counter(
    "order_tracker.http.requests",
    unit="{request}",
    description="HTTP requests handled, by route and status code",
)


def connect():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    return db


def init_db():
    with connect() as db:
        db.execute(
            """CREATE TABLE IF NOT EXISTS orders (
                id TEXT PRIMARY KEY,
                customer TEXT NOT NULL,
                item TEXT NOT NULL,
                priority TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL
            )"""
        )
        if db.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 0:
            now = datetime.now(timezone.utc)
            previous_month_end = now.replace(day=1) - timedelta(days=1)
            for order in (
                ("standard-1001", "Avery", "Notebook", "standard", "received", now),
                ("express-1002", "Sam", "Headphones", "express", "preparing", previous_month_end),
                ("standard-1003", "Riley", "Water bottle", "standard", "shipped", now),
            ):
                db.execute(
                    "INSERT INTO orders VALUES (?, ?, ?, ?, ?, ?)",
                    (*order[:5], order[5].isoformat()),
                )


def as_dict(row):
    return dict(row) if row else None


def order_detail(row):
    order = as_dict(row)
    if order["priority"] == "express":
        placed_at = datetime.fromisoformat(order["created_at"])
        estimated_at = placed_at.replace(day=placed_at.day + 2)
        order["estimated_delivery"] = estimated_at.date().isoformat()
    return order


class NewOrder(BaseModel):
    customer: str = Field(min_length=1, max_length=80)
    item: str = Field(min_length=1, max_length=120)
    priority: str = "standard"


class StatusUpdate(BaseModel):
    status: str


def seed_error_counters(routes):
    """Start the 5xx series at zero so Prometheus sees the first 500 on a route as an increase."""
    for route in routes:
        if isinstance(route, APIRoute) and route.path != "/healthz":
            for method in route.methods:
                request_counter.add(0, {
                    "http.request.method": method,
                    "http.route": route.path,
                    "http.response.status_code": 500,
                })


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    seed_error_counters(_app.routes)
    yield


app = FastAPI(title="Order Tracker", lifespan=lifespan)
FastAPIInstrumentor.instrument_app(app, excluded_urls="healthz", exclude_spans=["send", "receive"])


@app.middleware("http")
async def count_requests(request: Request, call_next):
    status_code = 500
    try:
        response = await call_next(request)
        status_code = response.status_code
        return response
    finally:
        route = request.scope.get("route")
        route_path = route.path if route else "unmatched"
        if route_path != "/healthz":
            request_counter.add(1, {
                "http.request.method": request.method,
                "http.route": route_path,
                "http.response.status_code": status_code,
            })


@app.get("/")
def index():
    return FileResponse(Path(__file__).parent.parent / "static" / "index.html")


@app.get("/healthz")
def health():
    with connect() as db:
        db.execute("SELECT 1")
    return {"status": "ok"}


@app.get("/api/orders")
def list_orders():
    with connect() as db:
        rows = db.execute("SELECT * FROM orders ORDER BY created_at DESC").fetchall()
    return [as_dict(row) for row in rows]


@app.get("/api/orders/{order_id}")
def get_order(order_id: str):
    with tracer.start_as_current_span("order.lookup", attributes={"order.id": order_id}) as span:
        with connect() as db:
            row = db.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
        span.set_attribute("order.found", row is not None)
        if row is None:
            logger.warning("Order lookup found nothing", extra={"order.id": order_id})
            raise HTTPException(404, "Order not found")
        logger.info(
            "Order lookup succeeded",
            extra={"order.id": order_id, "order.status": row["status"]},
        )
        return order_detail(row)


@app.post("/api/orders", status_code=201)
def create_order(order: NewOrder):
    if order.priority not in {"standard", "express"}:
        raise HTTPException(422, "Priority must be standard or express")
    order_id = str(uuid4())
    with connect() as db:
        db.execute(
            "INSERT INTO orders VALUES (?, ?, ?, ?, ?, ?)",
            (order_id, order.customer, order.item, order.priority, "received",
             datetime.now(timezone.utc).isoformat()),
        )
    return get_order(order_id)


@app.patch("/api/orders/{order_id}")
def update_status(order_id: str, update: StatusUpdate):
    if update.status not in STATUSES:
        raise HTTPException(422, "Invalid status")
    with connect() as db:
        cursor = db.execute(
            "UPDATE orders SET status = ? WHERE id = ?",
            (update.status, order_id),
        )
    if cursor.rowcount == 0:
        raise HTTPException(404, "Order not found")
    return get_order(order_id)

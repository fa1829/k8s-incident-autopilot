"""Demo service with built-in fault modes, so incidents can be reproduced on purpose.

FAULT_MODE (environment variable, read at startup):
  none        normal behaviour (default)
  memory_leak allocates memory in the background until the container is OOMKilled
  crash       exits shortly after start, so Kubernetes reports CrashLoopBackOff
  not_ready   liveness passes but readiness fails, so a rollout never completes
  slow        adds latency to every request (for latency alerts)
  errors      returns HTTP 500 on a share of requests (for error-rate alerts)
"""

from __future__ import annotations

import asyncio
import os
import random
import threading
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry, Counter, Histogram, generate_latest

VALID_MODES = {"none", "memory_leak", "crash", "not_ready", "slow", "errors"}

FAULT_MODE = os.getenv("FAULT_MODE", "none").strip().lower()
if FAULT_MODE not in VALID_MODES:
    FAULT_MODE = "none"

VERSION = os.getenv("APP_VERSION", "dev")
ERROR_RATE = float(os.getenv("ERROR_RATE", "0.3"))
SLOW_SECONDS = float(os.getenv("SLOW_SECONDS", "1.5"))
LEAK_MB_PER_SECOND = int(os.getenv("LEAK_MB_PER_SECOND", "8"))

REGISTRY = CollectorRegistry()
REQUESTS = Counter("http_requests_total", "HTTP requests", ["path", "status"], registry=REGISTRY)
LATENCY = Histogram(
    "http_request_duration_seconds",
    "HTTP request latency",
    ["path"],
    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2, 5),
    registry=REGISTRY,
)

_leak: list[bytes] = []


def _leak_memory() -> None:
    while True:
        _leak.append(b"x" * LEAK_MB_PER_SECOND * 1024 * 1024)
        time.sleep(1)


def _crash_soon() -> None:
    time.sleep(2)
    os._exit(1)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    if FAULT_MODE == "memory_leak":
        threading.Thread(target=_leak_memory, daemon=True).start()
    elif FAULT_MODE == "crash":
        threading.Thread(target=_crash_soon, daemon=True).start()
    yield


app = FastAPI(title="incident-autopilot-demo", version=VERSION, lifespan=lifespan)


@app.middleware("http")
async def observe(request: Request, call_next):
    path = request.url.path
    start = time.perf_counter()
    if path == "/" and FAULT_MODE == "slow":
        await asyncio.sleep(SLOW_SECONDS)
    if path == "/" and FAULT_MODE == "errors" and random.random() < ERROR_RATE:
        response: Response = JSONResponse({"error": "injected failure"}, status_code=500)
    else:
        response = await call_next(request)
    if path != "/metrics":
        LATENCY.labels(path=path).observe(time.perf_counter() - start)
        REQUESTS.labels(path=path, status=str(response.status_code)).inc()
    return response


@app.get("/")
def root() -> dict:
    return {"service": "incident-autopilot-demo", "version": VERSION, "fault_mode": FAULT_MODE}


@app.get("/healthz")
def healthz() -> dict:
    """Liveness: the process is up."""
    return {"status": "ok"}


@app.get("/readyz")
def readyz() -> Response:
    """Readiness: the pod should receive traffic."""
    if FAULT_MODE == "not_ready":
        return JSONResponse({"status": "not ready"}, status_code=503)
    return JSONResponse({"status": "ready"})


@app.get("/metrics")
def metrics() -> Response:
    return Response(generate_latest(REGISTRY), media_type=CONTENT_TYPE_LATEST)

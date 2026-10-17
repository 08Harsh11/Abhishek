import os
import secrets
import json
import threading
import time
import uuid
from collections import defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import jwt
from prometheus_client import CONTENT_TYPE_LATEST, Counter, generate_latest
import redis.asyncio as redis
import yaml
from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from fastapi.responses import JSONResponse, Response


BASE_DIR = Path(__file__).resolve().parent
JWT_ISSUER = "https://idp.exam.local"
JWT_AUDIENCE = "tds-37pgyvl7.apps.exam.local"
JWT_PUBLIC_KEY = """-----BEGIN PUBLIC KEY-----
MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEA2okOHspNjgA+2rTLbeuY
cxiP/hG8C6Sb9iwg3yiLAA4HCnpITcbWCSelbvbYGuc3EbNy4xFyf5Cbj5DHJMID
EkryOgyd2giIIIBOUBj8S63uGcnRpOBh9NFatfNwheKuzsPuVNldu6A9cNteNpXc
WyJjG2axVfmq7i6SuKr1JoWYG7xTTAvKPujSl4OtsQfO3h5NepzdfXpr28oNnzfW
ed+zclR6BcmNNo/WVfJ4xyCLSf0BCOgdTgW6PdaChd1l9VDetJZVEgC5tkyvXsfI
SI6iyrYbKR0NEBSqq4XkadEjsCs4F1RncsS4LlgniT7GlkL9Mce3b0wGLs9/7ZIX
dQIDAQAB
-----END PUBLIC KEY-----"""
REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")
redis_client = redis.from_url(REDIS_URL, decode_responses=True)
ANALYTICS_API_KEY = os.getenv("ANALYTICS_API_KEY", "")
USER_EMAIL = "24f3002908@ds.study.iitm.ac.in"
STARTED_AT = time.monotonic()
REQUEST_COUNTER = Counter("http_requests_total", "Total HTTP requests handled")
LOG_ENTRIES: deque[dict[str, Any]] = deque(maxlen=1000)
LOG_LOCK = threading.Lock()

DEFAULTS: dict[str, Any] = {
    "port": 8000,
    "workers": 1,
    "debug": False,
    "log_level": "info",
    "api_key": "default-secret-000",
}


def coerce(key: str, value: Any) -> Any:
    raw = str(value)
    if key in {"port", "workers"}:
        return int(raw)
    if key == "debug":
        return raw.strip().lower() in {"true", "1", "yes", "on"}
    return raw


def effective_config(overrides: list[str] | None = None) -> dict[str, Any]:
    config = dict(DEFAULTS)

    environment = os.getenv("APP_ENV", "development")
    yaml_path = BASE_DIR / f"config.{environment}.yaml"
    if yaml_path.exists():
        with yaml_path.open(encoding="utf-8") as stream:
            file_config = yaml.safe_load(stream) or {}
        config.update(file_config)

    # .env keys map to application keys. NUM_WORKERS is the documented alias.
    env_file_values = {
        "APP_PORT": "port",
        "NUM_WORKERS": "workers",
        "APP_WORKERS": "workers",
        "APP_DEBUG": "debug",
        "APP_LOG_LEVEL": "log_level",
        "APP_API_KEY": "api_key",
    }
    from dotenv import dotenv_values

    dotenv = dotenv_values(BASE_DIR / ".env")
    for source_key, target_key in env_file_values.items():
        value = dotenv.get(source_key)
        if value is not None:
            config[target_key] = coerce(target_key, value)

    # APP_* OS variables are the next layer. APP_ENV selects the YAML file.
    for source_key, value in os.environ.items():
        if not source_key.startswith("APP_") or source_key == "APP_ENV":
            continue
        target_key = source_key[4:].lower()
        config[target_key] = coerce(target_key, value)

    # CLI set parameters are applied last. Split only once so values may contain '='.
    for item in overrides or []:
        if "=" not in item:
            continue
        key, value = item.split("=", 1)
        key = key.strip().lower()
        if key:
            config[key] = coerce(key, value)

    config["api_key"] = "****"
    return config


app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://dash-cooukr.example.com",
        "https://exam.sanand.workers.dev",
    ],
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)


class VerifyRequest(BaseModel):
    token: str


class AnalyticsEvent(BaseModel):
    user: str
    amount: float
    ts: int


class AnalyticsRequest(BaseModel):
    events: list[AnalyticsEvent]


@app.middleware("http")
async def add_request_headers(request, call_next):
    started = time.perf_counter()
    request_id = str(uuid.uuid4())
    REQUEST_COUNTER.inc()
    try:
        response = await call_next(request)
    except Exception:
        # Keep the required headers on unexpected server-error responses too.
        response = JSONResponse(status_code=500, content={"detail": "Internal Server Error"})
    response.headers["X-Request-ID"] = request_id
    response.headers["X-Process-Time"] = f"{max(0.0, time.perf_counter() - started):.6f}"
    log_entry = {
        "level": "info",
        "ts": datetime.now(timezone.utc).isoformat(),
        "path": request.url.path,
        "request_id": request_id,
        "method": request.method,
        "status_code": response.status_code,
    }
    with LOG_LOCK:
        LOG_ENTRIES.append(log_entry)
    print(json.dumps(log_entry, separators=(",", ":")), flush=True)
    return response


@app.get("/effective-config")
def get_effective_config(set: list[str] | None = Query(default=None)) -> dict[str, Any]:
    return effective_config(set)


@app.get("/stats")
def get_stats(values: str = Query(...)) -> dict[str, Any]:
    try:
        numbers = [int(part.strip()) for part in values.split(",")]
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="values must be comma-separated integers") from exc
    if not numbers:
        raise HTTPException(status_code=422, detail="values must contain at least one integer")
    count = len(numbers)
    total = sum(numbers)
    return {
        "email": "24f3002908@ds.study.iitm.ac.in",
        "count": count,
        "sum": total,
        "min": min(numbers),
        "max": max(numbers),
        "mean": total / count,
    }


@app.post("/verify")
def verify_token(body: VerifyRequest) -> JSONResponse:
    try:
        claims = jwt.decode(
            body.token,
            JWT_PUBLIC_KEY,
            algorithms=["RS256"],
            issuer=JWT_ISSUER,
            options={"verify_aud": False, "require": ["iss", "aud", "exp"]},
        )
        if not isinstance(claims, dict):
            raise jwt.InvalidTokenError("JWT payload must be an object")
        if claims["aud"] != JWT_AUDIENCE:
            raise jwt.InvalidTokenError("audience mismatch")
        if not isinstance(claims.get("email"), str) or not isinstance(claims.get("sub"), str):
            raise jwt.InvalidTokenError("required identity claims are missing")
    except (jwt.InvalidTokenError, TypeError, KeyError, AttributeError):
        return JSONResponse(status_code=401, content={"valid": False})

    return JSONResponse(
        status_code=200,
        content={
            "valid": True,
            "email": claims["email"],
            "sub": claims["sub"],
            "aud": claims["aud"],
        },
    )


@app.post("/hit/{key}")
async def hit_counter(key: str) -> dict[str, Any]:
    count = await redis_client.incr(key)
    return {"key": key, "count": count}


@app.get("/count/{key}")
async def read_counter(key: str) -> dict[str, Any]:
    value = await redis_client.get(key)
    return {"key": key, "count": int(value) if value is not None else 0}


@app.get("/healthz")
async def health_check() -> dict[str, Any]:
    return {"status": "ok", "uptime_s": max(0.0, time.monotonic() - STARTED_AT)}


@app.get("/work")
def do_work(n: int = Query(..., ge=0)) -> dict[str, Any]:
    for value in range(n):
        _ = value * value
    return {"email": USER_EMAIL, "done": n}


@app.get("/metrics")
def metrics() -> Response:
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.get("/logs/tail")
def logs_tail(limit: int = Query(default=50, ge=0, le=1000)) -> list[dict[str, Any]]:
    with LOG_LOCK:
        return list(LOG_ENTRIES)[-limit:] if limit else []


@app.post("/analytics")
def analytics(
    body: AnalyticsRequest,
    api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict[str, Any]:
    if not secrets.compare_digest(api_key or "", ANALYTICS_API_KEY):
        raise HTTPException(status_code=401, detail="Unauthorized")

    user_totals: dict[str, float] = defaultdict(float)
    revenue = 0.0
    for event in body.events:
        if event.amount > 0:
            revenue += event.amount
            user_totals[event.user] += event.amount

    top_user = max(user_totals, key=user_totals.get) if user_totals else None
    return {
        "email": USER_EMAIL,
        "total_events": len(body.events),
        "unique_users": len({event.user for event in body.events}),
        "revenue": revenue,
        "top_user": top_user,
    }

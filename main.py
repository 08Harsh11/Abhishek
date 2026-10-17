import os
from pathlib import Path
from typing import Any

import yaml
from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware


BASE_DIR = Path(__file__).resolve().parent

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
    allow_origins=["*"],
    allow_methods=["GET", "OPTIONS"],
    allow_headers=["*"],
)


@app.get("/effective-config")
def get_effective_config(set: list[str] | None = Query(default=None)) -> dict[str, Any]:
    return effective_config(set)

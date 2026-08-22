from __future__ import annotations

import os


def _positive_int_from_env(name: str, default: int) -> int:
    raw_value = os.environ.get(name, str(default)).strip()
    try:
        value = int(raw_value)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer.") from exc
    if value <= 0:
        raise RuntimeError(f"{name} must be greater than zero.")
    return value


port = _positive_int_from_env("GDSA_PORT", 8765)
if port > 65535:
    raise RuntimeError("GDSA_PORT must be between 1 and 65535.")

wsgi_app = "practice_api:application"
bind = f"127.0.0.1:{port}"
workers = _positive_int_from_env("GDSA_WORKERS", 2)
worker_class = "sync"
timeout = 30
graceful_timeout = 30
keepalive = 5
accesslog = "-"
access_log_format = '%(h)s %(t)s "%(m)s %(U)s %(H)s" %(s)s %(b)s "%(a)s"'
errorlog = "-"
loglevel = os.environ.get("GDSA_LOG_LEVEL", "info").strip().lower() or "info"
capture_output = True
forwarded_allow_ips = "127.0.0.1"
secure_scheme_headers = {"X-FORWARDED-PROTO": "https"}
control_socket_disable = True


def on_starting(server: object) -> None:
    del server
    from practice_api import create_application_from_env

    create_application_from_env().repository.health()

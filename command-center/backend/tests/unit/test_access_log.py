"""Access logs without query strings: the text of a JARVIS Brain search (GET /api/brain/search?q=...) never reaches a
container log, neither uvicorn's (app/main.py) nor nginx's (dashboard/nginx.conf)."""

from __future__ import annotations

import logging
import re
from pathlib import Path

from uvicorn.logging import AccessFormatter

from app.main import NO_QUERY_STRING, create_app

NGINX_CONF = Path(__file__).resolve().parents[3] / "dashboard" / "nginx.conf"


def access_record(path: str) -> logging.LogRecord:
    """A record as uvicorn's HTTP protocols log a request."""
    return logging.LogRecord("uvicorn.access", logging.INFO, __file__, 1, '%s - "%s %s HTTP/%s" %d',
                             ("172.23.0.1:0", "GET", path, "1.1", 200), None)


def test_uvicorn_access_lines_lose_their_query_string():
    create_app(run_bootstrap=False)
    create_app(run_bootstrap=False)  # a second app adds nothing twice
    logger = logging.getLogger("uvicorn.access")
    assert logger.filters.count(NO_QUERY_STRING) == 1
    formatter = AccessFormatter('%(client_addr)s - "%(request_line)s" %(status_code)s', use_colors=False)
    record = access_record("/api/brain/search?q=msgquokka91&limit=5")
    assert logger.filter(record)
    line = formatter.format(record)
    assert line == '172.23.0.1:0 - "GET /api/brain/search HTTP/1.1" 200 OK'
    plain = access_record("/api/health")
    assert logger.filter(plain) and formatter.format(plain) == '172.23.0.1:0 - "GET /api/health HTTP/1.1" 200 OK'


def test_nginx_logs_request_lines_without_their_query_string():
    conf = NGINX_CONF.read_text(encoding="utf-8")
    fmt = re.search(r"log_format noquery ([^;]+);", conf)
    assert fmt and "$uri" in fmt.group(1)
    assert not re.search(r"\$(request|request_uri|args|query_string|arg_\w+)\b", fmt.group(1))
    assert re.findall(r"^\s*access_log\s+([^;]+);", conf, re.M) == ["/var/log/nginx/access.log noquery"]

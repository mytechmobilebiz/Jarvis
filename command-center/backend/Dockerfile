ARG REGISTRY=docker.io
FROM ${REGISTRY}/library/python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app
RUN useradd --system --uid 10001 --home-dir /app --shell /usr/sbin/nologin cc
COPY requirements.txt .
# EXTRA_CA_BUNDLE (optional build secret) is for networks whose egress proxy
# re-signs TLS. It adds trust for the build only; verification stays on.
RUN --mount=type=secret,id=extra_ca,required=false \
    if [ -s /run/secrets/extra_ca ]; then export PIP_CERT=/run/secrets/extra_ca; fi; \
    pip install -r requirements.txt
COPY app ./app
# APP_VERSION is this release's version, from the folder's VERSION file (the install and start scripts pass
# it). If the copied code says otherwise, Docker built from an out-of-date copy of the build folder: fail
# here rather than run old code. Empty (a manual build) skips the check.
ARG APP_VERSION=""
RUN if [ -n "$APP_VERSION" ]; then python -c "import sys, app; v = app.__version__; sys.exit(0 if v == sys.argv[1] else f'app/__init__.py in this build says {v}, but this release is {sys.argv[1]}: Docker used an out-of-date copy of the backend folder')" "$APP_VERSION"; fi

USER cc
EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=5 \
  CMD python -c "import sys, urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=4).status == 200 else 1)"
CMD ["uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*", "--no-server-header"]

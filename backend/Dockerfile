FROM python:3.12-slim

ARG APP_VERSION=v1

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    APP_VERSION=${APP_VERSION}

WORKDIR /app

RUN useradd --system --uid 10001 --user-group --home-dir /app app

COPY requirements.txt /tmp/requirements.txt
RUN test -s /etc/ssl/certs/ca-certificates.crt \
    && pip install --no-cache-dir -r /tmp/requirements.txt \
    && rm -f /tmp/requirements.txt

COPY --chown=app:app app.py /app/app.py

USER 10001
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD ["python", "-c", "import urllib.request as u; u.urlopen('http://127.0.0.1:8000/livez', timeout=4)"]

CMD ["gunicorn", "--workers", "2", "--bind", "0.0.0.0:8000", "--timeout", "30", "--no-control-socket", "--error-logfile", "-", "app:app"]

# One image for every environment: Docker Desktop now, cloud/Kubernetes later.
# Config comes only from EQUITY_* env vars; the container holds no state.
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONPATH=/app \
    TZ=Asia/Kolkata

RUN useradd --create-home --uid 10001 app
WORKDIR /app
RUN chown app:app /app

# Dependencies first so code edits don't invalidate this layer.
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY --chown=app:app . .
USER app

# Default is a no-op check; compose/Kubernetes pick the real command
# (e.g. `alembic upgrade head` as a one-shot migration job).
CMD ["python", "-c", "import core.db.models; print('equity image ok')"]

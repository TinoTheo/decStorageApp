# Build from the repo root so the browser library in web/src is included:
#   docker build -f coordinator/Dockerfile -t dstore-coordinator .
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

RUN useradd --create-home --uid 10001 dstore
WORKDIR /app

COPY coordinator/requirements.txt coordinator/requirements.txt
RUN pip install -r coordinator/requirements.txt

COPY web/src web/src
COPY coordinator coordinator

WORKDIR /app/coordinator
RUN DJANGO_SECRET_KEY=build-only python manage.py collectstatic --noinput \
    && mkdir -p /data/staging && chown -R dstore:dstore /data

USER dstore
ENV STAGING_DIR=/data/staging
EXPOSE 8000

# Segments are up to 4 MB each; a few workers with threads keep uploads flowing
# without holding many segments in memory at once.
CMD ["sh", "-c", "python manage.py migrate --noinput && exec gunicorn config.wsgi:application --bind 0.0.0.0:8000 --workers ${GUNICORN_WORKERS:-3} --threads ${GUNICORN_THREADS:-4} --timeout 120 --access-logfile -"]

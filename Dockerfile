FROM python:3.11-slim
WORKDIR /app

# WeasyPrint runtime deps (HTML→PDF rendering, used by the editor)
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpango-1.0-0 libpangoft2-1.0-0 libharfbuzz0b libcairo2 \
    libgdk-pixbuf-2.0-0 libffi8 fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

COPY requirements-server.txt .
# setuptools first (4.43.2): the base image's copy vendors old wheel / jaraco.context
# with HIGH advisories (found by a Trivy image scan).
RUN pip install --no-cache-dir --upgrade setuptools \
    && pip install --no-cache-dir -r requirements-server.txt
COPY . .
# The exact commit, for /api/health (4.43.2). .git is not copied into the image, so
# the build passes it in: update.sh and the release build set GIT_SHA.
ARG GIT_SHA=""
ENV PAWPOLLER_COMMIT=${GIT_SHA}

# Run as non-root for security — UID 1001 is fixed so Docker volume
# permissions can be set predictably during migration.  Port 8420 is
# above 1024 so no root privileges are needed to bind it.
RUN groupadd -r pawpoller && useradd -r -g pawpoller -u 1001 -d /app -s /sbin/nologin pawpoller \
    && mkdir -p /app/data /app/logs \
    && chown -R pawpoller:pawpoller /app
USER pawpoller

EXPOSE 8420
HEALTHCHECK --interval=60s --timeout=5s --start-period=30s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8420/api/health')" || exit 1
CMD ["python", "server.py"]

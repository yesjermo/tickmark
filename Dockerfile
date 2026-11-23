# Tickmark: one container serving the API and the built web app.
# Stage 1 builds the React frontend; stage 2 is a slim Python image that serves both with uvicorn.
FROM node:22-slim AS web
WORKDIR /src/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

FROM python:3.13-slim
ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHON_KEYRING_BACKEND=keyring.backends.null.Keyring \
    SWINGDESK_DATA_DIR=/app/data
WORKDIR /app
COPY backend/requirements.txt backend/requirements.txt
RUN pip install -r backend/requirements.txt
COPY backend/app backend/app
COPY --from=web /src/frontend/dist frontend/dist
RUN useradd --create-home --uid 10001 tickmark && mkdir -p /app/data && chown -R tickmark /app/data
USER tickmark
EXPOSE 8000
# --proxy-headers: the host terminates HTTPS; trust its X-Forwarded-* headers so cookies and links use https.
CMD ["sh", "-c", "uvicorn app.main:app --app-dir backend --host 0.0.0.0 --port ${PORT:-8000} --proxy-headers --forwarded-allow-ips='*'"]

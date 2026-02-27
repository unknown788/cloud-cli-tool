# ============================================================
# CloudLaunch — API Server Image
# Runs the FastAPI/Uvicorn server that powers the dashboard.
#
# Build:
#   docker build -t cloudlaunch-server .
#
# Run (pass credentials via --env-file):
#   docker run -d --name cloudlaunch \
#     --env-file .env \
#     -p 8000:8000 \
#     cloudlaunch-server
# ============================================================

FROM python:3.8-slim

# Keeps Python from generating .pyc files and enables unbuffered stdout/stderr
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Install dependencies first (layer cache-friendly)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# Railway injects PORT at runtime; fall back to 8000 for local use
EXPOSE ${PORT:-8000}

CMD ["sh", "-c", "python3 -m uvicorn api.app:app --host 0.0.0.0 --port ${PORT:-8000}"]

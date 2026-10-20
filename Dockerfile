FROM mcr.microsoft.com/playwright/python:v1.49.0-noble

WORKDIR /app

# Install Python requirements
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application source code
COPY . .

# Environment configuration
ENV PYTHONUNBUFFERED=1 \
    PORT=8000 \
    MAX_CONCURRENT_SCRAPES=5 \
    TIMEOUT_MS=20000

# Container listening port
EXPOSE 8000

# Production uvicorn server (1 worker because Playwright browser management is in-process async)
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]

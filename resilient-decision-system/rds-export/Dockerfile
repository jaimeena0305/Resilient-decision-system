FROM python:3.12-slim

# Prevent .pyc files and enable unbuffered stdout (better for Docker logs)
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Install dependencies first (layer-cached unless requirements.txt changes)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy source code
COPY . .

# Expose the API port
EXPOSE 8000

# Start the server
# Use --host 0.0.0.0 so the container accepts traffic from outside
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]

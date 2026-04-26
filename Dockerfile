FROM python:3.11-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y \
    libgl1 \
    libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application
COPY app/ ./app/

# Create config directory
RUN mkdir -p /app/config

# Set working directory to /app for proper imports
WORKDIR /app

# Run the main entry point
CMD ["python", "-u", "app/main.py"]

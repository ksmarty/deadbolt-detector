FROM python:3.11-slim

# No system packages are needed: opencv-python-headless is built without GUI
# (no libGL / libglib) dependencies.
WORKDIR /app

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application
COPY app/ ./app/

# Create config directory
RUN mkdir -p /app/config

# Run the main entry point
CMD ["python", "-u", "app/main.py"]

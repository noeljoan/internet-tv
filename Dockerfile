FROM python:3.12-slim

# Set working directory
WORKDIR /app

# Install system dependencies for any binary wheels
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements first to leverage Docker cache
COPY requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt \
    && pip install gunicorn

# Copy the application code
COPY src/ ./src/
COPY network/ ./network/

# Expose the port Gunicorn will run on
EXPOSE 8000

# Run with Gunicorn
CMD ["gunicorn", "-w", "4", "-b", "0.0.0.0:8000", "src.app:app"]
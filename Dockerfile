# =============================================================================
# Dockerfile - RedBus Dynamic Pricing Intelligence Platform
# Supports Streamlit Web Dashboard + Playwright Headless Scraping
# =============================================================================

# Base image with Python 3.11 and official Playwright Linux browser dependencies
FROM mcr.microsoft.com/playwright/python:v1.40.0-jammy

# Set working directory inside container
WORKDIR /app

# Prevent Python from writing .pyc files and buffer stdout
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    STREAMLIT_SERVER_PORT=8501 \
    STREAMLIT_SERVER_ADDRESS=0.0.0.0 \
    STREAMLIT_SERVER_HEADLESS=true \
    STREAMLIT_BROWSER_GATHER_USAGE_STATS=false

# Copy requirements and install
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Install Chromium and Firefox browser binaries with Playwright
RUN playwright install chromium firefox

# Copy the application code
COPY . .

# Expose default Streamlit port
EXPOSE 8501

# Container healthcheck
HEALTHCHECK CMD curl --fail http://localhost:8501/_stcore/health || exit 1

# Start the Streamlit server
CMD ["streamlit", "run", "app.py", "--server.port=8501", "--server.address=0.0.0.0"]

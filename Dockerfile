# =============================================================================
# Dockerfile - RedBus Dynamic Pricing Intelligence Platform
# Compatible with: Hugging Face Spaces (Port 7860), Render, Railway, Cloud Run
# =============================================================================

FROM mcr.microsoft.com/playwright/python:v1.40.0-jammy

# Set up user with UID 1000 for Hugging Face Spaces compatibility
RUN useradd -m -u 1000 user
ENV HOME=/home/user \
    PATH=/home/user/.local/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    STREAMLIT_SERVER_HEADLESS=true \
    STREAMLIT_BROWSER_GATHER_USAGE_STATS=false

WORKDIR $HOME/app

# Install dependencies as root
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Install Playwright browser binaries
RUN playwright install chromium firefox

# Copy application files and grant full ownership to user 1000
COPY --chown=user:user . $HOME/app
RUN chown -R user:user $HOME/app && chmod -R 777 $HOME/app

# Switch to non-root user
USER user

# Expose default Hugging Face (7860) and Streamlit (8501) ports
EXPOSE 7860 8501

# Healthcheck
HEALTHCHECK CMD curl --fail http://localhost:${PORT:-7860}/_stcore/health || exit 1

# Launch Streamlit dynamically on PORT (defaults to 7860 for Hugging Face)
CMD ["sh", "-c", "streamlit run app.py --server.port=${PORT:-7860} --server.address=0.0.0.0"]

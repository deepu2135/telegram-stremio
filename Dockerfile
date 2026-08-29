FROM python:3.10-slim

# Install system compilation packages for tgcrypto (C extensions)
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    gcc \
    python3-dev \
    git \
    && rm -rf /var/lib/apt/lists/*

# Set up a non-root application user
RUN useradd -m -u 1000 user

WORKDIR /app
RUN chown user:user /app

# Copy dependency specifications and install them globally
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt tgcrypto

# Copy application files and change ownership to the non-root user
COPY --chown=user:user . .

# Switch to the non-root user
USER user

# Expose port (default 7860)
EXPOSE 7860

# Command to run the addon dynamically supporting optional auto-update
CMD ["sh", "-c", "if [ \"$AUTO_UPDATE\" = \"true\" ]; then echo 'Auto-update enabled. Cloning latest code...'; git clone --depth=1 ${GITHUB_REPO_URL:-https://github.com/stremio-telegram-debrid/stremio-telegram-debrid.git} /tmp/app && cp -r /tmp/app/* . && rm -rf /tmp/app && pip install --no-cache-dir --user -r requirements.txt tgcrypto; fi && uvicorn addon:app --host 0.0.0.0 --port ${PORT:-7860}"]

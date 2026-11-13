FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    SONGETTER_LIBRARY_DIR=/library \
    XDG_CACHE_HOME=/cache

WORKDIR /app

# Install system runtime dependencies:
# - ffmpeg: audio stream probing, bitrate/metadata verification, and transcoding
# - nodejs: JavaScript runtime required by Mornye JS-based provider extensions
# - libsndfile1: native audio backend used by soundfile in verification/tests
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    nodejs \
    libsndfile1 \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Copy package metadata and source (downloads/ is excluded via .dockerignore)
COPY pyproject.toml README.md app.py ./
COPY src/ ./src/

RUN pip install --upgrade pip && pip install .

# Prepare runtime mount points for external music library and provider cache
RUN mkdir -p /library /cache

VOLUME ["/library"]

# Port 8501: current Streamlit UI (app.py)
# Port 8000: reserved for upcoming FastAPI server (Milestone 3: songetter.web.app)
EXPOSE 8501 8000

CMD ["streamlit", "run", "app.py", "--server.address=0.0.0.0", "--server.port=8501", "--server.headless=true"]

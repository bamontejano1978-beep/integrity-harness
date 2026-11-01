FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DEMO_HOST=0.0.0.0 \
    DEMO_PORT=7860 \
    DEMO_STATE_DIR=/data \
    META_MODEL_TIMEOUT_SECONDS=25

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src
COPY hackatons ./hackatons
COPY scripts ./scripts
COPY offline_verifier/requirements.txt ./offline_verifier/requirements.txt
COPY demo_app.py ./

RUN pip install --no-cache-dir -e . \
    && pip install --no-cache-dir -r offline_verifier/requirements.txt

RUN mkdir -p /data
VOLUME ["/data"]
EXPOSE 7860

CMD ["python", "-X", "utf8", "demo_app.py"]

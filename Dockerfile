FROM python:3.12-slim

WORKDIR /app

# paramiko needs cryptography's compiled deps at build time on some
# base images; slim already ships the required libs, but keep this
# minimal and explicit rather than relying on it silently.
RUN apt-get update \
    && apt-get install -y --no-install-recommends gcc libffi-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY main.py .

# config.yaml is provided at runtime via a bind mount / configmap, not
# baked into the image -- see docker-compose.yml
ENV PYTHONUNBUFFERED=1

ENTRYPOINT ["python", "main.py"]
CMD ["--config", "/app/config/config.yaml", "--interval", "900"]

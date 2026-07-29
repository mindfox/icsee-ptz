FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir flask==3.1.1

WORKDIR /workspace

COPY worker.sh /usr/local/bin/icsee-worker
RUN chmod +x /usr/local/bin/icsee-worker

ENTRYPOINT ["/usr/local/bin/icsee-worker"]

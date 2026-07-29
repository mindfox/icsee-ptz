FROM python:3.12-slim

RUN pip install --no-cache-dir flask==3.1.1 requests==2.32.4

WORKDIR /app

COPY web-supervisor.sh /usr/local/bin/icsee-web-supervisor
RUN chmod +x /usr/local/bin/icsee-web-supervisor

ENTRYPOINT ["/usr/local/bin/icsee-web-supervisor"]

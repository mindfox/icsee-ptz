FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY proxy.py ./
COPY multicam ./multicam

USER 65532:65532
EXPOSE 8080 8999
CMD ["python", "-m", "multicam.main"]

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY proxy.py config_loader.py relative_zoom.py ./

USER 65532:65532
EXPOSE 8999
CMD ["python", "/app/config_loader.py"]

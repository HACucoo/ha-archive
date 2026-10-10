FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY archiv ./archiv
COPY grafana ./grafana

# Healthy once the Grafana provisioning is in place, so Grafana can wait for it
HEALTHCHECK --interval=10s --timeout=3s CMD test -f /tmp/healthy
CMD ["python", "-m", "archiv"]

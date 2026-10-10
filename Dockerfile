# Offline town names from GeoNames for the "heute vor" view
FROM python:3.13-slim AS geo
COPY tools/build_geo.py /build_geo.py
RUN python /build_geo.py /geo /tmp/geonames && rm -rf /tmp/geonames

FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY archiv ./archiv
COPY grafana ./grafana
COPY --from=geo /geo ./geo

# Healthy once the Grafana provisioning is in place, so Grafana can wait for it
HEALTHCHECK --interval=10s --timeout=3s CMD test -f /tmp/healthy
CMD ["python", "-m", "archiv"]

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends chromium chromium-driver chromium-sandbox gosu \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 mietradar

COPY requirements-radar.txt requirements-send.txt ./
RUN pip install --no-cache-dir -r requirements-send.txt
COPY src/munich_radar.py src/munich_transit.py src/munich_delivery.py src/wg_source.py src/wg_alerts.py src/wg_preferences.py src/wg_delivery.py src/llm_personalizer.py ./src/
COPY config/llm_persona.txt ./config/
COPY --chmod=755 scripts/start-mietradar.sh /usr/local/bin/start-mietradar

ENTRYPOINT ["/usr/local/bin/start-mietradar"]
CMD ["python", "src/munich_radar.py", "watch"]

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app

RUN sed -i 's|http://deb.debian.org|https://deb.debian.org|g' /etc/apt/sources.list.d/debian.sources \
    && apt-get -o Acquire::Retries=3 update \
    && apt-get -o Acquire::Retries=3 install -y --no-install-recommends chromium chromium-driver chromium-sandbox gosu \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 mietradar

COPY requirements-radar.txt requirements-send.txt requirements-dashboard.txt ./
RUN pip install --no-cache-dir -r requirements-send.txt -r requirements-dashboard.txt
COPY src/munich_radar.py src/munich_transit.py src/munich_delivery.py src/wg_source.py src/wg_observations.py src/wg_alerts.py src/wg_preferences.py src/wg_delivery.py src/llm_personalizer.py src/kleinanzeigen_source.py src/kleinanzeigen_radar.py src/kleinanzeigen_delivery.py ./src/
COPY src/munich_tiers.py ./src/
COPY src/listing_profiles.py ./src/
COPY src/kleinanzeigen_activity.py src/kleinanzeigen_judge.py src/kleinanzeigen_replies.py ./src/
COPY src/kleinanzeigen_reply_queue.py src/kleinanzeigen_response.py src/outgoing_judge.py ./src/
COPY src/judge_smoke.py src/integration_smoke.py ./src/
COPY src/dashboard.py src/dashboard_auth.py src/dashboard_data.py src/munich_service.py ./src/
COPY config/llm_persona.txt ./config/
COPY --chmod=755 scripts/start-mietradar.sh /usr/local/bin/start-mietradar

ENTRYPOINT ["/usr/local/bin/start-mietradar"]
CMD ["python", "src/munich_service.py"]

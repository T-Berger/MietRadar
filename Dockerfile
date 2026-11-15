FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app

COPY requirements-radar.txt ./
RUN pip install --no-cache-dir -r requirements-radar.txt
COPY src/munich_radar.py src/munich_transit.py src/wg_source.py src/wg_alerts.py src/wg_preferences.py src/llm_personalizer.py ./src/
COPY config/llm_persona.txt ./config/

CMD ["python", "src/munich_radar.py", "watch"]

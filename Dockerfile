FROM python:3.12-slim

WORKDIR /app
COPY server.py .
COPY static/ static/

RUN pip install --no-cache-dir fastapi uvicorn

EXPOSE 8797

ENV SPUR_HOST=0.0.0.0
ENV SPUR_PORT=8797
ENV SPUR_DB=/data/spur.db

VOLUME /data

CMD ["python", "server.py"]

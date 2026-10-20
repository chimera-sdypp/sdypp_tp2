# Servicio tarea-video: ffmpeg + un servidor HTTP de la biblioteca estándar (sin pip).
# Es la misma imagen para el Hit 2 y el Hit 3: cerberusdistribuido/tarea-video.
FROM debian:trixie-slim
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg python3 \
    && rm -rf /var/lib/apt/lists/*
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
# UID 10001, el mismo que el servidor: los dos leen y escriben el volumen /trabajos.
RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin tarea
COPY tarea_video.py /app/tarea_video.py
USER tarea
EXPOSE 8080
CMD ["python3", "/app/tarea_video.py"]

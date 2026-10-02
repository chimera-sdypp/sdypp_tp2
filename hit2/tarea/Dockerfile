# Servicio tarea del Hit 2. Sólo biblioteca estándar: no hay requirements.
FROM python:3.14-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin tarea
COPY tarea.py /app/tarea.py
USER tarea
EXPOSE 8080
CMD ["python", "/app/tarea.py"]

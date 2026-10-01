FROM python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TP2_DIR_LOGS=/app/logs

WORKDIR /app

# requirements.txt antes que el código: esta capa se reutiliza mientras no
# cambien las dependencias.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app/ ./app/

# Sin root. El acceso al socket de Docker se da desde el compose con `group_add`.
RUN useradd --uid 10001 --no-create-home servidor && mkdir logs && chown servidor logs
USER servidor

EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=4)"]
CMD ["uvicorn", "--factory", "app.main:crear_app", "--host", "0.0.0.0", "--port", "8080"]

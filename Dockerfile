# Modellvergleich: Web-Oberfläche (Python-Standardbibliothek + Datenbank-Treiber)
FROM python:3.13-slim

# Datenbank-Treiber (PostgreSQL, MySQL) und das Add-on SymPy – alles andere nutzt nur die Standardbibliothek
RUN pip install --no-cache-dir "psycopg[binary]==3.*" "pymysql==1.*" "sympy==1.*"

WORKDIR /app
COPY . .
RUN find . -name __pycache__ -prune -exec rm -rf {} + \
 && useradd --system --uid 10001 --no-create-home app \
 && mkdir -p /data && chown app /data

USER app
ENV HOST=0.0.0.0 PORT=8720 DATEN_DIR=/data PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
EXPOSE 8720
VOLUME ["/data"]
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
  CMD python3 -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8720/api/einstellungen', timeout=4)"
CMD ["python3", "server.py"]

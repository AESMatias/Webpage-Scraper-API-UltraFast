#!/usr/bin/env bash
set -euo pipefail

# ==============================================================================
# Script de Despliegue Automatizado para Scraper API UltraFast (Docker Compose)
# ==============================================================================

WORK_DIR="/root/scraper_api"
CONTAINER_NAME="scraper-api"

echo "=========================================================="
echo " [1/6] Verificando entorno y permisos en el VPS..."
echo "=========================================================="
if [[ "$EUID" -ne 0 ]]; then
   echo "[ERROR] Este script debe ejecutarse como root en el servidor." >&2
   exit 1
fi

command -v docker >/dev/null 2>&1 || { echo "[ERROR] Docker no está instalado." >&2; exit 1; }
docker compose version >/dev/null 2>&1 || { echo "[ERROR] Docker Compose no está disponible." >&2; exit 1; }

echo "=========================================================="
echo " [2/6] Creando backup preventivo de /root/scraper_api..."
echo "=========================================================="
if [[ -d "$WORK_DIR" ]]; then
    BACKUP_DIR="/root/scraper_api_backup_$(date +%Y%m%d_%H%M%S)"
    echo "Guardando copia en: $BACKUP_DIR"
    cp -r "$WORK_DIR" "$BACKUP_DIR"
else
    echo "Creando directorio $WORK_DIR..."
    mkdir -p "$WORK_DIR"
fi

# ==============================================================================
# Detener proceso uvicorn manual/nohup si aún existiera
# ==============================================================================
echo "Verificando si existen procesos uvicorn antiguos no dockerizados..."
OLD_PIDS=$(pgrep -f "uvicorn.*(main:app|main.py)" || true)
if [[ -n "$OLD_PIDS" ]]; then
    echo "Deteniendo procesos uvicorn antiguos (PIDs: $OLD_PIDS)..."
    kill -15 $OLD_PIDS || true
    sleep 2
fi

echo "=========================================================="
echo " [3/6] Sincronizando archivos hacia $WORK_DIR..."
echo "=========================================================="
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

cp Dockerfile compose.yaml requirements.txt .dockerignore main.py "$WORK_DIR/"
cd "$WORK_DIR"

echo "=========================================================="
echo " [4/6] Construyendo e iniciando el contenedor Docker..."
echo "=========================================================="
# Iniciar únicamente el servicio scraper-api sin tocar otros contenedores
docker compose build --pull
docker compose up -d --remove-orphans

echo "Esperando 10 segundos para inicialización de Chromium..."
sleep 10

echo "=========================================================="
echo " [5/6] Verificando salud del contenedor..."
echo "=========================================================="
docker ps --filter "name=$CONTAINER_NAME"

echo -n "Comprobando endpoint /health en 127.0.0.1:8000... "
HEALTH_8000=$(curl -s -m 5 "http://127.0.0.1:8000/health" || echo "FAIL")
echo "$HEALTH_8000"

echo -n "Comprobando endpoint /health en 127.0.0.1:8001 (Nginx upstream)... "
HEALTH_8001=$(curl -s -m 5 "http://127.0.0.1:8001/health" || echo "FAIL")
echo "$HEALTH_8001"

if [[ "$HEALTH_8000" != *"ok"* ]]; then
    echo "[ADVERTENCIA] El health check no devolvió status: ok. Logs recientes:"
    docker compose logs --tail=50
    exit 1
fi

echo "=========================================================="
echo " [6/6] Ejecutando scraping de prueba en localhost..."
echo "=========================================================="
TEST_RESULT=$(curl -s -X POST "http://127.0.0.1:8000/scrape" \
    -H "Content-Type: application/json" \
    -d '{"domain": "example.com"}')

echo "Resultado del test:"
echo "$TEST_RESULT" | head -n 30

echo "=========================================================="
echo " ¡Despliegue completado con éxito en 127.0.0.1:8000 y 127.0.0.1:8001!"
echo "=========================================================="

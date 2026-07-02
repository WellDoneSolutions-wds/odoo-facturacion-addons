#!/bin/bash
# Deploy de addons en el EC2 — lo invoca el workflow deploy.yml vía SSM Run
# Command (rol OIDC odoo-mype-deploy-addon, definido en el repo de infra).
#
# Uso: deploy-addons.sh <git-sha>
#
# 1. Trae el código del repo al SHA exacto que CI aprobó
# 2. Descubre los addons TRACKEADOS en ese commit (addons/*/__manifest__.py)
# 3. Por cada BD: pg_dump pre-deploy y --update de los addons del repo que
#    esa BD tenga instalados (todos en una sola pasada de Odoo)
# 4. Reinicio del stack + espera de salud + invalidación de estáticos
#
# Por qué addons trackeados y no los del filesystem: los untracked del EC2
# (ej. l10n_pe_partner_lookup hasta que se mergee su rama) no cambian durante
# un deploy — el CI solo debe actualizar lo que el commit realmente trae.
# Al mergear la rama de un addon nuevo, se incorpora solo.
#
# El workflow lo copia a /tmp antes de ejecutarlo (un script no debe
# reescribirse a sí mismo durante el checkout). El password de la BD nunca
# vive acá: se toma del env PASSWORD del contenedor odoo (docker-compose.yml).
#
# NUNCA usar `git clean` acá: hay addons untracked en el EC2 (ver arriba).
set -euo pipefail

SHA=$1
if [ -z "$SHA" ]; then echo "Uso: $0 <git-sha>"; exit 1; fi
# Mismos valores hardcodeados que update-odoo.sh hornea vía Terraform:
REGION=us-east-1
DIST_PARAM=/odoo-mype/cloudfront-distribution-id

cd /home/ubuntu/odoo-facturacion-addons
git fetch origin
git checkout -B main "$SHA"
echo "Código en $(git rev-parse --short HEAD)"

# Addons del repo en este commit: addons/<nombre>/__manifest__.py trackeado
MODULOS=$(git ls-files 'addons/*/__manifest__.py' | awk -F/ '{print $2}' | sort -u | paste -sd, -)
if [ -z "$MODULOS" ]; then
  echo "ERROR: el commit no trae ningún addon trackeado en addons/"
  exit 1
fi
echo "Addons del repo: $MODULOS"
SQL_IN=$(echo "$MODULOS" | sed "s/,/','/g")

cd /home/ubuntu
mkdir -p backups

DBS=$(docker compose exec -T postgres psql -U odoo -d postgres -tAc \
  "SELECT datname FROM pg_database WHERE NOT datistemplate AND datname <> 'postgres'")

ACTUALIZADAS=""
for DB in $DBS; do
  INSTALADOS=$(docker compose exec -T postgres psql -U odoo -d "$DB" -tAc \
    "SELECT string_agg(name, ',') FROM ir_module_module WHERE name IN ('$SQL_IN') AND state IN ('installed','to upgrade')" 2>/dev/null || true)
  if [ -z "$INSTALADOS" ]; then
    echo "-- $DB: ningún addon del repo instalado, se salta"
    continue
  fi
  echo "-- $DB: backup pre-deploy"
  docker compose exec -T postgres pg_dump -U odoo -Fc "$DB" > "backups/pre-deploy-$DB.dump"
  echo "-- $DB: actualizando [$INSTALADOS]"
  docker compose exec -T -e DB="$DB" -e MODS="$INSTALADOS" odoo sh -c \
    'odoo -d "$DB" --db_host "$HOST" --db_user "$USER" --db_password "$PASSWORD" --update="$MODS" --stop-after-init --no-http --workers 0 --max-cron-threads 0'
  ACTUALIZADAS="$ACTUALIZADAS $DB"
done

if [ -z "$ACTUALIZADAS" ]; then
  echo "Ninguna BD tiene addons del repo instalados - nada que actualizar (no se reinicia)"
  exit 0
fi

echo "Reiniciando stack (BDs actualizadas:$ACTUALIZADAS)"
# build ANTES de bajar el stack: si el Dockerfile cambió (ej. nuevas deps
# Python como boto3), la imagen se reconstruye con el viejo stack aún arriba
# (menos downtime); si no cambió, el cache lo hace instantáneo.
docker compose build odoo
docker compose down
docker compose up -d

OK=""
for _ in $(seq 1 36); do
  sleep 5
  if curl -fsS -o /dev/null http://localhost:8069/web/health; then OK=1; break; fi
done
if [ -z "$OK" ]; then
  echo "ERROR: Odoo no respondió /web/health tras 3 minutos"
  exit 1
fi
echo "Odoo responde OK"

DIST_ID=$(aws ssm get-parameter --name "$DIST_PARAM" \
  --query Parameter.Value --output text --region "$REGION")
aws cloudfront create-invalidation --distribution-id "$DIST_ID" \
  --paths "/web/static/*" --region "$REGION" > /dev/null && echo "Cache CloudFront invalidado"

find backups -name 'pre-deploy-*.dump' -mtime +14 -delete
echo "Deploy completado: [$MODULOS] @ $SHA"

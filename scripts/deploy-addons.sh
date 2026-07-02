#!/bin/bash
# Deploy de addons en el EC2 — lo invoca el workflow deploy.yml vía SSM Run
# Command (rol OIDC odoo-mype-deploy-addon, definido en el repo de infra).
#
# Uso: deploy-addons.sh <git-sha>
#
# 1. Trae el código del repo al SHA exacto que CI aprobó
# 2. pg_dump local de cada BD que tenga el módulo (rollback rápido)
# 3. --update del módulo en cada una de esas BDs (todas, no una: tras el
#    restart el código nuevo aplica a todas, el esquema debe acompañarlo)
# 4. Reinicio del stack + espera de salud + invalidación de estáticos
#
# El workflow lo copia a /tmp antes de ejecutarlo (un script no debe
# reescribirse a sí mismo durante el checkout). El password de la BD nunca
# vive acá: se toma del env PASSWORD del contenedor odoo (docker-compose.yml).
#
# NUNCA usar `git clean` acá: l10n_pe_partner_lookup vive como archivos
# untracked en el EC2 hasta que se mergee su rama.
set -euo pipefail

SHA=$1
if [ -z "$SHA" ]; then echo "Uso: $0 <git-sha>"; exit 1; fi
MODULO=l10n_pe_ne_biller
# Mismos valores hardcodeados que update-odoo.sh hornea vía Terraform:
REGION=us-east-1
DIST_PARAM=/odoo-mype/cloudfront-distribution-id

cd /home/ubuntu/odoo-facturacion-addons
git fetch origin
git checkout -B main "$SHA"
echo "Código en $(git rev-parse --short HEAD)"

cd /home/ubuntu
mkdir -p backups

DBS=$(docker compose exec -T postgres psql -U odoo -d postgres -tAc \
  "SELECT datname FROM pg_database WHERE NOT datistemplate AND datname <> 'postgres'")

ACTUALIZADAS=""
for DB in $DBS; do
  TIENE=$(docker compose exec -T postgres psql -U odoo -d "$DB" -tAc \
    "SELECT 1 FROM ir_module_module WHERE name='$MODULO' AND state IN ('installed','to upgrade')" 2>/dev/null || true)
  if [ "$TIENE" != "1" ]; then
    echo "-- $DB: sin $MODULO, se salta"
    continue
  fi
  echo "-- $DB: backup pre-deploy"
  docker compose exec -T postgres pg_dump -U odoo -Fc "$DB" > "backups/pre-deploy-$DB.dump"
  echo "-- $DB: actualizando $MODULO"
  docker compose exec -T -e DB="$DB" odoo sh -c \
    'odoo -d "$DB" --db_host "$HOST" --db_user "$USER" --db_password "$PASSWORD" --update=l10n_pe_ne_biller --stop-after-init --no-http --workers 0 --max-cron-threads 0'
  ACTUALIZADAS="$ACTUALIZADAS $DB"
done

if [ -z "$ACTUALIZADAS" ]; then
  echo "Ninguna BD tiene $MODULO instalado - nada que actualizar (no se reinicia)"
  exit 0
fi

echo "Reiniciando stack (BDs actualizadas:$ACTUALIZADAS)"
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
echo "Deploy completado: $MODULO @ $SHA"

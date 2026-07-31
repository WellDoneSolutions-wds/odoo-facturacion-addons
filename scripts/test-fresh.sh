#!/usr/bin/env bash
# Corre la suite del addon contra una BD FRESCA (limpia + Plan Contable Perú cargado), para tener
# SEÑAL REAL — sin el ruido de la BD local sucia (varios tests _vacio/lotes fallan solo por datos
# acumulados) y sin el error "no tiene impuesto SUNAT" (los tests buscan el IGV 1000, que solo
# existe si el CoA l10n_pe está cargado en la compañía).
#
# Uso:
#   scripts/test-fresh.sh                         # instala l10n_pe_ne_biller + corre /l10n_pe_ne_biller
#   scripts/test-fresh.sh "MODULOS" "TEST-TAGS"   # p.ej. "l10n_pe_ne_biller,l10n_pe_ne_roles" "/l10n_pe_ne_biller:TestNotaVenta"
#   KEEP=1 scripts/test-fresh.sh                   # no borra la BD al terminar (para inspeccionarla)
#
# Requisitos: el ne-stack Docker corriendo (contenedores ne-stack-odoo-1 / ne-stack-db-1).
# Overridables por env: ODOO_CT, DB_CT, DB, DBUSER, DBPASS.
set -euo pipefail

ODOO_CT=${ODOO_CT:-ne-stack-odoo-1}
DB_CT=${DB_CT:-ne-stack-db-1}
DB=${DB:-odoo_test_fresh}
DBUSER=${DBUSER:-odoo}
DBPASS=${DBPASS:-odoo}
MODULES=${1:-l10n_pe_ne_biller}
TAGS=${2:-/l10n_pe_ne_biller}
HERE="$(cd "$(dirname "$0")" && pwd)"

DBARGS="--db_host=db --db_user=$DBUSER --db_password=$DBPASS --http-port=8899 --gevent-port=8901"
run() { docker exec "$ODOO_CT" odoo -d "$DB" $DBARGS --stop-after-init "$@"; }

echo ">> [1/4] BD fresca: $DB (drop + create)"
# Cierra conexiones colgadas (una corrida anterior interrumpida deja sesiones) antes del DROP.
docker exec "$DB_CT" psql -U "$DBUSER" -d postgres -c \
  "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname='$DB' AND pid<>pg_backend_pid();" >/dev/null 2>&1 || true
docker exec "$DB_CT" psql -U "$DBUSER" -d postgres -c "DROP DATABASE IF EXISTS $DB;" >/dev/null
docker exec "$DB_CT" psql -U "$DBUSER" -d postgres -c "CREATE DATABASE $DB OWNER $DBUSER;" >/dev/null

echo ">> [2/4] instalar módulos ($MODULES) sin demo"
run -i "$MODULES" --without-demo=all --log-level=warn >/dev/null

echo ">> [3/4] cargar Plan Contable Perú (IGV 1000)"
docker exec -i "$ODOO_CT" odoo shell -d "$DB" $DBARGS --log-level=error \
  < "$HERE/load_coa.py" 2>&1 | grep -E '== OK|== ERR' || true

echo ">> [4/4] correr tests ($TAGS)"
set +e
OUT=$(run -u "$MODULES" --test-tags "$TAGS" --log-level=test 2>&1)
set -e
echo "$OUT" | grep -iE 'FAIL:|ERROR: .*test|Traceback|of [0-9]+ tests|failed,' | grep -viE 'INFO|Starting' || true
echo "-----------------------------------------------------------------"
echo "$OUT" | grep -iE 'of [0-9]+ tests' | tail -1

if [ "${KEEP:-0}" != "1" ]; then
  docker exec "$DB_CT" psql -U "$DBUSER" -d postgres -c "DROP DATABASE IF EXISTS $DB;" >/dev/null
  echo ">> BD $DB borrada (KEEP=1 para conservarla)"
fi

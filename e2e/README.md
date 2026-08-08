# E2E contra SUNAT beta

Cómo emitir comprobantes reales (Odoo → `ms-ne-biller` → SUNAT beta) y verificar que el CDR
vuelve con `ResponseCode 0`.

## Archivos

| Archivo | Qué es |
|---|---|
| `harness.py` | El motor. Corre dentro de `odoo shell`, lee casos de `E2E_CASES_FILE`, emite y escribe resultados en `E2E_RESULTS_FILE`. Cada caso en su savepoint; al final hace rollback (los envíos a SUNAT ya ocurrieron como efecto externo, los registros Odoo no se persisten) |
| `plan.json` | Corpus principal: 66 casos (factura, boleta, nc, nd, ra, rc, retención, percepción) |
| `casos-verticales.json` | Los 10 casos de los verticales por unidad de medida y detracción — ver abajo |
| `prelude_sincrono.py` | Prólogo que fuerza el canal síncrono. **Casi siempre lo vas a necesitar** |
| `smoke_biller.py`, `caja_flow.py`, `e2e_masivo.py`, `e2e_ui_flow.py` | Flujos específicos |

## Receta completa desde cero

`/tmp/biller` es **efímero**: se pierde al reiniciar la máquina y hay que re-sembrarlo.

### 1. Truststore con la cadena FRESCA de SUNAT

⚠️ El `truststore.jks` de la raíz del workspace es el **dummy del README**: alcanza para que
el biller arranque (Quarkus valida el trust-store *eager*), pero al enviar da
`javax.net.ssl.SSLHandshakeException`. Hay que rehacerlo con la cadena real:

```bash
openssl s_client -connect e-beta.sunat.gob.pe:443 -servername e-beta.sunat.gob.pe \
  -showcerts </dev/null 2>/dev/null \
  | awk '/-----BEGIN CERTIFICATE-----/{n++} n{print > ("cert-" n ".pem")}'
for f in cert-*.pem; do
  keytool -importcert -noprompt -alias "sunat-${f%.pem}" -file "$f" \
    -keystore truststore.jks -storepass changeit
done
cp truststore.jks /tmp/biller/ALMCERT/
```

Son 4 certificados (leaf `*.sunat.gob.pe` + 2 Sectigo + USERTrust). No uses el `sunat.crt`
del repo: caduca.

### 2. Keystore de firma

`/tmp/biller/ALMCERT/FacturadorKey.jks`, alias `certContribuyente`, pass `changeit`.
SUNAT beta **no valida la cadena del certificado**, así que sirve uno auto-firmado. Hay una
copia en la raíz del workspace.

### 3. Levantar y reiniciar

```bash
docker compose up -d db odoo ms-ne-biller biller-pdf gateway
docker compose restart ms-ne-biller   # el TLS se carga EAGER: obligatorio tras tocar los .jks
curl -s -o /dev/null -w '%{http_code}\n' http://localhost:8090/q/health   # 200
```

### 4. Correr

```bash
cat e2e/prelude_sincrono.py e2e/harness.py > /tmp/harness-sync.py
docker cp e2e/casos-verticales.json <ct-odoo>:/tmp/casos.json

docker exec -i -e E2E_CASES_FILE=/tmp/casos.json -e E2E_RESULTS_FILE=/tmp/res.json \
  <ct-odoo> odoo shell -d odoo_ne_biller \
  --db_host=db --db_user=odoo --db_password=odoo --http-port=8897 --gevent-port=8903 \
  --log-level=error < /tmp/harness-sync.py

docker exec <ct-odoo> cat /tmp/res.json
```

`E2E_DRY=1` arma los comprobantes y corre las reglas L1 **sin** tocar SUNAT: úsalo primero,
es gratis y caza los errores de armado.

Resultado por caso: `state=enviado` + `code=0` = aceptado.

## Los verticales (`casos-verticales.json`)

10 casos que ejercitan las unidades SUNAT y la detracción de cada vertical. Re-verificados
**2026-08-07 → 10/10 CDR ResponseCode 0**:

| Caso | Vertical | Qué ejercita |
|---|---|---|
| `PESO-KGM-BOL` / `PESO-KGM-FAC` | Venta al peso / balanza | `KGM` con cantidad decimal |
| `FERR-MTR` / `FERR-MTK` | Ferretería | metro / metro cuadrado |
| `MAD-MTQ` | Maderera | metro cúbico |
| `TEX-MTR` / `TEX-DZN` | Textil | metro / **docena `DZN`** (no `DPC`) |
| `SERV-HUR` / `SERV-DAY` | Servicios por tiempo | hora / día |
| `ALQ-DETR-019` | Alquiler | detracción **019** arrendamiento al 10% |

El harness acepta **cualquier** código de unidad de cat-03 vía `l10n_pe_ne_unit_code`, aunque
la UoM de Odoo caiga a "unidad": `_uom()` solo mapea NIU/KGM/ZZ/MTR/LTR y eso **no** limita
lo que sale en el XML.

## Limitaciones conocidas

- **`doc` soportados**: `factura`, `boleta`, `nc`, `nd`, `ra`, `rc`, `retencion`,
  `percepcion`, `anticipo_ciclo`. **No hay `liquidacion`** (comprobante tipo 04).
- **Liquidación de compra (04) no está validada en beta** y no es un bug de código: SUNAT
  responde `soap-env:Client 0151` en los endpoints `gem` y `otros-cpe` porque el RUC de
  prueba no está enrolado para ese comprobante. Es tarea de OPS.
- El canal **asíncrono** (resúmenes RC/RA) exige el usuario SOL con el RUC prefijado
  (`20321856145MODDATOS`): `sendBill` es laxo y lo acepta sin RUC, pero `getStatus` da 401.

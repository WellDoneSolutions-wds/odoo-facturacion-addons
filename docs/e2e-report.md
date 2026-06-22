# Reporte E2E — Integración Odoo Community ↔ Facturador (ms-ne-biller)

**Fecha de corrida:** 2026-06-19
**Resultado global:** ✅ ÉXITO — los **4 comprobantes** (Factura 01, Boleta 03, Nota de Crédito 07, Nota de Débito 08) creados en Odoo 19 Community fueron emitidos a SUNAT beta y **aceptados (CDR ResponseCode 0)**, disparados desde el botón de la UI web.

## Resumen multi-documento (todos aceptados, ResponseCode 0)

| Comprobante | move id | Serie-Correlativo | Endpoint biller | CDR |
|---|---|---|---|---|
| Factura (01) | 14 | F001-81909926 | `/generator/factura` | ✅ aceptada |
| Boleta (03) | 39 | B001-81911196 | `/generator/factura` | ✅ aceptada |
| Nota de Crédito (07) | 64 | FC01-81911980 | `/generator/notaCredito` | ✅ aceptada |
| Nota de Débito (08) | 53 | FD01-81911656 | `/generator/notaDebito` | ✅ aceptada |

Ruteo en el addon (`_l10n_pe_document_type` / `_l10n_pe_target`): `out_refund`→07; `out_invoice` con `debit_origin_id`→08; cliente con RUC→01; cliente con DNI→03.

## Componentes en la corrida

| Componente | Estado |
|---|---|
| `ms-ne-biller` | :8090 (Spring Boot, Java 17), apuntando a `e-beta.sunat.gob.pe` |
| Odoo 19 Community | :8169, DB `odoo_ne_biller`, `l10n_pe` + addon `l10n_pe_ne_biller` |
| Empresa emisora | RUC `20321856145` (calza con cert/credenciales del biller) |
| SUNAT beta | **operativa** — aceptó los comprobantes con CDR ResponseCode 0 |

## Capa 0 — Smoke de contrato (sin UI)

`python3 e2e/smoke_biller.py` → **HTTP 200** + UBL con `<Invoice>` y `<ds:Signature>`.
El biller generó, firmó (XAdES), validó (XSD/XSLT) y envió a SUNAT beta, guardando el CDR
(`RPTA/R20321856145-01-F001-<corr>.zip`).

## Capa 1 — E2E completo por navegador (browser-harness)

Flujo ejecutado contra la UI web real (Chrome vía CDP):

1. Login `admin`/`admin` en `http://localhost:8169`.
2. Seed de maestros + factura posteada (`scripts/seed_e2e_data.py`): move id **14**, serie **F001**, correlativo **81909926**, cliente **CLIENTE E2E SAC** (RUC `20605145648`), 1 línea (DESARMADOR, S/ 7.20 + IGV 18%).
3. Apertura de la factura y click en **"Enviar al Facturador"** (coordenadas resueltas por DOM).
4. La UI transitó el statusbar de **"Por enviar" → "Enviado"** y el botón desapareció.

**Aserción determinística** (`odoo-bin shell`, move 14):
```
STATE= enviado
XML_ATTACH= True | 20321856145-F001-81909926.xml
MSG= Aceptado por el facturador (HTTP 200).
```

**CDR de SUNAT** (`evidence/cdr-F001-81909926.zip`):
```xml
<cbc:ReferenceID>F001-81909926</cbc:ReferenceID>
<cbc:ResponseCode>0</cbc:ResponseCode>
<cbc:Description>La Factura numero F001-81909926, ha sido aceptada</cbc:Description>
```
Sender: SUNAT (20131312955) · Receiver/emisor: 20321856145 · RecipientParty: 6-20605145648.

## Evidencia

- `e2e/screenshots/03-enviado.png` — factura en estado "Enviado".
- `e2e/evidence/ubl-firmado-F001-81909926.xml` — UBL 2.1 firmado (XAdES) generado por el biller.
- `e2e/evidence/cdr-F001-81909926.zip` — CDR de SUNAT con ResponseCode 0 (aceptada).

## Pruebas automatizadas del addon

`odoo-bin -d odoo_ne_biller -u l10n_pe_ne_biller --test-enable --test-tags /l10n_pe_ne_biller`
→ **5 tests, 0 failed, 0 error**: `TestInstall` (campos + default), `TestBillerMapper`
(JSON == contrato SFS), `TestBillerSend` (200→enviado+adjunto, 400→rechazado).
Helper de monto en letras: `pytest tests/test_amount_to_words.py` → 2 passed.

## Reproducción

> **Nota operativa (descubierta en la sesión):** un `./gradlew bootRun` lanzado como tarea de
> fondo del agente recibe SIGTERM y muere; correrlo en tu propia terminal (`!`) o como proceso
> `java` desacoplado lo mantiene vivo. Empaquetar `bootJar` **no** sirve: el biller lee plantillas
> `.ftl`/certificados del **filesystem** (no del classpath del jar) → da 422/500. La forma robusta
> es `java` con el classpath explotado (incluye `build/resources/main` en disco):
> ```bash
> cd ms-ne-biller
> CP=$(JAVA_HOME=<jdk17> ./gradlew -q -I /tmp/cp.gradle printCp)   # cp.gradle imprime sourceSets.main.runtimeClasspath
> nohup <jdk17>/bin/java -cp "$CP" com.wds.biller.BillerApplication </dev/null >/tmp/biller.log 2>&1 &
> ```
> **SUNAT beta es intermitente** en el handshake TLS: un envío puede dar 500 (`SslHandshakeTimeoutException`)
> y al reintentar dar 200. El addon marca esos casos como `error`/`rechazado`; basta re-enviar.

```bash
# 1) Biller (forma simple en terminal propia)
cd ms-ne-biller && JAVA_HOME=/Library/Java/JavaVirtualMachines/jdk-17.jdk/Contents/Home ./gradlew bootRun
# 2) Odoo (venv en fact/.venv-odoo19)
cd odoo-19.0 && ../.venv-odoo19/bin/python odoo-bin -c ../odoo-ne-integration/config/odoo-community.conf -d odoo_ne_biller
# 3) Seed + E2E
../.venv-odoo19/bin/python odoo-bin shell -c ../odoo-ne-integration/config/odoo-community.conf -d odoo_ne_biller --no-http < ../odoo-ne-integration/scripts/seed_e2e_data.py
# luego e2e/e2e_ui_flow.py (browser-harness con Chrome aislado, ver el docstring)
```

## Notas / brechas conocidas (futuro)

1. **CDR no se sube a Odoo todavía.** El biller devuelve solo el XML firmado en la respuesta HTTP;
   el CDR se persiste en su carpeta `RPTA/`. El campo `l10n_pe_biller_cdr` existe pero queda vacío.
   Para surfacearlo en Odoo: ajuste mínimo del biller para devolver el CDR (base64) junto al XML, o
   que el addon lo recupere. Fuera del alcance MVP (no cambia el contrato JSON de entrada).
2. **Emisor placeholder.** El `TaxPayerDao` mock del biller devuelve dirección/razón social
   placeholder para el emisor; SUNAT beta lo tolera. Para producción, poblar datos reales del emisor.
3. **Desviaciones de ejecución vs. plan** (menores, sin afectar el diseño):
   - El test puro `test_amount_to_words.py` se ubicó en `$INTEG/tests/` (fuera del paquete del addon)
     para evitar que pytest importe la cadena de `__init__.py` de Odoo. El self-review del plan ya
     anticipaba este riesgo de import.
   - browser-harness se conectó a un **Chrome aislado** (puerto 9333, perfil propio) en vez del Chrome
     del usuario, porque Chrome ≥144 exige un clic manual "Allow"; la Vía 2 del install.md lo evita.

## Hallazgos del biller (notas) y cómo se resolvieron desde el addon

Al extender a NC/ND aparecieron quirks del biller/SFS resueltos **sin tocar el biller**, sólo
ajustando el payload (campos válidos del contrato SFS):

1. **NC + "Contado" → rechazo.** El `CreditNoteMapper` del biller fuerza un
   `<cac:PaymentTerms>/<cbc:Amount>`. Con `datoPago:{formaPago:"Contado"}` el `currencyID`
   sale vacío → SUNAT `errorCode 2071`. Omitir `datoPago` → `errorCode 3245` (falta forma de
   pago). Enviar "Contado" con monto → `errorCode 3246`. **Solución:** para NC (07) el addon
   envía `datoPago` con `formaPago:"Credito"` + `tipMonedaMtoNetoPendientePago` + una cuota
   (`detallePago`) = total. Es el único patrón que valida el SFS bundle.
2. **ND (08)** valida **sin** `datoPago`; el addon no se lo agrega.
3. **Factura/Boleta** mantienen `datoPago:{formaPago:"Contado"}` (el `InvoiceMapper` sí lo maneja).

Estos comportamientos están encapsulados en `account.move._l10n_pe_build_note_request()` y
cubiertos por `test_documents.py`.

## Alcance cubierto

**Factura (01), Boleta (03), Nota de Crédito (07) y Nota de Débito (08)** — camino directo
Odoo Community → ms-ne-biller, hasta CDR real aceptado, disparado desde la UI web. Pendiente
(siguientes incrementos): surfaceo del CDR al campo `l10n_pe_biller_cdr` de Odoo, guías de
remisión (09/31) y resúmenes (RC/RA), y `codMotivo`/serie configurables por diario.

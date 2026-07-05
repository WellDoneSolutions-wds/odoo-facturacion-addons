# QW08 Ticket 80mm — verificación / notas de staging (Task 5)

Ramas `feat/oleada2-qw08-ticket` con **bases divergentes**: `ms-ne-biller` (solo `biller-pdf`) sobre **qw05** (`533bcef`); `odoo-facturacion-addons` + `ne-express` sobre **qw07** (`6abe651`/`20fd9fef`). **`biller-app`/firma SUNAT y el QR: CERO cambios** (paridad fiscal). Orden de deploy: **biller-pdf → addon → SPA**.

## Gate OBLIGATORIO de merge (verificado, verde)

- **biller-pdf (`./mvnw -pl biller-pdf test`): 19 tests, 0 fail.** `ReportePdfServiceTest` (esTicket, A4=555, **QR pinneado por reflexión** = anti-regresión de paridad), `ReportePdfFormatoTest` (@QuarkusTest: A5→400 Bean Validation, formato ausente→200 A4), `ReportePdfTicketTest` (**w=226**, **alto dinámico** 396→420 al duplicar línea, contenido PDFTextStripper, factura título, sufijo `-ticket`), + Logo/Sanitizar sin regresión.
- **addon (`TestBillerReportPdf`): 19 tests; suite `/l10n_pe_ne_biller`: 165, 0 fail.** `_l10n_pe_get_pdf_attachment(formato)` (caché separado, fallback A4, **A4 byte-idéntico** sin clave `formato`), kind `ticket`, `_serve_file` kind a **3** métodos (account.move + retención + **get_baja_files**, Critical corregido con test de regresión), botón oculto NC/ND.
- **SPA (`tsc --noEmit` + `vite build`): limpios.** `imprimirBlob`, POS selector+auto-print **gateado a la aceptación** (`if(ok)`), Comprobantes kind ticket solo 01/03.

## Verificado por composición

- **QR = misma cadena que el A4** (`buildQrString`/`generarQr` intactos; test de reflexión lo fija).
- **A4 request byte-idéntico** (sin clave `formato`; el `test_pdf_payload_sin_logo_default` de QW05 sigue verde) → un micro no actualizado devuelve A4 (degradación aceptable).
- **Alto dinámico** recortado al contenido (`setPageHeight(maxY+12)`), página única (`isIgnorePagination`), filas de tributo cero removidas (`isRemoveLineWhenBlank`).
- **Sin logo en el ticket** (plantilla no declara `RUTA_IMAGEN_LOGO`; el micro lo pasa pero Jasper lo ignora).

## DIFERIDO a staging

- **E2E vivo** (`e2e/harness.py`, paso `E2E_PDF_FORMATS=TICKET`): genera el ticket de cada boleta/factura aceptada contra el **biller-pdf real** con el **XML firmado real** (valida el pipeline XSL/QR/plantilla completo) y asevera `%PDF` + nombre `-ticket.pdf`. NO corrido (el stack Docker corre código previo). Correr en staging:
  ```bash
  cd /Users/joel/Desktop/wds-dir/facturador/odoo-facturacion-addons
  E2E_CASES_FILE=e2e/plan.json E2E_RESULTS_FILE=/tmp/r.json E2E_PDF_FORMATS=TICKET \
    <odoo-bin> shell -c <conf> -d odoo_ne_biller --no-http < e2e/harness.py
  # esperado: casos boleta/factura → "ticket_ok": true
  ```
- **QA térmica (gate humano, hardware):** Epson TM-T20 (o compatible, driver 80mm) — boleta con 3 productos (desc de 2 líneas, uno exonerado, bolsa ICBPER): auto-print abre el diálogo, imprime **sin cortes laterales**, **QR legible** por la app de consulta CPE de SUNAT; selector A4 persiste al recargar; Comprobantes reimprime; backoffice muestra "Descargar Ticket" solo en firmados.
- **Fallback NC (07+TICKET→A4)** del micro con NC firmada real: cubierto solo en E2E (no en unit, para no craftear un fixture de nota de crédito).

## ⚠️ DECISIÓN pendiente (Joel) — P.U. en el ticket

La plantilla del ticket muestra por línea **CANT · DESCRIPCIÓN · IMPORTE**, **sin columna de P.U.** (el precio unitario solo alimenta el importe = PU×cant). Coincide con **HU4 de la spec** (que solo lista "cantidad, descripción e importe") pero la **tabla de campos de la spec sí lista 'P.U. con IGV'**. Para **factura(01)** SUNAT suele exigir valor/precio unitario visible → **posible observación**. Resolver en la QA térmica: si se requiere, añadir el P.U. como columna/sub-línea en la jrxml (ajuste de plantilla + render test).

## Ops / deploy

- **QW08 no agrega config de ops** (sin dominio/SMTP/nginx; la impresión es cliente vía iframe). Impresión 100% silenciosa = arrancar Chrome del POS con `--kiosk-printing` y la térmica como predeterminada (setup del punto de venta, documentar en el runbook operativo).
- **Sin migración de datos:** `l10n_pe_biller_pdf_ticket` (M2o) lo crea el ORM en `-u`; los tickets históricos se generan on-demand (lazy-cache, misma invariante que el A4). La plantilla viaja en el JAR (`ensureCompiled` la compila en caliente).
- **Minors whole-branch:** `DecimalFormat` locale-default en la columna importe (fix con `Locale` explícito); fallback de importe cambia base (PU con IGV vs LineExtensionAmount); `SUBREPORT_DIR`+imports muertos en la jrxml; ruta pública `.../ticket` → 404 (fuera de alcance).

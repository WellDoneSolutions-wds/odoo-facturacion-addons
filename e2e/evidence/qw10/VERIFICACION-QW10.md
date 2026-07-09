# QW10 Facturación masiva — verificación / notas de staging (Task 7)

Ramas `feat/oleada3-qw10-masiva` **sobre qw08** (odoo `9569042`, ne-express `08da33a5`). **`ms-ne-biller` NO se toca** (reusa `action_l10n_pe_send_to_biller` doc-a-doc). Modelos propios `l10n_pe_ne.lote` + `.lote.fila`; parseo/validación en Odoo con openpyxl (fuente única de reglas fiscales).

## Gate OBLIGATORIO de merge (verificado, verde)

- **Unit Odoo `TestMasivo` 14/14** (`tests/test_masivo.py`): aislamiento multi-compañía ambos modelos + defaults; parseo openpyxl header-normalizado + agrupado; validación por fila (RUC módulo-11 hand-verified, serie prefix-coherente, límites) con `filaExcel` exacto; `crear_lote` sha256/`duplicadoDe`; **procesar secuencial, rechazo-no-detiene, idempotente, reintento-reusa-move (0 moves nuevos), cancelar (+guard cancelado→terminado), chunk multi-fila**; `resultados_xlsx`.
- **HttpCase `TestMasivoHttp` 6/6** (`tests/test_masivo.py`): las 8 rutas `/ne/api/lotes*` por HTTP real — 401/404/**403 cross-tenant**, list, plantilla (filename/contentB64), crear (xlsx base64), detalle.
- **SPA `tsc --noEmit` + `vite build`**: limpios (Tasks 5-6).
- Suite completa `/l10n_pe_ne_biller`: **145+ tests, 0 fail** (sin regresión de caja/cotización/ticket/etc.).

## Verificado por composición

- **`payload_json` = subset EXACTO de `quick_emit`** verificado byte-for-byte contra el consumidor real (`taxCode`/`productCod`/`icbper`/`cliente` cat-06) → cada fila emite con el mismo code-path que la emisión individual (**cero lógica de emisión nueva**).
- **Idempotencia sólida** (verificada por lectura de código): `action_l10n_pe_send_to_biller` nunca lanza en red/rechazo → el move persiste → `reintentar` reenvía el MISMO `move_id` (misma serie-correlativo; nunca lo limpia); backstop BD `account_move_unique_name_latam`. `test_reintento_reusa_move` prueba 0 moves nuevos.
- **Commit por fila** (`_masivo_can_commit`) suprimido bajo `test_enable`/`E2E_NO_COMMIT` → un CPE aceptado por SUNAT no se pierde por el rollback de una fila posterior; en test/E2E la BD hace rollback normal.

## DIFERIDO a staging

- **E2E vivo** (`e2e/e2e_masivo.py`, `E2E_NO_COMMIT=1`): construye un xlsx en memoria con **3 ventas reales** (factura 2 líneas gravadas a RUC `20100070970`, boleta exonerada+ICBPER a DNI, boleta público general) + 1 negativa (RUC fuera de padrón), `crear_lote`→`procesar` en loop contra **ms-ne-biller real + SUNAT beta**, assert `validado`/≥3 `emitido`/`resultados_ok`. NO corrido (el stack Docker corre código previo). Correr en staging:
  ```bash
  cd /Users/joel/Desktop/wds-dir/facturador/odoo
  E2E_RESULTS_FILE=/tmp/r_masivo.json E2E_NO_COMMIT=1 \
    <odoo-bin> shell -c <conf> -d odoo_ne_biller --no-http < ../odoo-facturacion-addons/e2e/e2e_masivo.py
  # esperado: "E2E_MASIVO_DONE 5 filas · 3+ emitidas"; caveats beta 401-throttle/TLS-timeout → reintentar
  ```
- **E2E UI (browser-harness):** login → `/masivo` → descargar plantilla → subir xlsx con 1 error → ver reporte por fila → subir corregido → "Emitir N" → barra de progreso en vivo (serie-correlativo verde / motivo rojo) → descargar resultados → reintentar fallidos; cerrar pestaña a mitad → "Reanudar" retoma sin duplicar; cruzar en `/comprobantes` (los CPE aparecen Enviado).

## Ops / deploy

- **QW10 no agrega config de ops** (sin dominio/SMTP/nginx; reusa el envío existente a SUNAT vía biller). Throttling por diseño: chunks secuenciales de 1 = ≤1 request concurrente por tenant, sin rate-limiter nuevo.
- **Sin migración de datos:** los 2 modelos los crea el ORM en `-u l10n_pe_ne_biller`; ACL/ir.rule ya en `__manifest__.py.data`; 4 params con default en código. Sin deps Python nuevas (openpyxl/xlsxwriter ya en `requirements.txt`).
- QW10 apila sobre qw08 (Oleada 3) → aplican transitivamente los ops de QW05 al deploy (ver `evidence/qw05/OPS-DEPLOY-QW05.md`).
- **Minors whole-branch (SPA T6):** `abrirLote` no limpia el draft `ne_lote_activo` si el lote 404 (id stale re-falla en cada montaje) — fix rápido `clearDraft` en el catch; "Reanudar" tras pausa resetea visualmente la lista viva (cosmético, sin pérdida — el resumen re-fetchea); frame en blanco entre "Emitir" y la 1ª fila; sin busy-guard contra doble-click. **(addon):** sin validación enum de `moneda` a nivel línea (downstream default PEN); `_validar_grupo` ~90 líneas.

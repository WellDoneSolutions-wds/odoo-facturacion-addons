# QW07 Caja/arqueo — verificación / notas de staging (Task 7)

Rama `feat/oleada2-qw07-caja` en `odoo-facturacion-addons` (addon T1-T4 + e2e T7) y `ne-express`
(SPA T5-T6). **QW07 apila sobre QW06** (que apila sobre QW05) y **NO toca ms-ne-biller**: el
arqueo imprimible es HTML→print del navegador y reusa el `@media print` de QW06; el amarre de
ventas reusa el flujo de emisión existente. Toda la aritmética del arqueo vive en el tool puro
`tools/caja_arqueo.py` (testeado sin Odoo); los modelos solo serializan y llaman.

## Gate OBLIGATORIO de merge (verificado, verde)

Comando (3× consecutivos, verde cada vez — prueba que el flake de `test_amarre_ventas` se eliminó):

```bash
cd /Users/joel/Desktop/wds-dir/facturador/odoo
/Users/joel/Desktop/wds-dir/facturador/.venv-odoo19/bin/python odoo-bin \
  -c /Users/joel/Desktop/wds-dir/facturador/odoo-facturacion-addons/config/odoo-community.conf \
  -d odoo_ne_biller -u l10n_pe_ne_biller --test-enable \
  --test-tags /l10n_pe_ne_biller:TestCaja --stop-after-init --log-level=info
```

- **Unit puro `test_caja_arqueo.py` 12/12** (fuera del paquete del addon, se carga por ruta):
  `agrupar_ventas` (contado c/medios, contado sin medios→Efectivo, crédito sin medios no suma,
  crédito con medios suma solo medios, USD aparte, mezcla) + `calcular_arqueo` (Efectivo =
  saldo+ingresos−retiros, medios contado, redondeo a 2 decimales, arqueo parcial `None`).
- **Odoo `TestCaja` 13/13** (`tests/test_caja.py`): defaults, aislamiento multi-compañía (cabecera
  **y** movimiento con `company_id` propio), índice único parcial de sesión abierta, ACL sin unlink,
  ciclo abrir/movimiento/cerrar/actual/list, guardas (doble apertura, saldo negativo, sin caja,
  tipo/motivo/monto, re-cierre, cerrar sin conteos), amarre de ventas por medio + `sinMedio` + USD
  aparte + crédito excluido, y arqueo cross-tenant → `AccessError`.
- **HttpCase `TestCajaHttp` (`test_flujo`)** (`tests/test_caja_http.py`): las 6 rutas
  `/ne/api/caja*` por HTTP real (:8269), flujo completo abrir→movimiento→actual→cerrar→list→arqueo
  + 404/403.
- **Suite completa del addon `--test-tags /l10n_pe_ne_biller`: `0 failed, 0 error(s) of 119 tests`**
  (159 métodos con subtests, 35 s) — QW07 NO regresiona QW01-06.

### Verificado por composición (hermético, sin red)

- **Aritmética del arqueo**: unit-testeada byte a byte en el tool puro (12 casos); los modelos
  (`_l10n_pe_ne_sesion_dict`/`_arqueo_dict`/`_fila_dict`) solo la delegan.
- **Dominio del amarre**: `test_amarre_ventas` crea ventas `posted`+`enviado` y verifica agrupación
  por medio, contado sin medios→Efectivo+sinMedio, crédito excluido del esperado, USD contabilizado
  aparte (`countUsd`/`totalUsd`). **El amarre real vs SUNAT beta** (ventas emitidas de verdad) es
  `caja_flow.py`, diferido a staging.
- **Inmutabilidad del snapshot BAJO MUTACIÓN** (HU4): `test_snapshot_inmutable_bajo_mutacion`
  abre→amarra una venta→cierra (congela `conteos_cierre`/`ventas_cierre`)→**anula la venta amarrada**
  (`l10n_pe_biller_state='anulado'`, que una re-consulta excluiría)→re-lee el arqueo y afirma que es
  **idéntico** (`after == before`, `ventas.count` sigue en 1). Prueba que el arqueo histórico lee el
  snapshot congelado y NO re-consulta las ventas — cierra el gap del review de T3 (el
  `test_cerrar_y_snapshot` previo solo probaba idempotencia de lectura sin mutación).
- **Aislamiento multi-RUC**: `ir.rule` de cabecera y de movimiento ejercitadas por
  `test_multicompania` + `test_arqueo_cross_tenant` (arqueo de sesión ajena → `AccessError`).

### El flake de `test_amarre_ventas` (eliminado)

`create_date` la fija Postgres al INICIO de la transacción (constante en toda la `TransactionCase`),
mientras `fecha_apertura` es un `fields.Datetime.now()` truncado al segundo. Al cruzar un borde de
segundo entre el arranque de la transacción y la apertura, `create_date` podía caer una fracción
ANTES de `fecha_apertura` y la venta salía de la ventana `create_date >= fecha_apertura` →
`KeyError: 'Yape'` intermitente en corridas completas. **Fix determinista**: `_caja_abrir` ancla
`fecha_apertura = now() − 5 min` y `_venta_enviada` fuerza `create_date = fecha_apertura + 5 s`
(vía `cr.execute("UPDATE account_move SET create_date=%s …")` + `invalidate_recordset`), dejando
todas las ventas holgadamente dentro de `[fecha_apertura, now()]`. Verificado 3× verde.

## E2E VIVO diferido a staging (decisión = QW05 T13 / QW06 T12)

- **`e2e/caja_flow.py`** (Odoo shell): abrir S/ 150 + guarda de doble apertura → **emitir 3 ventas
  REALES a SUNAT beta** (boleta Efectivo 60 + Yape 40; boleta contado sin medios → Efectivo + sinMedio;
  factura a crédito con cuota futura → excluida) → retiro 80 → assert del esperado en vivo
  (Efectivo = 150+60+118−80, Yape = 40, `sinMedio`=1, `count`=3, crédito fuera de `porMedio`) → cerrar
  con conteo que da **−2.30** → re-cierre → `UserError` → **anular** una venta y afirmar arqueo
  **inmutable** (`before['arqueo'] == after['arqueo']`; la anulación va en `try/except` porque beta
  throttlea/hace timeout — el assert de inmutabilidad no depende de que la anulación complete) →
  arqueo cross-tenant → `AccessError`. **NO corrido en esta sesión**: el stack Docker local
  (:8169/:8090) corre código anterior a la rama qw07. Correr en staging levantando Odoo sobre la rama
  + biller-app:
  ```bash
  cd /Users/joel/Desktop/wds-dir/facturador/odoo
  /Users/joel/Desktop/wds-dir/facturador/.venv-odoo19/bin/python odoo-bin shell \
    -c /Users/joel/Desktop/wds-dir/facturador/odoo-facturacion-addons/config/odoo-community.conf \
    -d odoo_ne_biller --no-http < ../odoo-facturacion-addons/e2e/caja_flow.py
  # esperado: "E2E QW07 OK: sesion NN diferencia -2.3"
  # (beta 401-throttlea / timeout TLS aleatorio — reintentar el envío; espaciar boletas ~30 s)
  ```
- **E2E UI (browser-harness):** login → `/caja` → abrir S/ 150 → en `/pos` emitir una boleta
  Efectivo+Yape → volver a `/caja` y verificar el esperado en vivo → registrar un retiro → "Cerrar
  caja" con conteo que provoque **−2.30** (verificar color/valor de la diferencia) → imprimir y
  confirmar que **solo** aparece `.arqueo-print` (sin sidebar/topbar) → Historial → "Ver arqueo"
  reabre el reporte → con la caja **cerrada**, confirmar que `/pos` **sigue vendiendo** (la caja
  nunca bloquea la venta). Evidencia a guardar en `e2e/evidence/qw07/`.
- **Impresión visual (A4 / 80 mm):** confirmar en Chrome + Safari + móvil que el `@media print`
  (reutilizado de QW06) oculta el cromo y no deja offset lateral; sin regresión de `CotizacionPrint`
  ni de la vista pública.

## Ops / deploy

- **QW07 no agrega config de ops propia:** no toca dominio público, SMTP ni nginx. El arqueo se
  imprime 100% en cliente (browser print) reutilizando el `@media print` de QW06; la razón social /
  RUC salen de `res.company` (ya expuesta). Sin manifest bump (`__manifest__.py` `19.0.1.0.0`), sin
  migración, sin vistas nuevas, sin cambio en ms-ne-biller.
- Como QW07 apila sobre QW06/QW05, **al desplegar aplican transitivamente los ítems de ops de esas
  oleadas** (public_base_url, ir.mail_server, nginx rate-limit) — ver `evidence/qw06/VERIFICACION-QW06.md`
  y `evidence/qw05/OPS-DEPLOY-QW05.md`.
- **Orden de merge:** consolidar Oleada 1 (QW01-05) → Oleada 2 (QW06) → QW07 sobre esa base.
  Renumerar `__manifest__.py` al merge (QW07 no lo bumpea).

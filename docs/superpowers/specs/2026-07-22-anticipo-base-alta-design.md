# Anticipo sobre operaciones de base alta (fix del rechazo SUNAT 4322)

**Fecha:** 2026-07-22 · **Repo:** `odoo-facturacion-addons` · **Rama:** `fix/anticipo-base-alta`

## Problema (reproducido contra SUNAT beta)

Una factura que regulariza un anticipo sobre una operación de **base grande (≳ S/ 200.000)**
es **rechazada por SUNAT** con error **4322** ("El valor de cargo/descuento global difiere de
los importes consignados"). Reproducido: venta de S/ 354.000 (base 300.000) que regulariza un
anticipo → RECHAZADA, error 4322 sobre `cac:AllowanceCharge/cbc:Amount`.

Relevante justo para el segmento de **ticket alto** (maquinaria, construcción, proyectos),
que es donde el multi-anticipo apunta.

## Causa raíz (dos reglas SUNAT en tensión)

El descuento global por anticipo (`AllowanceCharge` código 04) hoy se emite con:
`mtoBaseImpVariableGlobal = base` (base completa de la operación),
`porVariableGlobal = round(valor/base, 5)`, `mtoVariableGlobal = valor`
(`account_move_biller.py:688-703`).

1. **Regla 3052** (regex `MultiplierFactorNumeric`): el factor admite **máximo 5 decimales**
   — no se puede ganar precisión.
2. **Regla 4322** (validador SUNAT): `|Amount − BaseAmount × MultiplierFactorNumeric| ≤ 1 sol`.

El redondeo del factor a 5 decimales, multiplicado por una base grande, hace que
`base × round(valor/base, 5)` se aleje del `valor` más de 1 sol:
- Base 300.000, valor 45.001,50 → factor `round(0,150005; 5) = 0,15001` →
  `300.000 × 0,15001 = 45.003` → desvío **1,50 > 1** → **4322**.
- Umbral ≈ base 200.000 (ahí el desvío llega a 1,0).

**Nota de validador**: la regla 4322 está como `isError=false` (observación) en el XSL local
empaquetado (SEE-SFS), pero **SUNAT beta la rechaza como error duro** — el validador de
producción es más estricto que el XSL bundle. Verificado emitiendo contra beta.

## Decisión tomada (con el usuario): Fix B — factor unitario

Emitir el descuento 04 con:
- `mtoBaseImpVariableGlobal = valor` (la base del descuento = el propio valor del anticipo).
- `porVariableGlobal = "1.00000"`.
- `mtoVariableGlobal = valor` (sin cambio).

Entonces `BaseAmount × Factor = valor × 1 = valor = Amount` → `|Amount − Base×Factor| = 0` para
**cualquier monto**, sin importar la base. La regla 4322 pasa siempre. Semánticamente limpio:
"el valor del anticipo se descuenta al 100 %".

**Por qué no rompe nada más** (verificado en el XSL de SUNAT):
- El IGV de cabecera se valida con `sum(AllowanceCharge[04]/Amount)` (el `Amount`, NO el
  `BaseAmount`) — `ValidaExprRegFactura:361,398`. Como `Amount = valor` no cambia, la
  reducción de la base gravada / IGV es idéntica a hoy → cabecera intacta.
- El `BaseAmount` del descuento 04 NO se cruza contra la base de la operación en ninguna regla
  (solo se usa en el cálculo del 4322).
- El `PrepaidAmount`/`PrepaidPayment` (el anticipo real, lo que el cliente pagó) es
  independiente del descuento 04 (no hay regla que exija `descuento × 1,18 = PrepaidAmount`)
  — el importe a cobrar (`sumImpVenta = total − anticipo_total`) no cambia.

**Alcance del cambio**: solo el bloque del código 04 en `_l10n_pe_variables_globales`
(`account_move_biller.py:698, 702`) — `porVariableGlobal` y `mtoBaseImpVariableGlobal`.
`mtoVariableGlobal` (Amount), cabecera, tributos, relacionados y PrepaidAmount **no cambian**.

**Fallback (Fix A)**: si el smoke de beta rechazara `BaseAmount = valor` para el código 04
(por si SUNAT esperara la base de la operación), caer a: mantener `BaseAmount = base` y emitir
`mtoVariableGlobal = round(base × round(valor/base, 5), 2)` (auto-consistente con el factor),
reduciendo la base/IGV de cabecera por ese mismo monto ajustado. Más invasivo (toca cabecera);
solo si B no pasa.

## Verificación

- **Unit (Odoo, docker)**: el descuento 04 emite `porVariableGlobal="1.00000"`,
  `mtoBaseImpVariableGlobal == mtoVariableGlobal == valor`; cabecera (sumTotTributos,
  sumImpVenta, IGV/base reducidos) **idéntica** a la de hoy para un caso de base normal
  (paridad — `TestBillerAnticipo` no debe cambiar sus expectativas de cabecera); y un test
  nuevo de base alta (base 300.000) que hoy daría desvío >1 con el factor viejo y con el fix
  cumple `|Amount − Base×Factor| ≤ 1` (de hecho = 0).
- **E2E real contra SUNAT beta (el reproductor)**: emitir la venta de base 300.000 que
  regulariza un anticipo → **CDR ResponseCode 0** (antes: rechazada 4322). Este smoke DECIDE
  B vs el fallback A: si beta rechaza B, implementar A y re-verificar.
- **Multi-anticipo de base alta**: verificar que la suma agregada del 04 con varios anticipos
  también pasa (el mismo mecanismo: Base = valor agregado, factor 1).

## Fuera de alcance / follow-up

- **Mensaje de rechazo vacío**: en el reproductor, el comprobante quedó `rechazado` con
  `l10n_pe_biller_message` VACÍO (el motivo 4322 solo quedó en el log del biller). Es un bug
  aparte de cómo el addon captura el rechazo de SUNAT en este camino — merece su propia
  investigación (parsing de la respuesta del biller/SUNAT), NO se aborda aquí para mantener el
  fix del 4322 quirúrgico. Anotar como issue.
- Boletas/NC/ND (el anticipo solo aplica a factura de venta). POS/cotización.

## Archivos

- `addons/l10n_pe_ne_biller/models/account_move_biller.py` (`_l10n_pe_variables_globales`,
  bloque código 04).
- `addons/l10n_pe_ne_biller/tests/test_multi_anticipo.py` (o `test_anticipo.py`) — tests de
  factor unitario + base alta + paridad de cabecera.

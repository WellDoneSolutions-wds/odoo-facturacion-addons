# Fix anticipo base alta (SUNAT 4322) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Que una factura que regulariza anticipo(s) sobre una operación de base grande (≳ S/ 200.000) sea ACEPTADA por SUNAT — hoy la rechaza con error 4322 por el redondeo del factor del descuento 04.

**Architecture:** Cambio quirúrgico en el descuento global código 04 (`_l10n_pe_variables_globales`): emitirlo con `BaseAmount = valor`, `Factor = 1.00000`, `Amount = valor`, de modo que `Base×Factor = Amount` exacto para cualquier base → la regla 4322 (`|Amount − Base×Factor| ≤ 1`) pasa siempre. La cabecera (IGV/base reducidos, importe a cobrar) NO cambia porque SUNAT reduce la base con el `Amount` (invariante). Spec: `docs/superpowers/specs/2026-07-22-anticipo-base-alta-design.md`.

**Tech Stack:** Odoo 19 (addon `l10n_pe_ne_biller`). Sin dependencias nuevas.

## Global Constraints

- Rama: **`fix/anticipo-base-alta`** (ya existe con el spec). Directorio: `/Users/joel/Desktop/workspace/hernan/odoo-facturacion-addons`.
- Tests EN docker: `docker cp addons/l10n_pe_ne_biller ne-stack-odoo-1:/mnt/extra-addons/` + `docker exec ne-stack-odoo-1 odoo -c /etc/odoo/odoo.conf --db_host db --db_user odoo --db_password odoo -d odoo_ne_biller -u l10n_pe_ne_biller --test-enable --stop-after-init --http-port 8899 --gevent-port 8898 --test-tags '/l10n_pe_ne_biller:<Clase>'`. Known-flaky (NO regresión): `TestMasivoHttp.test_list_lotes_vacio`.
- **La cabecera NO debe cambiar**: `TestBillerAnticipo` (13 tests) valida `sumTotTributos`/`sumImpVenta`/IGV reducido — deben seguir IGUALES sin tocar sus asserts (paridad). Solo cambian los campos `porVariableGlobal` y `mtoBaseImpVariableGlobal` del código 04.
- **El E2E de beta (Task 2) DECIDE Fix B vs el fallback A** — si beta rechaza `BaseAmount=valor`, se implementa el fallback A y se re-verifica; documentar cuál aceptó beta.
- Tras `-u` que cambie el modelo, `docker restart ne-stack-odoo-1` (el servidor en vivo cachea el modelo). En esta task NO hay cambio de esquema (solo lógica), pero un restart no daña.
- Commits con footer `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`.

---

### Task 1: Emitir el descuento 04 con factor unitario (Fix B) + tests

**Files:**
- Modify: `addons/l10n_pe_ne_biller/models/account_move_biller.py` (`_l10n_pe_variables_globales`, bloque `codTipoVariableGlobal "04"`, ~líneas 690-703)
- Test: `addons/l10n_pe_ne_biller/tests/test_multi_anticipo.py`

**Interfaces:**
- Consumes: `_l10n_pe_anticipo()` (agregado `(valor, igv, total)`), `self.amount_untaxed`.
- Produces: en el request, el `variableGlobal` código 04 con `porVariableGlobal="1.00000"`, `mtoVariableGlobal == mtoBaseImpVariableGlobal == fmt(valor)`.

- [ ] **Step 1: Tests que fallan** — agregar a `TestMultiAnticipo` (o `TestBillerAnticipo` si el helper de venta vive ahí; usar el que tenga el `_venta`/`_move` a mano):

```python
    def test_descuento_04_factor_unitario(self):
        # anticipo 118 (valor 100) sobre venta 590: el 04 debe ir con factor 1 y base = valor.
        payload = self._venta(anticipos=[{'doc': 'F001-00000100', 'monto': 118.0, 'tipo': '02'}],
                              precio=500.0)._l10n_pe_build_invoice_request()
        vg = [v for v in payload['variablesGlobales'] if v['codTipoVariableGlobal'] == '04'][0]
        self.assertEqual(vg['porVariableGlobal'], '1.00000')
        self.assertEqual(vg['mtoVariableGlobal'], '100.00')
        self.assertEqual(vg['mtoBaseImpVariableGlobal'], '100.00')  # base = valor (no la base de la operación)

    def test_base_alta_cumple_regla_4322(self):
        # base 300000; anticipo con valor 45001.50 (total 53101.77) — el factor viejo (5 dec) daba
        # base×factor = 45003 → desvío 1.50 > 1 → 4322. Con factor unitario: base×factor = valor exacto.
        payload = self._venta(anticipos=[{'doc': 'F001-00000100', 'monto': 53101.77, 'tipo': '02'}],
                              precio=300000.0)._l10n_pe_build_invoice_request()
        vg = [v for v in payload['variablesGlobales'] if v['codTipoVariableGlobal'] == '04'][0]
        amount = float(vg['mtoVariableGlobal'])
        base = float(vg['mtoBaseImpVariableGlobal'])
        factor = float(vg['porVariableGlobal'])
        self.assertLessEqual(abs(amount - base * factor), 1.0)  # regla 4322
        self.assertEqual(vg['porVariableGlobal'], '1.00000')

    def test_cabecera_no_cambia_con_factor_unitario(self):
        # paridad: la reducción de IGV/base de cabecera es la misma que antes (usa el valor del
        # anticipo, no el BaseAmount del descuento). Venta 590 + anticipo 118 → base gravada 400, IGV 72.
        cab = self._venta(anticipos=[{'doc': 'F001-00000100', 'monto': 118.0, 'tipo': '02'}],
                          precio=500.0)._l10n_pe_build_invoice_request()['cabecera']
        self.assertEqual(cab['sumTotalAnticipos'], '118.00')
        self.assertEqual(cab['sumImpVenta'], '472.00')   # 590 − 118
        # el IGV de cabecera queda reducido a 72 (400×0.18) — igual que en test_anticipo.
```

  (Si `_venta` no existe en la clase, usar el helper real —`_move`/`_venta`— o copiarlo del test vecino; el `precio` es el valor SIN IGV de la línea.)

- [ ] **Step 2: Verificar que fallan** — `test_descuento_04_factor_unitario`/`test_base_alta...` fallan (hoy `porVariableGlobal` es `round(valor/base,5)`, no `1.00000`). `test_cabecera_no_cambia` PASA ya (la cabecera no depende del bloque 04).

- [ ] **Step 3: Implementar** — en el bloque del código 04 (`account_move_biller.py:691-703`), reemplazar `porVariableGlobal` y `mtoBaseImpVariableGlobal`:

```python
        ant = self._l10n_pe_anticipo()
        if ant:
            valor, _igv, _total = ant
            # Descuento 04 con FACTOR UNITARIO: base = el propio valor del anticipo, factor 1.00000,
            # monto = valor. Así base×factor = monto EXACTO para cualquier importe → la regla SUNAT
            # 4322 (|monto − base×factor| ≤ 1) pasa siempre, incluso en operaciones de base alta
            # (≳ S/ 200.000), donde el factor a 5 decimales sobre la base completa se desviaba > 1 sol
            # y SUNAT rechazaba. El IGV/base de cabecera NO cambian: SUNAT los reduce con el `Amount`
            # (mtoVariableGlobal = valor), no con el BaseAmount.
            out.append(
                {
                    "tipVariableGlobal": "false",
                    "codTipoVariableGlobal": "04",
                    "porVariableGlobal": "1.00000",
                    "monMontoVariableGlobal": moneda,
                    "mtoVariableGlobal": fmt(valor),
                    "monBaseImponibleVariableGlobal": moneda,
                    "mtoBaseImpVariableGlobal": fmt(valor),
                }
            )
```

  (Quitar el comentario viejo del factor de 5 decimales; el `base = self.amount_untaxed` de la línea anterior queda sin uso en este bloque — quitarlo o dejarlo solo si otro bloque de la función lo usa. Verificar: el bloque del `desc_no_afecta` (código diferente) usa su propio `base`; no confundir.)

- [ ] **Step 4: Verde** — los 3 tests nuevos + `TestBillerAnticipo` completo (13/13, paridad de cabecera intacta) + `TestMultiAnticipo`. Comando con `--test-tags '/l10n_pe_ne_biller:TestMultiAnticipo,/l10n_pe_ne_biller:TestBillerAnticipo'`.

- [ ] **Step 5: Commit**

```bash
git add addons/l10n_pe_ne_biller
git commit -m "fix(anticipo): descuento 04 con factor unitario — base alta ya no rechaza SUNAT 4322"
```

---

### Task 2: Verificación E2E contra SUNAT beta (decide B vs fallback A)

**Files:** ninguno nuevo (salvo el fallback si beta rechaza B).

- [ ] **Step 1: Desplegar** — `docker cp addons/l10n_pe_ne_biller ne-stack-odoo-1:/mnt/extra-addons/` + el `-u` de las Global Constraints + `docker restart ne-stack-odoo-1` (recarga el modelo en el servidor en vivo) + esperar `curl -s http://localhost:8169/ne/api/config` (401 = arriba).

- [ ] **Step 2: Emitir el reproductor contra beta** — vía `/ne/api/emitir` (token admin/admin):
  1. Doc A: factura de anticipo `esAnticipo:true`, monto ~S/ 53.101,77 (valor 45.001,50).
  2. Venta final: factura de S/ 354.000 (línea con `precioUnitario = 354000/1.18`) que regulariza el anticipo (`anticipos:[{doc, monto:53101.77, tipo:'02', origenId}]`).
  3. Verificar la venta final: `estado == 'enviado'` y el mensaje contiene **"CDR ResponseCode 0"** (ACEPTADA). Antes del fix esto daba `rechazado` (4322).
  Script de referencia: el `emitql`-style ya usado (payload de Emitir: `precioUnitario` SIN IGV, `conceptoLibre:true`).

- [ ] **Step 3: Inspeccionar el XML emitido** — descargar el XML de la venta final (`/ne/api/comprobantes/<id>/xml`) y confirmar el `AllowanceCharge` código 04: `MultiplierFactorNumeric = 1.00000`, `BaseAmount == Amount == 45001.50`. Y que la cabecera (TaxTotal/IGV, PrepaidAmount) es coherente.

- [ ] **Step 4: Decisión B vs A**
  - **Si CDR ResponseCode 0** → Fix B confirmado. Fin.
  - **Si RECHAZADA** con un error sobre el BaseAmount del 04 (SUNAT exige base = base de la operación) → implementar el **fallback A**: revertir `mtoBaseImpVariableGlobal` a `fmt(base)`, `porVariableGlobal` a `"%.5f" % (valor/base)`, y cambiar `mtoVariableGlobal` a `fmt(round(base * round(valor/base,5), 2))` (auto-consistente con el factor redondeado), y **ajustar la reducción de IGV/base de cabecera** por ese mismo monto ajustado (buscar dónde `_l10n_pe_tributos`/`_l10n_pe_cabecera` restan `valor`/`igv` y usar el monto ajustado + su IGV). Re-correr los tests (los asserts de cabecera de Task 1 y `TestBillerAnticipo` cambiarán por céntimos — actualizarlos con los valores reales) y re-emitir a beta hasta CDR 0. Documentar en el reporte qué aceptó beta.

- [ ] **Step 5: Multi-anticipo de base alta (smoke)** — opcional pero recomendado: emitir una venta de base alta que regularice 2 anticipos y confirmar CDR 0 (el 04 agregado con base = suma de valores + factor 1).

- [ ] **Step 6: Reporte** — qué estructura del 04 aceptó beta (B o A), el CDR de la venta de base 300.000, y el XML del AllowanceCharge 04.

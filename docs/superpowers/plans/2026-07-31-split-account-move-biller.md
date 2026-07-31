# Split de account_move_biller.py — Plan de Implementación

> Refactor mecánico sin cambio de comportamiento. Red: `scripts/test-fresh.sh` (674/674) tras cada tarea.

**Goal:** Extraer 5 concerns periféricos de `account_move_biller.py` a archivos `_inherit` propios; core ~7899 → ~4039.

**Spec:** `docs/superpowers/specs/2026-07-31-split-account-move-biller-design.md`

## Global Constraints
- Sin cambio de lógica: mover métodos tal cual + agregar imports. Nada más.
- Campos y constantes de módulo QUEDAN en `account_move_biller.py`.
- Cada archivo extraído: `class AccountMove(models.Model): _inherit = "account.move"` + sus imports + `from .account_move_biller import <constantes usadas>`.
- Registrar en `models/__init__.py` DESPUÉS de `account_move_biller`.
- Verificación por tarea: `scripts/test-fresh.sh` = 674/674 (NO 673, NO error).

## Procedimiento por tarea (idéntico para las 5)
1. Identificar el rango de líneas del concern (por el comentario divisor).
2. Cortar esos métodos del core y pegarlos en el archivo nuevo, dentro de `class AccountMove(models.Model): _inherit = "account.move"`.
3. En el archivo nuevo: agregar los imports de stdlib/odoo que usen esos métodos (grep del bloque: `base64|io\.|zipfile|json|re\.|requests|pytz|timedelta|float_round|html2plaintext|leyenda_monto`), y `from .account_move_biller import <CONST>` para cada constante de módulo usada (grep del bloque contra la lista: `TAX_CODE_MAP DEFAULT_TAX_CODE ND_MOTIVO_DESC DETRACCION_TASAS DESC_GLOBAL_NO_AFECTA_COD UOM_CODE_BY_XMLID DEFAULT_UNIT_CODE UNIDAD_IMPORT AFECT_IMPORT TIPO_IMPORT _UNIDAD_CODES _BOTO_CLIENTS`).
4. Registrar el archivo en `models/__init__.py`.
5. `scripts/test-fresh.sh` → **674/674**. Si baja: falta un import/constante → agregarlo (el traceback dice cuál) y re-correr.
6. Commit: `refactor(biller): extrae <concern> a account_move_<x>.py (sin cambio de comportamiento)`.

---

### Task 1: PLE (libros electrónicos) → `account_move_ple.py`
**Sección:** 14.1 ventas + 8.1 compras + 12.1 inventario (~4262–5858, 1596 líneas). El más grande y autocontenido (generación de reportes/ZIP; usa `io`, `zipfile`). Empezar por acá valida el patrón con el mayor volumen.

### Task 2: Compras → `account_move_compras.py`
**Sección:** compras (~6219–7095, 876 líneas).

### Task 3: Baja RA + Resumen Diario RC → `account_move_baja.py`
**Sección:** ~7402–7899 (497 líneas). Fin del archivo.

### Task 4: Importación de productos → `account_move_importacion.py`
**Sección:** ~5858–6219 (361 líneas). Usa `UNIDAD_IMPORT`, `AFECT_IMPORT`, `TIPO_IMPORT`.

### Task 5: Descargas/PDF → `account_move_pdf.py`
**Sección:** ~7095–7402 (307 líneas). Usa `requests` (POST /report/pdf), `base64`.

---

## Verificación final
- `scripts/test-fresh.sh` = **674/674**.
- `wc -l account_move_biller.py` ≈ 4039; 5 archivos nuevos.
- `git diff --stat` muestra solo movimiento de líneas (mismos métodos) + imports; ninguna línea de lógica cambiada.

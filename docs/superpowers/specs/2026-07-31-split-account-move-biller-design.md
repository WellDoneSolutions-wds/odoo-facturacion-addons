# Split de account_move_biller.py — Diseño

**Fecha:** 2026-07-31
**Repo:** `odoo-facturacion-addons` (addon `l10n_pe_ne_biller`). Refactor interno — **NO cambia comportamiento**, NO toca la SPA ni el biller Java ni el XML.

## Objetivo

`account_move_biller.py` tiene **7899 líneas / 216 métodos** — un god-file difícil de razonar y frágil de editar. Dividirlo por responsabilidad en varios archivos, **preservando el comportamiento** (mismos métodos, mismo modelo, mismos tests), para bajar el core a la mitad y aislar los concerns periféricos.

## Mecanismo (patrón Odoo `_inherit`)

Odoo fusiona en **un solo modelo** todas las `class AccountMove(models.Model): _inherit = "account.move"` de cualquier archivo del addon. Se mueven **grupos de métodos** a archivos nuevos, cada uno con `_inherit`. Todo sigue siendo el mismo `self`/modelo:
- Los **69 campos** y las **constantes de módulo** (`TAX_CODE_MAP`, `DETRACCION_TASAS`, `UNIDAD_IMPORT`, `AFECT_IMPORT`, `TIPO_IMPORT`, `DESC_GLOBAL_NO_AFECTA_COD`, etc.) se **quedan en el core** (`account_move_biller.py`), se definen una sola vez.
- Cada archivo extraído **importa del core** lo que use: `from .account_move_biller import TAX_CODE_MAP, ...`.
- Cada archivo extraído trae **sus propios imports** de stdlib/odoo (base64, io, zipfile, requests, `_`, `api`, `fields`, `models`, `UserError`, …) según lo que usen sus métodos.
- Se registran en `models/__init__.py` DESPUÉS de `account_move_biller` (para que las constantes ya existan al importar).
- La clase `AccountMoveLine` y todo el CORE de emisión (dinero L3, validación L1, helpers, constructores del payload, async, acción, API BFF, negocio/estado) **NO se tocan** — quedan en `account_move_biller.py`.

## Decisiones (confirmadas)

- **Enfoque A (periférico):** se extraen los 5 concerns que NO son el hot-path de emisión. El core de emisión queda intacto (~4039 líneas).
- **Fields quedan en el core.** No se mueven definiciones de campos (evita el riesgo de re-declararlos y su orden).
- **Sin cambios de lógica.** Copiar-pegar métodos tal cual; solo se agregan imports. Cualquier desvío lo caza el harness (674 tests).
- **Red de seguridad:** `scripts/test-fresh.sh` (BD fresca, 674/674) tras CADA extracción. Un método que quede referenciando una constante no importada revienta un test → se ve al instante.

## Los 5 archivos a extraer

| Nuevo archivo | Concern | Líneas aprox | Sección origen |
|---|---|---|---|
| `account_move_ple.py` | Libros electrónicos PLE 14.1 (ventas) + 8.1 (compras) + 12.1 (inventario) | ~1596 | 4262–5858 |
| `account_move_compras.py` | Compras (margen, XML de compra, etc.) | ~876 | 6219–7095 |
| `account_move_baja.py` | Comunicación de baja (RA) + Resumen Diario de Boletas (RC) | ~497 | 7402–7899 |
| `account_move_importacion.py` | Importación masiva de productos | ~361 | 5858–6219 |
| `account_move_pdf.py` | Descargas / representación impresa (SFS 2.4) | ~307 | 7095–7402 |

**Core resultante** (`account_move_biller.py`): ~4039 líneas (el motor de emisión, cohesivo).

## Riesgos y mitigación

- **Método movido usa una constante/helper del core no importado** → `NameError` al ejecutar → **lo caza un test**. Antes de cada commit, grep de las constantes usadas en la sección para importarlas.
- **Colisión de nombres de método** → imposible: se MUEVEN (no duplican) métodos; cada nombre existe una vez.
- **Orden de carga en `__init__.py`** → irrelevante para métodos; los archivos extraídos van después del core por prolijidad (constantes disponibles al importar).
- **Un test importa un método por su ubicación** → los tests llaman métodos por `self.<metodo>()` / `env['account.move'].<metodo>()`, no por archivo → transparente.

## Testing

- **Baseline:** `scripts/test-fresh.sh` = **674/674** ANTES de empezar (verde comprobado).
- Tras cada una de las 5 extracciones: `scripts/test-fresh.sh` debe seguir **674/674**. Si baja, se revierte/ajusta esa extracción antes de continuar.
- No se agregan tests nuevos (es refactor sin cambio de comportamiento); la garantía es que los 674 existentes siguen verdes.

## Fuera de alcance

- Descomponer el CORE de emisión (enfoque B) — follow-up si se desea.
- Mover helpers a `tools/` (enfoque C).
- Cualquier cambio de lógica, optimización o "de paso arreglo esto".

## Criterios de aceptación

1. `account_move_biller.py` baja de ~7899 a ~4039 líneas; 5 archivos nuevos con los concerns periféricos.
2. `scripts/test-fresh.sh` sigue en **674/674** al final.
3. Cero cambio de comportamiento: sin tocar SPA, biller Java, XML, ni la lógica de ningún método (solo su ubicación + imports).

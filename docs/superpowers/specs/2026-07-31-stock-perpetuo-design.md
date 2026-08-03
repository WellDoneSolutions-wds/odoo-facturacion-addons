# Stock perpetuo — completar el inventario permanente + kardex — Diseño

**Fecha:** 2026-07-31
**Repos:** `odoo-facturacion-addons` (addon `l10n_pe_ne_biller`) + `ne-express` (SPA `apps/web-bff`).
Ramas: `feat/stock-perpetuo` en ambos (van juntas). No toca el biller Java ni el XML UBL.

## Contexto: lo que YA existe (no se rehace)

El inventario permanente **ya está implementado y es sólido** en el backend:

- La **emisión mueve stock real** (`account_move_producto.py::_l10n_pe_ne_mover_stock` / `_mover_stock_compra` / `_revertir_stock`): factura/boleta → salida, NC → entrada, compra → entrada, reversa de rechazo → revierte. Con `stock.move` de Odoo (`_action_confirm`/`_action_assign`/`_action_done`), fecha del documento en el movimiento, política "**nunca bloquear**" (deja negativo + aviso `l10n_pe_ne_stock_aviso`).
- **Lotes + FEFO** (product_expiry), **fraccionamiento** (`_l10n_pe_ne_stock_qty`), enlace `stock.move.l10n_pe_ne_move_id → account.move`.
- **PLE 12.1** (Inventario Permanente Valorizado) se genera de movimientos reales (`account_move_ple.py`, busca `stock.move.line` de productos `is_storable`).
- **SPA**: `Productos.tsx` muestra el stock (columna solo lectura) + toggle "llevar inventario".

El motor central de movimientos es `account.move::_l10n_pe_ne_stock_aplicar(lineas, origen, destino, reversa, con_lote)`.

## Los tres huecos que cierra este trabajo

### Parte 1 · La nota de venta mueve stock

Hoy `l10n_pe_ne.nota_venta` **no toca inventario**, pese a ser una venta real cobrada. Cada venta "sin comprobante" desfasa el stock. Se cierra:

- **Al registrar** (estado `registrada`) → **salida** (existencias → clientes) de las líneas con producto que lleva stock.
- **Al anular** (`registrada → anulada`) → **reversa** (entrada), repone.
- **Al convertir a comprobante** → el comprobante **NO** vuelve a mover (evita el doble descuento); el movimiento existente de la nota se **re-vincula** al comprobante emitido (atribución fiscal correcta en kardex/PLE).

Como la línea de la nota (`l10n_pe_ne.nota_venta.line`) tiene otra forma que `account.move.line`, el motor `_l10n_pe_ne_stock_aplicar` se refactoriza a un **helper genérico** `_l10n_pe_ne_stock_crear_moves(items, origen, destino, ...)` donde `items` es una lista de tuplas `(product, qty_en_uom_producto, uom, lote_o_None)`. `account.move` y `nota_venta` lo llaman ambos. Nuevo campo `stock.move.l10n_pe_ne_nota_venta_id` (espeja `l10n_pe_ne_move_id`).

### Parte 2 · Ajuste / carga inicial de inventario

No hay forma de fijar/corregir stock. Se agrega:

- **Backend** — método `@api.model _l10n_pe_ne_ajustar_stock(product_id, modo, cantidad, motivo)` en `account_move_producto.py` (junto al motor de stock que reusa):
  - `modo="fijar"` (conteo físico / carga inicial): calcula el delta contra `qty_available` y mueve la diferencia contra la ubicación de ajuste (`usage='inventory'`).
  - `modo="restar"` (merma/robo/rotura): salida existencias → ajuste.
  - `modo="sumar"` (corrección/devolución interna): entrada ajuste → existencias.
  - Guarda `motivo` (texto) en el `origin`/aviso del movimiento; mapea al **tipo de operación cat. 12** del PLE.
- **SPA**: acción **"Ajustar"** por fila en Productos (`rowAction`) → modal `AjusteStockModal` (modo · cantidad · motivo; muestra stock actual → resultante). Campo **"Existencia inicial"** opcional al crear producto (bien que lleva stock). Corrige los negativos.

La ubicación de ajuste se resuelve por búsqueda: `stock.location` con `usage='inventory'` de la compañía (existe por defecto: "Inventory adjustment").

### Parte 3 · Kardex / movimientos por producto (SPA)

- **Backend** `@api.model _l10n_pe_ne_kardex(product_id, desde=None, hasta=None)`: devuelve los `stock.move.line` `done` del producto ordenados por fecha, cada uno con `{fecha, documento, tipo, entrada, salida, saldo}` (saldo = acumulado corriente). El documento se deriva del enlace (`l10n_pe_ne_move_id` → comprobante, `l10n_pe_ne_nota_venta_id` → nota, o `origin`/ajuste).
- **Controlador** `GET /ne/api/productos/<id>/kardex?desde&hasta`.
- **SPA**: en Productos, `rowAction` "Kardex" → `KardexDrawer` con tabla (fecha · documento · entrada · salida · **saldo**) + filtro de fechas. Enfocado en **cantidad** (el PLE 12.1 ya valoriza; el valor puede sumarse después).

## Mecanismo y decisiones

- **Reuso, no reescritura**: el motor de `stock.move` ya probado se generaliza; nota y ajuste lo comparten. Nada de lógica de stock nueva paralela.
- **No-doble-descuento** (decisión de correctitud, única opción correcta): la nota mueve al registrar; el comprobante que la convierte NO mueve y hereda el movimiento (re-vinculación).
- **Nunca bloquear** se mantiene también en la nota y el ajuste: un ajuste que no puede aplicarse (p. ej. producto rastreado sin lote) deja aviso, no rompe.
- **Fecha**: el movimiento de la nota lleva `fecha` de la nota; el ajuste lleva la fecha del ajuste (hoy).

## SUNAT (cat. 12 · tipo de operación del PLE 12.1)

El PLE 12.1 ya incluye todo movimiento real. Se agrega mapeo de tipo de operación para los movimientos nuevos:
- Venta (comprobante/nota) → salida por venta.
- Compra → entrada por compra.
- Ajuste `restar`/merma → salida por merma/desmedro.
- Ajuste `sumar`/`fijar` positivo / carga inicial → entrada por ajuste / inventario inicial.

El **sustento documental** de mermas/desmedros (informe técnico, acta) es responsabilidad del contador; el sistema registra el motivo y lo deja en el kardex.

## Riesgos y mitigación

- **Doble descuento nota→comprobante** → cubierto por la re-vinculación + skip en la emisión; test explícito.
- **Producto rastreado (lote) sin existencias en la salida de la nota** → misma política que la emisión: no bloquea, deja aviso.
- **Ajuste de producto rastreado** → la entrada por ajuste necesita lote; si no se indica, se crea un lote de ajuste genérico o se deja aviso (no rompe).
- **Concurrencia con el negativo** → el ajuste "fijar" recalcula el delta contra `qty_available` en el momento de aplicar.

## Testing

- **Backend TDD** (harness `scripts/test-fresh.sh`, BD fresca): 
  - nota registrada descuenta stock; anulada lo repone.
  - conversión nota→comprobante: stock se movió **una sola vez**; el move quedó vinculado al comprobante.
  - ajuste `fijar`/`sumar`/`restar` deja `qty_available` correcto.
  - kardex: saldo acumulado correcto y documento atribuido.
- **SPA**: `tsc --noEmit` + `vitest` (el builder de payload de ajuste, si aplica; y que la suite siga verde).

## Fuera de alcance

- Valorización del kardex en la SPA (costo/valor) — el PLE 12.1 ya la hace; se puede sumar después.
- Multi-almacén / transferencias entre ubicaciones.
- Órdenes de compra / recepción parcial (la compra ya entra por la emisión de la compra).
- Conteo físico masivo (hoja de inventario completa) — el ajuste es por producto; el masivo es follow-up.

## Criterios de aceptación

1. Registrar una nota de venta con un bien que lleva stock **descuenta** el inventario; anularla lo **repone**.
2. Convertir esa nota a comprobante **no vuelve a descontar** y el movimiento queda atribuido al comprobante.
3. Desde Productos se puede **fijar / sumar / restar** stock con un motivo, y el negativo se corrige.
4. Al crear un producto que lleva stock se puede cargar una **existencia inicial**.
5. Desde Productos se ve el **kardex** de un producto (movimientos + saldo corriente).
6. El harness sigue verde y suma tests de los puntos 1–5.

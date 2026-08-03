# Stock perpetuo — Plan de implementación

> **Para el ejecutor:** implementar tarea por tarea. Cada tarea termina con el harness verde (`scripts/test-fresh.sh`) o `tsc`+`vitest` verdes. Steps con checkbox `- [ ]`.

**Goal:** Cerrar los 3 huecos del inventario permanente: (1) la nota de venta mueve stock, (2) ajuste/carga inicial, (3) kardex en la SPA. Reusando el motor de `stock.move` existente.

**Spec:** `docs/superpowers/specs/2026-07-31-stock-perpetuo-design.md`

**Arquitectura:** El motor probado `account.move::_l10n_pe_ne_stock_aplicar` se generaliza a un `@api.model _l10n_pe_ne_stock_crear_moves(items, ...)` sin estado; comprobante, nota y ajuste lo comparten. La nota mueve al registrar y repone al anular; al convertir, el comprobante no re-mueve (hereda el movimiento).

## Global Constraints
- **Nunca bloquear** una venta/registro por stock: los fallos de movimiento se tragan y dejan aviso (`l10n_pe_ne_stock_aviso` en comprobante; campo análogo o log en nota/ajuste). Copiado del motor actual.
- **Sin cambio de comportamiento** en la emisión existente: la refactor de Task 1 debe dejar los tests actuales de stock verdes (`test_stock_emision.py`).
- Controlador: rutas con los dicts `_GET`/`_POST` ya definidos en `controllers/main.py` (auth public + Bearer, `type=http`). Respuestas con el helper `_json`/`_ok` que ya usan las rutas vecinas — copiar el patrón de `nota_venta_estado`.
- SPA: precio/stock en cantidad de la unidad del producto. `tsc --noEmit` limpio y `vitest` verde tras cada tarea de front.
- Ubicación de ajuste: `stock.location` con `usage='inventory'` de la compañía (existe: "Inventory adjustment"). Resolver por búsqueda, no por xmlid.

---

## FASE A — Backend (odoo), rama `feat/stock-perpetuo`

### Task 1: Generalizar el motor de stock + campo nota en `stock.move`

**Files:**
- Modify: `addons/l10n_pe_ne_biller/models/stock_move_biller.py` (agregar campo)
- Modify: `addons/l10n_pe_ne_biller/models/account_move_producto.py` (`_l10n_pe_ne_stock_aplicar` → wrapper de un nuevo `_l10n_pe_ne_stock_crear_moves`)
- Test: `addons/l10n_pe_ne_biller/tests/test_stock_emision.py` (debe seguir verde; no se agrega test nuevo — es refactor)

**Interfaces (Produces):**
- `@api.model _l10n_pe_ne_stock_crear_moves(self, items, origen, destino, company, *, reversa=False, origin="", fecha=False, link_field=None, link_id=False) -> (stock.move recordset, error_str_or_None)`. `items` = lista de dicts `{"product": product.product, "qty": float, "uom": uom.uom, "lote": stock.lot|None}`.
- `stock.move.l10n_pe_ne_nota_venta_id` (Many2one `l10n_pe_ne.nota_venta`).

- [ ] **Step 1: Campo en stock.move.** En `stock_move_biller.py`, tras `l10n_pe_ne_reversa`, agregar:
```python
    l10n_pe_ne_nota_venta_id = fields.Many2one(
        "l10n_pe_ne.nota_venta",
        string="Nota de venta (NE Express)",
        index=True,
        ondelete="set null",
        copy=False,
        help="Nota de venta cuyo registro generó este movimiento (venta sin comprobante).",
    )
```

- [ ] **Step 2: Extraer el motor.** En `account_move_producto.py`, agregar el método `@api.model` (copiando el cuerpo actual del bucle create + confirm/assign/done/lote/date de `_l10n_pe_ne_stock_aplicar`, pero tomando `items` en vez de `lineas` y `link_field`/`link_id` en vez de `l10n_pe_ne_move_id=self.id`):
```python
    @api.model
    def _l10n_pe_ne_stock_crear_moves(self, items, origen, destino, company, *,
                                      reversa=False, origin="", fecha=False,
                                      link_field=None, link_id=False):
        """Motor común de creación+validación de stock.move. Stateless: todo entra por args, así lo
        comparten comprobante, nota de venta y ajuste. Devuelve (moves, error|None); NUNCA levanta:
        el documento ya existe y no puede caerse porque el inventario no cuadre."""
        Move = self.env["stock.move"]
        moves = Move.browse()
        for it in items:
            vals = {
                "product_id": it["product"].id,
                "product_uom_qty": it["qty"],
                "product_uom": it["uom"].id,
                "location_id": origen.id,
                "location_dest_id": destino.id,
                "company_id": company.id,
                "origin": origin or "",
                "l10n_pe_ne_reversa": reversa,
            }
            if link_field:
                vals[link_field] = link_id
            moves |= Move.create(vals)
        try:
            moves._action_confirm()
            moves._action_assign()
            for m, it in zip(moves, items):
                lote = it.get("lote")
                if lote:
                    if not m.move_line_ids:
                        m.move_line_ids = [(0, 0, {
                            "product_id": m.product_id.id,
                            "location_id": m.location_id.id,
                            "location_dest_id": m.location_dest_id.id,
                            "company_id": m.company_id.id,
                        })]
                    m.move_line_ids.write({"lot_id": lote.id})
                m.quantity = m.product_uom_qty
                m.picked = True
            moves._action_done()
            if fecha:
                moves.write({"date": fecha})
                moves.move_line_ids.write({"date": fecha})
        except Exception as e:  # noqa: BLE001
            _logger.exception("stock: no se pudo mover el stock (%s): %s", origin, e)
            return Move.browse(), str(e)
        return moves, None
```

- [ ] **Step 3: `_l10n_pe_ne_stock_aplicar` pasa a ser wrapper.** Reemplazar su cuerpo por:
```python
    def _l10n_pe_ne_stock_aplicar(self, lineas, origen, destino, reversa=False, con_lote=False):
        self.ensure_one()
        items = [{
            "product": l.product_id,
            "qty": self._l10n_pe_ne_stock_qty(l),
            "uom": l.product_uom_id,
            "lote": self._l10n_pe_ne_lote_de(l) if con_lote else None,
        } for l in lineas]
        moves, err = self.env["account.move"]._l10n_pe_ne_stock_crear_moves(
            items, origen, destino, self.company_id, reversa=reversa,
            origin=self.name or "", fecha=self.invoice_date,
            link_field="l10n_pe_ne_move_id", link_id=self.id)
        self.l10n_pe_ne_stock_aviso = (
            (_("No se pudo mover el inventario de este documento: %s") % err)[:500] if err else False
        )
        return moves
```

- [ ] **Step 4: Correr harness (refactor sin cambio de comportamiento).**
Run: `scripts/test-fresh.sh` → **685/685** (0 failed, 0 error). Si baja, revisar la refactor.

- [ ] **Step 5: Commit.**
```bash
git add addons/l10n_pe_ne_biller/models/stock_move_biller.py addons/l10n_pe_ne_biller/models/account_move_producto.py
git commit -m "refactor(stock): motor _l10n_pe_ne_stock_crear_moves reusable + campo nota en stock.move"
```

---

### Task 2: La nota de venta mueve stock (registrar) y lo repone (anular)

**Files:**
- Modify: `addons/l10n_pe_ne_biller/models/l10n_pe_ne_nota_venta.py`
- Test: `addons/l10n_pe_ne_biller/tests/test_stock_nota_venta.py` (nuevo)

**Interfaces (Consumes):** `env["account.move"]._l10n_pe_ne_stock_crear_moves(...)` de Task 1.
**Produces:** `l10n_pe_ne.nota_venta::_l10n_pe_ne_nv_mover_stock(reversa=False)`.

- [ ] **Step 1: Test que falla.** Crear `tests/test_stock_nota_venta.py`:
```python
from odoo.tests import TransactionCase, tagged
from .common import L10nPeSeedMixin


@tagged("post_install", "-at_install")
class TestStockNotaVenta(L10nPeSeedMixin, TransactionCase):
    def _producto(self, qty_inicial=10):
        p = self.env["product.product"].create({
            "name": "Gaseosa", "type": "consu", "is_storable": True,
            "lst_price": 10.0, "l10n_pe_ne_unit_code": "NIU",
        })
        # carga inicial vía quant
        wh = self.env["stock.warehouse"].search([("company_id", "=", self.env.company.id)], limit=1)
        self.env["stock.quant"]._update_available_quantity(p, wh.lot_stock_id, qty_inicial)
        return p

    def test_nota_registrada_descuenta_y_anulada_repone(self):
        p = self._producto(10)
        nv = self.env["l10n_pe_ne.nota_venta"].create({
            "currency_id": self.env.company.currency_id.id,
            "company_id": self.env.company.id,
            "line_ids": [(0, 0, {"product_id": p.id, "cantidad": 3, "precio_unitario": 10.0})],
        })
        nv._l10n_pe_ne_nv_mover_stock()
        self.assertEqual(p.qty_available, 7.0)   # 10 - 3
        nv._l10n_pe_ne_nv_mover_stock(reversa=True)
        self.assertEqual(p.qty_available, 10.0)  # repuesto
```
Run: `scripts/test-fresh.sh "l10n_pe_ne_biller" "/l10n_pe_ne_biller:TestStockNotaVenta"` → FAIL (método no existe).

- [ ] **Step 2: Implementar el movimiento.** En `l10n_pe_ne_nota_venta.py`, dentro de `class L10nPeNeNotaVenta`, agregar (importar `_logger` si falta):
```python
    def _l10n_pe_ne_nv_mover_stock(self, reversa=False):
        """Descuenta (registrar) o repone (anular) el stock de las líneas de bien de la nota.
        Espeja _l10n_pe_ne_mover_stock del comprobante pero para el modelo propio. Nunca bloquea."""
        self.ensure_one()
        lineas = self.line_ids.filtered(
            lambda l: l.product_id and l.product_id.is_storable and (l.cantidad or 0) > 0)
        if not lineas:
            return
        wh = self.env["stock.warehouse"].search([("company_id", "=", self.company_id.id)], limit=1)
        clientes = self.env.ref("stock.stock_location_customers", raise_if_not_found=False)
        if not wh or not clientes:
            _logger.warning("stock nota %s: sin almacén/ubicación clientes; no se mueve", self.name)
            return
        origen, destino = (clientes, wh.lot_stock_id) if reversa else (wh.lot_stock_id, clientes)
        items = [{
            "product": l.product_id,
            "qty": abs(l.cantidad or 0.0),
            "uom": l.product_id.uom_id,
            "lote": None,
        } for l in lineas]
        self.env["account.move"]._l10n_pe_ne_stock_crear_moves(
            items, origen, destino, self.company_id, reversa=reversa,
            origin=self.name or "", fecha=self.fecha,
            link_field="l10n_pe_ne_nota_venta_id", link_id=self.id)
```

- [ ] **Step 3: Enganchar en el ciclo de vida.** En `_l10n_pe_ne_crear_nota_venta` (el método que crea la nota `registrada` desde React), después de crear `nv` y antes de devolver, agregar:
```python
        nv._l10n_pe_ne_nv_mover_stock()  # venta real → descuenta inventario
```
Y en `l10n_pe_ne_set_estado_nota_venta`, cuando pasa a `anulada` (antes/después de escribir el estado), reponer si la nota había movido stock (estado previo `registrada`):
```python
        if estado == "anulada" and self.estado == "registrada":
            self._l10n_pe_ne_nv_mover_stock(reversa=True)
```
(colocarlo ANTES del `self.estado = estado`, para leer el estado previo).

- [ ] **Step 4: Test pasa.** Run: `scripts/test-fresh.sh "l10n_pe_ne_biller" "/l10n_pe_ne_biller:TestStockNotaVenta"` → PASS. Luego el harness completo → 686/686.

- [ ] **Step 5: Commit.**
```bash
git add addons/l10n_pe_ne_biller/models/l10n_pe_ne_nota_venta.py addons/l10n_pe_ne_biller/tests/test_stock_nota_venta.py
git commit -m "feat(stock): la nota de venta descuenta stock al registrar y lo repone al anular"
```

---

### Task 3: No-doble-descuento al convertir la nota a comprobante

**Files:**
- Modify: `addons/l10n_pe_ne_biller/models/account_move_api.py` (skip mover_stock si viene de nota; re-vincular en `_l10n_pe_ne_vincular_nota_venta`)
- Test: `addons/l10n_pe_ne_biller/tests/test_stock_nota_venta.py` (agregar caso)

**Interfaces (Consumes):** `payload["notaVentaId"]` en `l10n_pe_ne_quick_emit`; hook `_l10n_pe_ne_vincular_nota_venta` (ya existe, api.py ~429).

- [ ] **Step 1: Test que falla.** Agregar a `TestStockNotaVenta`:
```python
    def test_conversion_no_doble_descuenta(self):
        p = self._producto(10)
        nv = self.env["l10n_pe_ne.nota_venta"].create({
            "currency_id": self.env.company.currency_id.id, "company_id": self.env.company.id,
            "line_ids": [(0, 0, {"product_id": p.id, "cantidad": 3, "precio_unitario": 10.0})],
        })
        nv._l10n_pe_ne_nv_mover_stock()
        self.assertEqual(p.qty_available, 7.0)
        # emula la conversión: emite un comprobante con notaVentaId
        payload = {
            "tipoDoc": "03", "moneda": "PEN", "notaVentaId": nv.id,
            "cliente": {"tipoDoc": "0", "numDoc": "", "razonSocial": "VARIOS"},
            "lineas": [{"descripcion": "Gaseosa", "cantidad": 3, "precioUnitario": 10.0,
                        "taxCode": "1000", "productId": p.id}],
        }
        move = self.env["account.move"].l10n_pe_ne_quick_emit(payload, enviar=False)
        self.assertEqual(p.qty_available, 7.0)               # NO volvió a bajar (sigue 7, no 4)
        moves = self.env["stock.move"].search([("l10n_pe_ne_nota_venta_id", "=", nv.id)])
        self.assertTrue(all(m.l10n_pe_ne_move_id == move for m in moves))  # re-vinculado
```
(Ajustar el retorno de `l10n_pe_ne_quick_emit` a como devuelva el move real — usar el dict de respuesta y `browse` si hace falta.)
Run → FAIL (hoy descuenta de nuevo: quedaría 4).

- [ ] **Step 2: Skip en la emisión.** En `account_move_api.py`, donde llama `move._l10n_pe_ne_mover_stock()` (~línea 225), envolver:
```python
        if not payload.get("notaVentaId"):
            move._l10n_pe_ne_mover_stock()
        # si viene de una nota, el stock ya se movió al registrarla; se re-vincula en el hook.
```

- [ ] **Step 3: Re-vincular en el hook.** En `_l10n_pe_ne_vincular_nota_venta` (api.py ~429), tras marcar la nota convertida, re-apuntar sus movimientos al comprobante:
```python
    def _l10n_pe_ne_vincular_nota_venta(self, nota_venta_id, move_id):
        nv = self.env["l10n_pe_ne.nota_venta"].browse(int(nota_venta_id)).exists()
        if nv:
            nv.l10n_pe_ne_vincular_comprobante(int(move_id))
            # El stock ya lo movió la nota; se re-atribuye al comprobante (kardex/PLE fiscal).
            self.env["stock.move"].search([
                ("l10n_pe_ne_nota_venta_id", "=", nv.id),
                ("l10n_pe_ne_move_id", "=", False),
            ]).write({"l10n_pe_ne_move_id": int(move_id)})
```

- [ ] **Step 4: Test pasa + harness completo.** Run: `scripts/test-fresh.sh "l10n_pe_ne_biller" "/l10n_pe_ne_biller:TestStockNotaVenta"` → PASS. Harness completo → verde.

- [ ] **Step 5: Commit.**
```bash
git add addons/l10n_pe_ne_biller/models/account_move_api.py addons/l10n_pe_ne_biller/tests/test_stock_nota_venta.py
git commit -m "fix(stock): convertir nota a comprobante no vuelve a descontar (re-vincula el movimiento)"
```

---

### Task 4: Ajuste de inventario (backend + ruta)

**Files:**
- Modify: `addons/l10n_pe_ne_biller/models/account_move_producto.py` (`_l10n_pe_ne_ajustar_stock`)
- Modify: `addons/l10n_pe_ne_biller/controllers/main.py` (ruta `POST /ne/api/productos/<id>/ajustar-stock`)
- Test: `addons/l10n_pe_ne_biller/tests/test_ajuste_stock.py` (nuevo)

**Produces:** `@api.model _l10n_pe_ne_ajustar_stock(product_id, modo, cantidad, motivo="") -> {"stock": float, "aviso": str|False}`. `modo ∈ {"fijar","sumar","restar"}`.

- [ ] **Step 1: Test que falla.** `tests/test_ajuste_stock.py`:
```python
from odoo.tests import TransactionCase, tagged
from .common import L10nPeSeedMixin


@tagged("post_install", "-at_install")
class TestAjusteStock(L10nPeSeedMixin, TransactionCase):
    def _prod(self):
        return self.env["product.product"].create({
            "name": "Tornillo", "type": "consu", "is_storable": True, "lst_price": 1.0})

    def test_fijar_sumar_restar(self):
        AM = self.env["account.move"]
        p = self._prod()
        AM._l10n_pe_ne_ajustar_stock(p.id, "fijar", 100, "carga inicial")
        self.assertEqual(p.qty_available, 100.0)
        AM._l10n_pe_ne_ajustar_stock(p.id, "restar", 5, "merma")
        self.assertEqual(p.qty_available, 95.0)
        AM._l10n_pe_ne_ajustar_stock(p.id, "sumar", 10, "correccion")
        self.assertEqual(p.qty_available, 105.0)
        AM._l10n_pe_ne_ajustar_stock(p.id, "fijar", 90, "conteo")
        self.assertEqual(p.qty_available, 90.0)
```
Run → FAIL.

- [ ] **Step 2: Implementar.** En `account_move_producto.py`:
```python
    @api.model
    def _l10n_pe_ne_ajustar_stock(self, product_id, modo, cantidad, motivo=""):
        """Ajuste de inventario por producto contra la ubicación de ajuste (usage='inventory').
        modo: 'fijar' (conteo/carga inicial → deja el stock EN `cantidad`), 'sumar', 'restar'.
        Reusa el motor _l10n_pe_ne_stock_crear_moves. Nunca levanta."""
        prod = self.env["product.product"].browse(int(product_id)).exists()
        if not prod or not prod.is_storable:
            return {"stock": 0.0, "aviso": _("El producto no lleva inventario.")}
        company = self.env.company
        wh = self.env["stock.warehouse"].search([("company_id", "=", company.id)], limit=1)
        ajuste = self.env["stock.location"].search(
            [("usage", "=", "inventory"), ("company_id", "in", [company.id, False])],
            order="company_id desc", limit=1)
        if not wh or not ajuste:
            return {"stock": prod.qty_available, "aviso": _("Sin almacén o ubicación de ajuste.")}
        actual = prod.qty_available
        qty = abs(float(cantidad or 0.0))
        if modo == "fijar":
            delta = round(qty - actual, 4)
        elif modo == "restar":
            delta = -qty
        else:  # sumar
            delta = qty
        if not delta:
            return {"stock": actual, "aviso": False}
        entrada = delta > 0
        origen, destino = (ajuste, wh.lot_stock_id) if entrada else (wh.lot_stock_id, ajuste)
        items = [{"product": prod, "qty": abs(delta), "uom": prod.uom_id, "lote": None}]
        _moves, err = self._l10n_pe_ne_stock_crear_moves(
            items, origen, destino, company,
            origin=(_("Ajuste: %s") % (motivo or "-"))[:60], fecha=False)
        return {"stock": prod.qty_available, "aviso": (err[:300] if err else False)}
```

- [ ] **Step 3: Ruta del controlador.** En `controllers/main.py`, junto a las rutas de producto, agregar (copiar el patrón de `nota_venta_estado` para auth/uid/JSON):
```python
    @http.route("/ne/api/productos/<int:rec_id>/ajustar-stock", **_POST)
    def producto_ajustar_stock(self, rec_id, **kw):
        uid = self._uid_or_401()
        data = self._json_body()
        res = self._move(uid)._l10n_pe_ne_ajustar_stock(
            rec_id, data.get("modo") or "fijar", data.get("cantidad") or 0, data.get("motivo") or "")
        return self._ok(res)
```
(Ajustar `_uid_or_401`/`_json_body`/`_move`/`_ok` a los helpers reales que usan las rutas vecinas — copiar exactamente de `nota_venta_estado`.)

- [ ] **Step 4: Test pasa + harness.** Run: `scripts/test-fresh.sh "l10n_pe_ne_biller" "/l10n_pe_ne_biller:TestAjusteStock"` → PASS. Harness completo verde.

- [ ] **Step 5: Commit.**
```bash
git add addons/l10n_pe_ne_biller/models/account_move_producto.py addons/l10n_pe_ne_biller/controllers/main.py addons/l10n_pe_ne_biller/tests/test_ajuste_stock.py
git commit -m "feat(stock): ajuste de inventario por producto (fijar/sumar/restar) + ruta"
```

---

### Task 5: Existencia inicial al crear producto

**Files:**
- Modify: `addons/l10n_pe_ne_biller/models/account_move_producto.py` o el método de crear producto (`l10n_pe_ne_quick_product` / el usado por `POST /ne/api/productos`)
- Test: `addons/l10n_pe_ne_biller/tests/test_ajuste_stock.py` (agregar caso)

- [ ] **Step 1: Localizar** el método que crea el producto desde el controlador `POST /ne/api/productos` (grep `def l10n_pe_ne_crear_producto` / `create_producto` en los modelos). Confirmar dónde termina de crear el `product.product`.

- [ ] **Step 2: Test que falla.** En `TestAjusteStock`:
```python
    def test_existencia_inicial_al_crear(self):
        res = self.env["account.move"].l10n_pe_ne_crear_producto({
            "descripcion": "Foco LED", "precio": 12, "llevaStock": True,
            "existenciaInicial": 25,
        })
        p = self.env["product.product"].browse(res["id"])
        self.assertEqual(p.qty_available, 25.0)
```
(Ajustar el nombre real del método de creación y la forma del payload/retorno.)
Run → FAIL.

- [ ] **Step 3: Implementar.** Tras crear el producto en ese método, si `payload.get("existenciaInicial")` y el producto lleva stock:
```python
        inicial = float(payload.get("existenciaInicial") or 0)
        if inicial > 0 and prod.is_storable:
            self._l10n_pe_ne_ajustar_stock(prod.id, "fijar", inicial, _("Existencia inicial"))
```

- [ ] **Step 4: Test pasa + harness verde.**

- [ ] **Step 5: Commit.**
```bash
git commit -am "feat(stock): existencia inicial opcional al crear un producto"
```

---

### Task 6: Kardex (backend + ruta)

**Files:**
- Modify: `addons/l10n_pe_ne_biller/models/account_move_producto.py` (`_l10n_pe_ne_kardex`)
- Modify: `addons/l10n_pe_ne_biller/controllers/main.py` (ruta `GET /ne/api/productos/<id>/kardex`)
- Test: `addons/l10n_pe_ne_biller/tests/test_kardex.py` (nuevo)

**Produces:** `@api.model _l10n_pe_ne_kardex(product_id, desde=None, hasta=None) -> {"stock": float, "movimientos": [{"fecha","documento","tipo","entrada","salida","saldo"}]}`.

- [ ] **Step 1: Test que falla.** `tests/test_kardex.py`: crea producto, carga inicial 100 (ajuste), una salida de nota de 3, verifica que `_l10n_pe_ne_kardex` devuelve 2 movimientos con `saldo` 100 y 97 (acumulado), y `stock` 97. (Reusa el patrón de los tests anteriores.)
Run → FAIL.

- [ ] **Step 2: Implementar.**
```python
    @api.model
    def _l10n_pe_ne_kardex(self, product_id, desde=None, hasta=None):
        prod = self.env["product.product"].browse(int(product_id)).exists()
        if not prod:
            return {"stock": 0.0, "movimientos": []}
        dom = [("product_id", "=", prod.id), ("state", "=", "done"),
               ("company_id", "=", self.env.company.id)]
        if desde:
            dom.append(("date", ">=", desde))
        if hasta:
            dom.append(("date", "<=", hasta + " 23:59:59"))
        lineas = self.env["stock.move.line"].search(dom, order="date asc, id asc")
        stock_locs = self.env["stock.location"].search(
            [("usage", "=", "internal"), ("company_id", "in", [self.env.company.id, False])])
        movimientos, saldo = [], 0.0
        for ml in lineas:
            entra = ml.location_dest_id in stock_locs
            sale = ml.location_id in stock_locs
            qty = ml.quantity
            entrada = qty if entra and not sale else 0.0
            salida = qty if sale and not entra else 0.0
            saldo += entrada - salida
            mv = ml.move_id
            doc = (mv.l10n_pe_ne_move_id.name or mv.l10n_pe_ne_move_id.l10n_pe_ne_numero
                   if mv.l10n_pe_ne_move_id else
                   (mv.l10n_pe_ne_nota_venta_id.name if mv.l10n_pe_ne_nota_venta_id else mv.origin))
            movimientos.append({
                "fecha": ml.date and str(ml.date)[:10] or "",
                "documento": doc or "—",
                "tipo": "entrada" if entrada else "salida",
                "entrada": entrada, "salida": salida, "saldo": round(saldo, 3),
            })
        return {"stock": prod.qty_available, "movimientos": movimientos}
```

- [ ] **Step 3: Ruta.** En `controllers/main.py` (patrón `_GET`):
```python
    @http.route("/ne/api/productos/<int:rec_id>/kardex", **_GET)
    def producto_kardex(self, rec_id, desde=None, hasta=None, **kw):
        uid = self._uid_or_401()
        return self._ok(self._move(uid)._l10n_pe_ne_kardex(rec_id, desde, hasta))
```

- [ ] **Step 4: Test pasa + harness verde.**

- [ ] **Step 5: Commit.**
```bash
git add addons/l10n_pe_ne_biller/models/account_move_producto.py addons/l10n_pe_ne_biller/controllers/main.py addons/l10n_pe_ne_biller/tests/test_kardex.py
git commit -m "feat(stock): kardex por producto (movimientos + saldo corriente) + ruta"
```

- [ ] **Step 6: Push FASE A.** `git push origin feat/stock-perpetuo`

---

## FASE B — Frontend (ne-express), rama `feat/stock-perpetuo`

### Task 7: Endpoints en api.ts

**Files:** Modify `ne-express/apps/web-bff/src/api.ts`

- [ ] **Step 1:** Junto a los endpoints de producto, agregar:
```typescript
  ajustarStock: (id: number, modo: 'fijar' | 'sumar' | 'restar', cantidad: number, motivo: string) =>
    jsend('POST', `/api/productos/${id}/ajustar-stock`, { modo, cantidad, motivo }),
  kardex: (id: number, desde?: string, hasta?: string) =>
    jget(`/api/productos/${id}/kardex${desde || hasta ? `?desde=${desde || ''}&hasta=${hasta || ''}` : ''}`),
```

- [ ] **Step 2:** `npx tsc --noEmit` → limpio. Commit: `git commit -am "feat(stock): api ajustarStock + kardex"`

---

### Task 8: Acción "Ajustar" + `AjusteStockModal` en Productos

**Files:**
- Create: `ne-express/apps/web-bff/src/components/AjusteStockModal.tsx`
- Modify: `ne-express/apps/web-bff/src/pages/Productos.tsx` (usar `rowAction`)

- [ ] **Step 1: Modal.** Crear `AjusteStockModal.tsx`: props `{ open, producto, onClose, onDone }`. Modos (fijar/sumar/restar) como segmented, input cantidad, select motivo (`['conteo','carga_inicial','merma','robo','rotura','correccion']` con labels), muestra "Stock actual: N → Resultante: M". Al guardar: `await api.ajustarStock(producto.id, modo, cantidad, motivo); onDone()`. Reusar `Dialog`/`FancyInput`/`Button` como `NuevoProductoModal`.

- [ ] **Step 2: Cablear en Productos.** Agregar estado `const [ajuste, setAjuste] = useState<any|null>(null)` y `const [kardex, setKardex] = useState<any|null>(null)` (para Task 10). Envolver el `<CrudPage>` en un fragment y pasar:
```tsx
      rowAction={it => it.llevaStock ? (
        <>
          <ActBtn title="Kardex" cls="actbtn--view" onClick={() => setKardex(it)}><HistoryOutlined fontSize="small" /></ActBtn>
          <ActBtn title="Ajustar stock" cls="actbtn--edit" onClick={() => setAjuste(it)}><TuneOutlined fontSize="small" /></ActBtn>
        </>
      ) : null}
```
(Definir un `ActBtn` local o reusar el de otras páginas; importar íconos MUI.) Y renderizar `<AjusteStockModal open={!!ajuste} producto={ajuste} onClose={()=>setAjuste(null)} onDone={()=>{setAjuste(null); /* recargar */}} />`. Para recargar tras el ajuste, usar el `reload` que expone CrudPage vía `headerExtra`/ref, o un `key` que fuerce refetch.

- [ ] **Step 3:** `npx tsc --noEmit` + `npx vitest run --exclude '**/e2e/**'` → verde. Commit.

---

### Task 9: Campo "Existencia inicial" al crear producto

**Files:** Modify `ne-express/apps/web-bff/src/pages/Productos.tsx`

- [ ] **Step 1:** En `fields`, agregar (condicional a llevaStock, y solo en alta — CrudPage puede no soportar condicional: agregarlo simple y que el backend lo ignore en update):
```tsx
        { key: 'existenciaInicial', label: 'Existencia inicial (opcional)', type: 'number', def: '',
          hint: 'Stock con el que arranca este producto. Se puede corregir luego con «Ajustar».' },
```
Verificar que `productoSchema` (lib/schemas) acepte el campo opcional (agregarlo si el schema es estricto). El backend (Task 5) lo consume solo al crear.

- [ ] **Step 2:** `tsc` + `vitest` verde. Commit.

---

### Task 10: `KardexDrawer`

**Files:**
- Create: `ne-express/apps/web-bff/src/components/KardexDrawer.tsx`
- Modify: `ne-express/apps/web-bff/src/pages/Productos.tsx` (render del drawer con el estado `kardex` de Task 8)

- [ ] **Step 1: Drawer.** `KardexDrawer.tsx`: props `{ producto, onClose }`. Al abrir, `api.kardex(producto.id, desde, hasta)`; tabla (Fecha · Documento · Entrada · Salida · **Saldo**) con `DataTable`; filtro de fechas (dos `FancyDate`); encabezado con nombre + stock actual. Salida en rojo, entrada en verde; saldo en mono.

- [ ] **Step 2: Render.** En Productos, `{kardex && <KardexDrawer producto={kardex} onClose={() => setKardex(null)} />}`.

- [ ] **Step 3:** `tsc` + `vitest` verde. Commit + push `feat/stock-perpetuo`.

---

## Verificación final
- Harness `scripts/test-fresh.sh` verde con los tests nuevos (nota mueve/repone, no-doble, ajuste, kardex).
- SPA `tsc --noEmit` limpio + `vitest` verde.
- Prueba visual (browser-harness o manual): registrar nota de un bien → stock baja; ajustar → corrige; kardex muestra los movimientos.
- Las dos ramas `feat/stock-perpetuo` (odoo + ne-express) se mergean juntas.

## Self-review del plan
- **Cobertura del spec:** Parte 1 → Tasks 1-3; Parte 2 → Tasks 4-5, 8-9; Parte 3 → Tasks 6, 10. ✓
- **Riesgo doble-descuento** → Task 3 con test explícito. ✓
- **Ambigüedad conocida:** los helpers exactos del controlador (`_uid_or_401`/`_json_body`/`_ok`/`_move`) deben copiarse de una ruta vecina real (p. ej. `nota_venta_estado`) al implementar Task 4/6; el nombre real del método "crear producto" se confirma en Task 5 Step 1. Señalado en cada task.

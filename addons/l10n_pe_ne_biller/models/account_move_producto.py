# -*- coding: utf-8 -*-
"""account.move — Producto: margen, tipo, stock (movimientos, kardex de venta).
Extraído de account_move_biller.py (refactor sin cambio de comportamiento)."""
import logging

from odoo import _, api, fields, models
from odoo.exceptions import UserError
from ..tools.caja_arqueo import normalizar_medio
from .account_move_biller import DEFAULT_UNIT_CODE, _percep_float

_logger = logging.getLogger(__name__)


class AccountMove(models.Model):
    _inherit = "account.move"

    @api.model
    def _l10n_pe_ne_margen_default(self):
        """Margen por defecto del negocio, en %. Configurable en caliente sin redeploy.
        30% es un punto de partida razonable para el retail peruano, no una verdad: cada
        negocio lo ajusta, y cada producto puede tener el suyo."""
        raw = self.env["ir.config_parameter"].sudo().get_param(
            "l10n_pe_ne.margen_default", "30"
        )
        try:
            return float(raw)
        except (TypeError, ValueError):
            return 30.0

    @api.model
    def _l10n_pe_ne_precio_con_margen(self, costo, margen):
        """Costo → precio de venta, ambos CON IGV.

        El margen se aplica sobre el bruto porque toda la app trabaja con precios de vitrina:
        así el número que sale es el que va en la etiqueta, sin desarmar el impuesto para
        pensar el negocio. Redondea a 2: es un precio, no una base imponible.

        `margen=None` usa el default del negocio; `margen=0` es un margen de CERO (vender al
        costo) y se respeta. Se distingue con `is None` a propósito: en Python 0 == False, y
        un `if not margen` convertiría el 0% en el default — el producto de promoción saldría
        30% más caro sin que nadie lo pidiera.

        El campo del producto es Float y no distingue "sin margen" de "0%": su 0 significa
        "usa el default", y quien llama lo traduce a None."""
        c = float(costo or 0)
        m = self._l10n_pe_ne_margen_default() if margen is None else float(margen)
        return round(c * (1 + m / 100.0), 2)

    @api.model
    def _l10n_pe_ne_rastreo_producto(self, rastreo):
        """Rastreo en Odoo: 'lot' | 'serial' | 'none'. La API habla el vocabulario del
        negocio ("lote"/"serie"), no el de Odoo.

        Solo tiene sentido con existencias: Odoo no rastrea lo que no cuenta. Y el rastreo
        NO se decide solo — es del producto: la misma caja de paracetamol necesita lote y un
        tornillo no."""
        r = (rastreo or "").strip().lower()
        if r in ("lote", "lot"):
            return "lot"
        if r in ("serie", "serial", "imei"):
            return "serial"
        return "none"

    @api.model
    def _l10n_pe_ne_tipo_producto(self, tipo=None, unidad=None):
        """Tipo del producto en Odoo: 'consu' (bien) o 'service' (servicio).

        Manda lo que el usuario eligió (`tipo`: "bien"/"servicio"). Si no eligió —el producto
        se auto-crea al emitir, donde no hay quién responda— se deduce de la UNIDAD, que es la
        única señal real que tiene la línea: ZZ es la unidad de servicio del catálogo 03 de
        SUNAT; cualquier otra (NIU, KGM, …) describe algo tangible.

        Antes esto era "service" fijo, lo que además se contradecía con SUNAT: sin unidad se
        emite NIU (DEFAULT_UNIT_CODE), o sea que al mismo producto se le declaraba BIEN a SUNAT
        y servicio en Odoo. Ahora ambos dicen lo mismo, y el default coincide con el de Odoo.

        No se toca `is_storable` (llevar stock o no): ese campo solo existe con el módulo
        `stock` instalado, y hoy no lo está. Es una decisión aparte, por producto.
        """
        t = (tipo or "").strip().lower()
        if t in ("bien", "bienes", "producto", "consu"):
            return "consu"
        if t in ("servicio", "servicios", "service"):
            return "service"
        return "service" if (unidad or "").strip().upper() == "ZZ" else "consu"
    def _l10n_pe_ne_lineas_con_stock(self):
        """Líneas del comprobante cuyo producto lleva existencias. En Odoo 19 eso es
        type='consu' Y is_storable=True: 'consu' solo dice que es tangible; el booleano decide
        si se rastrea. Un servicio nunca mueve stock."""
        self.ensure_one()
        return self.invoice_line_ids.filtered(
            lambda l: l.product_id
            and l.product_id.type == "consu"
            and l.product_id.is_storable
            and (l.quantity or 0) > 0
        )

    def _l10n_pe_ne_mover_stock(self, reversa=False):
        """Descuenta (o repone) el stock de las líneas de bien del comprobante.

        `reversa=True` invierte la dirección y marca los movimientos como reversa: lo usa
        _l10n_pe_ne_revertir_stock cuando SUNAT rechaza.

        La factura NO mueve stock en Odoo: los movimientos nacen de un stock.picking, que en
        el flujo estándar viene de un sale.order. Esta app no usa sale.order —emite el
        account.move directo— así que el movimiento se crea aquí, igual que hace el POS de
        Odoo (pos_order._create_order_picking() corre aparte de _generate_pos_order_invoice()).

        Dirección según el documento:
          * 01/03 (factura/boleta) → SALIDA: existencias → cliente.
          * 07 (nota de crédito)   → ENTRADA: cliente → existencias. Sin esto, anular una
            venta dejaría el stock descontado para siempre y el kardex se iría en falso.
          * 08 (nota de débito)    → nada: es un cargo (mora, penalidad), no mueve bienes.

        NUNCA bloquea la venta: si no hay existencias el movimiento igual se hace y el stock
        queda negativo — coherente con la caja, que tampoco bloquea. Un negativo es una señal
        visible de que falta un ajuste, y es preferible a impedirle vender a quien tiene el
        producto en la mano. Los fallos se tragan a propósito: el comprobante ya es válido
        ante SUNAT y no puede caerse porque el inventario no cuadre.
        """
        self.ensure_one()
        # Solo documentos de VENTA. _l10n_pe_document_type() no distingue: para un `in_invoice`
        # (una compra) devuelve '03', así que sin esta guarda una compra entraría por acá y
        # SACARÍA el stock en vez de meterlo. La compra va por _l10n_pe_ne_mover_stock_compra.
        if self.move_type not in ("out_invoice", "out_refund"):
            return self.env["stock.move"].browse()
        tipo = self.l10n_pe_ne_tipo_doc or self._l10n_pe_document_type()
        if tipo not in ("01", "03", "07"):
            return self.env["stock.move"].browse()
        lineas = self._l10n_pe_ne_lineas_con_stock()
        if not lineas:
            return self.env["stock.move"].browse()
        wh = self.env["stock.warehouse"].search(
            [("company_id", "=", self.company_id.id)], limit=1
        )
        clientes = self.env.ref(
            "stock.stock_location_customers", raise_if_not_found=False
        )
        if not wh or not clientes:
            _logger.warning(
                "stock: sin almacén o ubicación de clientes para %s; no se mueve stock",
                self.name,
            )
            return self.env["stock.move"].browse()
        # La NC va al revés que la factura; y una reversa va al revés de lo que sea.
        # Los dos XOR: la reversa de una NC vuelve a ser una salida.
        entrada = (tipo == "07") != bool(reversa)
        origen, destino = (
            (clientes, wh.lot_stock_id) if entrada else (wh.lot_stock_id, clientes)
        )
        return self._l10n_pe_ne_stock_aplicar(lineas, origen, destino, reversa=reversa)

    def _l10n_pe_ne_lote_de(self, linea):
        """stock.lot de una línea de compra, creándolo si hace falta. None si el producto no
        se rastrea o la línea no trae lote.

        Solo la ENTRADA define el lote. En la salida no se pide: Odoo lo asigna al reservar,
        por su estrategia de salida — con vencimiento, lo que caduca antes sale primero, que
        es justo lo que una farmacia necesita. Verificado contra Odoo 19."""
        prod = linea.product_id
        if not prod or prod.tracking == "none":
            return None
        nombre = (linea.l10n_pe_ne_lote or "").strip()
        if not nombre:
            return None
        Lot = self.env["stock.lot"]
        lote = Lot.search(
            [("name", "=", nombre), ("product_id", "=", prod.id),
             ("company_id", "=", self.company_id.id)], limit=1)
        if not lote:
            vals = {"name": nombre, "product_id": prod.id, "company_id": self.company_id.id}
            lote = Lot.create(vals)
        # El vencimiento se escribe aparte: el campo lo agrega product_expiry y solo tiene
        # sentido si el producto lo usa. Se pisa solo si la línea trae uno.
        if linea.l10n_pe_ne_vence and prod.use_expiration_date:
            lote.expiration_date = linea.l10n_pe_ne_vence
        return lote

    def _l10n_pe_ne_stock_qty(self, line):
        """Cantidad a mover en la UoM del producto (el empaque). Normal = |cantidad|. Fraccionada
        = |cantidad| / unidades_por_empaque: la línea va en sub-unidades pero el stock se lleva en
        empaques, así vender 5 unidades de una caja de 30 descuenta 5/30 de caja."""
        qty = abs(line.quantity or 0.0)
        if line.l10n_pe_ne_fraccionado:
            factor = line.product_id.l10n_pe_ne_unidades_por_empaque or 0.0
            if factor > 0:
                return qty / factor
        return qty

    @api.model
    def _l10n_pe_ne_stock_crear_moves(self, items, origen, destino, company, *,
                                      reversa=False, origin="", fecha=False,
                                      link_field=None, link_id=False):
        """Motor común de creación+validación de stock.move entre dos ubicaciones. STATELESS: todo
        entra por args (no lee campos de `self`), así lo comparten el comprobante, la nota de venta
        y el ajuste de inventario. `items` = lista de dicts
        {"product": product.product, "qty": float, "uom": uom.uom, "lote": stock.lot|None}.

        Devuelve (moves, error|None). NUNCA levanta: el documento de origen ya existe y no puede
        caerse porque el inventario no cuadre; el llamador decide qué hacer con el error (aviso).
        """
        Move = self.env["stock.move"]
        moves = Move.browse()
        for it in items:
            # Sin 'name': stock.move no lo tiene en Odoo 19 (su `reference` se computa). `origin`
            # deja el rastro legible; link_field (l10n_pe_ne_move_id / _nota_venta_id) es el enlace
            # real (por id).
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
                    # Entrada de un producto rastreado: el lote va en la LÍNEA del movimiento
                    # (stock.move_line), no en el move. Sin esto Odoo lanza "debe proporcionar
                    # un número de serie o lote" y la mercadería no entraría.
                    if not m.move_line_ids:
                        m.move_line_ids = [(0, 0, {
                            "product_id": m.product_id.id,
                            "location_id": m.location_id.id,
                            "location_dest_id": m.location_dest_id.id,
                            "company_id": m.company_id.id,
                        })]
                    m.move_line_ids.write({"lot_id": lote.id})
                # quantity explícito: sin esto _action_done mueve solo lo reservado, y sin
                # existencias no reserva nada → la salida quedaría en 0 y el kardex mentiría.
                m.quantity = m.product_uom_qty
                m.picked = True
            moves._action_done()
            # La fecha del movimiento es la del DOCUMENTO, no la de cuando se registró (Odoo pone
            # `date`=ahora al validar; sin corregirlo, una compra de marzo cargada en julio caería
            # en el kardex de julio). Se escribe después de _action_done porque antes lo pisa él.
            if fecha:
                moves.write({"date": fecha})
                moves.move_line_ids.write({"date": fecha})
        except Exception as e:  # noqa: BLE001 — el documento ya existe: el stock no lo tumba
            _logger.exception("stock: no se pudo mover el stock (%s): %s", origin, e)
            return Move.browse(), str(e)
        return moves, None

    def _l10n_pe_ne_stock_aplicar(self, lineas, origen, destino, reversa=False, con_lote=False):
        """Prepara los items de `lineas` (account.move.line) y delega en el motor común.

        Lo comparten la venta (existencias → cliente), la devolución por NC, la reversa de un
        rechazo y la compra (proveedor → existencias). Lo único que cambia entre ellas son las
        dos ubicaciones y el sentido; la mecánica —y el "nunca bloquear"— es la misma.

        `con_lote`: la ENTRADA asigna el lote que trae la línea. La salida no lo necesita —
        Odoo lo asigna al reservar.
        """
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
        # Se deja RASTRO en el documento, no solo en el log: un movimiento que no ocurre y nadie ve
        # es un kardex mintiendo en silencio (caso típico: producto rastreado sin existencias).
        self.l10n_pe_ne_stock_aviso = (
            (_("No se pudo mover el inventario de este documento: %s") % err)[:500] if err else False
        )
        return moves

    @api.model
    def _l10n_pe_ne_ajustar_stock(self, product_id, modo, cantidad, motivo=""):
        """Ajuste de inventario por producto contra la ubicación de ajuste (usage='inventory').
        `modo`: 'fijar' (conteo físico / carga inicial → deja el stock EN `cantidad`, calculando el
        delta contra qty_available), 'sumar' (corrección/devolución interna) o 'restar' (merma/robo).
        Reusa el motor _l10n_pe_ne_stock_crear_moves. Nunca levanta.
        Devuelve {"stock": float, "aviso": str|False}."""
        prod = self.env["product.product"].browse(int(product_id)).exists()
        if not prod or not prod.is_storable:
            return {"stock": prod.qty_available if prod else 0.0,
                    "aviso": _("El producto no lleva inventario.")}
        company = self.env.company
        wh = self.env["stock.warehouse"].search([("company_id", "=", company.id)], limit=1)
        ajuste = self.env["stock.location"].search(
            [("usage", "=", "inventory"), ("company_id", "in", [company.id, False])],
            order="company_id desc", limit=1)
        if not wh or not ajuste:
            return {"stock": prod.qty_available,
                    "aviso": _("Sin almacén o ubicación de ajuste de inventario.")}
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
        # delta > 0 = ENTRADA (ajuste → existencias); delta < 0 = SALIDA (existencias → ajuste).
        entrada = delta > 0
        origen, destino = (ajuste, wh.lot_stock_id) if entrada else (wh.lot_stock_id, ajuste)
        items = [{"product": prod, "qty": abs(delta), "uom": prod.uom_id, "lote": None}]
        _moves, err = self._l10n_pe_ne_stock_crear_moves(
            items, origen, destino, company, origin=(_("Ajuste: %s") % (motivo or "-"))[:60])
        return {"stock": prod.qty_available, "aviso": (err[:300] if err else False)}

    @api.model
    def _l10n_pe_ne_kardex(self, product_id, desde=None, hasta=None):
        """Movimientos de stock 'done' de un producto con SALDO acumulado corriente. Cantidad-first
        (el PLE 12.1 ya valoriza). El documento se deriva del enlace: comprobante / nota / ajuste.
        Devuelve {"stock": float, "movimientos": [{fecha, documento, tipo, entrada, salida, saldo}]}."""
        prod = self.env["product.product"].browse(int(product_id)).exists()
        if not prod:
            return {"stock": 0.0, "movimientos": []}
        dom = [("product_id", "=", prod.id), ("state", "=", "done"),
               ("company_id", "=", self.env.company.id)]
        if desde:
            dom.append(("date", ">=", desde))
        if hasta:
            dom.append(("date", "<=", str(hasta) + " 23:59:59"))
        lineas = self.env["stock.move.line"].search(dom, order="date asc, id asc")
        internas = self.env["stock.location"].search(
            [("usage", "=", "internal"), ("company_id", "in", [self.env.company.id, False])])
        movimientos, saldo = [], 0.0
        for ml in lineas:
            entra = ml.location_dest_id in internas and ml.location_id not in internas
            sale = ml.location_id in internas and ml.location_dest_id not in internas
            if not entra and not sale:
                continue  # movimiento interno (existencias→existencias): no altera el saldo total
            qty = ml.quantity
            entrada = qty if entra else 0.0
            salida = qty if sale else 0.0
            saldo += entrada - salida
            mv = ml.move_id
            if mv.l10n_pe_ne_move_id:
                doc = mv.l10n_pe_ne_move_id.name
            elif mv.l10n_pe_ne_nota_venta_id:
                doc = mv.l10n_pe_ne_nota_venta_id.name
            else:
                doc = mv.origin
            movimientos.append({
                "fecha": str(ml.date)[:10] if ml.date else "",
                "documento": doc or "—",
                "tipo": "entrada" if entrada else "salida",
                "entrada": round(entrada, 3), "salida": round(salida, 3),
                "saldo": round(saldo, 3),
            })
        return {"stock": prod.qty_available, "movimientos": movimientos}

    @api.model
    def _l10n_pe_ne_asegurar_fefo(self, wh):
        """Pone la ubicación de existencias en FEFO: sale primero lo que vence antes.

        El default de Odoo es FIFO —sale lo que entró primero—, y para lo que caduca eso está
        MAL: comprobado con dos lotes (uno vence 2026, otro 2028), la venta se llevó el de
        2028 y dejó el de 2026 pudriéndose en el almacén. En una farmacia eso es plata tirada
        y riesgo sanitario.

        FEFO no perjudica a lo que no vence: sin fecha de caducidad, Odoo cae de vuelta al
        orden de entrada. Por eso se pone en la ubicación y no producto por producto.

        Idempotente: si ya está, no toca nada. Se llama al ingresar mercadería porque es
        cuando la ubicación empieza a importar — no hay un lugar mejor sin un asistente de
        configuración, que esta app no tiene."""
        fefo = self.env.ref("product_expiry.removal_fefo", raise_if_not_found=False)
        loc = wh.lot_stock_id if wh else None
        if fefo and loc and not loc.removal_strategy_id:
            loc.sudo().removal_strategy_id = fefo.id

    def _l10n_pe_ne_mover_stock_compra(self):
        """Entrada de mercadería por una compra: proveedor → existencias.

        Es la otra mitad del kardex. Sin esto solo hay salidas y todo negocio deriva a
        negativo: un inventario permanente es entradas MENOS salidas.

        Va aparte de _l10n_pe_ne_mover_stock y no reusa su dirección a propósito: aquella
        deduce el sentido de _l10n_pe_document_type(), que para un `in_invoice` devuelve '03'
        — o sea que trataría la compra como una boleta y SACARÍA el stock en vez de meterlo.
        """
        self.ensure_one()
        if self.move_type != "in_invoice":
            return self.env["stock.move"].browse()
        lineas = self._l10n_pe_ne_lineas_con_stock()
        if not lineas:
            return self.env["stock.move"].browse()
        wh = self.env["stock.warehouse"].search(
            [("company_id", "=", self.company_id.id)], limit=1
        )
        proveedores = self.env.ref(
            "stock.stock_location_suppliers", raise_if_not_found=False
        )
        if not wh or not proveedores:
            _logger.warning(
                "stock: sin almacén o ubicación de proveedores para %s; no entra mercadería",
                self.name,
            )
            return self.env["stock.move"].browse()
        # La mercadería que entra decide cómo saldrá: FEFO para que lo que vence antes se
        # venda primero (el default de Odoo, FIFO, dejaría caducar el lote más viejo).
        self._l10n_pe_ne_asegurar_fefo(wh)
        # con_lote: la entrada es la única que define el lote (la salida lo asigna Odoo).
        return self._l10n_pe_ne_stock_aplicar(
            lineas, proveedores, wh.lot_stock_id, con_lote=True
        )

    def _l10n_pe_ne_revertir_stock(self):
        """Deshace el movimiento de un comprobante que SUNAT rechazó.

        Un rechazado NO existe para SUNAT: hay que corregir y emitir uno NUEVO. Ese nuevo
        comprobante vuelve a descontar, así que si el rechazado se queda con su movimiento,
        el bien sale DOS VECES del kardex por una sola venta.

        Se REVIERTE, no se borra: el kardex es un libro: se compensa con el movimiento
        contrario y queda el rastro de que hubo un intento. Borrar el original escondería que
        pasó algo, que es justo lo que un inventario permanente no debe hacer.

        Idempotente: si ya se revirtió, no hace nada. Lo llama el write() al detectar la
        transición a 'rechazado' — por ahí pasan los tres caminos que la fijan (el envío
        síncrono, el cron de pendientes y el resumen diario de boletas), y también cualquiera
        que se agregue después.
        """
        self.ensure_one()
        Move = self.env["stock.move"]
        hechos = Move.search(
            [
                ("l10n_pe_ne_move_id", "=", self.id),
                ("l10n_pe_ne_reversa", "=", False),
                ("state", "=", "done"),
            ]
        )
        if not hechos:
            return Move.browse()
        ya = Move.search_count(
            [("l10n_pe_ne_move_id", "=", self.id), ("l10n_pe_ne_reversa", "=", True)]
        )
        if ya:
            return Move.browse()
        return self._l10n_pe_ne_mover_stock(reversa=True)

    @staticmethod
    def _l10n_pe_ne_normaliza_medios(medios):
        """C1: canoniza el NOMBRE de cada medio de pago antes de persistirlo.

        El medio es texto libre y llega de cuatro orígenes (POS, Emitir, cobro de cotización,
        adelanto/abono de órdenes). Sin esto, 'Efectivo' y 'efectivo' eran DOS filas del arqueo:
        el cajero cuenta un solo cajón y el sistema le pedía contarlo dos veces, así que una de
        las dos filas cerraba con diferencia sin que faltara un sol. Se normaliza AL ESCRIBIR
        (aquí) y además se agrupa AL LEER (tools.caja_arqueo), que es lo que consolida la
        historia ya escrita sin migrar datos. El monto no se toca."""
        if not isinstance(medios, (list, tuple)):
            return medios
        out = []
        for mp in medios:
            if isinstance(mp, dict):
                mp = dict(mp)
                mp["medio"] = normalizar_medio(mp.get("medio"))
            out.append(mp)
        return out

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get("l10n_pe_ne_medios_pago"):
                vals["l10n_pe_ne_medios_pago"] = self._l10n_pe_ne_normaliza_medios(
                    vals["l10n_pe_ne_medios_pago"])
        return super().create(vals_list)

    def write(self, vals):
        """Revierte el stock al pasar a 'rechazado'.

        Va en el write y no en cada sitio que fija el estado porque son tres (envío síncrono,
        cron de pendientes, resumen diario de boletas) y mañana pueden ser cuatro: la
        invariante no debe depender de que alguien se acuerde de llamar al helper.

        Solo los que ENTRAN a rechazado (los que ya lo estaban no se re-revierten).

        Y congela el establecimiento emisor una vez asignado el número fiscal: el codLocalEmisor
        viaja dentro del XML firmado, así que cambiarlo después dejaría al comprobante diciendo
        que salió de un local distinto del que SUNAT recibió. El contexto l10n_pe_ne_bypass_lock
        lo deja pasar para migraciones/mantenimiento, igual que en la caja.
        """
        # C1: mismo choke point que en create — TODO origen que escriba medios pasa por aquí
        # (la SPA por quick_flags, y los roles con `move.sudo().l10n_pe_ne_medios_pago = [...]`).
        if vals.get("l10n_pe_ne_medios_pago"):
            vals = dict(vals)
            vals["l10n_pe_ne_medios_pago"] = self._l10n_pe_ne_normaliza_medios(
                vals["l10n_pe_ne_medios_pago"])
        if "l10n_pe_ne_cod_establecimiento" in vals and not self.env.context.get(
            "l10n_pe_ne_bypass_lock"
        ):
            nuevo = vals.get("l10n_pe_ne_cod_establecimiento") or "0000"
            for m in self:
                if m.l10n_pe_ne_corr_emit and (
                    m.l10n_pe_ne_cod_establecimiento or "0000"
                ) != nuevo:
                    raise UserError(
                        _(
                            "El comprobante %(doc)s ya tiene número fiscal asignado: su "
                            "establecimiento emisor (%(actual)s) es parte del documento y no se "
                            "puede cambiar. Si el local está mal, corrígelo con una nota."
                        )
                        % {
                            "doc": "%s-%s" % m._l10n_pe_ne_doc_id(),
                            "actual": m.l10n_pe_ne_cod_establecimiento or "0000",
                        }
                    )
        revertir = self.browse()
        if vals.get("l10n_pe_biller_state") == "rechazado":
            revertir = self.filtered(
                lambda m: m.l10n_pe_biller_state != "rechazado"
            )
        res = super().write(vals)
        for m in revertir:
            m._l10n_pe_ne_revertir_stock()
        return res

    def _l10n_pe_ne_quick_product(self, ln, tax=None, create=True, precio_con_igv=True):
        """Resuelve el product.product de una línea para que el documento USE un registro de Odoo:
        busca por id, por código (default_code) o por nombre exacto; si no existe y hay datos, lo
        CREA simplificado y lo enlaza (igual que el cliente por vat). Devuelve recordset vacío si la
        línea no aporta nada por lo que crear (queda como texto libre, compatible hacia atrás).
        Con create=False solo resuelve y NUNCA crea: las líneas de notas (07/08) pueden traer
        texto sintético (p. ej. "DICE: … DEBE DECIR: …" del motivo 03) que no debe convertirse
        en producto del catálogo.
        `precio_con_igv`: la convención del catálogo es list_price CON IGV (Productos, POS e
        import lo tratan como precio de vitrina). El payload de EMISIÓN trae el valor unitario
        SIN IGV (ni ISC): quick_emit pasa False y al crear se repone el impuesto — sin esto el
        producto auto-creado quedaba ~15% más barato al revenderlo desde el catálogo."""
        Product = self.env["product.product"]
        # `conceptoLibre`: el usuario dijo que esto NO es un producto, sino el detalle de un
        # servicio, distinto en cada comprobante ("POR EL SERVICIO DE TRANSPORTE LIMA-JULIACA …
        # DAM NRO. …"). No hay nada que resolver ni que crear, y se respeta al pie de la letra:
        # engancharlo a uno del catálogo que se llame igual movería su stock, que es justo lo
        # que el usuario dijo que no era. Uno por factura, además, volvería basura el catálogo.
        if ln.get("conceptoLibre"):
            return Product.browse()
        pid = ln.get("productId")
        if pid:
            prod = Product.browse(int(pid)).exists()
            if prod:
                return prod
        cod = (ln.get("productCod") or ln.get("codProducto") or "").strip()
        if cod:
            found = Product.search([("default_code", "=", cod)], limit=1)
            if found:
                return found
        desc = (ln.get("descripcion") or "").strip()
        if desc:
            found = Product.search([("name", "=", desc)], limit=1)
            if found:
                return found
        if not (cod or desc) or not create:
            return Product.browse()
        precio = float(ln.get("precioUnitario") or 0)
        if not precio_con_igv and tax and (tax.amount or 0) > 0:
            # Valor SIN IGV (ni ISC) del payload de emisión → precio de vitrina CON IGV.
            isc = float(ln.get("isc") or 0)
            precio = round(precio * (1 + isc / 100.0) * (1 + (tax.amount or 0) / 100.0), 4)
        uni = (ln.get("unidad") or "").strip()
        vals = {
            "name": desc or cod or "PRODUCTO",
            "type": self._l10n_pe_ne_tipo_producto(ln.get("tipo"), uni),
            "sale_ok": True,
            "list_price": precio,
            # is_storable va en False por defecto en Odoo: sin decirlo explícito, el producto
            # NO llevaría existencias y nunca movería stock. El auto-creado al emitir se queda
            # sin stock a propósito (nadie eligió); el catálogo lo manda por llevaStock.
            "is_storable": bool(ln.get("llevaStock")),
            "tracking": self._l10n_pe_ne_rastreo_producto(ln.get("rastreo")),
            # use_expiration_date lo agrega product_expiry; solo tiene sentido con rastreo.
            "use_expiration_date": bool(ln.get("vence")),
            "l10n_pe_ne_margen": float(ln.get("margen") or 0),
            # company_id del emisor: aísla el producto por RUC (igual que el cliente).
            "company_id": self.env.company.id,
        }
        # Costo: solo lo trae quien lo conoce (crear desde una línea de compra sabe cuánto se
        # pagó). Al emitir no viene, y ahí no se toca: el costo de venta no es el de compra.
        costo = float(ln.get("costo") or 0)
        if costo > 0:
            vals["standard_price"] = costo
        # Stock mínimo (umbral de reposición): solo lo trae el alta desde el catálogo.
        sm = float(ln.get("stockMinimo") or 0)
        if sm > 0:
            vals["l10n_pe_ne_stock_minimo"] = sm
        if cod:
            vals["default_code"] = cod
        bc = (ln.get("barcode") or "").strip()
        if bc:
            vals["barcode"] = bc
        cs = (ln.get("codSunat") or "").strip()
        if cs:
            vals["l10n_pe_ne_cod_producto_sunat"] = cs
        if ln.get("detraCod"):
            vals["l10n_pe_ne_detraccion_cod"] = str(ln["detraCod"]).strip()
        if ln.get("percepTasa"):
            vals["l10n_pe_ne_percepcion_tasa"] = _percep_float(ln["percepTasa"])
        if uni:
            vals["l10n_pe_ne_unit_code"] = uni
        # Categoría (product.category propia del negocio): la hoja elegida (subcategoría si la
        # hay, si no la categoría). Solo desde el catálogo; al auto-crear emitiendo no viene.
        cid = int(ln.get("categId") or 0)
        if cid:
            vals["categ_id"] = cid
        if tax:
            vals["taxes_id"] = [(6, 0, tax.ids)]
        return Product.create(vals)

    @api.model
    def l10n_pe_ne_crear_categoria(self, body):
        """Crea un departamento/subcategoría bajo la raíz 'Supermercado' (creable al vuelo desde
        el form). Comparte el árbol con el filtro del catálogo."""
        body = body or {}
        return self.env["product.category"]._l10n_pe_ne_crear_bajo_super(
            body.get("nombre"), body.get("parentId")
        )

    def _l10n_pe_ne_product_dict(self, p):
        sale_taxes = p.taxes_id.filtered(lambda t: t.type_tax_use == "sale")
        # El ICBPER (7152) NO es la afectación: es un tributo aparte (bolsa plástica). Se
        # deriva como flag propio y se excluye al elegir la afectación (IGV) del producto.
        icbper = bool(sale_taxes.filtered(lambda t: t.l10n_pe_edi_tax_code == "7152"))
        tax = sale_taxes.filtered(lambda t: t.l10n_pe_edi_tax_code != "7152")[:1]
        return {
            "id": p.id,
            "descripcion": p.name or "",
            "codigo": p.default_code or "",
            "barcode": p.barcode or "",
            "categId": p.categ_id.id or None,
            "categoria": p.categ_id.complete_name or "",
            "codSunat": p.l10n_pe_ne_cod_producto_sunat or "",
            "detraCod": p.l10n_pe_ne_detraccion_cod or "",
            "percepTasa": p.l10n_pe_ne_percepcion_tasa or 0.0,
            "precio": p.list_price,
            "taxCode": (tax.l10n_pe_edi_tax_code or "1000") if tax else "1000",
            "unidad": p.l10n_pe_ne_unit_code or "",
            "icbper": icbper,
            # Fraccionamiento (farma): el producto se puede vender por sub-unidad. El front muestra
            # el toggle "fraccionar" solo cuando `fraccionable`; `unidadFraccion` es la sub-unidad SUNAT.
            "fraccionable": (p.l10n_pe_ne_unidades_por_empaque or 0.0) > 0,
            "unidadesPorEmpaque": p.l10n_pe_ne_unidades_por_empaque or 0.0,
            "unidadFraccion": p.l10n_pe_ne_unidad_fraccion or "",
            "registroSanitario": p.l10n_pe_ne_registro_sanitario or "",
            "controlado": bool(p.l10n_pe_ne_controlado),
            # "bien" | "servicio" — el vocabulario del negocio, no el de Odoo (consu/service).
            # 'combo' no lo usa esta app; si apareciera, se trata como bien (es tangible).
            "tipo": "servicio" if p.type == "service" else "bien",
            # ¿Se le llevan existencias? (Odoo: is_storable). Va en False por defecto, así que
            # SIN esto ningún producto movería stock nunca: es lo que activa _l10n_pe_ne_mover_stock.
            "llevaStock": bool(p.is_storable),
            # Existencias actuales. Solo tiene sentido si llevaStock; si no, va en 0 y la UI
            # muestra un guion (no es "cero unidades", es "no aplica").
            "stock": p.qty_available if p.is_storable else 0.0,
            # Costo (con IGV) y margen: lo que hace falta para proponer el precio de venta
            # cuando una compra trae un costo distinto.
            "costo": p.standard_price or 0.0,
            "margen": p.l10n_pe_ne_margen or 0.0,
            # Stock mínimo (umbral de reposición). 0 = sin alerta. La lista lo usa para marcar
            # "bajo el mínimo" cuando 0 < stock <= stockMinimo.
            "stockMinimo": p.l10n_pe_ne_stock_minimo or 0.0,
            # Rastreo por lote o serie (Odoo: tracking). "lote" agrupa unidades (farmacia,
            # alimentos); "serie" es un número por unidad (celulares, equipos).
            "rastreo": {"lot": "lote", "serial": "serie"}.get(p.tracking, "ninguno"),
            # ¿Los lotes llevan vencimiento? Solo aplica con rastreo por lote/serie.
            "vence": bool(p.use_expiration_date),
        }


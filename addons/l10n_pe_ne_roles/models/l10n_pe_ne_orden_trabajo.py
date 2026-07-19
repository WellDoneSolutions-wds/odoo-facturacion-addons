# -*- coding: utf-8 -*-
"""CN-02 — la ORDEN DE TRABAJO como modelo de FLUJO (cotiza+adelanta → cola → taller la toma →
termina → el cliente vuelve, paga el saldo y recoge).

Segundo proceso sobre l10n_pe_ne.flujo.mixin, y el primero que estrena la COLA con TOMA ATÓMICA:
la orden nace SIN dueño (user_id NULL = en cola); un usuario con rol taller la TOMA al avanzar
(NULL→yo, atómico) — a diferencia de la cotización de CN-01, que nace con dueño. Con 1 usuario
que lleva todos los sombreros la cola colapsa a una bandeja única; con N usuarios cada rol ve solo
su tramo. Jamás se compara identidad de usuarios (escala libre): solo has_group.

Decisión del usuario (2026-07-18, P4): el ADELANTO se trata como RECIBO INTERNO / a cuenta (Vía B).
No se emite comprobante de anticipo (0104): el adelanto se registra como un movimiento de caja
estructurado ('adelanto', con su medio) que cuadra el arqueo por su medio; al recoger se emite UN
comprobante por el TOTAL cuyos 'medios' registran solo el SALDO (lo que entra físicamente en esa
sesión) — así el adelanto no se cuenta dos veces entre sesiones de caja distintas. El enganche para
la Vía A (0104) queda previsto (factura_final_id + un futuro adelanto_move_id) sin construirse aún.

REGLA DURA: 'entregada' lo escribe SOLO l10n_pe_ne_cobrar_saldo tras emitir el comprobante final;
ninguna arista de _transiciones la escribe (sería una entrega sin cobro). 'entregada'/'anulada' son
terminales.
"""
from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError
# Misma fuente que el desglose del biller: si el IGV cambia, la conversión de la emisión no driftea.
from odoo.addons.l10n_pe_ne_biller.models.l10n_pe_ne_cotizacion import IGV_RATE

_G = "l10n_pe_ne_roles."
_G_CAJA = _G + "group_l10n_pe_ne_caja"
_G_TALLER = _G + "group_l10n_pe_ne_taller"
_G_SUPERVISOR = _G + "group_l10n_pe_ne_supervisor"


class L10nPeNeOrdenTrabajo(models.Model):
    _name = "l10n_pe_ne.orden.trabajo"
    _inherit = ["l10n_pe_ne.flujo.mixin"]
    _description = "Orden de trabajo (NE Express · CN-02 taller)"
    _order = "id desc"

    # El mixin NO declara estado (cada modelo trae el suyo). Lo declara aquí con tracking.
    estado = fields.Selection(
        [("borrador", "Borrador"), ("encolada", "En cola"), ("en_proceso", "En proceso"),
         ("terminada", "Terminada"), ("entregada", "Entregada"), ("anulada", "Anulada")],
        string="Estado", default="borrador", required=True, index=True, tracking=True)
    # user_id lo aporta el mixin SIN default (NULL = en cola): la orden nace sin dueño y el taller
    # la toma. (La cotización de CN-01 sí le pone default; aquí NO, a propósito.)

    name = fields.Char(string="Referencia", compute="_compute_name")
    partner_id = fields.Many2one("res.partner", string="Cliente", required=True, index=True)
    cotizacion_id = fields.Many2one(
        "l10n_pe_ne.cotizacion", string="Cotización de origen", index=True,
        help="Trazabilidad opcional: la cotización de la que nació la orden. La orden es "
             "autónoma (congela sus propias líneas), no depende del ciclo de la cotización.")
    linea_ids = fields.One2many("l10n_pe_ne.orden.trabajo.linea", "orden_id", string="Detalle")
    fecha_pactada = fields.Date(string="Fecha de recojo pactada", tracking=True)

    # Cobro en dos tiempos (Vía B). El adelanto y el saldo son consolidados de la ORDEN (viven aquí,
    # no en la sesión de caja: caen en sesiones distintas que se congelan por separado).
    adelanto_monto = fields.Monetary(string="Adelanto a cuenta", currency_field="currency_id",
                                     readonly=True, tracking=True)
    medio_adelanto = fields.Char(string="Medio del adelanto", readonly=True)
    adelanto_movimiento_id = fields.Many2one(
        "l10n_pe_ne.caja.movimiento", string="Movimiento del adelanto", readonly=True,
        ondelete="set null")
    # A16: restrict — el comprobante de una entrega no se borra por debajo (un set null silencioso
    # dejaría una 'entregada' sin factura a nivel SQL, esquivando la constraint).
    factura_final_id = fields.Many2one("account.move", string="Comprobante final", readonly=True,
                                       ondelete="restrict")
    fecha_entrega = fields.Datetime(string="Fecha de entrega", readonly=True)

    amount_untaxed = fields.Monetary(string="Valor venta", compute="_compute_amounts", store=True,
                                     currency_field="currency_id")
    amount_tax = fields.Monetary(string="IGV", compute="_compute_amounts", store=True,
                                 currency_field="currency_id")
    amount_total = fields.Monetary(string="Total", compute="_compute_amounts", store=True,
                                   currency_field="currency_id")
    saldo = fields.Monetary(string="Saldo por cobrar", compute="_compute_amounts", store=True,
                            currency_field="currency_id")

    currency_id = fields.Many2one("res.currency", required=True,
                                  default=lambda s: s.env.company.currency_id)
    company_id = fields.Many2one("res.company", required=True, index=True,
                                 default=lambda s: s.env.company)

    # ───────────────────────────────────────────── computes
    def _compute_name(self):
        for o in self:
            o.name = ("OT-%05d" % o.id) if o.id else _("Nueva orden")

    @api.depends("linea_ids.subtotal", "linea_ids.afecto_igv", "adelanto_monto")
    def _compute_amounts(self):
        for o in self:
            # El precio de línea es CON IGV (misma convención que la cotización): el subtotal ya es
            # el bruto que paga el cliente. El desglose descompone el gravado en base+IGV.
            bruto_gravado = sum(l.subtotal for l in o.linea_ids if l.afecto_igv)
            no_gravado = sum(l.subtotal for l in o.linea_ids if not l.afecto_igv)
            base_gravado = round(bruto_gravado / (1 + IGV_RATE), 2)
            o.amount_total = round(bruto_gravado + no_gravado, 2)
            o.amount_tax = round(bruto_gravado - base_gravado, 2)
            o.amount_untaxed = round(o.amount_total - o.amount_tax, 2)
            o.saldo = round(o.amount_total - (o.adelanto_monto or 0.0), 2)

    @api.constrains("estado", "factura_final_id")
    def _check_entregada_con_comprobante(self):
        # Regla dura a nivel de MODELO (no solo del método): una orden 'entregada' DEBE tener su
        # comprobante final. Cierra el hueco de un write RPC directo que salte cobrar_saldo y marque
        # 'entregada' sin emitir (entrega sin cobro). Dispara en CUALQUIER vía de escritura.
        for o in self:
            if o.estado == "entregada" and not o.factura_final_id:
                raise ValidationError(_(
                    "Una orden entregada debe tener su comprobante final: se entrega al cobrar."))

    # ───────────────────────────────────────────── flujo
    @api.model
    def _transiciones(self):
        # SIN arista →entregada (regla dura: la escribe el fold de cobro tras emitir). borrador→encolada
        # NO es arista: la hace registrar_adelanto (encolar sin adelanto no tiene sentido; una arista
        # suelta pintaría una acción "Encolar" que se lo saltaría). 'anulada' es rama de excepción
        # (motivo, NUNCA cadena). Cada estado no-terminal tiene ≥1 salida (invariante escala libre):
        # terminada sale por 'anulada' (cliente que no recoge) además del fold de cobro.
        return {
            **super()._transiciones(),
            ("encolada", "en_proceso"): {"grupo": _G_TALLER, "toma": True, "cadena": True,
                                         "label": "Tomar orden"},
            ("en_proceso", "terminada"): {"grupo": _G_TALLER, "cadena": True,
                                          "label": "Terminar trabajo"},
            ("borrador", "anulada"): {"grupo": _G_CAJA, "motivo": True, "label": "Anular"},
            ("encolada", "anulada"): {"grupo": _G_SUPERVISOR, "motivo": True, "label": "Anular"},
            # A17: cancelar con el trabajo EN CURSO es real (cliente desiste, vehículo retirado);
            # sin esta arista había que 'terminar' un trabajo no terminado (bitácora mentirosa).
            ("en_proceso", "anulada"): {"grupo": _G_SUPERVISOR, "motivo": True,
                                        "label": "Anular (trabajo en curso)"},
            ("terminada", "anulada"): {"grupo": _G_SUPERVISOR, "motivo": True,
                                       "label": "Anular (no recogido)"},
        }

    @api.model
    def _estados_terminales(self):
        return ("entregada", "anulada")

    @api.model
    def _campos_flujo(self):
        # A7: el blindaje cubre también el DINERO — el registro del cobro (adelanto y comprobante
        # final) lo escriben SOLO registrar_adelanto/cobrar_saldo (con _FLUJO_OK), nunca un write
        # RPC directo. Sin esto, un operativo podía reescribir adelanto_monto/factura_final_id de
        # una orden ya entregada, divergiendo el registro del comprobante emitido.
        return super()._campos_flujo() + (
            "adelanto_monto", "medio_adelanto", "adelanto_movimiento_id", "factura_final_id")

    # Transiciones con nombre (validan los 3 ejes vía _avanzar).
    def l10n_pe_ne_tomar(self):
        """El operario toma la orden de la cola (encolada→en_proceso). 'toma' asigna user_id
        atómicamente (NULL→yo): quien la toma se la queda."""
        self._avanzar("en_proceso")
        return self._l10n_pe_ne_orden_dict()

    def l10n_pe_ne_terminar(self):
        self._avanzar("terminada")
        return self._l10n_pe_ne_orden_dict()

    def l10n_pe_ne_anular(self, motivo=None):
        # Nota: anular una orden con adelanto pagado NO devuelve el dinero automáticamente; el
        # reembolso es un retiro de caja manual (v1). Por eso 'encolada'→anulada exige supervisor.
        self._avanzar("anulada", motivo=motivo)
        return self._l10n_pe_ne_orden_dict()

    # ───────────────────────────────────────────── adelanto (Vía B · recibo interno)
    def l10n_pe_ne_registrar_adelanto(self, monto, medio=None):
        """El cajero cobra el adelanto a cuenta y encola la orden (borrador→encolada). El dinero se
        registra como un movimiento de caja 'adelanto' (cuadra el arqueo por su medio); no se emite
        comprobante (Vía B). Eje 2: grupo caja."""
        self.ensure_one()
        if not self.env.user.has_group(_G_CAJA):
            raise AccessError(_("No tienes permiso para cobrar el adelanto."))
        # A2: serializa la fila — dos registros de adelanto concurrentes no se duplican.
        self._l10n_pe_ne_lock()
        if self.estado != "borrador":
            raise UserError(_("El adelanto solo se registra sobre una orden en borrador."))
        if self.adelanto_movimiento_id:
            raise UserError(_("Esta orden ya tiene un adelanto registrado."))
        monto = round(float(monto or 0.0), 2)
        if monto <= 0:
            raise UserError(_("El adelanto debe ser mayor a 0."))
        if monto >= self.amount_total:
            # A17: en Vía B el pago 100% adelantado NO tiene flujo (habría que emitir al recibirlo,
            # que es exactamente la Vía A / anticipo 0104, aún no construida). Mensaje honesto.
            raise UserError(_(
                "El adelanto (S/ %(a).2f) no puede cubrir o superar el total (S/ %(t).2f): es un "
                "pago PARCIAL a cuenta. Si el cliente paga todo por adelantado, emite el "
                "comprobante de una vez (el trabajo queda pendiente de entrega).",
                a=monto, t=self.amount_total))
        medio = (medio or "Efectivo").strip() or "Efectivo"
        # La caja (biller) crea el movimiento estructurado sobre la sesión abierta.
        mov = self.env["l10n_pe_ne.caja.sesion"]._l10n_pe_ne_registrar_adelanto(
            monto, medio, self.partner_id, _("Adelanto %s") % self.name)
        mov.orden_trabajo_id = self.id
        # Encolar (borrador→encolada) NO es una arista del mixin: se hace aquí, atado al adelanto.
        # Flag _FLUJO_OK: escritura de estado AUTORIZADA (no es un write RPC crudo).
        self.with_context(l10n_pe_ne_flujo_ok=True).write({
            "adelanto_monto": monto, "medio_adelanto": medio,
            "adelanto_movimiento_id": mov.id, "estado": "encolada"})
        self.message_post(body=_("Adelanto de S/ %(m).2f (%(me)s) cobrado por %(u)s. En cola.",
                                 m=monto, me=medio, u=self.env.user.name))
        return self._l10n_pe_ne_orden_dict()

    # ───────────────────────────────────────────── fold: cobrar saldo y entregar
    def l10n_pe_ne_cobrar_saldo(self, payload=None):
        """FOLD de CN-02: el cliente vuelve, paga el SALDO y recoge. Emite el comprobante final por
        el TOTAL (cuyos 'medios' registran solo el saldo → no re-cuenta el adelanto) y entrega, en un
        commit. Eje 2: grupo caja. payload: {medio?}."""
        self.ensure_one()
        payload = payload or {}
        if not self.env.user.has_group(_G_CAJA):
            raise AccessError(_("No tienes permiso para cobrar."))
        # A2: serializa la fila — dos cobros concurrentes (doble clic) no emiten dos comprobantes.
        self._l10n_pe_ne_lock()
        if self.estado != "terminada":
            raise UserError(_("Solo se cobra el saldo de una orden TERMINADA (trabajo listo)."))
        if self.factura_final_id:
            raise UserError(_("Esta orden ya se cobró en el comprobante %s.")
                            % (self.factura_final_id.name or self.factura_final_id.id))
        if not self.linea_ids:
            raise UserError(_("La orden no tiene líneas que facturar."))
        # A6: saldo positivo y no mayor al total. Si las líneas cambiaron tras el adelanto y el total
        # cayó por debajo del adelanto, un saldo negativo emitiría medios=[{monto:-X}] y esa fila
        # DESAPARECERÍA del arqueo (el filtro esperado>0) → descuadre silencioso.
        if self.saldo <= 0 or self.saldo > self.amount_total:
            raise UserError(_(
                "El saldo por cobrar (S/ %(s).2f) es inválido frente al total (S/ %(t).2f); revisa "
                "las líneas y el adelanto.", s=self.saldo, t=self.amount_total))
        medio = (payload.get("medio") or self.medio_adelanto or "Efectivo").strip() or "Efectivo"
        medios = [{"medio": medio,
                   "monto": self.saldo}]
        res = self.env["account.move"].l10n_pe_ne_quick_emit(
            self._l10n_pe_ne_payload_emision(medios))
        move_id = res.get("id") if isinstance(res, dict) else False
        # A14: el motor de impuestos redondea por línea; si el total del comprobante difiere en
        # céntimos del total de la orden, el arqueo debe contar el dinero REAL: se reescribe el
        # monto del medio único con (total del move − adelanto).
        if move_id:
            move = self.env["account.move"].browse(move_id)
            real = round((move.amount_total or 0.0) - (self.adelanto_monto or 0.0), 2)
            if real > 0 and abs(real - self.saldo) > 0.005:
                move.sudo().l10n_pe_ne_medios_pago = [{"medio": medio, "monto": real}]
        # Flag _FLUJO_OK: escritura de estado AUTORIZADA (fold de cobro, tras emitir). La constraint
        # de 'entregada exige comprobante' igual la valida (factura_final_id se setea en el mismo write).
        self.with_context(l10n_pe_ne_flujo_ok=True).write({
            "factura_final_id": move_id,
            "estado": "entregada",
            "fecha_entrega": fields.Datetime.now(),
        })
        self.message_post(body=_("Saldo cobrado y entregado por %s. Comprobante %s.")
                          % (self.env.user.name, (res or {}).get("numero") or move_id or "—"))
        return {
            "comprobanteId": move_id,
            "estado": self.estado,
            "saldoCobrado": self.saldo,
            "total": self.amount_total,
        }

    def _l10n_pe_ne_payload_emision(self, medios=None):
        """Payload de quick_emit desde las líneas de la orden. La línea guarda precio CON IGV; quick_
        emit espera precioUnitario SIN IGV → se convierte (afecto: /(1+IGV); no gravado: tal cual)."""
        self.ensure_one()
        p = self.partner_id
        # A13: tipoDoc por el TIPO de identificación real (cat. 06); fallback a la heurística.
        vat_code = p.l10n_latam_identification_type_id.l10n_pe_vat_code or ""
        tipo_doc = "01" if (vat_code == "6" or (p.vat and len(p.vat) == 11)) else "03"
        lineas = []
        for l in self.linea_ids:
            base = round(l.precio_unitario / (1 + IGV_RATE), 6) if l.afecto_igv else l.precio_unitario
            lineas.append({
                "descripcion": l.descripcion or (l.product_id.display_name or ""),
                "cantidad": l.cantidad,
                "precioUnitario": base,
                "taxCode": "1000" if l.afecto_igv else "9997",
                **({"descuento": l.descuento} if l.descuento else {}),
                **({"productId": l.product_id.id} if l.product_id else {}),
                **({"productCod": l.product_id.default_code} if l.product_id.default_code else {}),
            })
        return {
            "tipoDoc": tipo_doc,
            # A13: clienteId ancla el comprobante al MISMO partner de la orden.
            "cliente": {"clienteId": p.id,
                        "tipoDoc": vat_code or ("6" if tipo_doc == "01" else "1"),
                        "numDoc": p.vat or "", "razonSocial": p.name or ""},
            "lineas": lineas,
            "formaPago": {"tipo": "Contado",
                          "medios": medios or [{"medio": "Efectivo", "monto": self.saldo}]},
        }

    # ───────────────────────────────────────────── acciones (para la SPA)
    def _acciones(self):
        """Además de las transiciones del mixin, inyecta el registro de adelanto y el cobro de saldo
        (que cruzan caja/emisión, no son aristas). Así la escala libre se ve: al que tiene todos los
        roles le aparece toda la ruta; al operario solo tomar/terminar; al cajero cobrar."""
        self.ensure_one()
        out = super()._acciones()
        if self.estado == "borrador" and not self.adelanto_movimiento_id \
                and self.env.user.has_group(_G_CAJA):
            out.append({"key": "registrar-adelanto", "label": "Cobrar adelanto",
                        "pasos": ["encolada"], "requiereMotivo": False})
        if self.estado == "terminada" and not self.factura_final_id \
                and self.env.user.has_group(_G_CAJA):
            out.append({"key": "cobrar-saldo", "label": "Cobrar saldo y entregar",
                        "pasos": ["entregada"], "requiereMotivo": False})
        return out

    def unlink(self):
        # Integridad: no se borra una orden con dinero de por medio (adelanto cobrado o comprobante
        # emitido). El origen de un cobro no se destruye — se anula.
        for o in self:
            if o.factura_final_id or o.adelanto_movimiento_id:
                raise UserError(_(
                    "No se puede borrar la orden %s: tiene dinero cobrado (adelanto o comprobante). "
                    "Anúlala en su lugar.") % o.name)
        return super().unlink()

    # ───────────────────────────────────────────── creación + serialización
    @api.model
    def l10n_pe_ne_crear_orden(self, payload):
        """Crea una orden (borrador) desde React: {clienteId|cliente, cotizacionId?, items:[{...}],
        fechaPactada?}. Las líneas se CONGELAN en la orden (autónoma de la cotización)."""
        payload = payload or {}
        partner = self._l10n_pe_ne_resolver_partner(payload)
        lineas = self._l10n_pe_ne_build_lines(payload.get("items") or payload.get("lineas"))
        if not lineas:
            raise UserError(_("La orden necesita al menos un ítem."))
        # A16: la referencia a la cotización de origen se VALIDA (exists + leerla dispara la
        # ir.rule → cross-RUC = AccessError), no se siembra cruda como int.
        cot_id = False
        if payload.get("cotizacionId"):
            cot = self.env["l10n_pe_ne.cotizacion"].browse(int(payload["cotizacionId"])).exists()
            if cot:
                cot.company_id   # noqa: B018 — lectura a propósito: dispara la regla de compañía
                cot_id = cot.id
        orden = self.create({
            "company_id": self.env.company.id,
            "partner_id": partner.id,
            "cotizacion_id": cot_id,
            "fecha_pactada": payload.get("fechaPactada") or False,
            "linea_ids": lineas,
        })
        return orden._l10n_pe_ne_orden_dict()

    def _l10n_pe_ne_resolver_partner(self, payload):
        if payload.get("clienteId"):
            partner = self.env["res.partner"].browse(int(payload["clienteId"])).exists()
            if partner:
                return partner
        if payload.get("cliente"):
            return self.env["account.move"]._l10n_pe_ne_quick_partner(payload["cliente"])
        raise UserError(_("Indica el cliente de la orden."))

    def _l10n_pe_ne_build_lines(self, items):
        vals = []
        for it in (items or []):
            desc = (it.get("descripcion") or "").strip()
            prod = False
            if it.get("productId"):
                prod = self.env["product.product"].browse(int(it["productId"])).exists()
                if prod and not desc:
                    desc = prod.display_name
            if not desc:
                raise UserError(_("Cada ítem necesita una descripción (o un producto)."))
            vals.append((0, 0, {
                "product_id": prod.id if prod else False,
                "descripcion": desc,
                "cantidad": float(it.get("cantidad") or 1),
                "precio_unitario": float(it.get("precio") or 0),
                "descuento": float(it.get("descuento") or 0),
                "afecto_igv": bool(it.get("afectoIgv", True)),
            }))
        return vals

    def _l10n_pe_ne_orden_dict(self):
        self.ensure_one()
        return {
            "id": self.id,
            "name": self.name,
            "estado": self.estado,
            "estadoLabel": self._estado_label(self.estado),
            "cliente": self.partner_id.name or "",
            "clienteId": self.partner_id.id,
            "clienteDoc": self.partner_id.vat or "",
            "responsable": self.user_id.name or "",
            "enCola": not self.user_id,
            "cotizacionId": self.cotizacion_id.id or None,
            "fechaPactada": self.fecha_pactada and str(self.fecha_pactada) or "",
            "total": self.amount_total,
            "adelanto": self.adelanto_monto or 0.0,
            "medioAdelanto": self.medio_adelanto or "",
            "saldo": self.saldo,
            "facturaId": self.factura_final_id.id or None,
            "facturaNumero": self.factura_final_id.name or "",
            "lineas": [{
                "descripcion": l.descripcion or (l.product_id.display_name or ""),
                "cantidad": l.cantidad,
                "precio": l.precio_unitario,
                "descuento": l.descuento,
                "subtotal": l.subtotal,
                "afectoIgv": l.afecto_igv,
                "productId": l.product_id.id or None,
            } for l in self.linea_ids],
        }

    # ───────────────────────────────────────────── colas (server-side)
    @api.model
    def l10n_pe_ne_cola_ordenes(self, offset=0, limit=10):
        """Cola del taller: órdenes encoladas (por tomar) y las que ya tomó (en proceso)."""
        return self._l10n_pe_ne_cola_dict(
            [("estado", "in", ("encolada", "en_proceso"))], offset, limit)

    @api.model
    def l10n_pe_ne_cola_adelanto(self, offset=0, limit=10):
        """Cola de cobro del ADELANTO (cajero): órdenes en borrador que recepción creó y esperan el
        adelanto que las encola al taller. Sin esta bandeja, un cajero SEGREGADO no tenía cómo
        encontrarlas — hallazgo del e2e con roles puros (con el usuario modal no se veía: el que
        creaba también cobraba en el mismo modal)."""
        return self._l10n_pe_ne_cola_dict([("estado", "=", "borrador")], offset, limit)

    @api.model
    def l10n_pe_ne_cola_saldo(self, offset=0, limit=10):
        """Cola de cobro del cajero: órdenes terminadas con saldo por cobrar."""
        return self._l10n_pe_ne_cola_dict(
            [("estado", "=", "terminada"), ("factura_final_id", "=", False)], offset, limit)

    @api.model
    def _l10n_pe_ne_cola_dict(self, dominio, offset, limit):
        r = self._cola(dominio, offset=offset, limit=limit)
        return {
            "items": [o._l10n_pe_ne_orden_dict() for o in r["items"]],
            "total": r["total"], "offset": r["offset"], "limit": r["limit"],
        }


class L10nPeNeOrdenTrabajoLinea(models.Model):
    _name = "l10n_pe_ne.orden.trabajo.linea"
    _description = "Línea de orden de trabajo (NE Express)"

    orden_id = fields.Many2one("l10n_pe_ne.orden.trabajo", required=True, ondelete="cascade",
                               index=True)
    product_id = fields.Many2one("product.product", string="Producto/Servicio")
    descripcion = fields.Char(string="Descripción", required=True)
    cantidad = fields.Float(string="Cantidad", default=1.0)
    precio_unitario = fields.Monetary(string="P. unitario", currency_field="currency_id")
    descuento = fields.Float(string="Descuento %")
    afecto_igv = fields.Boolean(string="Afecto a IGV", default=True)
    subtotal = fields.Monetary(string="Subtotal", compute="_compute_subtotal", store=True,
                               currency_field="currency_id")
    currency_id = fields.Many2one(related="orden_id.currency_id", store=True)

    @api.depends("cantidad", "precio_unitario", "descuento")
    def _compute_subtotal(self):
        for line in self:
            bruto = (line.cantidad or 0.0) * (line.precio_unitario or 0.0)
            factor = 1 - (line.descuento or 0.0) / 100.0
            line.subtotal = round(bruto * factor, 2)

    # ───────────────────────────────────────────── freeze (A7 · revisión Fable)
    def _check_orden_editable(self):
        """El detalle se edita SOLO en borrador (antes del adelanto). Después, cambiar precio o
        cantidad divergiría el saldo del adelanto ya cobrado (y, entregada, del comprobante
        emitido). Sistema (su) y acciones internas (_FLUJO_OK) quedan exentos."""
        if self.env.su or self.env.context.get("l10n_pe_ne_flujo_ok"):
            return
        for line in self:
            if line.orden_id.estado != "borrador":
                raise UserError(_(
                    "El detalle de la orden %(o)s ya no se edita (está «%(e)s»): el trabajo y el "
                    "dinero cobrados no se reescriben.",
                    o=line.orden_id.name, e=line.orden_id.estado))

    def write(self, vals):
        self._check_orden_editable()
        return super().write(vals)

    def unlink(self):
        self._check_orden_editable()
        return super().unlink()

    @api.model_create_multi
    def create(self, vals_list):
        lines = super().create(vals_list)
        lines._check_orden_editable()   # tras crear: el orden_id ya está resuelto
        return lines

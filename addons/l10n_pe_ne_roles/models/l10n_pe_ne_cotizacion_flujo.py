# -*- coding: utf-8 -*-
"""CN-01 — la cotización como modelo de FLUJO (cotiza → cobra en caja → recoge en despacho).

Primer proceso sobre l10n_pe_ne.flujo.mixin. La cotización gana estados+colas+roles (vendedor
cotiza, cajero cobra, despachador entrega) y un eje de despacho ORTOGONAL al comercial.

REGLA DURA: estado='convertida' lo escribe SOLO l10n_pe_ne_vincular_comprobante al emitir. Ninguna
arista de _transiciones escribe 'convertida' (si lo hiciera, _avanzar fabricaría una convertida
huérfana sin comprobante — justo la corrupción que H4 evita). 'convertida' es terminal, alcanzable
solo por emisión. El fold 'cobrar y entregar' emite (crea el comprobante) y de paso entrega.

Decisiones del usuario (2026-07-18): P5 despacho EN EL ACTO (el fold entrega en el mismo commit);
P6 validez VINCULANTE (guarda de vigencia al cobrar + estado 'vencida' por cron).
"""
from datetime import timedelta

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError

_G = "l10n_pe_ne_roles."
_G_VENTAS = _G + "group_l10n_pe_ne_ventas"
_G_CAJA = _G + "group_l10n_pe_ne_caja"
_G_DESPACHO = _G + "group_l10n_pe_ne_despacho"

# Estado→método de transición (para el override de set_estado, que así queda grupo-gateado).
_ESTADO_METODO = {
    "enviada": "l10n_pe_ne_enviar",
    "aceptada": "l10n_pe_ne_aceptar",
    "rechazada": "l10n_pe_ne_rechazar",
}


class L10nPeNeCotizacionFlujo(models.Model):
    _name = "l10n_pe_ne.cotizacion"
    _inherit = ["l10n_pe_ne.cotizacion", "l10n_pe_ne.flujo.mixin"]

    # Override PARCIAL: solo añade tracking (conserva el Selection del biller). El mixin necesita
    # leer self.estado y su selection.
    estado = fields.Selection(tracking=True)
    # El vendedor que armó la cotización (el mixin declara user_id sin default = "en cola"; aquí
    # sí tiene dueño: quien la crea).
    user_id = fields.Many2one(default=lambda s: s.env.user)

    # Eje de DESPACHO, ortogonal al eje comercial. no_aplica = solo servicios (sin mercadería).
    estado_despacho = fields.Selection(
        [("no_aplica", "No aplica"), ("pendiente", "Pendiente de entrega"),
         ("entregado", "Entregado"), ("anulado_despacho", "Anulado")],
        string="Despacho", default="no_aplica", required=True, index=True, tracking=True)
    despachador_id = fields.Many2one("res.users", string="Despachado por", tracking=True)
    fecha_entrega = fields.Datetime(string="Fecha de entrega")
    receptor_nombre = fields.Char(string="Recibido por")
    receptor_doc = fields.Char(string="Doc. del receptor")

    # ───────────────────────────────────────────── flujo comercial
    @api.model
    def _transiciones(self):
        # SIN arista →convertida (regla dura). vencida solo por guarda/cron (P6 vinculante).
        return {
            **super()._transiciones(),
            ("borrador", "enviada"): {"grupo": _G_VENTAS, "cadena": True, "label": "Enviar al cliente"},
            ("borrador", "aceptada"): {"grupo": _G_VENTAS, "cadena": True, "label": "Cliente acepta"},
            ("enviada", "aceptada"): {"grupo": _G_VENTAS, "cadena": True, "label": "Cliente acepta"},
            ("enviada", "rechazada"): {"grupo": _G_VENTAS, "motivo": True, "label": "Cliente rechaza"},
            ("aceptada", "rechazada"): {"grupo": _G_VENTAS, "motivo": True, "label": "Cliente rechaza"},
            # 'convertida' (por emisión) y 'vencida' (por el cron, escritura directa) NO son aristas:
            # no se transicionan a mano. 'aceptada' sale por 'rechazada' o por el fold de cobro.
        }

    @api.model
    def _estados_terminales(self):
        return ("convertida", "rechazada", "vencida")

    # Transiciones con nombre (reemplazan al setter crudo; cada una valida los 3 ejes vía _avanzar).
    def l10n_pe_ne_enviar(self):
        self._avanzar("enviada")
        return self._l10n_pe_ne_cotizacion_dict()

    def l10n_pe_ne_aceptar(self):
        # Salto de un clic borrador→aceptada (D4): si el cliente aceptó de frente, "enviada" es ficción.
        self._avanzar("aceptada")
        return self._l10n_pe_ne_cotizacion_dict()

    def l10n_pe_ne_rechazar(self, motivo=None):
        self._avanzar("rechazada", motivo=motivo)
        return self._l10n_pe_ne_cotizacion_dict()

    def l10n_pe_ne_set_estado(self, estado):
        """Override: la ruta legada /estado se mapea a la transición con nombre (así queda gateada
        por grupo). 'convertida'/'vencida' no se setean a mano por aquí."""
        self.ensure_one()
        metodo = _ESTADO_METODO.get(estado)
        if not metodo:
            raise UserError(_("No se puede pasar de «%(o)s» a «%(d)s».", o=self.estado, d=estado))
        return getattr(self, metodo)()

    # ───────────────────────────────────────────── P6 · vigencia (vinculante)
    def _l10n_pe_ne_vencida(self):
        """¿La cotización pasó su validez? (fecha + validez_dias < hoy)."""
        self.ensure_one()
        if not self.fecha or not self.validez_dias:
            return False
        return (self.fecha + timedelta(days=self.validez_dias)) < fields.Date.context_today(self)

    def _l10n_pe_ne_guard_cobrable(self):
        """REALIDAD (no permiso): no se cobra al precio viejo una cotización vencida (P6 vinculante).
        Ningún grupo la levanta: hay que re-cotizar a precio vigente."""
        self.ensure_one()
        if self.comprobante_id:
            raise UserError(_("Esta cotización ya se convirtió en %s.")
                            % self._l10n_pe_ne_comprobante_numero())
        if self.estado == "vencida" or self._l10n_pe_ne_vencida():
            raise UserError(_(
                "La cotización venció el %s. Re-cotiza a precio vigente antes de cobrar.")
                % self.l10n_pe_ne_valida_hasta())

    @api.model
    def _l10n_pe_ne_cron_vencer(self):
        """Cron diario: marca 'vencida' las cotizaciones aceptadas/enviadas que pasaron su validez.
        Idempotente. Corre como sistema (bypass del eje grupo vía escritura directa del estado)."""
        for cot in self.search([("estado", "in", ("aceptada", "enviada"))]):
            if cot._l10n_pe_ne_vencida():
                cot.write({"estado": "vencida"})

    # ───────────────────────────────────────────── eje despacho
    def _l10n_pe_ne_tiene_despacho(self):
        """¿Hay mercadería que entregar? (alguna línea con producto almacenable). Solo-servicios no."""
        self.ensure_one()
        return any(
            l.product_id and l.product_id.type == "consu" and getattr(l.product_id, "is_storable", True)
            and l.cantidad > 0
            for l in self.line_ids)

    def l10n_pe_ne_vincular_comprobante(self, comprobante_id):
        """Al emitir: además de vincular+convertir (biller), abre el eje de despacho si hay mercadería."""
        res = super().l10n_pe_ne_vincular_comprobante(comprobante_id)
        if self._l10n_pe_ne_tiene_despacho() and self.estado_despacho == "no_aplica":
            self.estado_despacho = "pendiente"
        return res

    def l10n_pe_ne_entregar(self, receptor_nombre=None, receptor_doc=None):
        """Entrega la mercadería (despachador). Eje 2: grupo despacho. Eje 3: solo mercadería COBRADA."""
        self.ensure_one()
        if not self.env.user.has_group(_G_DESPACHO):
            raise AccessError(_("No tienes permiso para entregar mercadería."))
        if self.estado != "convertida" or self.estado_despacho != "pendiente":
            raise UserError(_("Solo se entrega mercadería ya cobrada y pendiente de despacho."))
        self.write({
            "estado_despacho": "entregado",
            "despachador_id": self.env.uid,
            "fecha_entrega": fields.Datetime.now(),
            "receptor_nombre": (receptor_nombre or "").strip() or self.partner_id.name or False,
            "receptor_doc": (receptor_doc or "").strip() or self.partner_id.vat or False,
        })
        self.message_post(body=_("Entregado por %s.") % self.env.user.name)
        return self._l10n_pe_ne_cotizacion_dict()

    # ───────────────────────────────────────────── fold: cobrar y entregar
    def l10n_pe_ne_cobrar_entregar(self, payload=None):
        """FOLD de CN-01: emite el comprobante desde la cotización (cobro) y, si P5 en el acto,
        entrega — todo en un commit. Con 1 usuario que tiene todos los roles es 'un clic'; con
        roles segregados, el cajero cobra y (si tiene despacho) entrega, o cae a la cola de despacho.
        payload: {medios?, entregar?, receptorNombre?, receptorDoc?}."""
        self.ensure_one()
        payload = payload or {}
        if not self.env.user.has_group(_G_CAJA):
            raise AccessError(_("No tienes permiso para cobrar."))
        # P6: no cobrar una cotización vencida al precio viejo (ni una ya convertida).
        self._l10n_pe_ne_guard_cobrable()
        # D4: asegurar 'aceptada' (recorre borrador/enviada→aceptada por cadenas, auditado). En modo
        # segregado el cajero la recibe ya aceptada desde su cola; el salto requiere ventas+caja.
        if self.estado != "aceptada":
            self._avanzar_hasta("aceptada")
        # Emitir desde la cotización (no reusa el redirect a /emitir).
        emitir = self.env["account.move"].l10n_pe_ne_quick_emit(
            self._l10n_pe_ne_payload_emision(payload.get("medios")))
        # (quick_emit ya hizo: post + mover_stock + vincular_comprobante(→convertida + despacho
        # pendiente) + send_to_biller.)
        entregado = False
        if payload.get("entregar") and self.estado_despacho == "pendiente" \
                and self.env.user.has_group(_G_DESPACHO):
            self.l10n_pe_ne_entregar(payload.get("receptorNombre"), payload.get("receptorDoc"))
            entregado = True
        return {
            "comprobanteId": emitir.get("id") if isinstance(emitir, dict) else self.comprobante_id.id,
            "comprobanteNumero": self._l10n_pe_ne_comprobante_numero(),
            "estado": self.estado,
            "estadoDespacho": self.estado_despacho,
            "entregado": entregado,
        }

    def _l10n_pe_ne_payload_emision(self, medios=None):
        """Construye el payload de quick_emit desde la cotización. CLAVE: la línea guarda
        precio_unitario CON IGV, pero quick_emit espera precioUnitario SIN IGV — se convierte
        (afecto: /1.18; no gravado: tal cual), o el comprobante saldría ~18% más alto."""
        self.ensure_one()
        p = self.partner_id
        # tipoDoc por el documento del cliente (11=RUC→factura, si no boleta).
        tipo_doc = "01" if (p.vat and len(p.vat) == 11) else "03"
        lineas = []
        for l in self.line_ids:
            base = round(l.precio_unitario / 1.18, 6) if l.afecto_igv else l.precio_unitario
            lineas.append({
                "descripcion": l.descripcion or (l.product_id.display_name or ""),
                "cantidad": l.cantidad,
                "precioUnitario": base,
                "taxCode": "1000" if l.afecto_igv else "9997",
                **({"descuento": l.descuento} if l.descuento else {}),
                **({"productId": l.product_id.id} if l.product_id else {}),
                **({"productCod": l.product_id.default_code} if l.product_id.default_code else {}),
            })
        payload = {
            "tipoDoc": tipo_doc,
            "cliente": {"tipoDoc": "6" if tipo_doc == "01" else "1",
                        "numDoc": p.vat or "", "razonSocial": p.name or ""},
            "lineas": lineas,
            "cotizacionId": self.id,
            "formaPago": {"tipo": "Contado", "medios": medios or [{"medio": "Efectivo",
                                                                    "monto": self.amount_total}]},
        }
        return payload

    # ───────────────────────────────────────────── acciones para la SPA
    def _acciones(self):
        """Además de las transiciones comerciales, inyecta el fold de cobro/entrega y la entrega,
        que NO son aristas de _transiciones (cruzan ejes + emisión). Así la escala libre se ve: al
        que tiene todos los roles le aparece 'Cobrar y entregar'; al cajero puro, 'Cobrar'."""
        self.ensure_one()
        out = super()._acciones()
        if self.estado == "aceptada" and not self.comprobante_id \
                and self.env.user.has_group(_G_CAJA):
            if self.env.user.has_group(_G_DESPACHO) and self._l10n_pe_ne_tiene_despacho():
                out.append({"key": "cobrar-entregar", "label": "Cobrar y entregar",
                            "pasos": ["convertida", "entregado"], "requiereMotivo": False})
            out.append({"key": "cobrar", "label": "Cobrar y emitir",
                        "pasos": ["convertida"], "requiereMotivo": False})
        if self.estado == "convertida" and self.estado_despacho == "pendiente" \
                and self.env.user.has_group(_G_DESPACHO):
            out.append({"key": "entregar", "label": "Entregar", "pasos": ["entregado"],
                        "requiereMotivo": False})
        return out

    # ───────────────────────────────────────────── serialización
    def _l10n_pe_ne_cotizacion_dict(self):
        d = super()._l10n_pe_ne_cotizacion_dict()
        d.update({
            "estadoDespacho": self.estado_despacho,
            "despachador": self.despachador_id.name or "",
            "receptorNombre": self.receptor_nombre or "",
            "vendedor": self.user_id.name or "",
        })
        return d

    # ───────────────────────────────────────────── colas (server-side)
    @api.model
    def l10n_pe_ne_cola_cobro(self, offset=0, limit=10):
        """Cola de cobro (cajero): aceptadas sin convertir."""
        return self._l10n_pe_ne_cola_dict(
            [("estado", "=", "aceptada"), ("comprobante_id", "=", False)], offset, limit)

    @api.model
    def l10n_pe_ne_cola_despacho(self, offset=0, limit=10):
        """Cola de despacho (despachador) = 'pagado y no despachado' (también la ve el supervisor)."""
        return self._l10n_pe_ne_cola_dict(
            [("estado", "=", "convertida"), ("estado_despacho", "=", "pendiente")], offset, limit)

    @api.model
    def _l10n_pe_ne_cola_dict(self, dominio, offset, limit):
        r = self._cola(dominio, offset=offset, limit=limit)
        return {
            "items": [c._l10n_pe_ne_cotizacion_dict() for c in r["items"]],
            "total": r["total"], "offset": r["offset"], "limit": r["limit"],
        }

# -*- coding: utf-8 -*-
"""CN-02 — la ORDEN DE TRABAJO como modelo de FLUJO (cotiza+adelanta → cola → taller la toma →
termina → el cliente vuelve, paga el saldo y recoge).

Segundo proceso sobre l10n_pe_ne.flujo.mixin, y el primero que estrena la COLA con TOMA ATÓMICA:
la orden nace SIN dueño (user_id NULL = en cola); un usuario con rol taller la TOMA al avanzar
(NULL→yo, atómico) — a diferencia de la cotización de CN-01, que nace con dueño. Con 1 usuario
que lleva todos los sombreros la cola colapsa a una bandeja única; con N usuarios cada rol ve solo
su tramo. Jamás se compara identidad de usuarios (escala libre): solo has_group.

Dos vías del ADELANTO, elegidas por RUC con el switch l10n_pe_ne_adelanto_facturado (default OFF):

  Vía B (default) — RECIBO INTERNO / a cuenta. No se emite comprobante de anticipo: el adelanto se
  registra como un movimiento de caja estructurado ('adelanto', con su medio) que cuadra el arqueo
  por su medio; al recoger se emite UN comprobante por el TOTAL cuyos 'medios' registran solo el
  SALDO — así el adelanto no se cuenta dos veces entre sesiones de caja distintas.

  Vía A (switch ON) — ANTICIPO FACTURADO ante SUNAT. Cada adelanto EMITE su comprobante gravado
  (factura si RUC / boleta si DNI) vía anticipo_factura_id; el comprobante final lo referencia y lo
  descuenta (descuento global 04 + relacionados + sumTotalAnticipos, que el biller ya sabe armar). En
  esta vía la plata entra al arqueo por los MEDIOS del comprobante del anticipo (venta de la sesión),
  no por el movimiento de caja — que se salta en el seam para no contar doble.

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
    # FIFO real: la "llegada a la cola" del taller es el momento del ADELANTO (cuando la orden pasa
    # a 'encolada'), NO la creación del borrador — un borrador puede quedarse días sin adelanto. Se
    # estampa en el MISMO write flujo_ok de registrar_adelanto que pone estado=encolada. copy=False:
    # una orden duplicada re-encola desde cero, no hereda su turno.
    fecha_encolada = fields.Datetime(string="Encolada el", readonly=True, copy=False)

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
    # Vía A: el comprobante del ANTICIPO (factura/boleta) que emite el adelanto. restrict como la
    # factura final — un comprobante fiscal emitido no se borra por debajo dejando la orden colgada;
    # además el final lo referencia (anticipo 04), así que su vida está atada a la orden.
    anticipo_factura_id = fields.Many2one("account.move", string="Comprobante del anticipo",
                                          readonly=True, ondelete="restrict", copy=False)
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
            "adelanto_monto", "medio_adelanto", "adelanto_movimiento_id", "factura_final_id",
            "anticipo_factura_id")

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
        self.ensure_one()
        # Vía A: si el adelanto ya se facturó, anular la orden dejaría vivo un comprobante de anticipo
        # sin regularizar ante SUNAT. La reversión es un acto fiscal explícito (nota de crédito del
        # anticipo), no un efecto colateral de anular: se exige revertirlo ANTES.
        if self.anticipo_factura_id and \
                self.anticipo_factura_id.l10n_pe_biller_state not in ("anulado", "rechazado"):
            raise UserError(_(
                "El adelanto ya se facturó en el comprobante %s: emite primero su nota de crédito "
                "y luego anula la orden.")
                % (self.anticipo_factura_id.name or self.anticipo_factura_id.id))
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
            # A17: el adelanto es PARCIAL por definición en AMBAS vías. Un pago 100% adelantado no es
            # un anticipo: se emite el comprobante ÚNICO por el total de una vez (el trabajo queda
            # pendiente de entrega). En Vía A un "anticipo" que iguala el total no tendría saldo que
            # regularizar; en Vía B dejaría el comprobante final con medios=0. Mensaje honesto.
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
        # Vía A (anticipo facturado ante SUNAT): si el RUC lo activó, el adelanto EMITE su propio
        # comprobante gravado (factura/boleta) que el comprobante final referenciará y descontará. Va
        # ANTES del write de estado: si SUNAT rechaza, quick_emit lanza y toda la transacción (incluido
        # el movimiento de caja) revierte — no queda un adelanto a medias. Si está apagado, cero cambios.
        anticipo_move_id = False
        anticipo_numero = ""
        if self.company_id.l10n_pe_ne_adelanto_facturado:
            res_ant = self.env["account.move"].l10n_pe_ne_quick_emit(
                self._l10n_pe_ne_payload_anticipo(monto, medio))
            anticipo_move_id = res_ant.get("id") if isinstance(res_ant, dict) else False
            anticipo_numero = (res_ant or {}).get("numero") or ""
            if anticipo_move_id:
                move = self.env["account.move"].browse(anticipo_move_id)
                anticipo_numero = anticipo_numero or move.name or str(anticipo_move_id)
                # Ajuste de céntimos (espejo del A14 de cobrar_saldo): el motor de impuestos redondea
                # por línea; si el total emitido difiere del monto pedido, la referencia fiscal del
                # final debe calzar con el doc REALMENTE emitido — se reescriben los medios del
                # anticipo con el real y se usa el real como adelanto.
                real = round(move.amount_total or 0.0, 2)
                if real > 0 and abs(real - monto) > 0.005:
                    move.sudo().l10n_pe_ne_medios_pago = [{"medio": medio, "monto": real}]
                    monto = real
        # Encolar (borrador→encolada) NO es una arista del mixin: se hace aquí, atado al adelanto.
        # Flag _FLUJO_OK: escritura de estado AUTORIZADA (no es un write RPC crudo). anticipo_factura_id
        # se guarda en el MISMO write que adelanto_monto/estado: la referencia y el importe fiscal viajan
        # juntos y quedan bajo el blindaje de _campos_flujo.
        vals = {"adelanto_monto": monto, "medio_adelanto": medio,
                "adelanto_movimiento_id": mov.id, "estado": "encolada",
                # FIFO por llegada: el turno se toma AHORA (al adelantar y encolar), no al crear.
                "fecha_encolada": fields.Datetime.now()}
        if anticipo_move_id:
            vals["anticipo_factura_id"] = anticipo_move_id
        self.with_context(l10n_pe_ne_flujo_ok=True).write(vals)
        if anticipo_move_id:
            self.message_post(body=_(
                "Adelanto de S/ %(m).2f (%(me)s) facturado por %(u)s en el comprobante %(c)s. En cola.",
                m=monto, me=medio, u=self.env.user.name, c=anticipo_numero))
        else:
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
        # Vía A: el final debe referenciar un anticipo VIGENTE. Si el comprobante del anticipo quedó en
        # error/rechazado/anulado, la referencia 04 apuntaría a un doc que SUNAT no reconoce (la factura
        # final sería rechazada). Se bloquea con mensaje claro: primero resolver el anticipo.
        if self.anticipo_factura_id and \
                self.anticipo_factura_id.l10n_pe_biller_state in ("error", "rechazado", "anulado"):
            raise UserError(_(
                "El comprobante del anticipo %(n)s está en «%(e)s»; resuélvelo antes de cobrar el "
                "saldo (el final debe referenciar un anticipo vigente).",
                n=self.anticipo_factura_id.name or self.anticipo_factura_id.id,
                e=self.anticipo_factura_id.l10n_pe_biller_state))
        medio = (payload.get("medio") or self.medio_adelanto or "Efectivo").strip() or "Efectivo"
        medios = [{"medio": medio,
                   "monto": self.saldo}]
        payload_emision = self._l10n_pe_ne_payload_emision(medios)
        # Vía A: el final descuenta el anticipo ya facturado. El biller mapea 'anticipo' → descuento
        # global 04 + relacionados + sumTotalAnticipos (ver test_anticipo). 'doc' es el número fiscal
        # del anticipo (serie-correlativo a 8 dígitos, ej. F001-00000100); 'tipo' 02 si fue factura, 03
        # si boleta.
        if self.anticipo_factura_id:
            payload_emision["anticipo"] = {
                "total": self.adelanto_monto,
                "doc": self._l10n_pe_ne_anticipo_doc_ref(),
                "tipo": "02" if self._l10n_pe_ne_anticipo_es_factura() else "03",
                # origenId enlaza la regularización con el doc. A local: el biller lleva el saldo
                # (aplicado/disponible), valida moneda, e impide aplicar más de lo que queda.
                "origenId": self.anticipo_factura_id.id,
            }
        res = self.env["account.move"].l10n_pe_ne_quick_emit(payload_emision)
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

    def _l10n_pe_ne_cliente_emision(self):
        """tipoDoc del comprobante + bloque 'cliente' de quick_emit, resueltos del partner de la orden.
        Compartido por el anticipo (Vía A) y el comprobante final: ambos van al MISMO cliente y con el
        MISMO criterio factura/boleta (A13: cat. 06 real; fallback a la heurística por longitud del vat).
        clienteId ancla el comprobante al mismo partner de la orden."""
        self.ensure_one()
        p = self.partner_id
        vat_code = p.l10n_latam_identification_type_id.l10n_pe_vat_code or ""
        tipo_doc = "01" if (vat_code == "6" or (p.vat and len(p.vat) == 11)) else "03"
        cliente = {"clienteId": p.id,
                   "tipoDoc": vat_code or ("6" if tipo_doc == "01" else "1"),
                   "numDoc": p.vat or "", "razonSocial": p.name or ""}
        return tipo_doc, cliente

    def _l10n_pe_ne_payload_emision(self, medios=None):
        """Payload de quick_emit desde las líneas de la orden. La línea guarda precio CON IGV; quick_
        emit espera precioUnitario SIN IGV → se convierte (afecto: /(1+IGV); no gravado: tal cual)."""
        self.ensure_one()
        tipo_doc, cliente = self._l10n_pe_ne_cliente_emision()
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
            "cliente": cliente,
            "lineas": lineas,
            "formaPago": {"tipo": "Contado",
                          "medios": medios or [{"medio": "Efectivo", "monto": self.saldo}]},
        }

    def _l10n_pe_ne_payload_anticipo(self, monto, medio):
        """Payload de quick_emit para el comprobante del ANTICIPO (Vía A). Una sola línea gravada
        ('Anticipo a cuenta — OT-xxxxx', taxCode 1000): el anticipo se factura GRAVADO, así el final
        puede aplicar el descuento global 04 sobre la base gravada. La línea espera precioUnitario SIN
        IGV → monto/(1+IGV). Mismo cliente/tipoDoc que el final (así factura↔factura, boleta↔boleta)."""
        self.ensure_one()
        tipo_doc, cliente = self._l10n_pe_ne_cliente_emision()
        base = round(monto / (1 + IGV_RATE), 6)
        return {
            "tipoDoc": tipo_doc,
            "cliente": cliente,
            # esAnticipo: marca el doc. A en el modelo formal del biller (l10n_pe_ne_es_anticipo):
            # entra a "anticipos pendientes" del cliente, lleva saldo aplicado/disponible, y la
            # validación del biller impide consumirlo dos veces desde cualquier regularización.
            "esAnticipo": True,
            "lineas": [{
                "descripcion": _("Anticipo a cuenta — %s") % self.name,
                "cantidad": 1,
                "precioUnitario": base,
                "taxCode": "1000",
            }],
            "formaPago": {"tipo": "Contado", "medios": [{"medio": medio, "monto": monto}]},
        }

    def _l10n_pe_ne_anticipo_doc_ref(self):
        """Número fiscal del comprobante de anticipo en el formato que el biller espera en
        l10n_pe_ne_anticipo_doc (serie-correlativo a 8 dígitos, ej. F001-00000100). Se toma de
        _l10n_pe_serie_correlativo del move (resuelve el correlativo del folio cuando no es manual);
        se prefiere lo REALMENTE emitido (serie/corr congelados al enviar) si ya existe."""
        self.ensure_one()
        move = self.anticipo_factura_id
        serie, corr = move._l10n_pe_serie_correlativo()
        serie = move.l10n_pe_ne_serie_emit or serie
        corr = move.l10n_pe_ne_corr_emit or corr
        return "%s-%s" % (serie, str(corr).zfill(8))

    def _l10n_pe_ne_anticipo_es_factura(self):
        """True si el anticipo se emitió como FACTURA (tipoDoc 01 / serie que empieza con F). Define el
        'tipo' del relacionado en el final (02 factura, 03 boleta, cat. 12)."""
        self.ensure_one()
        move = self.anticipo_factura_id
        serie, _corr = move._l10n_pe_serie_correlativo()
        serie = move.l10n_pe_ne_serie_emit or serie
        return (move.l10n_pe_ne_tipo_doc or "").strip() == "01" or (serie or "")[:1].upper() == "F"

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
        fechaPactada?}. Las líneas se CONGELAN en la orden (autónoma de la cotización).

        PUENTE cotización→taller: si llega cotizacionId y NO ítems explícitos, la orden NACE de la
        cotización — copia sus líneas y (si falta) su cliente. Con ítems explícitos la SPA manda (los
        ítems ganan) y la cotización queda solo como referencia trazable."""
        payload = payload or {}
        # A16: la referencia a la cotización de origen se VALIDA (exists + leerla dispara la
        # ir.rule → cross-RUC = AccessError), no se siembra cruda como int.
        cot = False
        if payload.get("cotizacionId"):
            cot = self.env["l10n_pe_ne.cotizacion"].browse(int(payload["cotizacionId"])).exists()
            if cot:
                cot.company_id   # noqa: B018 — lectura a propósito: dispara la regla de compañía
        items = payload.get("items") or payload.get("lineas")
        if cot and not items:
            # PUENTE. La orden de taller nace SOLO de una cotización ACEPTADA: hay acuerdo con el
            # cliente pero aún NO se vendió por mostrador. Ni borrador/enviada (sin acuerdo cerrado),
            # ni convertida (ya se facturó por mostrador → montar un taller sería doble venta), ni
            # vencida/rechazada (sin acuerdo vigente).
            if cot.estado != "aceptada":
                raise UserError(_(
                    "La orden de taller nace de una cotización ACEPTADA; la %(n)s está «%(e)s».",
                    n=cot.name, e=cot.estado))
            # Anti-duplicado: una cotización aceptada abre UNA orden de taller. Si ya existe una orden
            # no anulada que la referencia, se devuelve su nombre (no se abre una segunda en paralelo).
            previa = self.search(
                [("cotizacion_id", "=", cot.id), ("estado", "!=", "anulada")], limit=1)
            if previa:
                raise UserError(_("Esta cotización ya tiene la orden %s.") % previa.name)
            lineas = self._l10n_pe_ne_lineas_desde_cotizacion(cot)
        else:
            lineas = self._l10n_pe_ne_build_lines(items)
        if not lineas:
            raise UserError(_("La orden necesita al menos un ítem."))
        partner = self._l10n_pe_ne_resolver_partner(payload, cot)
        orden = self.create({
            "company_id": self.env.company.id,
            "partner_id": partner.id,
            "cotizacion_id": cot.id if cot else False,
            "fecha_pactada": payload.get("fechaPactada") or False,
            "linea_ids": lineas,
        })
        return orden._l10n_pe_ne_orden_dict()

    def _l10n_pe_ne_lineas_desde_cotizacion(self, cot):
        """PUENTE: copia las líneas de la cotización ACEPTADA a la orden. AMBOS modelos guardan el
        precio CON IGV (misma convención) → mapeo directo, sin conversión. Las líneas se CONGELAN en
        la orden (autónoma: no sigue el ciclo de la cotización tras nacer)."""
        vals = []
        for l in cot.line_ids:
            vals.append((0, 0, {
                "product_id": l.product_id.id or False,
                "descripcion": l.descripcion or (l.product_id.display_name or ""),
                "cantidad": l.cantidad,
                "precio_unitario": l.precio_unitario,
                "afecto_igv": l.afecto_igv,
                "descuento": l.descuento,
            }))
        return vals

    def _l10n_pe_ne_resolver_partner(self, payload, cot=False):
        if payload.get("clienteId"):
            partner = self.env["res.partner"].browse(int(payload["clienteId"])).exists()
            if partner:
                return partner
        if payload.get("cliente"):
            return self.env["account.move"]._l10n_pe_ne_quick_partner(payload["cliente"])
        # PUENTE: sin cliente en el payload pero con cotización válida, el partner de la cotización es
        # el default natural — la orden va al MISMO cliente que aceptó la cotización.
        if cot and cot.partner_id:
            return cot.partner_id
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
            # FIFO: momento en que la orden llegó a la cola (al adelantar). "" mientras es borrador.
            "fechaEncolada": self.fecha_encolada and str(self.fecha_encolada) or "",
            "total": self.amount_total,
            "adelanto": self.adelanto_monto or 0.0,
            "medioAdelanto": self.medio_adelanto or "",
            "saldo": self.saldo,
            "facturaId": self.factura_final_id.id or None,
            "facturaNumero": self.factura_final_id.name or "",
            # Vía A: el comprobante del anticipo (vacío en Vía B). La SPA solo lo pinta.
            "anticipoFacturaId": self.anticipo_factura_id.id or None,
            "anticipoNumero": self.anticipo_factura_id.name or "",
            "anticipoEstado": self.anticipo_factura_id.l10n_pe_biller_state or "",
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
        """Cola del taller: órdenes encoladas (por tomar) y las que ya tomó (en proceso). FIFO por
        llegada A LA COLA: se atiende primero la que se adelantó antes (fecha_encolada), NO la más
        nueva. Las órdenes viejas SIN fecha_encolada (creadas antes de este cambio) caen al final —
        Postgres pone los NULLs al último en ASC — tolerable en bases pre-cambio."""
        return self._l10n_pe_ne_cola_dict(
            [("estado", "in", ("encolada", "en_proceso"))], offset, limit,
            order="fecha_encolada asc, id asc")

    @api.model
    def l10n_pe_ne_cola_adelanto(self, offset=0, limit=10):
        """Cola de cobro del ADELANTO (cajero): órdenes en borrador que recepción creó y esperan el
        adelanto que las encola al taller. Sin esta bandeja, un cajero SEGREGADO no tenía cómo
        encontrarlas — hallazgo del e2e con roles puros (con el usuario modal no se veía: el que
        creaba también cobraba en el mismo modal). FIFO por registro (id asc): se cobra primero la
        que entró antes al sistema."""
        return self._l10n_pe_ne_cola_dict([("estado", "=", "borrador")], offset, limit,
                                          order="id asc")

    @api.model
    def l10n_pe_ne_cola_saldo(self, offset=0, limit=10):
        """Cola de cobro del cajero: órdenes terminadas con saldo por cobrar. FIFO por registro."""
        return self._l10n_pe_ne_cola_dict(
            [("estado", "=", "terminada"), ("factura_final_id", "=", False)], offset, limit,
            order="id asc")

    @api.model
    def _l10n_pe_ne_cola_dict(self, dominio, offset, limit, order=None):
        r = self._cola(dominio, offset=offset, limit=limit, order=order)
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

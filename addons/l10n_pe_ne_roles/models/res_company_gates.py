# -*- coding: utf-8 -*-
"""Motor de gates de política por RUC (iteración 4).

Toda compuerta de aprobación del catálogo es un parámetro de res.company (multi-tenant;
ir.config_parameter es global a la BD y no serviría). Un solo mecanismo con tres modos:

  off      No pasa nada. DEFAULT de todo tenant nuevo.
  aviso    No bloquea: marca la excepción (control_estado='excepcion') y cae en la cola de
           revisión. Es el modo que un negocio chico puede permitirse.
  bloquea  Bloquea salvo que quien opera pueda aprobar (y entonces auto-aprueba REGISTRADO).

`modo` y `umbral` son ejes ORTOGONALES: modo='off' apaga; modo='bloquea'+umbral=0 es tolerancia
cero (la política más estricta). NO se colapsan en "0 = apagado" (haría inexpresable el rigor).

La aprobación NO añade estados (nada de 'pendiente_aprobacion'): es un ATRIBUTO del documento
(ver el mixin de flujo). Ver docs/procesos-negocio/decision-escala-libre.md.
"""
from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError

_GATE_MODO = [
    ("off", "Sin control"),
    ("aviso", "Registra y avisa"),
    ("bloquea", "Exige aprobación"),
]

# Registro ÚNICO de las 7 compuertas reales del catálogo. Añadir un gate = una fila aquí + sus
# campos abajo. clave -> {modo, umbral(None si no aplica), grupo que aprueba, unidad, label}.
_GATES = {
    "descuadre": {"modo": "l10n_pe_ne_gate_descuadre", "umbral": "l10n_pe_ne_umbral_descuadre",
                  "grupo": "l10n_pe_ne_roles.group_l10n_pe_ne_supervisor", "unidad": "monto",
                  "label": "Descuadre de caja"},
    "descuento": {"modo": "l10n_pe_ne_gate_descuento", "umbral": "l10n_pe_ne_umbral_descuento",
                  "grupo": "l10n_pe_ne_roles.group_l10n_pe_ne_supervisor", "unidad": "pct",
                  "label": "Descuento fuera de política"},
    "credito": {"modo": "l10n_pe_ne_gate_credito", "umbral": "l10n_pe_ne_umbral_credito",
                "grupo": "l10n_pe_ne_roles.group_l10n_pe_ne_supervisor", "unidad": "monto",
                "label": "Venta al crédito"},
    "gasto": {"modo": "l10n_pe_ne_gate_gasto", "umbral": "l10n_pe_ne_umbral_gasto",
              "grupo": "l10n_pe_ne_roles.group_l10n_pe_ne_supervisor", "unidad": "monto",
              "label": "Egreso sobre el tope de caja chica"},
    "devolucion": {"modo": "l10n_pe_ne_gate_devolucion", "umbral": None,
                   "grupo": "l10n_pe_ne_biller.group_l10n_pe_ne_anulacion", "unidad": None,
                   "label": "Reversión de venta"},
    "merma": {"modo": "l10n_pe_ne_gate_merma", "umbral": "l10n_pe_ne_umbral_merma",
              "grupo": "l10n_pe_ne_roles.group_l10n_pe_ne_supervisor", "unidad": "monto",
              "label": "Baja de existencias"},
    "deposito": {"modo": "l10n_pe_ne_gate_deposito", "umbral": "l10n_pe_ne_umbral_deposito",
                 "grupo": "l10n_pe_ne_roles.group_l10n_pe_ne_supervisor", "unidad": "monto",
                 "label": "Depósito en banco"},
}


class ResCompanyGates(models.Model):
    _inherit = "res.company"

    # Modos (todos 'off' de fábrica: un tenant nuevo no bloquea nada). SIN groups= a propósito:
    # los lee /ne/api/config para TODO usuario (el operador debe poder ver la regla que le cae
    # encima); el gate de ESCRITURA es has_group dentro de l10n_pe_ne_set_politica.
    l10n_pe_ne_gate_descuadre = fields.Selection(_GATE_MODO, default="off", required=True)
    l10n_pe_ne_gate_descuento = fields.Selection(_GATE_MODO, default="off", required=True)
    l10n_pe_ne_gate_credito = fields.Selection(_GATE_MODO, default="off", required=True)
    l10n_pe_ne_gate_gasto = fields.Selection(_GATE_MODO, default="off", required=True)
    l10n_pe_ne_gate_devolucion = fields.Selection(_GATE_MODO, default="off", required=True)
    l10n_pe_ne_gate_merma = fields.Selection(_GATE_MODO, default="off", required=True)
    l10n_pe_ne_gate_deposito = fields.Selection(_GATE_MODO, default="off", required=True)

    # Umbral = "hasta aquí NO pasa nada". Independiente del modo (ver l10n_pe_ne_gate).
    l10n_pe_ne_umbral_descuadre = fields.Monetary(currency_field="currency_id", default=0.0)
    l10n_pe_ne_umbral_descuento = fields.Float(default=0.0, digits=(5, 2), help="% sobre precio de lista")
    l10n_pe_ne_umbral_credito = fields.Monetary(currency_field="currency_id", default=0.0)
    l10n_pe_ne_umbral_gasto = fields.Monetary(currency_field="currency_id", default=0.0)
    l10n_pe_ne_umbral_merma = fields.Monetary(currency_field="currency_id", default=0.0)
    l10n_pe_ne_umbral_deposito = fields.Monetary(currency_field="currency_id", default=0.0)

    # Escape hatch de segregación. Default False. Con él encendido, quien registra NO puede aprobar
    # su propia solicitud aunque tenga el permiso — y si no hay un segundo aprobador, los documentos
    # se quedan esperando. Es el ÚNICO sitio del producto donde se comparan identidades, y solo
    # porque el dueño lo pidió a sabiendas.
    l10n_pe_ne_exigir_segregacion = fields.Boolean(
        string="Exigir que apruebe otra persona", default=False,
        help="Si se activa, quien registra no puede aprobar lo suyo. Actívalo solo si tienes dos "
             "aprobadores reales, o los documentos se quedarán esperando sin poder destrabarse.")

    # Vía A del adelanto. Default False = Vía B intacta (el adelanto es un recibo interno y el
    # comprobante ÚNICO se emite al final por el total). Con él encendido cada adelanto EMITE su
    # propio comprobante (factura si RUC / boleta si DNI) y el final lo referencia y descuenta
    # (anticipo 04 ante SUNAT). Es una decisión FISCAL del dueño del RUC —cambia qué se le entrega al
    # cliente por su prepago—, por eso vive por compañía y no se colapsa con la política de gates.
    l10n_pe_ne_adelanto_facturado = fields.Boolean(
        string="Facturar los adelantos (Vía A)", default=False,
        help="Si se activa, cada adelanto emite su propio comprobante (factura o boleta) y el "
             "comprobante final lo descuenta. Apagado: el adelanto es un recibo interno y el "
             "comprobante único va al final por el total.")

    def l10n_pe_ne_gate(self, key, magnitud=None):
        """Modo EFECTIVO del gate para esta magnitud: 'off' | 'aviso' | 'bloquea'. modo='off' o
        magnitud bajo el umbral -> 'off'. modo y umbral son dos ejes; con umbral=0 y modo activo la
        tolerancia es cero (dispara con cualquier magnitud > 0)."""
        self.ensure_one()
        cfg = _GATES.get(key)
        if not cfg:
            raise UserError(_("Política desconocida: %s") % key)
        modo = self[cfg["modo"]] or "off"
        if modo == "off":
            return "off"
        if cfg["umbral"] and magnitud is not None:
            if abs(float(magnitud)) <= (self[cfg["umbral"]] or 0.0):
                return "off"
        return modo

    def _l10n_pe_ne_politica_frase(self, key, modo, umbral):
        """Texto ya redactado por el addon (la SPA NO compone frases de política — ahí nacen las
        divergencias)."""
        cfg = _GATES[key]
        if modo == "off":
            return ""
        tope = ""
        if cfg["umbral"] and umbral:
            tope = (" sobre %.2f%%" % umbral) if cfg["unidad"] == "pct" else (" sobre S/ %.2f" % umbral)
        if modo == "aviso":
            return "%s%s: se registra como excepción para revisión." % (cfg["label"], tope)
        return "%s%s: requiere aprobación." % (cfg["label"], tope)

    def l10n_pe_ne_politicas_dict(self):
        """Contrato de /ne/api/config: las 7 políticas con su modo, umbral, etiqueta y frase."""
        self.ensure_one()
        out = {}
        for key, cfg in _GATES.items():
            modo = self[cfg["modo"]] or "off"
            umbral = self[cfg["umbral"]] if cfg["umbral"] else None
            out[key] = {
                "modo": modo, "umbral": umbral, "unidad": cfg["unidad"], "etiqueta": cfg["label"],
                "aviso": self._l10n_pe_ne_politica_frase(key, modo, umbral),
            }
        out["exigirSegregacion"] = self.l10n_pe_ne_exigir_segregacion
        out["adelantoFacturado"] = self.l10n_pe_ne_adelanto_facturado
        return out

    @api.model
    def l10n_pe_ne_set_politica(self, key, modo, umbral=None):
        """Cambiar una política es decisión del DUEÑO/SUPERVISOR del RUC (el modelo es la autoridad;
        el controller solo serializa)."""
        if not (self.env.user.has_group("l10n_pe_ne_roles.group_l10n_pe_ne_supervisor")
                or self.env.user.has_group("base.group_system")):
            raise AccessError(_("Solo el dueño o un supervisor cambia las políticas de control."))
        cfg = _GATES.get(key)
        if not cfg or modo not in dict(_GATE_MODO):
            raise UserError(_("Política o modo no válido."))
        company = self.env.user.company_id.sudo()   # scope duro por RUC
        vals = {cfg["modo"]: modo}
        if cfg["umbral"] and umbral is not None:
            vals[cfg["umbral"]] = float(umbral)
        company.write(vals)
        return company.l10n_pe_ne_politicas_dict()

    @api.model
    def l10n_pe_ne_set_exigir_segregacion(self, activo):
        if not (self.env.user.has_group("l10n_pe_ne_roles.group_l10n_pe_ne_supervisor")
                or self.env.user.has_group("base.group_system")):
            raise AccessError(_("Solo el dueño o un supervisor cambia las políticas de control."))
        company = self.env.user.company_id.sudo()
        company.write({"l10n_pe_ne_exigir_segregacion": bool(activo)})
        return company.l10n_pe_ne_politicas_dict()

    @api.model
    def l10n_pe_ne_set_adelanto_facturado(self, activo):
        # Elegir la vía del adelanto (facturarlo o no) es la misma autoridad que cualquier política de
        # control: dueño/supervisor del RUC, scope duro por compañía. Espeja set_exigir_segregacion.
        if not (self.env.user.has_group("l10n_pe_ne_roles.group_l10n_pe_ne_supervisor")
                or self.env.user.has_group("base.group_system")):
            raise AccessError(_("Solo el dueño o un supervisor cambia las políticas de control."))
        company = self.env.user.company_id.sudo()
        company.write({"l10n_pe_ne_adelanto_facturado": bool(activo)})
        return company.l10n_pe_ne_politicas_dict()

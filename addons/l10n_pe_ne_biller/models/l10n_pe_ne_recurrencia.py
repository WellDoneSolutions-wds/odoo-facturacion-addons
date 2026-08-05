# -*- coding: utf-8 -*-
"""V11 · Facturación recurrente / membresías (fase 2 del módulo de rubros).

El caso de negocio: gimnasios (membresía mensual), colegios/institutos (pensión),
SaaS y alquileres con canon fijo. El dueño registra UNA vez «cliente + concepto +
monto + día del mes» y el cron diario emite el comprobante solo — reusa el motor
de emisión completo (l10n_pe_ne_quick_emit): impuestos, series, caja, envío async
a SUNAT. Sin motor propio de facturación: una recurrencia ES una emisión normal
programada.

Decisiones:
  * Concepto libre (sin producto de catálogo): la membresía/pensión rara vez es un
    SKU; si el negocio lo quiere en el kardex, que emita manual. YAGNI.
  * El monto se guarda CON impuesto (lo que paga el socio — el precio de vitrina,
    convención de toda la app); al emitir se deriva la base según la afectación.
  * Pensiones educativas → afectación exonerada (9997): la educación está
    exonerada de IGV (art. 19 Constitución / D.L. 882). El default sigue 1000.
  * Si una emisión falla (SUNAT caída, cliente inválido), la recurrencia NO
    avanza su fecha: el cron reintenta al día siguiente y el error queda visible
    en la pantalla (`ultimo_error`). Cada recurrencia corre en su savepoint —
    una que falle no bloquea a las demás.
"""
import logging

from dateutil.relativedelta import relativedelta

from odoo import _, api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

# Con qué afectación sale la línea (catálogo 07). El IGV/IVAP se descuenta del monto
# bruto al armar la línea (el payload de emisión viaja con el valor SIN impuesto).
_DIVISOR = {"1000": 1.18, "1016": 1.04}


class Recurrencia(models.Model):
    _name = "l10n_pe_ne.recurrencia"
    _description = "Facturación recurrente / membresía"
    _order = "proxima_fecha, id"

    name = fields.Char(string="Concepto", required=True)   # «Membresía full» / «Pensión 3° A»
    partner_id = fields.Many2one("res.partner", string="Cliente", required=True, ondelete="restrict")
    monto = fields.Float(required=True, help="Precio CON impuesto (lo que paga el cliente).")
    tax_code = fields.Selection(
        [("1000", "Gravado (IGV 18%)"), ("9997", "Exonerado"), ("9998", "Inafecto")],
        default="1000", required=True, string="Afectación")
    tipo_doc = fields.Selection(
        [("03", "Boleta"), ("01", "Factura")], default="03", required=True)
    periodicidad = fields.Selection(
        [("mensual", "Mensual"), ("anual", "Anual")], default="mensual", required=True)
    # 1..28 para que TODOS los meses tengan el día (evita el drift del 29/30/31: una
    # membresía del 31 emitiría el 28 en febrero y luego "se quedaría" en 28).
    dia_emision = fields.Integer(default=1, required=True)
    proxima_fecha = fields.Date(index=True)
    activa = fields.Boolean(default=True)
    ultima_move_id = fields.Many2one("account.move", string="Última emisión")
    ultimo_error = fields.Char()
    company_id = fields.Many2one(
        "res.company", required=True, index=True, default=lambda self: self.env.company)

    # ------------------------------------------------------------- validación
    @api.constrains("dia_emision")
    def _check_dia(self):
        for r in self:
            if not 1 <= r.dia_emision <= 28:
                raise UserError(_("El día de emisión debe estar entre 1 y 28 (todos los meses lo tienen)."))

    @api.constrains("monto")
    def _check_monto(self):
        for r in self:
            if r.monto <= 0:
                raise UserError(_("El monto debe ser mayor a 0."))

    @api.constrains("tipo_doc", "partner_id")
    def _check_factura_ruc(self):
        for r in self:
            vat = (r.partner_id.vat or "").strip()
            if r.tipo_doc == "01" and (len(vat) != 11 or not vat.isdigit()):
                raise UserError(_(
                    "Una FACTURA recurrente exige un cliente con RUC válido (11 dígitos). "
                    "«%s» no lo tiene: usa Boleta o corrige el cliente.") % (r.partner_id.name or "-"))

    # ------------------------------------------------------------ fechas
    def _siguiente_desde(self, base):
        """Próxima ocurrencia ESTRICTAMENTE posterior a `base`, en el día configurado."""
        self.ensure_one()
        candidata = base.replace(day=self.dia_emision)
        if candidata <= base:
            candidata += relativedelta(months=12 if self.periodicidad == "anual" else 1)
        return candidata

    # ------------------------------------------------------------- emisión
    def _payload_emision(self):
        self.ensure_one()
        p = self.partner_id
        vat = (p.vat or "").strip()
        tipo_doc_cliente = "6" if len(vat) == 11 else ("1" if len(vat) == 8 else "0")
        base = round(self.monto / _DIVISOR.get(self.tax_code, 1.0), 10)
        return {
            "tipoDoc": self.tipo_doc,
            "cliente": {"tipoDoc": tipo_doc_cliente, "numDoc": vat, "razonSocial": p.name or ""},
            # Sin formaPago: el default del motor es CONTADO (una membresía se cobra al emitir).
            "lineas": [{
                "descripcion": self.name,
                "cantidad": 1,
                "precioUnitario": base,   # SIN impuesto (contrato de quick_emit)
                "taxCode": self.tax_code,
                "unidad": "ZZ",           # servicio
            }],
        }

    def l10n_pe_ne_emitir_una(self):
        """Emite el comprobante de ESTA recurrencia (la usa el cron y el botón «Emitir ahora»).
        Éxito → guarda la referencia, limpia el error y avanza la fecha. Fracaso → deja el
        error visible y NO avanza (el cron reintenta)."""
        self.ensure_one()
        AM = self.env["account.move"].with_company(self.company_id)
        AM._l10n_pe_ne_check_modulo("V11", "Facturación recurrente / membresías")
        res = AM.l10n_pe_ne_quick_emit(self._payload_emision())
        self.write({
            "ultima_move_id": res.get("id") if isinstance(res, dict) else False,
            "ultimo_error": False,
            "proxima_fecha": self._siguiente_desde(
                max(self.proxima_fecha or fields.Date.context_today(self),
                    fields.Date.context_today(self))),
        })
        return res

    @api.model
    def _l10n_pe_ne_cron_recurrencias(self):
        """Cron diario: emite todas las recurrencias activas vencidas. Cada una en su
        savepoint — la membresía del cliente moroso con RUC dado de baja no puede frenar
        las otras 200 del gimnasio."""
        hoy = fields.Date.context_today(self)
        pendientes = self.sudo().search([
            ("activa", "=", True), ("proxima_fecha", "<=", hoy), ("proxima_fecha", "!=", False)])
        for rec in pendientes:
            try:
                with self.env.cr.savepoint():
                    rec.with_company(rec.company_id).l10n_pe_ne_emitir_una()
            except Exception as e:  # noqa: BLE001 — se registra y sigue con la siguiente
                _logger.warning("recurrencia %s (%s): %s", rec.id, rec.name, e)
                try:
                    with self.env.cr.savepoint():
                        rec.ultimo_error = str(e)[:250]
                except Exception:  # noqa: BLE001
                    pass

    # ------------------------------------------------------------- API SPA
    def _dict(self):
        self.ensure_one()
        return {
            "id": self.id,
            "concepto": self.name,
            "clienteId": self.partner_id.id,
            "cliente": self.partner_id.name or "",
            "numDoc": self.partner_id.vat or "",
            "monto": round(self.monto, 2),
            "taxCode": self.tax_code,
            "tipoDoc": self.tipo_doc,
            "periodicidad": self.periodicidad,
            "diaEmision": self.dia_emision,
            "proximaFecha": str(self.proxima_fecha or ""),
            "activa": self.activa,
            # Número emitido «F001-00000123» desde los campos de emisión; cae al name de Odoo
            # si aún no hay correlativo (emisión en cola).
            "ultimaEmision": ("%s-%s" % (self.ultima_move_id.l10n_pe_ne_serie_emit,
                                         self.ultima_move_id.l10n_pe_ne_corr_emit)
                              if self.ultima_move_id and self.ultima_move_id.l10n_pe_ne_serie_emit
                              else (self.ultima_move_id.name or "" if self.ultima_move_id else "")),
            "ultimoError": self.ultimo_error or "",
        }

    @api.model
    def l10n_pe_ne_list(self):
        return [r._dict() for r in self.search([("company_id", "=", self.env.company.id)])]

    @api.model
    def l10n_pe_ne_save(self, payload):
        """Alta/edición desde la SPA. El muro V11 corta aquí (y en la emisión): sin el módulo
        activo no se pueden crear membresías por API directa."""
        self.env["account.move"]._l10n_pe_ne_check_modulo("V11", "Facturación recurrente / membresías")
        payload = payload or {}
        pid = int(payload.get("clienteId") or 0)
        partner = self.env["res.partner"].browse(pid).exists()
        if not partner:
            raise UserError(_("Elige el cliente de la membresía."))
        vals = {
            "name": (payload.get("concepto") or "").strip(),
            "partner_id": partner.id,
            "monto": float(payload.get("monto") or 0),
            "tax_code": payload.get("taxCode") or "1000",
            "tipo_doc": payload.get("tipoDoc") or "03",
            "periodicidad": payload.get("periodicidad") or "mensual",
            "dia_emision": int(payload.get("diaEmision") or 1),
            "activa": bool(payload.get("activa", True)),
            "company_id": self.env.company.id,
        }
        if not vals["name"]:
            raise UserError(_("Escribe el concepto (ej. «Membresía mensual»)."))
        rec_id = int(payload.get("id") or 0)
        rec = self.browse(rec_id).exists() if rec_id else self.browse()
        if rec:
            rec.write(vals)
        else:
            rec = self.create(vals)
        # La próxima fecha se (re)calcula si no hay una futura: HOY cuenta si el día
        # configurado es hoy (la membresía dada de alta el mismo día se emite hoy).
        hoy = fields.Date.context_today(self)
        if not rec.proxima_fecha or rec.proxima_fecha < hoy:
            candidata = hoy.replace(day=rec.dia_emision)
            rec.proxima_fecha = candidata if candidata >= hoy else rec._siguiente_desde(hoy)
        return rec._dict()

    @api.model
    def l10n_pe_ne_delete(self, rec_id):
        """Elimina si NUNCA emitió (alta por error); si ya tiene historia, solo se PAUSA —
        el vínculo con sus comprobantes emitidos no se destruye."""
        rec = self.browse(int(rec_id or 0)).exists()
        if not rec:
            return {"ok": True, "modo": "inexistente"}
        if rec.ultima_move_id:
            rec.activa = False
            return {"ok": True, "modo": "pausada"}
        rec.unlink()
        return {"ok": True, "modo": "eliminada"}

    @api.model
    def l10n_pe_ne_emitir_ahora(self, rec_id):
        rec = self.browse(int(rec_id or 0)).exists()
        if not rec:
            raise UserError(_("Membresía no encontrada."))
        res = rec.l10n_pe_ne_emitir_una()
        out = rec._dict()
        out["resultado"] = res
        return out

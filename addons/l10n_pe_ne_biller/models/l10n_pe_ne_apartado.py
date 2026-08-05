# -*- coding: utf-8 -*-
"""V09 · Reserva / apartado — layaway (fase 2 del módulo de rubros).

El cliente separa un producto y lo va pagando en abonos; cuando completa, se
entrega y recién ahí sale el comprobante. Integridad de caja SIN doble conteo:

  * cada ABONO entra a la caja abierta como INGRESO con su medio (el dinero
    entra al cajón el día que entra de verdad). Si no hay caja abierta, el
    abono se registra igual en el apartado — la caja nunca bloquea (doctrina
    del repo);
  * la BOLETA final se emite SIN medios de pago: el arqueo la clasifica en
    su bucket «sin medio» y no vuelve a exigir un dinero que ya ingresó
    abono a abono.

La devolución de un apartado cancelado es un RETIRO manual de caja (con su
motivo), no un flujo propio: así queda con el mismo respaldo que cualquier
salida de dinero.
"""
from odoo import _, api, fields, models
from odoo.exceptions import UserError

from ..tools.caja_arqueo import normalizar_medio

ESTADOS = [("activo", "Activo"), ("entregado", "Entregado"), ("cancelado", "Cancelado")]


class Apartado(models.Model):
    _name = "l10n_pe_ne.apartado"
    _description = "Reserva / apartado (layaway)"
    _order = "id desc"

    cliente = fields.Char(required=True)
    telefono = fields.Char()
    numdoc = fields.Char(string="DNI/RUC")     # opcional: para la boleta final identificada
    name = fields.Char(string="Qué aparta", required=True)
    total = fields.Float(required=True)
    estado = fields.Selection(ESTADOS, default="activo", required=True, index=True)
    abono_ids = fields.One2many("l10n_pe_ne.apartado.abono", "apartado_id")
    abonado = fields.Float(compute="_compute_abonado")
    saldo = fields.Float(compute="_compute_abonado")
    move_id = fields.Many2one("account.move", string="Comprobante final")
    company_id = fields.Many2one(
        "res.company", required=True, index=True, default=lambda self: self.env.company)

    @api.depends("abono_ids.monto", "total")
    def _compute_abonado(self):
        for a in self:
            a.abonado = round(sum(a.abono_ids.mapped("monto")), 2)
            a.saldo = round(a.total - a.abonado, 2)

    @api.constrains("total")
    def _check_total(self):
        for a in self:
            if a.total <= 0:
                raise UserError(_("El total del apartado debe ser mayor a 0."))

    # ------------------------------------------------------------- API SPA
    def _dict(self):
        self.ensure_one()
        return {
            "id": self.id, "cliente": self.cliente, "telefono": self.telefono or "",
            "numDoc": self.numdoc or "", "descripcion": self.name,
            "total": round(self.total, 2), "abonado": self.abonado, "saldo": self.saldo,
            "estado": self.estado,
            "abonos": [{"fecha": str(b.fecha), "monto": round(b.monto, 2), "medio": b.medio or "Efectivo"}
                       for b in self.abono_ids],
            "comprobante": ("%s-%s" % (self.move_id.l10n_pe_ne_serie_emit, self.move_id.l10n_pe_ne_corr_emit)
                            if self.move_id and self.move_id.l10n_pe_ne_serie_emit else ""),
        }

    @api.model
    def l10n_pe_ne_list(self):
        return [a._dict() for a in self.search([("company_id", "=", self.env.company.id)])]

    @api.model
    def l10n_pe_ne_save(self, payload):
        self.env["account.move"]._l10n_pe_ne_check_modulo("V09", "Reserva / apartado")
        payload = payload or {}
        vals = {
            "cliente": (payload.get("cliente") or "").strip(),
            "telefono": (payload.get("telefono") or "").strip() or False,
            "numdoc": (payload.get("numDoc") or "").strip() or False,
            "name": (payload.get("descripcion") or "").strip(),
            "total": float(payload.get("total") or 0),
            "company_id": self.env.company.id,
        }
        if not vals["cliente"]:
            raise UserError(_("Escribe el nombre del cliente."))
        if not vals["name"]:
            raise UserError(_("Escribe qué aparta (ej. «Refrigeradora LG 250L»)."))
        ap_id = int(payload.get("id") or 0)
        ap = self.browse(ap_id).exists() if ap_id else self.browse()
        if ap:
            if ap.estado != "activo":
                raise UserError(_("Un apartado %s ya no se edita.") % dict(ESTADOS)[ap.estado].lower())
            if vals["total"] < ap.abonado:
                raise UserError(_("El total no puede ser menor a lo ya abonado (S/ %.2f).") % ap.abonado)
            ap.write(vals)
        else:
            ap = self.create(vals)
        return ap._dict()

    @api.model
    def l10n_pe_ne_abonar(self, ap_id, payload):
        """Registra un abono y lo mete a la caja abierta como ingreso (si la hay)."""
        self.env["account.move"]._l10n_pe_ne_check_modulo("V09", "Reserva / apartado")
        ap = self.browse(int(ap_id or 0)).exists()
        if not ap or ap.estado != "activo":
            raise UserError(_("Apartado no encontrado o ya cerrado."))
        monto = round(float((payload or {}).get("monto") or 0), 2)
        if monto <= 0:
            raise UserError(_("El abono debe ser mayor a 0."))
        if monto > ap.saldo:
            raise UserError(_("El abono (S/ %(m).2f) supera el saldo (S/ %(s).2f).",
                              m=monto, s=ap.saldo))
        medio = normalizar_medio((payload or {}).get("medio"))
        self.env["l10n_pe_ne.apartado.abono"].create({
            "apartado_id": ap.id, "monto": monto, "medio": medio,
            "fecha": fields.Date.context_today(self)})
        # A la caja como ingreso, para que el arqueo del día cuadre con el dinero real.
        # Sin caja abierta el abono vale igual (la caja nunca bloquea): quedará como
        # dinero fuera de arqueo, exactamente como una venta sin caja.
        try:
            self.env["l10n_pe_ne.caja.sesion"].l10n_pe_ne_caja_movimiento({
                "tipo": "ingreso", "monto": monto, "medio": medio,
                "motivo": "Abono apartado #%d — %s" % (ap.id, ap.cliente)})
        except UserError:
            pass
        return ap._dict()

    @api.model
    def l10n_pe_ne_entregar(self, ap_id, payload=None):
        """Entrega el bien y emite la boleta/factura FINAL por el total, SIN medios de pago
        (el dinero ya entró abono a abono — ver docstring del módulo)."""
        ap = self.browse(int(ap_id or 0)).exists()
        if not ap or ap.estado != "activo":
            raise UserError(_("Apartado no encontrado o ya cerrado."))
        if ap.saldo > 0.009:
            raise UserError(_("Aún hay saldo pendiente (S/ %.2f): completa los abonos antes de entregar.") % ap.saldo)
        tipo = (payload or {}).get("tipoDoc") or "03"
        num = (ap.numdoc or "").strip()
        AM = self.env["account.move"].with_company(ap.company_id)
        res = AM.l10n_pe_ne_quick_emit({
            "tipoDoc": tipo,
            "cliente": {
                "tipoDoc": "6" if len(num) == 11 else ("1" if len(num) == 8 else "0"),
                "numDoc": num, "razonSocial": ap.cliente,
            },
            # Valor SIN IGV (contrato de quick_emit); el apartado se pacta en precio final.
            "lineas": [{"descripcion": ap.name, "cantidad": 1,
                        "precioUnitario": ap.total / 1.18, "taxCode": "1000",
                        "unidad": "NIU"}],
        })
        ap.write({"estado": "entregado",
                  "move_id": res.get("id") if isinstance(res, dict) else False})
        out = ap._dict()
        out["resultado"] = res
        return out

    @api.model
    def l10n_pe_ne_cancelar(self, ap_id):
        """Cancela la reserva. La devolución del dinero (si el negocio devuelve) es un retiro
        manual de caja con su motivo — mismo respaldo que cualquier salida."""
        ap = self.browse(int(ap_id or 0)).exists()
        if not ap:
            return {"ok": True}
        if ap.estado == "entregado":
            raise UserError(_("Un apartado entregado no se cancela (ya tiene comprobante)."))
        ap.estado = "cancelado"
        return ap._dict()


class ApartadoAbono(models.Model):
    _name = "l10n_pe_ne.apartado.abono"
    _description = "Abono de apartado"
    _order = "id"

    apartado_id = fields.Many2one("l10n_pe_ne.apartado", required=True, ondelete="cascade", index=True)
    fecha = fields.Date(required=True)
    monto = fields.Float(required=True)
    medio = fields.Char(default="Efectivo")

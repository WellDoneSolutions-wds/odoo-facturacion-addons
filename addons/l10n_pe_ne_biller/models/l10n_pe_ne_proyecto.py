"""Proyecto / contrato para facturación por avance de obra (valorizaciones). Lleva el valor
total del contrato y controla lo acumulado facturado para no pasarse del 100% (QA-039)."""
from odoo import api, fields, models


class L10nPeNeProyecto(models.Model):
    _name = "l10n_pe_ne.proyecto"
    _description = "Proyecto / contrato (facturación por avance)"
    _order = "name"

    name = fields.Char(required=True, string="Proyecto / contrato")
    valor_total = fields.Monetary(required=True, string="Valor total del contrato")
    currency_id = fields.Many2one(
        "res.currency", default=lambda s: s.env.company.currency_id, required=True
    )
    company_id = fields.Many2one(
        "res.company", required=True, index=True, default=lambda s: s.env.company
    )
    facturado = fields.Monetary(compute="_compute_facturado", string="Facturado acumulado")
    saldo = fields.Monetary(compute="_compute_facturado", string="Saldo por facturar")
    avance = fields.Float(compute="_compute_facturado", string="Avance %")
    valorizaciones = fields.Integer(compute="_compute_facturado", string="N° de valorizaciones")
    retencion_acumulada = fields.Monetary(
        compute="_compute_facturado", string="Fondo de garantía retenido")
    adelanto = fields.Monetary(
        string="Adelanto recibido",
        help="Adelanto (directo + de materiales) que la entidad pagó al inicio; se amortiza en las "
        "valorizaciones.")
    adelanto_amortizado = fields.Monetary(
        compute="_compute_facturado", string="Adelanto amortizado")
    adelanto_saldo = fields.Monetary(
        compute="_compute_facturado", string="Adelanto por amortizar")

    def _compute_facturado(self):
        Move = self.env["account.move"].sudo()
        for p in self:
            moves = Move.search([
                ("l10n_pe_ne_proyecto_id", "=", p.id),
                ("l10n_pe_biller_state", "in", ("enviado", "en_proceso")),
            ])
            p.facturado = sum(moves.mapped("amount_total"))
            p.saldo = (p.valor_total or 0.0) - p.facturado
            p.avance = round(p.facturado / p.valor_total * 100.0, 2) if p.valor_total else 0.0
            p.valorizaciones = len(moves)
            p.retencion_acumulada = round(
                sum(m._l10n_pe_ne_retencion_garantia_monto() for m in moves), 2)
            p.adelanto_amortizado = sum(moves.mapped("l10n_pe_ne_amortizacion_adelanto"))
            p.adelanto_saldo = (p.adelanto or 0.0) - p.adelanto_amortizado

    def _l10n_pe_ne_dict(self):
        self.ensure_one()
        # Recalcula en caliente: facturado/saldo/avance son computed NO almacenados. Si el
        # comprobante se emitió en esta misma transacción, la caché podría tener el acumulado
        # previo al envío (en producción el detalle es otro request y no ocurre).
        self.invalidate_recordset(
            ["facturado", "saldo", "avance", "valorizaciones", "retencion_acumulada",
             "adelanto_amortizado", "adelanto_saldo"])
        guias = self.env["l10n_pe_ne.guia_remision"].sudo().search_count(
            [("l10n_pe_ne_proyecto_id", "=", self.id)])
        return {"id": self.id, "name": self.name, "valorTotal": self.valor_total,
                "facturado": self.facturado, "saldo": self.saldo,
                "avance": self.avance, "valorizaciones": self.valorizaciones,
                "retencionAcumulada": self.retencion_acumulada,
                "adelanto": self.adelanto, "adelantoAmortizado": self.adelanto_amortizado,
                "adelantoSaldo": self.adelanto_saldo, "guias": guias}

    @api.model
    def l10n_pe_ne_list(self):
        return [p._l10n_pe_ne_dict()
                for p in self.search([("company_id", "=", self.env.company.id)])]

    @api.model
    def l10n_pe_ne_upsert(self, vals):
        pid = vals.get("id")
        data = {"name": vals.get("name") or "Proyecto", "valor_total": float(vals.get("valorTotal") or 0)}
        if "adelanto" in vals:
            data["adelanto"] = float(vals.get("adelanto") or 0)
        rec = self.browse(int(pid)) if pid else self.create(data)
        if pid:
            rec.write(data)
        return rec._l10n_pe_ne_dict()

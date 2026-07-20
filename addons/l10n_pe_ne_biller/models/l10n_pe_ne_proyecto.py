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

    def _compute_facturado(self):
        Move = self.env["account.move"].sudo()
        for p in self:
            moves = Move.search([
                ("l10n_pe_ne_proyecto_id", "=", p.id),
                ("l10n_pe_biller_state", "in", ("enviado", "en_proceso")),
            ])
            p.facturado = sum(moves.mapped("amount_total"))
            p.saldo = (p.valor_total or 0.0) - p.facturado

    @api.model
    def l10n_pe_ne_list(self):
        return [
            {"id": p.id, "name": p.name, "valorTotal": p.valor_total,
             "facturado": p.facturado, "saldo": p.saldo}
            for p in self.search([("company_id", "=", self.env.company.id)])
        ]

    @api.model
    def l10n_pe_ne_upsert(self, vals):
        pid = vals.get("id")
        data = {"name": vals.get("name") or "Proyecto", "valor_total": float(vals.get("valorTotal") or 0)}
        rec = self.browse(int(pid)) if pid else self.create(data)
        if pid:
            rec.write(data)
        return {"id": rec.id, "name": rec.name, "valorTotal": rec.valor_total,
                "facturado": rec.facturado, "saldo": rec.saldo}

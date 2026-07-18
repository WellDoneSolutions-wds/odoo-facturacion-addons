# -*- coding: utf-8 -*-
"""CN-02 · adelanto a cuenta en caja (Vía B, recibo interno).

Extiende la caja del biller para el prepago del cliente SIN emitir comprobante: un movimiento de
tipo 'adelanto' con su MEDIO (para cuadrar el arqueo por medio, no solo efectivo) y su CLIENTE, y
el enganche a la orden de trabajo. El adelanto NO es un ingreso genérico (que iría solo a Efectivo
y mezclaría un pasivo del cliente con el fondo propio): entra al esperado del arqueo por su medio
vía el seam _l10n_pe_ne_por_medio_arqueo del biller.
"""
from odoo import _, api, fields, models


class L10nPeNeCajaMovimientoAdelanto(models.Model):
    _inherit = "l10n_pe_ne.caja.movimiento"

    # 'adelanto': prepago a cuenta ligado a una orden. cascade: si se desinstala roles, el concepto
    # desaparece con sus movimientos (no quedan 'adelanto' huérfanos en una caja solo-biller).
    tipo = fields.Selection(selection_add=[("adelanto", "Adelanto a cuenta")],
                            ondelete={"adelanto": "cascade"})
    medio = fields.Char(string="Medio de pago")
    partner_id = fields.Many2one("res.partner", string="Cliente", index=True)
    orden_trabajo_id = fields.Many2one("l10n_pe_ne.orden.trabajo", string="Orden de trabajo",
                                       index=True, ondelete="set null")


class L10nPeNeCajaSesionAdelanto(models.Model):
    _inherit = "l10n_pe_ne.caja.sesion"

    def _l10n_pe_ne_por_medio_arqueo(self, agr):
        """Suma los adelantos de la sesión al por-medio del arqueo (cada uno por SU medio). Así el
        prepago físico del cliente cuadra el esperado aunque venga por Yape/Tarjeta, sin inflar
        Efectivo como lo haría un ingreso genérico."""
        por_medio = super()._l10n_pe_ne_por_medio_arqueo(agr)
        for mv in self.movimiento_ids:
            if mv.tipo == "adelanto":
                medio = (mv.medio or "Efectivo").strip() or "Efectivo"
                por_medio[medio] = round(por_medio.get(medio, 0.0) + (mv.monto or 0.0), 2)
        return por_medio

    @api.model
    def _l10n_pe_ne_registrar_adelanto(self, monto, medio, partner, motivo):
        """Crea el movimiento de adelanto sobre la sesión abierta. Lo llama la orden de trabajo
        (que luego enlaza orden_trabajo_id). Exige caja abierta (el helper del biller lanza si no)."""
        sesion = self._l10n_pe_ne_sesion_abierta()
        return self.env["l10n_pe_ne.caja.movimiento"].create({
            "sesion_id": sesion.id,
            "tipo": "adelanto",
            "motivo": motivo or _("Adelanto a cuenta"),
            "monto": round(float(monto or 0.0), 2),
            "medio": (medio or "Efectivo").strip() or "Efectivo",
            "partner_id": partner.id if partner else False,
        })

    def _l10n_pe_ne_movimientos_dicts(self):
        """Enriquece los adelantos con su medio/cliente/orden para la vista de caja."""
        dicts = super()._l10n_pe_ne_movimientos_dicts()
        by_id = {mv.id: mv for mv in self.movimiento_ids}
        for d in dicts:
            mv = by_id.get(d["id"])
            if mv and mv.tipo == "adelanto":
                d["medio"] = mv.medio or ""
                d["cliente"] = mv.partner_id.name or ""
                d["orden"] = mv.orden_trabajo_id.name or ""
        return dicts

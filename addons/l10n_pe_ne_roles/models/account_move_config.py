# -*- coding: utf-8 -*-
"""Extiende /ne/api/config con las políticas de gates del RUC (iteración 4) y la resolución de
cliente en la emisión con clienteId (A13, revisión Fable)."""
from odoo import api, models


class AccountMove(models.Model):
    _inherit = "account.move"

    @api.model
    def l10n_pe_ne_config(self):
        """La SPA ya consume {igv, icbperRate}: se AÑADE 'politicas', no se quita nada. Así muere
        el AVISO_DIF hardcodeado del navegador: la política de control vive en el RUC."""
        cfg = super().l10n_pe_ne_config()
        cfg["politicas"] = self.env.company.l10n_pe_ne_politicas_dict()
        return cfg

    def _l10n_pe_ne_quick_partner(self, c):
        """A13: si el payload trae clienteId (los folds de CN-01/CN-02 lo mandan), el comprobante
        se ancla a ESE partner. El biller resuelve solo por vat: un cliente sin documento se
        re-creaba homónimo en cada cobro, y el comprobante quedaba en un partner distinto al de la
        cotización/orden. Fallback intacto: sin clienteId (o inválido), la resolución del biller."""
        pid = (c or {}).get("clienteId")
        if pid:
            p = self.env["res.partner"].browse(int(pid)).exists()
            if p and (not p.company_id or p.company_id == self.env.company):
                return p
        return super()._l10n_pe_ne_quick_partner(c)

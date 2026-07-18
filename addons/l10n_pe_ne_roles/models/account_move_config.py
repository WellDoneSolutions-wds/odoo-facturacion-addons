# -*- coding: utf-8 -*-
"""Extiende /ne/api/config con las políticas de gates del RUC (iteración 4)."""
from odoo import models


class AccountMove(models.Model):
    _inherit = "account.move"

    def l10n_pe_ne_config(self):
        """La SPA ya consume {igv, icbperRate}: se AÑADE 'politicas', no se quita nada. Así muere
        el AVISO_DIF hardcodeado del navegador: la política de control vive en el RUC."""
        cfg = super().l10n_pe_ne_config()
        cfg["politicas"] = self.env.company.l10n_pe_ne_politicas_dict()
        return cfg

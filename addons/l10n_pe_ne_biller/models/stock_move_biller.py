# -*- coding: utf-8 -*-
"""Enlace entre el movimiento de stock y el comprobante que lo generó.

Sin este enlace habría que buscar los movimientos por `origin` (un Char con el nombre del
comprobante), que no es identidad: se repite entre compañías y una NC arrastra el origen de
su afectado. Con el Many2one la reversa de un rechazo apunta exactamente a lo que movió ese
comprobante, y de paso el kardex puede responder "¿qué venta generó esta salida?".
"""

from odoo import fields, models


class StockMove(models.Model):
    _inherit = "stock.move"

    l10n_pe_ne_move_id = fields.Many2one(
        "account.move",
        string="Comprobante (NE Express)",
        index=True,
        ondelete="set null",
        copy=False,
        help="Comprobante electrónico cuya emisión generó este movimiento.",
    )
    l10n_pe_ne_reversa = fields.Boolean(
        string="Reversa de rechazo",
        default=False,
        copy=False,
        help="Movimiento que revierte al del comprobante rechazado por SUNAT. Se marca para "
        "no reversarlo a su vez: sin esto, la reversa se buscaría a sí misma.",
    )

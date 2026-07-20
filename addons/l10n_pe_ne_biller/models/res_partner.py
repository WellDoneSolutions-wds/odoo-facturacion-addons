from odoo import fields, models


class ResPartner(models.Model):
    _inherit = "res.partner"

    # Cliente exceptuado del régimen de percepciones del IGV (SUNAT): buen contribuyente,
    # agente de percepción u otra condición que lo excluye. Si está marcado, la emisión
    # BLOQUEA aplicarle percepción aunque el bien esté afecto (ver QA-028 /
    # account_move_biller.action_l10n_pe_send_to_biller). Evita un cobro adicional indebido.
    l10n_pe_ne_exceptuado_percepcion = fields.Boolean(
        string="Exceptuado de percepción",
        help="Si está marcado, no se aplica percepción del IGV a este cliente "
        "aunque el bien esté afecto (cliente excluido del régimen).",
    )

    # Parte vinculada (mismo grupo económico): informativo, para identificar y reportar
    # operaciones sujetas a precios de transferencia (QA-046). No bloquea la emisión.
    l10n_pe_ne_parte_vinculada = fields.Boolean(
        string="Parte vinculada",
        help="Cliente del mismo grupo económico. Marca la operación para el análisis de "
        "precios de transferencia (declaración jurada anual informativa).",
    )

from odoo import api, fields, models


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
    # Tipo de vínculo (V3): supuesto de vinculación del art. 24 del Reglamento LIR con el que se
    # relaciona esta parte. Alimenta la DJ Informativa de Precios de Transferencia (Reporte Local).
    # Solo tiene sentido con parte_vinculada marcada.
    l10n_pe_ne_tipo_vinculo = fields.Selection(
        [
            ("01", "Capital: ≥30% directa/indirecta"),
            ("02", "Capital común: mismo socio ≥30% en ambas"),
            ("03", "Directorio / administración común"),
            ("04", "Influencia dominante en las decisiones"),
            ("05", "Establecimiento permanente / casa matriz"),
            ("06", "Contrato de colaboración / consorcio"),
            ("07", "Otro supuesto de vinculación"),
        ],
        string="Tipo de vínculo",
        help="Supuesto de vinculación (art. 24 Reglamento LIR) para la DJ de precios de "
        "transferencia. Solo aplica si la parte está marcada como vinculada.",
    )
    # No domiciliada: se DERIVA del país (≠ PE = no domiciliada). Las operaciones con vinculadas
    # no domiciliadas siempre entran al ámbito de precios de transferencia (sin umbral de país).
    l10n_pe_ne_no_domiciliada = fields.Boolean(
        string="No domiciliada",
        compute="_compute_l10n_pe_ne_no_domiciliada",
        help="Verdadero si el país del cliente no es Perú. Las operaciones con vinculadas no "
        "domiciliadas entran a precios de transferencia sin umbral de país.",
    )

    @api.depends("country_id")
    def _compute_l10n_pe_ne_no_domiciliada(self):
        for p in self:
            p.l10n_pe_ne_no_domiciliada = bool(p.country_id) and p.country_id.code != "PE"

from odoo import fields, models


class UomUom(models.Model):
    _inherit = 'uom.uom'

    # Código de unidad de medida de SUNAT (cat. 03, basado en UN/ECE Rec. 20) que va como
    # `unitCode` en cada línea del comprobante. Patrón tomado de l10n_pe_edi (enterprise), que
    # no está instalado en community; aquí lo proveemos de forma autocontenida.
    l10n_pe_ne_unit_code = fields.Char(
        string='Código unidad SUNAT',
        help="Código de unidad de medida de SUNAT (Catálogo 03) para esta unidad de Odoo. "
             "Si está vacío, el comprobante usa 'NIU' (unidad).")

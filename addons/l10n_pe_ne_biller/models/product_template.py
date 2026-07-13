from odoo import fields, models


class ProductTemplate(models.Model):
    _inherit = 'product.template'

    # Código de unidad de medida SUNAT (Catálogo 03, basado en UN/ECE Rec. 20) que va como
    # `unitCode` en cada línea del comprobante cuando se factura este producto. Se guarda el
    # código plano (ej. NIU, KGM, ZZ) en lugar de amarrarlo al uom_id de Odoo, para evitar el
    # problema de categorías de unidad de medida (mismo criterio que el override por línea en
    # account.move.line). Vacío = el comprobante usa 'NIU' (unidad).
    l10n_pe_ne_unit_code = fields.Char(
        string='Unidad SUNAT (cat.03)',
        help="Código de unidad de medida SUNAT (Catálogo 03, ej. NIU, KGM, LTR) que se usa al "
             "facturar este producto. Si está vacío, el comprobante usa 'NIU' (unidad).")

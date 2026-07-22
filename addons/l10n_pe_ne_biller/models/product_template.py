from odoo import fields, models


class ProductTemplate(models.Model):
    _inherit = 'product.template'

    # Código de unidad de medida SUNAT (Catálogo 03, basado en UN/ECE Rec. 20) que va como
    # `unitCode` en cada línea del comprobante cuando se factura este producto. Se guarda el
    # código plano (ej. NIU, KGM, ZZ) en lugar de amarrarlo al uom_id de Odoo, para evitar el
    # problema de categorías de unidad de medida (mismo criterio que el override por línea en
    # account.move.line). Vacío = el comprobante usa 'NIU' (unidad).
    # Margen de venta del producto, en %. Vive en el producto porque no es uniforme: una
    # ferretería no gana lo mismo en brocas que en cemento. Vacío = se usa el default del
    # negocio (param `l10n_pe_ne.margen_default`).
    #
    # Se aplica sobre el precio CON IGV, que es la convención de toda la app: costo bruto ×
    # (1 + margen) = precio de vitrina. No hay que desarmar el impuesto para pensar el margen.
    l10n_pe_ne_margen = fields.Float(
        string="Margen de venta (%)",
        digits=(5, 2),
        help="Porcentaje sobre el costo (ambos con IGV) para calcular el precio de venta. "
        "En 0 usa el margen por defecto del negocio: un Float no distingue vacío de cero, "
        "así que para vender al costo se fija el precio a mano y no se deja que la compra "
        "lo recalcule.",
    )

    l10n_pe_ne_unit_code = fields.Char(
        string='Unidad SUNAT (cat.03)',
        help="Código de unidad de medida SUNAT (Catálogo 03, ej. NIU, KGM, LTR) que se usa al "
             "facturar este producto. Si está vacío, el comprobante usa 'NIU' (unidad).")

    l10n_pe_ne_cod_producto_sunat = fields.Char(
        string="Cód. producto SUNAT (cat.25)",
        help="Código de producto SUNAT (UNSPSC, catálogo 25). Aparece en la guía como "
             "bien normalizado.")

    l10n_pe_ne_detraccion_cod = fields.Char(
        string="Sujeto a detracción (cat. 54)",
        help="Código del bien/servicio en el catálogo 54 de SUNAT (SPOT). Vacío = no "
             "sujeto. Solo el código: la TASA la sugiere la app al emitir (cambia por "
             "resolución) y queda editable. Emitir lo usa para detectar operaciones "
             "mixtas — la RS 183-2004 (art. 19) exige comprobantes separados.",
    )

    l10n_pe_ne_percepcion_tasa = fields.Float(
        string="Percepción sugerida (%)",
        digits=(5, 2),
        help="Tasa de percepción del IGV sugerida si el bien está en el Apéndice 1 "
             "(Ley 29173): 2% general, 1% combustibles. 0 = no sujeto. Es sugerencia: "
             "la tasa final se confirma al emitir (el 0.5% del listado SUNAT se ajusta "
             "a mano). Solo aplica si el negocio es agente de percepción.",
    )

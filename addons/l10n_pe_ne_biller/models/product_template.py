from odoo import _, api, fields, models
from odoo.exceptions import ValidationError


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

    # Umbral de reposición: cuando las existencias bajan de este nivel, la UI avisa "bajo el
    # mínimo". No auto-compra (no crea reglas de reabastecimiento de Odoo): es una alerta simple
    # que basta para una PYME. Solo tiene sentido en bienes que llevan inventario; 0 = sin alerta.
    l10n_pe_ne_stock_minimo = fields.Float(
        string="Stock mínimo",
        digits=(12, 4),
        default=0.0,
        help="Nivel de reposición. Cuando el saldo baja de este número, la app avisa para "
             "reponer a tiempo. Solo aplica a bienes con inventario. 0 = sin alerta.",
    )

    l10n_pe_ne_unit_code = fields.Char(
        string='Unidad SUNAT (cat.03)',
        help="Código de unidad de medida SUNAT (Catálogo 03, ej. NIU, KGM, LTR) que se usa al "
             "facturar este producto. Si está vacío, el comprobante usa 'NIU' (unidad).")

    l10n_pe_ne_cod_producto_sunat = fields.Char(
        string="Cód. producto SUNAT (cat.25)",
        help="Código de producto SUNAT (UNSPSC, catálogo 25). Aparece en la guía como "
             "bien normalizado.")

    # Fraccionamiento (farma/perecibles): el producto se compra/stockea en un empaque (caja,
    # blíster) pero se puede vender por unidad. `unidades_por_empaque` es el factor (ej. 30
    # tabletas por caja); `unidad_fraccion` es el código SUNAT de la sub-unidad al vender
    # fraccionado (ej. NIU). 0 = no fraccionable.
    l10n_pe_ne_unidades_por_empaque = fields.Float(
        string="Unidades por empaque", digits=(12, 4), default=0.0,
        help="Fraccionamiento: cuántas unidades trae el empaque (caja/blíster). Al vender "
             "fraccionado, el stock descuenta cantidad/este factor del empaque. 0 = no fracciona.")
    l10n_pe_ne_unidad_fraccion = fields.Char(
        string="Unidad de fracción (cat.03)",
        help="Código SUNAT de la sub-unidad al vender fraccionado (ej. NIU). Vacío = NIU.")

    l10n_pe_ne_registro_sanitario = fields.Char(
        string="Registro sanitario (DIGEMID)",
        help="Farma: código de registro sanitario DIGEMID del producto. Si está, se anota en la "
             "descripción del ítem del comprobante (trazabilidad). Vacío = no se emite.")

    l10n_pe_ne_controlado = fields.Boolean(
        string="Producto controlado (psicotrópico/estupefaciente)",
        help="Farma: sustancia controlada por DIGEMID. Su venta EXIGE receta retenida (número + "
             "colegiatura del médico); se bloquea la emisión sin esos datos y se anotan en la línea.")

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

    @api.constrains("l10n_pe_ne_stock_minimo")
    def _check_stock_minimo(self):
        # Defensa en profundidad: la API y el import ya validan >= 0, pero un write directo no
        # pasa por esos caminos. El modelo es el único lugar que ningún caller puede saltarse.
        for rec in self:
            if rec.l10n_pe_ne_stock_minimo < 0:
                raise ValidationError(_("El stock mínimo no puede ser negativo."))

    @api.constrains("l10n_pe_ne_percepcion_tasa")
    def _check_percepcion_tasa(self):
        # Defensa en profundidad: el import masivo y la API de productos ya validan el rango
        # 0-10 antes de llegar acá, pero un write directo (shell, otra vía) no pasa por esos
        # caminos. El modelo es el único lugar que ningún caller puede saltarse.
        for rec in self:
            if rec.l10n_pe_ne_percepcion_tasa and not (0 <= rec.l10n_pe_ne_percepcion_tasa <= 10):
                raise ValidationError(_("La percepción sugerida debe estar entre 0 y 10%."))

# -*- coding: utf-8 -*-
"""Nota de venta (NE Express) — venta REAL cobrada SIN comprobante SUNAT. NO es CPE: no firma
XML, no va a SUNAT, no consume correlativo fiscal. Documento interno de control (sin valor
tributario). A diferencia de la cotización (una oferta), es una venta cobrada: alimenta la caja.
Modelo propio simplificado (misma filosofía que l10n_pe_ne.cotizacion / l10n_pe_ne.gasto): TODA
la lógica —CRUD, totales, serialización, PDF— vive en el addon; React solo llama /ne/api."""

from odoo import _, api, fields, models

IGV_RATE = 0.18  # IGV Perú 18%


class L10nPeNeNotaVenta(models.Model):
    _name = 'l10n_pe_ne.nota_venta'
    _description = 'Nota de venta (NE Express)'
    _order = 'fecha desc, id desc'

    name = fields.Char(string='Número', required=True, copy=False, readonly=True,
                       default=lambda s: _('Nueva'))
    # Cliente OPCIONAL: muchas ventas sin comprobante son al paso. Sin partner se usa cliente_nombre
    # (o "Cliente varios" en el impreso). A diferencia de la cotización (cliente obligatorio).
    partner_id = fields.Many2one('res.partner', string='Cliente', index=True)
    cliente_nombre = fields.Char(string='Cliente (texto)')
    fecha = fields.Date(string='Fecha', required=True, default=fields.Date.context_today)
    estado = fields.Selection([
        ('borrador', 'Borrador'),
        ('registrada', 'Registrada'),
        ('convertida', 'Convertida'),
        ('anulada', 'Anulada'),
    ], string='Estado', default='borrador', required=True)
    # Medios de pago del cobro (mismo shape que account.move.l10n_pe_ne_medios_pago) + redondeo de
    # efectivo (≤ 0, dato de caja). NO hay XML: son dato de caja/impreso.
    medios_pago = fields.Json(string='Medios de pago', copy=False)
    redondeo = fields.Monetary(string='Redondeo efectivo', currency_field='currency_id',
                               help='Ajuste (≤ 0) del efectivo cobrado por redondeo al décimo.')
    comprobante_id = fields.Many2one('account.move', string='Comprobante emitido', copy=False,
                                     index=True, help='Comprobante generado al convertir la nota.')
    caja_sesion_id = fields.Many2one('l10n_pe_ne.caja.sesion', string='Sesión de caja',
                                     copy=False, index=True)
    notas = fields.Text(string='Notas')
    currency_id = fields.Many2one('res.currency', required=True,
                                  default=lambda s: s.env.company.currency_id)
    company_id = fields.Many2one('res.company', required=True, index=True,
                                 default=lambda s: s.env.company)
    line_ids = fields.One2many('l10n_pe_ne.nota_venta.line', 'nota_venta_id',
                               string='Líneas', copy=True)
    amount_untaxed = fields.Monetary(string='Valor venta', compute='_compute_amounts',
                                     store=True, currency_field='currency_id')
    amount_tax = fields.Monetary(string='IGV', compute='_compute_amounts', store=True,
                                 currency_field='currency_id')
    amount_total = fields.Monetary(string='Total', compute='_compute_amounts', store=True,
                                   currency_field='currency_id')
    amount_op_gravada = fields.Monetary(string='Op. gravada', compute='_compute_amounts',
                                        store=True, currency_field='currency_id')
    amount_op_no_gravada = fields.Monetary(string='Op. exonerada/inafecta',
                                           compute='_compute_amounts', store=True,
                                           currency_field='currency_id')

    @api.depends('line_ids.subtotal', 'line_ids.afecto_igv')
    def _compute_amounts(self):
        # Espejo EXACTO de l10n_pe_ne.cotizacion._compute_amounts (precio unitario CON IGV: el
        # subtotal de línea ya es el bruto que paga el cliente; el gravado se descompone en
        # base (bruto/1.18) + IGV; lo no gravado ya es base. Total == suma de brutos).
        for nv in self:
            bruto_gravado = sum(l.subtotal for l in nv.line_ids if l.afecto_igv)
            no_gravado = sum(l.subtotal for l in nv.line_ids if not l.afecto_igv)
            base_gravado = round(bruto_gravado / (1 + IGV_RATE), 2)
            nv.amount_total = round(bruto_gravado + no_gravado, 2)
            nv.amount_tax = round(bruto_gravado - base_gravado, 2)
            nv.amount_untaxed = round(nv.amount_total - nv.amount_tax, 2)
            nv.amount_op_gravada = base_gravado
            nv.amount_op_no_gravada = round(no_gravado, 2)

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if not vals.get('name') or vals.get('name') == _('Nueva'):
                vals['name'] = self.env['ir.sequence'].next_by_code('l10n_pe.ne.nota_venta') or _('Nueva')
        return super().create(vals_list)


class L10nPeNeNotaVentaLine(models.Model):
    _name = 'l10n_pe_ne.nota_venta.line'
    _description = 'Línea de nota de venta (NE Express)'
    _order = 'id'

    nota_venta_id = fields.Many2one('l10n_pe_ne.nota_venta', string='Nota de venta',
                                    required=True, ondelete='cascade', index=True)
    product_id = fields.Many2one('product.product', string='Producto')
    descripcion = fields.Char(string='Descripción', required=True)
    cantidad = fields.Float(string='Cantidad', default=1.0)
    precio_unitario = fields.Monetary(string='P. unitario', currency_field='currency_id')
    descuento = fields.Float(string='Descuento %', default=0.0,
                             help='Descuento porcentual aplicado a la línea (0–100).')
    afecto_igv = fields.Boolean(string='Afecto a IGV', default=True)
    subtotal = fields.Monetary(string='Subtotal', compute='_compute_subtotal', store=True,
                               currency_field='currency_id')
    currency_id = fields.Many2one(related='nota_venta_id.currency_id', store=True)
    company_id = fields.Many2one(related='nota_venta_id.company_id', store=True, index=True)

    @api.depends('cantidad', 'precio_unitario', 'descuento')
    def _compute_subtotal(self):
        for line in self:
            bruto = (line.cantidad or 0.0) * (line.precio_unitario or 0.0)
            factor = 1.0 - min(max(line.descuento or 0.0, 0.0), 100.0) / 100.0
            line.subtotal = round(bruto * factor, 2)

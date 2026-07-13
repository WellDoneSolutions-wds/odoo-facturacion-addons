# -*- coding: utf-8 -*-
"""Cotización (NE Express) — documento comercial (proforma/presupuesto) para enviar al
cliente. NO es comprobante electrónico: no firma XML, no va a SUNAT, no consume
correlativo fiscal. Es "vender sin emitir": se cotiza el producto y se envía el PDF.

Modelo propio simplificado (misma filosofía que l10n_pe_ne.gasto): TODA la lógica
—CRUD, totales, serialización, PDF— vive en el addon; React solo llama /ne/api.
Aislado por compañía (regla multi-compañía global en security). El PDF se genera con
un reporte QWeb nativo de Odoo (report/cotizacion_report.xml)."""
import base64

from odoo import _, api, fields, models
from odoo.exceptions import UserError

IGV_RATE = 0.18  # IGV Perú 18%


class L10nPeNeCotizacion(models.Model):
    _name = 'l10n_pe_ne.cotizacion'
    _description = 'Cotización (NE Express)'
    _order = 'fecha desc, id desc'

    name = fields.Char(string='Número', required=True, copy=False, readonly=True,
                       default=lambda s: _('Nueva'))
    partner_id = fields.Many2one('res.partner', string='Cliente', required=True, index=True)
    fecha = fields.Date(string='Fecha', required=True, default=fields.Date.context_today)
    validez_dias = fields.Integer(string='Validez (días)', default=15,
                                  help='Días que la cotización se mantiene vigente.')
    estado = fields.Selection([
        ('borrador', 'Borrador'),
        ('enviada', 'Enviada'),
        ('aceptada', 'Aceptada'),
        ('rechazada', 'Rechazada'),
        ('convertida', 'Convertida'),
    ], string='Estado', default='borrador', required=True)
    comprobante_id = fields.Many2one('account.move', string='Comprobante emitido',
                                     copy=False, index=True,
                                     help='Comprobante generado al convertir esta cotización.')
    notas = fields.Text(string='Notas / condiciones')
    forma_pago = fields.Char(string='Forma de pago', help='p.ej. Contado, Crédito 30 días.')
    tiempo_entrega = fields.Char(string='Tiempo de entrega', help='p.ej. 5 días hábiles.')
    garantia = fields.Char(string='Garantía')
    currency_id = fields.Many2one('res.currency', required=True,
                                  default=lambda s: s.env.company.currency_id)
    company_id = fields.Many2one('res.company', required=True, index=True,
                                 default=lambda s: s.env.company)
    line_ids = fields.One2many('l10n_pe_ne.cotizacion.line', 'cotizacion_id',
                               string='Líneas', copy=True)
    amount_untaxed = fields.Monetary(string='Valor venta', compute='_compute_amounts',
                                     store=True, currency_field='currency_id')
    amount_tax = fields.Monetary(string='IGV', compute='_compute_amounts', store=True,
                                 currency_field='currency_id')
    amount_total = fields.Monetary(string='Total', compute='_compute_amounts', store=True,
                                   currency_field='currency_id')
    # Desglose para la representación impresa (op. gravada base vs op. exonerada/inafecta).
    amount_op_gravada = fields.Monetary(string='Op. gravada', compute='_compute_amounts',
                                        store=True, currency_field='currency_id')
    amount_op_no_gravada = fields.Monetary(string='Op. exonerada/inafecta',
                                           compute='_compute_amounts', store=True,
                                           currency_field='currency_id')

    @api.depends('line_ids.subtotal', 'line_ids.afecto_igv')
    def _compute_amounts(self):
        for cot in self:
            # El precio unitario es CON IGV: el subtotal de línea ya es el importe bruto
            # (lo que paga el cliente). Para el desglose, el gravado se descompone en base
            # (bruto/1.18) + IGV; lo no gravado ya es base. Así el Total == suma de brutos.
            bruto_gravado = sum(l.subtotal for l in cot.line_ids if l.afecto_igv)
            no_gravado = sum(l.subtotal for l in cot.line_ids if not l.afecto_igv)
            base_gravado = round(bruto_gravado / (1 + IGV_RATE), 2)
            cot.amount_total = round(bruto_gravado + no_gravado, 2)
            cot.amount_tax = round(bruto_gravado - base_gravado, 2)
            cot.amount_untaxed = round(cot.amount_total - cot.amount_tax, 2)
            cot.amount_op_gravada = base_gravado
            cot.amount_op_no_gravada = round(no_gravado, 2)

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if not vals.get('name') or vals.get('name') == _('Nueva'):
                vals['name'] = self.env['ir.sequence'].next_by_code('l10n_pe.ne.cotizacion') or _('Nueva')
        return super().create(vals_list)

    # ---------------------------------------------------------- serialización
    def _l10n_pe_ne_cotizacion_dict(self):
        self.ensure_one()
        return {
            'id': self.id,
            'numero': self.name,
            'cliente': self.partner_id.name or '',
            'clienteId': self.partner_id.id,
            'clienteDoc': self.partner_id.vat or '',
            'fecha': self.fecha.strftime('%Y-%m-%d') if self.fecha else '',
            'validezDias': self.validez_dias,
            'estado': self.estado,
            'moneda': self.currency_id.name or 'PEN',
            'total': self.amount_total,
            'items': len(self.line_ids),
            'comprobanteId': self.comprobante_id.id if self.comprobante_id else None,
            'comprobanteNumero': self._l10n_pe_ne_comprobante_numero(),
        }

    def _l10n_pe_ne_comprobante_numero(self):
        """'serie-correlativo' del comprobante vinculado (o '' si no hay)."""
        m = self.comprobante_id
        if not m:
            return ''
        serie = m.l10n_pe_ne_serie_emit or m.l10n_pe_serie or ''
        corr = m.l10n_pe_ne_corr_emit or ''
        return ('%s-%s' % (serie, corr)) if (serie or corr) else (m.name or '')

    def l10n_pe_ne_vincular_comprobante(self, comprobante_id):
        """Vincula el comprobante emitido y marca la cotización como 'convertida'.
        Lo llama l10n_pe_ne_quick_emit cuando la emisión vino de 'Convertir a comprobante'."""
        self.ensure_one()
        self.write({'comprobante_id': int(comprobante_id), 'estado': 'convertida'})
        return self._l10n_pe_ne_cotizacion_dict()

    def l10n_pe_ne_cotizacion_detalle(self):
        """Detalle completo para la vista/PDF: cabecera + líneas + totales."""
        self.ensure_one()
        return {
            **self._l10n_pe_ne_cotizacion_dict(),
            'notas': self.notas or '',
            'formaPago': self.forma_pago or '',
            'tiempoEntrega': self.tiempo_entrega or '',
            'garantia': self.garantia or '',
            'valorVenta': self.amount_untaxed,
            'igv': self.amount_tax,
            'opGravada': self.amount_op_gravada,
            'opNoGravada': self.amount_op_no_gravada,
            'lineas': [{
                'descripcion': l.descripcion or (l.product_id.display_name or ''),
                'cantidad': l.cantidad,
                'precio': l.precio_unitario,
                'descuento': l.descuento,
                'subtotal': l.subtotal,
                'afectoIgv': l.afecto_igv,
                # Unidad SUNAT derivada del producto (la cotización no la almacena; se usa
                # para mostrar en la vista/PDF y da paridad con el comprobante que se emita).
                'unidad': l.product_id.l10n_pe_ne_unit_code or 'NIU',
                # productId/codigo: para que "Convertir a comprobante" reuse el producto del
                # catálogo. Sin esto la línea llegaba a Emitir sin productId y la emisión se
                # bloqueaba ("Elige el producto del catálogo o créalo con Crear producto").
                'productId': l.product_id.id or None,
                'codigo': l.product_id.default_code or '',
            } for l in self.line_ids],
        }

    # ------------------------------------------------------------- API React
    @api.model
    def l10n_pe_ne_list_cotizaciones(self, query=None, limit=100, offset=None):
        """Lista de cotizaciones (para la UI). Paginación opt-in: con `offset`
        devuelve {items, total}; sin él, lista plana."""
        domain = []
        if query:
            q = query.strip()
            domain += ['|', '|',
                       ('name', 'ilike', q),
                       ('partner_id.name', 'ilike', q),
                       ('partner_id.vat', 'ilike', q)]
        recs = self.search(domain, order='fecha desc, id desc', limit=limit, offset=offset or 0)
        items = [c._l10n_pe_ne_cotizacion_dict() for c in recs]
        if offset is None:
            return items
        return {'items': items, 'total': self.search_count(domain)}

    def _l10n_pe_ne_build_lines(self, lineas):
        """Traduce las líneas simplificadas de React a comandos O2M de Odoo."""
        vals = []
        for it in (lineas or []):
            desc = (it.get('descripcion') or '').strip()
            prod = False
            if it.get('productId'):
                prod = self.env['product.product'].browse(int(it['productId'])).exists()
                if prod and not desc:
                    desc = prod.display_name
            if not desc:
                raise UserError(_('Cada ítem necesita una descripción (o un producto).'))
            vals.append((0, 0, {
                'product_id': prod.id if prod else False,
                'descripcion': desc,
                'cantidad': float(it.get('cantidad') or 1),
                'precio_unitario': float(it.get('precio') or 0),
                'descuento': float(it.get('descuento') or 0),
                'afecto_igv': bool(it.get('afectoIgv', True)),
            }))
        return vals

    @staticmethod
    def _l10n_pe_ne_condiciones_vals(payload):
        """Extrae las condiciones comerciales del payload (formaPago/tiempoEntrega/garantia)
        como vals de escritura; solo incluye las claves presentes."""
        vals = {}
        for key, field in (('formaPago', 'forma_pago'),
                           ('tiempoEntrega', 'tiempo_entrega'),
                           ('garantia', 'garantia')):
            if key in payload:
                vals[field] = (payload.get(key) or '').strip() or False
        return vals

    def _l10n_pe_ne_resolve_partner(self, payload):
        partner = False
        if payload.get('clienteId'):
            partner = self.env['res.partner'].browse(int(payload['clienteId'])).exists()
        if not partner and payload.get('cliente'):
            # Reusa el alta rápida de cliente de account.move (no duplica el padrón).
            partner = self.env['account.move']._l10n_pe_ne_quick_partner(payload['cliente'])
            if partner and not partner.customer_rank:
                partner.customer_rank = 1
        if not partner:
            raise UserError(_('Indica el cliente de la cotización.'))
        return partner

    @api.model
    def l10n_pe_ne_quick_cotizar(self, payload):
        """Crea una cotización (borrador) desde el payload de React:
        {clienteId | cliente:{...}, items:[{productId|descripcion, cantidad, precio,
        afectoIgv}], fecha, validezDias, notas}."""
        payload = payload or {}
        partner = self._l10n_pe_ne_resolve_partner(payload)
        lines = self._l10n_pe_ne_build_lines(payload.get('items') or payload.get('lineas'))
        if not lines:
            raise UserError(_('La cotización necesita al menos un ítem.'))
        cot = self.create({
            # Ancla explícita del company_id a self.env.company — la MISMA fuente que usa
            # la factura (que deriva su compañía del diario de ventas de self.env.company).
            # Sin esto, la cotización dependía del default del campo y podía quedar bajo una
            # compañía distinta a la de los comprobantes; entonces la regla multi-compañía
            # (company_id ∈ company_ids) la ocultaba de la lista aunque el POST devolviera OK
            # (se creaba, pero "invisible"). Anclándola igual que la factura, la cotización
            # queda siempre en la misma compañía del emisor y se ve donde se ven las facturas.
            'company_id': self.env.company.id,
            'partner_id': partner.id,
            'fecha': payload.get('fecha') or fields.Date.context_today(self),
            'validez_dias': int(payload.get('validezDias') or 15),
            'notas': payload.get('notas') or False,
            'line_ids': lines,
            **self._l10n_pe_ne_condiciones_vals(payload),
        })
        return cot._l10n_pe_ne_cotizacion_dict()

    @api.model
    def l10n_pe_ne_update_cotizacion(self, payload):
        """Reemplaza cabecera + líneas de una cotización existente (por id)."""
        payload = payload or {}
        cot = self.browse(int(payload.get('id') or 0)).exists()
        if not cot:
            raise UserError(_('Cotización no encontrada.'))
        vals = {}
        if payload.get('clienteId') or payload.get('cliente'):
            vals['partner_id'] = self._l10n_pe_ne_resolve_partner(payload).id
        if payload.get('fecha'):
            vals['fecha'] = payload['fecha']
        if payload.get('validezDias') is not None:
            vals['validez_dias'] = int(payload['validezDias'])
        if 'notas' in payload:
            vals['notas'] = payload.get('notas') or False
        vals.update(self._l10n_pe_ne_condiciones_vals(payload))
        if payload.get('estado'):
            vals['estado'] = payload['estado']
        if payload.get('items') is not None or payload.get('lineas') is not None:
            vals['line_ids'] = [(5, 0, 0)] + self._l10n_pe_ne_build_lines(
                payload.get('items') or payload.get('lineas'))
        cot.write(vals)
        return cot._l10n_pe_ne_cotizacion_dict()

    def l10n_pe_ne_set_estado(self, estado):
        """Cambia el estado (borrador/enviada/aceptada/rechazada)."""
        self.ensure_one()
        valid = dict(self._fields['estado'].selection)
        if estado not in valid:
            raise UserError(_('Estado no válido.'))
        self.estado = estado
        return self._l10n_pe_ne_cotizacion_dict()

    @api.model
    def l10n_pe_ne_delete_cotizacion(self, rec_id):
        cot = self.browse(int(rec_id or 0)).exists()
        if cot:
            cot.unlink()
        return {'ok': True, 'modo': 'eliminado'}

    def l10n_pe_ne_importe_en_letras(self):
        """Importe total en letras, formato peruano estándar: 'OCHO CON 50/100 SOLES'.
        Se usa en la representación impresa (el 'SON:'). Degradación segura: si num2words
        falla, cae al amount_to_text nativo de la moneda."""
        self.ensure_one()
        entero = int(self.amount_total)
        centimos = int(round((self.amount_total - entero) * 100))
        moneda = {'PEN': 'SOLES', 'USD': 'DÓLARES AMERICANOS'}.get(
            self.currency_id.name, self.currency_id.name or '')
        try:
            from num2words import num2words  # Odoo ya depende de num2words
            letras = num2words(entero, lang='es').upper()
        except Exception:  # noqa: BLE001
            return self.currency_id.amount_to_text(self.amount_total)
        return "%s CON %02d/100 %s" % (letras, centimos, moneda)

    def l10n_pe_ne_get_pdf_b64(self):
        """Renderiza el PDF (reporte QWeb) y lo devuelve en base64."""
        self.ensure_one()
        pdf, _ctype = self.env['ir.actions.report']._render_qweb_pdf(
            'l10n_pe_ne_biller.action_report_cotizacion', res_ids=self.ids)
        return base64.b64encode(pdf).decode()


class L10nPeNeCotizacionLine(models.Model):
    _name = 'l10n_pe_ne.cotizacion.line'
    _description = 'Línea de cotización (NE Express)'
    _order = 'id'

    cotizacion_id = fields.Many2one('l10n_pe_ne.cotizacion', string='Cotización',
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
    currency_id = fields.Many2one(related='cotizacion_id.currency_id', store=True)
    company_id = fields.Many2one(related='cotizacion_id.company_id', store=True, index=True)

    @api.depends('cantidad', 'precio_unitario', 'descuento')
    def _compute_subtotal(self):
        for line in self:
            bruto = (line.cantidad or 0.0) * (line.precio_unitario or 0.0)
            factor = 1.0 - min(max(line.descuento or 0.0, 0.0), 100.0) / 100.0
            line.subtotal = round(bruto * factor, 2)

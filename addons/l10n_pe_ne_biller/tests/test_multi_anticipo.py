from odoo.tests import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestMultiAnticipo(TransactionCase):
    """Regularización de VARIOS anticipos en una misma factura final (pagos escalonados).
    SUNAT lo soporta (N AdditionalDocumentReference + N PrepaidPayment, numIdeAnticipo 1..N).
    El dato vive en una lista JSON; el saldo de cada anticipo (doc. A) suma los `monto` de la
    lista de todas las regularizaciones que lo enlazan por `origenId`."""

    def setUp(self):
        super().setUp()
        self.company = self.env.company
        self.igv = self.env['account.tax'].search([
            ('company_id', '=', self.company.id), ('type_tax_use', '=', 'sale'),
            ('l10n_pe_edi_tax_code', '=', '1000')], limit=1)
        ruc_type = self.env['l10n_latam.identification.type'].search(
            [('l10n_pe_vat_code', '=', '6')], limit=1)
        self.partner = self.env['res.partner'].create({
            'name': 'CLIENTE SAC', 'vat': '20100070970',
            'l10n_latam_identification_type_id': ruc_type.id})
        self.product = self.env['product.product'].create({'name': 'SERVICIO', 'default_code': 'S1'})

    def _venta(self, anticipos=None, precio=1000.0):
        vals = {
            'move_type': 'out_invoice', 'partner_id': self.partner.id, 'invoice_date': '2026-06-20',
            'l10n_pe_serie': 'F001', 'l10n_pe_correlativo': '9',
            'invoice_line_ids': [(0, 0, {'product_id': self.product.id, 'quantity': 1.0,
                                         'price_unit': precio, 'tax_ids': [(6, 0, self.igv.ids)]})]}
        if anticipos is not None:
            vals['l10n_pe_ne_anticipos'] = anticipos
        m = self.env['account.move'].create(vals)
        m.action_post()
        return m

    def test_lista_normalizada(self):
        m = self._venta(anticipos=[
            {'doc': 'F001-00000100', 'monto': 236.0, 'tipo': '02'},
            {'doc': 'F001-00000101', 'monto': 118.0, 'tipo': '02'},
        ])
        lst = m._l10n_pe_ne_anticipos_list()
        self.assertEqual(len(lst), 2)
        self.assertEqual(lst[0]['doc'], 'F001-00000100')
        self.assertEqual(lst[0]['monto'], 236.0)
        self.assertEqual([a['monto'] for a in lst], [236.0, 118.0])

    def test_sin_anticipos_lista_vacia(self):
        self.assertEqual(self._venta()._l10n_pe_ne_anticipos_list(), [])

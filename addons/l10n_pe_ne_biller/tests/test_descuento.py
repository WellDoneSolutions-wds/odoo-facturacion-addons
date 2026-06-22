from odoo.tests import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestBillerDescuento(TransactionCase):
    """Descuento por ítem: la línea va neta (IGV sobre el neto) y el descuento se muestra explícito
    en adicionalDetalle (cat. 53 código 00)."""

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
        self.product = self.env['product.product'].create({'name': 'PROD', 'default_code': 'P1'})

    def _move(self, discount):
        move = self.env['account.move'].create({
            'move_type': 'out_invoice', 'partner_id': self.partner.id, 'invoice_date': '2026-06-20',
            'l10n_pe_serie': 'F001', 'l10n_pe_correlativo': '1',
            'invoice_line_ids': [(0, 0, {'product_id': self.product.id, 'quantity': 1.0,
                                         'price_unit': 500.0, 'discount': discount,
                                         'tax_ids': [(6, 0, self.igv.ids)]})]})
        move.action_post()
        return move

    def test_descuento_item(self):
        payload = self._move(10.0)._l10n_pe_build_invoice_request()
        d = payload['detalle'][0]
        self.assertEqual(d['mtoValorUnitario'], '500.00')    # unitario BRUTO (regla 3271)
        self.assertEqual(d['mtoValorVentaItem'], '450.00')   # neto (500 - 10%)
        self.assertEqual(d['mtoBaseIgvItem'], '450.00')
        self.assertEqual(d['mtoIgvItem'], '81.00')           # IGV sobre el neto
        self.assertEqual(len(payload['adicionalDetalle']), 1)
        a = payload['adicionalDetalle'][0]
        self.assertEqual(a['idLinea'], '1')
        self.assertEqual(a['nomPropiedad'], '-')             # salta el bloque AdditionalItemProperty
        self.assertEqual(a['codTipoVariable'], '00')
        self.assertEqual(a['porVariable'], '0.10')
        self.assertEqual(a['mtoVariable'], '50.00')          # descuento
        self.assertEqual(a['mtoBaseImpVariable'], '500.00')  # base bruta

    def test_sin_descuento(self):
        payload = self._move(0.0)._l10n_pe_build_invoice_request()
        self.assertEqual(payload['adicionalDetalle'], [])
        self.assertEqual(payload['detalle'][0]['mtoValorVentaItem'], '500.00')

from odoo.tests import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestBillerMapper(TransactionCase):
    def setUp(self):
        super().setUp()
        self.company = self.env.company
        self.igv = self.env['account.tax'].search([
            ('company_id', '=', self.company.id), ('type_tax_use', '=', 'sale'),
            ('l10n_pe_edi_tax_code', '=', '1000')], limit=1)
        self.assertTrue(self.igv, "IGV (code 1000) no existe — correr setup_company.py (Task 2)")
        ruc_type = self.env['l10n_latam.identification.type'].search(
            [('l10n_pe_vat_code', '=', '6')], limit=1)
        self.partner = self.env['res.partner'].create({
            'name': 'CLIENTE SAC', 'vat': '20605145648',
            'l10n_latam_identification_type_id': ruc_type.id})
        self.product = self.env['product.product'].create(
            {'name': 'DESARMADOR', 'default_code': 'P001'})

    def _make_invoice(self):
        move = self.env['account.move'].create({
            'move_type': 'out_invoice', 'partner_id': self.partner.id,
            'invoice_date': '2026-06-19', 'l10n_pe_serie': 'F001', 'l10n_pe_correlativo': '1',
            'invoice_line_ids': [(0, 0, {
                'product_id': self.product.id, 'quantity': 1.0, 'price_unit': 7.20,
                'tax_ids': [(6, 0, self.igv.ids)]})]})
        move.action_post()
        return move

    def test_invoice_request_structure(self):
        payload = self._make_invoice()._l10n_pe_build_invoice_request()
        self.assertEqual(payload['id']['documentType'], '01')
        self.assertEqual(payload['id']['ruc'], self.company.vat)
        self.assertEqual(payload['id']['serie'], 'F001')
        self.assertEqual(payload['id']['correlativo'], '00000001')
        self.assertEqual(payload['cabecera']['tipDocUsuario'], '6')
        self.assertEqual(payload['cabecera']['numDocUsuario'], '20605145648')
        self.assertEqual(payload['cabecera']['sumTotValVenta'], '7.20')
        self.assertEqual(payload['cabecera']['sumTotTributos'], '1.30')
        self.assertEqual(payload['cabecera']['sumImpVenta'], '8.50')
        self.assertEqual(len(payload['detalle']), 1)
        d = payload['detalle'][0]
        self.assertEqual(d['mtoValorUnitario'], '7.20')
        self.assertEqual(d['mtoBaseIgvItem'], '7.20')
        self.assertEqual(d['mtoIgvItem'], '1.30')
        self.assertEqual(d['mtoPrecioVentaUnitario'], '8.50')
        self.assertEqual(d['porIgvItem'], '18.00')
        self.assertEqual(payload['tributos'][0]['mtoTributo'], '1.30')
        self.assertEqual(payload['leyendas'][0]['codLeyenda'], '1000')
        self.assertEqual(payload['leyendas'][0]['desLeyenda'], 'OCHO CON 50/100 SOLES')

    def test_emisor_desde_company(self):
        """El bloque emisor lleva los datos de empresa de res.company (no secretos)."""
        payload = self._make_invoice()._l10n_pe_build_invoice_request()
        emisor = payload['emisor']
        self.assertEqual(emisor['razonSocial'], self.company.name)
        self.assertEqual(emisor['nombreComercial'], self.company.name)
        # La dirección es todo-o-nada: solo va si la compañía tiene distrito (ubigeo) configurado.
        if 'direccion' in emisor:
            for k in ('ubigeo', 'direccion', 'departamento', 'provincia', 'distrito', 'urbanizacion'):
                self.assertIn(k, emisor['direccion'])
        # Las credenciales/cert NUNCA viajan en el request.
        self.assertNotIn('credential', emisor)
        self.assertNotIn('keystore', emisor)

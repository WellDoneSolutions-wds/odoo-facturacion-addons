from odoo.tests import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestBillerDetraccion(TransactionCase):
    """Factura sujeta a detracción: tipOperacion 1001 + adicionalCabecera + leyenda 2006."""

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

    def test_detraccion(self):
        self.company.l10n_pe_ne_cuenta_detraccion = '00-000-000000'
        move = self.env['account.move'].create({
            'move_type': 'out_invoice', 'partner_id': self.partner.id, 'invoice_date': '2026-06-20',
            'l10n_pe_serie': 'F001', 'l10n_pe_correlativo': '1',
            'l10n_pe_ne_detraccion': True, 'l10n_pe_ne_detraccion_code': '037',
            'l10n_pe_ne_detraccion_rate': 12.0,
            'invoice_line_ids': [(0, 0, {'product_id': self.product.id, 'quantity': 1.0,
                                         'price_unit': 600.0, 'tax_ids': [(6, 0, self.igv.ids)]})]})
        move.action_post()
        payload = move._l10n_pe_build_invoice_request()
        cab = payload['cabecera']
        self.assertEqual(cab['tipOperacion'], '1001')
        adic = cab['adicionalCabecera']
        self.assertEqual(adic['codBienDetraccion'], '037')
        self.assertEqual(adic['porDetraccion'], '12.00')
        self.assertEqual(adic['mtoDetraccion'], '84.96')   # 12% de 708
        self.assertEqual(adic['ctaBancoNacionDetraccion'], '00-000-000000')
        self.assertEqual(adic['codMedioPago'], '001')
        self.assertIn('2006', {l['codLeyenda'] for l in payload['leyendas']})
        self.assertEqual(payload['datoPago']['mtoNetoPendientePago'], '708.00')

    def test_detraccion_cuenta_del_comprobante_gana(self):
        """La cuenta de detracción tecleada en el comprobante gana sobre la configurada
        en la empresa. Regresión: antes el cuentaBN del payload solo se guardaba en la
        empresa (y solo la primera vez), así que emisiones posteriores salían con la
        cuenta vieja en el PDF/XML."""
        self.company.l10n_pe_ne_cuenta_detraccion = 'CUENTA-EMPRESA-VIEJA'
        move = self.env['account.move'].create({
            'move_type': 'out_invoice', 'partner_id': self.partner.id, 'invoice_date': '2026-06-20',
            'l10n_pe_serie': 'F001', 'l10n_pe_correlativo': '9',
            'invoice_line_ids': [(0, 0, {'product_id': self.product.id, 'quantity': 1.0,
                                         'price_unit': 600.0, 'tax_ids': [(6, 0, self.igv.ids)]})]})
        move._l10n_pe_ne_quick_flags(move, {'detraccion': {
            'codBien': '037', 'tasa': 12, 'cuentaBN': 'CUENTA-NUEVA-123'}})
        # La cuenta queda EN el comprobante, no pisa la de la empresa (ya tenía una).
        self.assertEqual(move.l10n_pe_ne_detraccion_cuenta, 'CUENTA-NUEVA-123')
        self.assertEqual(self.company.l10n_pe_ne_cuenta_detraccion, 'CUENTA-EMPRESA-VIEJA')
        move.action_post()
        adic = move._l10n_pe_build_invoice_request()['cabecera']['adicionalCabecera']
        self.assertEqual(adic['ctaBancoNacionDetraccion'], 'CUENTA-NUEVA-123')

    def test_detraccion_cuenta_fija_default_empresa_si_vacia(self):
        """Si la empresa aún no tiene cuenta de detracción, la primera emisión la fija."""
        self.company.l10n_pe_ne_cuenta_detraccion = False
        move = self.env['account.move'].create({
            'move_type': 'out_invoice', 'partner_id': self.partner.id, 'invoice_date': '2026-06-20',
            'l10n_pe_serie': 'F001', 'l10n_pe_correlativo': '10',
            'invoice_line_ids': [(0, 0, {'product_id': self.product.id, 'quantity': 1.0,
                                         'price_unit': 600.0, 'tax_ids': [(6, 0, self.igv.ids)]})]})
        move._l10n_pe_ne_quick_flags(move, {'detraccion': {
            'codBien': '037', 'tasa': 12, 'cuentaBN': 'PRIMERA-CUENTA'}})
        self.assertEqual(move.l10n_pe_ne_detraccion_cuenta, 'PRIMERA-CUENTA')
        self.assertEqual(self.company.l10n_pe_ne_cuenta_detraccion, 'PRIMERA-CUENTA')

    def test_percepcion_en_factura(self):
        move = self.env['account.move'].create({
            'move_type': 'out_invoice', 'partner_id': self.partner.id, 'invoice_date': '2026-06-20',
            'l10n_pe_serie': 'F001', 'l10n_pe_correlativo': '3', 'l10n_pe_ne_percepcion': True,
            'invoice_line_ids': [(0, 0, {'product_id': self.product.id, 'quantity': 1.0,
                                         'price_unit': 600.0, 'tax_ids': [(6, 0, self.igv.ids)]})]})
        move.action_post()
        payload = move._l10n_pe_build_invoice_request()
        cab = payload['cabecera']
        self.assertEqual(cab['tipOperacion'], '2001')
        self.assertEqual(cab['adicionalCabecera']['mtoTotPercepcion'], '722.16')  # 708 + 2%
        vg = payload['variablesGlobales'][0]
        self.assertEqual(vg['codTipoVariableGlobal'], '51')
        self.assertEqual(vg['porVariableGlobal'], '0.02')
        self.assertEqual(vg['mtoVariableGlobal'], '14.16')          # 2% de 708
        self.assertEqual(vg['mtoBaseImpVariableGlobal'], '708.00')

    def test_sin_detraccion_no_cambia(self):
        move = self.env['account.move'].create({
            'move_type': 'out_invoice', 'partner_id': self.partner.id, 'invoice_date': '2026-06-20',
            'l10n_pe_serie': 'F001', 'l10n_pe_correlativo': '2',
            'invoice_line_ids': [(0, 0, {'product_id': self.product.id, 'quantity': 1.0,
                                         'price_unit': 600.0, 'tax_ids': [(6, 0, self.igv.ids)]})]})
        move.action_post()
        payload = move._l10n_pe_build_invoice_request()
        self.assertEqual(payload['cabecera']['tipOperacion'], '0101')
        self.assertNotIn('adicionalCabecera', payload['cabecera'])
        self.assertNotIn('mtoNetoPendientePago', payload['datoPago'])

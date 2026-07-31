from odoo.tests import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestAlquiler(TransactionCase):
    """Vertical Alquiler / rental: servicio de arrendamiento sujeto a detracción (SPOT) con el
    código cat.54 019 al 10%. El motor de detracción ya es genérico; este test fija que el
    arrendamiento emite ese código y tasa (el gesto propio de la vertical en el front es el preset
    de detracción 019 y el período del alquiler, ver lib/alquiler.ts)."""

    def setUp(self):
        super().setUp()
        self.company = self.env.company
        self.company.l10n_pe_ne_cuenta_detraccion = '00-000-000000'
        self.igv = self.env['account.tax'].search([
            ('company_id', '=', self.company.id), ('type_tax_use', '=', 'sale'),
            ('l10n_pe_edi_tax_code', '=', '1000')], limit=1)
        ruc_type = self.env['l10n_latam.identification.type'].search(
            [('l10n_pe_vat_code', '=', '6')], limit=1)
        self.partner = self.env['res.partner'].create({
            'name': 'INQUILINO SAC', 'vat': '20100070970',
            'l10n_latam_identification_type_id': ruc_type.id})
        self.product = self.env['product.product'].create({'name': 'ALQUILER LOCAL', 'default_code': 'ALQ'})

    def test_arrendamiento_detraccion_019(self):
        move = self.env['account.move'].create({
            'move_type': 'out_invoice', 'partner_id': self.partner.id, 'invoice_date': '2026-07-20',
            'l10n_pe_serie': 'F001', 'l10n_pe_correlativo': '7',
            'l10n_pe_ne_detraccion': True, 'l10n_pe_ne_detraccion_code': '019',
            'l10n_pe_ne_detraccion_rate': 10.0,
            'invoice_line_ids': [(0, 0, {
                'product_id': self.product.id, 'quantity': 1.0, 'price_unit': 3000.0,
                'name': 'ALQUILER LOCAL · Alquiler del 01/07/2026 al 31/07/2026',
                'l10n_pe_ne_unit_code': 'ZZ', 'tax_ids': [(6, 0, self.igv.ids)]})]})
        move.action_post()
        payload = move._l10n_pe_build_invoice_request()
        cab = payload['cabecera']
        self.assertEqual(cab['tipOperacion'], '1001')            # operación con detracción
        adic = cab['adicionalCabecera']
        self.assertEqual(adic['codBienDetraccion'], '019')       # arrendamiento de bienes
        self.assertEqual(adic['porDetraccion'], '10.00')
        self.assertEqual(adic['ctaBancoNacionDetraccion'], '00-000-000000')
        # El período del alquiler viaja en la descripción del ítem.
        self.assertIn('Alquiler del 01/07/2026 al 31/07/2026', payload['detalle'][0]['desItem'])

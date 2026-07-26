from odoo.tests import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestRazonSocialBoleta(TransactionCase):
    """Boleta ≤700: la razón social del comprobante puede ser un override (nombre de institución)
    sin renombrar el partner del DNI. En factura el override NO aplica (usa partner.name = padrón)."""

    def setUp(self):
        super().setUp()
        self.company = self.env.company
        self.igv = self.env['account.tax'].search([
            ('company_id', '=', self.company.id), ('type_tax_use', '=', 'sale'),
            ('l10n_pe_edi_tax_code', '=', '1000')], limit=1)
        dni_type = self.env['l10n_latam.identification.type'].search(
            [('l10n_pe_vat_code', '=', '1')], limit=1)
        self.partner = self.env['res.partner'].create({
            'name': 'JUAN PEREZ', 'vat': '12345678',
            'l10n_latam_identification_type_id': dni_type.id})
        self.product = self.env['product.product'].create({'name': 'CUADERNO', 'default_code': 'C1'})

    def _move(self, serie, **vals):
        base = {
            'move_type': 'out_invoice', 'partner_id': self.partner.id, 'invoice_date': '2026-06-20',
            'l10n_pe_serie': serie, 'l10n_pe_correlativo': '9',
            'invoice_line_ids': [(0, 0, {'product_id': self.product.id, 'quantity': 1.0,
                                         'price_unit': 50.0, 'tax_ids': [(6, 0, self.igv.ids)]})]}
        base.update(vals)
        move = self.env['account.move'].create(base)
        move.action_post()
        return move

    def test_boleta_usa_override_y_no_renombra_partner(self):
        m = self._move('B001', l10n_pe_ne_cliente_nombre='E.I.P PRIMARIA N 123')
        m.l10n_pe_ne_tipo_doc = '03'
        cab = m._l10n_pe_build_invoice_request()['cabecera']
        self.assertEqual(cab['rznSocialUsuario'], 'E.I.P PRIMARIA N 123')
        self.assertEqual(self.partner.name, 'JUAN PEREZ')  # NO renombrado

    def test_sin_override_usa_partner_name(self):
        cab = self._move('B001')._l10n_pe_build_invoice_request()['cabecera']
        self.assertEqual(cab['rznSocialUsuario'], 'JUAN PEREZ')

    def test_quick_flags_setea_override_solo_en_boleta(self):
        m03 = self._move('B001'); m03.l10n_pe_ne_tipo_doc = '03'
        self.env['account.move']._l10n_pe_ne_quick_flags(
            m03, {'tipoDoc': '03', 'cliente': {'razonSocial': 'INSTITUCION X'}})
        self.assertEqual(m03.l10n_pe_ne_cliente_nombre, 'INSTITUCION X')
        m01 = self._move('F001'); m01.l10n_pe_ne_tipo_doc = '01'
        self.env['account.move']._l10n_pe_ne_quick_flags(
            m01, {'tipoDoc': '01', 'cliente': {'razonSocial': 'INSTITUCION X'}})
        self.assertFalse(m01.l10n_pe_ne_cliente_nombre)  # factura: no override

from odoo.tests import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestVencimiento(TransactionCase):
    """fecVencimiento (cbc:DueDate) solo cuando es un vencimiento REAL (diferido). Odoo autopobla
    invoice_date_due = fecha de emisión; un contado sin plazo NO debe emitir vencimiento (evita el
    ruido 'Vencimiento: <fecha de emisión>' en el PDF de toda factura contado)."""

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

    def _move(self, **vals):
        base = {
            'move_type': 'out_invoice', 'partner_id': self.partner.id, 'invoice_date': '2026-06-20',
            'l10n_pe_serie': 'F001', 'l10n_pe_correlativo': '9',
            'invoice_line_ids': [(0, 0, {'product_id': self.product.id, 'quantity': 1.0,
                                         'price_unit': 500.0, 'tax_ids': [(6, 0, self.igv.ids)]})]}
        base.update(vals)
        move = self.env['account.move'].create(base)
        move.action_post()
        return move

    def test_contado_sin_plazo_no_emite_vencimiento(self):
        # contado, vencimiento = fecha de emisión (no diferido) → NO se emite fecVencimiento.
        cab = self._move(invoice_date_due='2026-06-20')._l10n_pe_build_invoice_request()['cabecera']
        self.assertEqual(cab['fecVencimiento'], '')

    def test_contado_con_plazo_diferido_se_emite(self):
        # contado con plazo posterior a la emisión → sí se emite.
        cab = self._move(invoice_date_due='2026-08-15')._l10n_pe_build_invoice_request()['cabecera']
        self.assertEqual(cab['fecVencimiento'], '2026-08-15')

    def test_credito_emite_vencimiento_aunque_sea_misma_fecha(self):
        # crédito: la forma de pago manda; se emite el vencimiento aunque coincida con la emisión.
        cab = self._move(l10n_pe_ne_forma_pago='Credito',
                         invoice_date_due='2026-06-20')._l10n_pe_build_invoice_request()['cabecera']
        self.assertEqual(cab['fecVencimiento'], '2026-06-20')

from odoo.tests import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestVencimiento(TransactionCase):
    """fecVencimiento (cbc:DueDate) es AUTOMÁTICO y siempre presente: al contado = la fecha de
    EMISIÓN (vence el mismo día); al crédito = la última cuota. No lo edita el emisor. El contado usa
    invoice_date explícito (no invoice_date_due, que Odoo autopobla con la fecha contable/HOY)."""

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

    def test_contado_emite_la_fecha_de_emision(self):
        # contado → el vencimiento es la propia fecha de emisión (vence el mismo día).
        cab = self._move()._l10n_pe_build_invoice_request()['cabecera']
        self.assertEqual(cab['fecVencimiento'], '2026-06-20')

    def test_contado_ignora_invoice_date_due_autopoblado(self):
        # Odoo pudo autopoblar invoice_date_due con otra fecha (contable/HOY); al contado se ignora:
        # el vencimiento siempre es la fecha de emisión.
        cab = self._move(invoice_date_due='2026-08-15')._l10n_pe_build_invoice_request()['cabecera']
        self.assertEqual(cab['fecVencimiento'], '2026-06-20')

    def test_credito_emite_la_ultima_cuota(self):
        # crédito → el vencimiento es la última cuota (invoice_date_due la fija quick_flags/cuotas).
        cab = self._move(l10n_pe_ne_forma_pago='Credito',
                         invoice_date_due='2026-09-30')._l10n_pe_build_invoice_request()['cabecera']
        self.assertEqual(cab['fecVencimiento'], '2026-09-30')

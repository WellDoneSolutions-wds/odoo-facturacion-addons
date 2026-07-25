from odoo.tests import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestObservacion(TransactionCase):
    """Observación general (print-only): narration desde el payload, formateada como
    'Observación: <texto>' para el ticket y el A4 (adicionalTxt). No va al XML firmado."""

    def setUp(self):
        super().setUp()
        self.igv = self.env['account.tax'].search([
            ('company_id', '=', self.env.company.id), ('type_tax_use', '=', 'sale'),
            ('l10n_pe_edi_tax_code', '=', '1000')], limit=1)
        ruc_type = self.env['l10n_latam.identification.type'].search(
            [('l10n_pe_vat_code', '=', '6')], limit=1)
        self.partner = self.env['res.partner'].create({
            'name': 'CLIENTE SAC', 'vat': '20100070970',
            'l10n_latam_identification_type_id': ruc_type.id})
        self.product = self.env['product.product'].create({'name': 'SERVICIO', 'default_code': 'S1'})

    def _move(self, **vals):
        base = {'move_type': 'out_invoice', 'partner_id': self.partner.id, 'invoice_date': '2026-06-20',
                'l10n_pe_serie': 'F001', 'l10n_pe_correlativo': '9',
                'invoice_line_ids': [(0, 0, {'product_id': self.product.id, 'quantity': 1.0,
                                             'price_unit': 100.0, 'tax_ids': [(6, 0, self.igv.ids)]})]}
        base.update(vals)
        m = self.env['account.move'].create(base); m.action_post(); return m

    def test_observacion_impresa_con_narration(self):
        m = self._move(narration='ENTREGA EN OBRA')
        self.assertEqual(m._l10n_pe_ne_observacion_impresa(), 'Observación: ENTREGA EN OBRA')

    def test_observacion_impresa_sin_narration_vacia(self):
        self.assertEqual(self._move()._l10n_pe_ne_observacion_impresa(), '')

    def test_observacion_impresa_sanitiza_html(self):
        m = self._move(narration='<p>NOTA</p>')
        self.assertEqual(m._l10n_pe_ne_observacion_impresa(), 'Observación: NOTA')

    def test_quick_flags_setea_narration(self):
        m = self._move()
        self.env['account.move']._l10n_pe_ne_quick_flags(m, {'observacion': 'PAGO EN 30 DIAS'})
        # narration es un campo Html del core (account.move): Odoo lo sanitiza y envuelve el
        # texto plano en <p>...</p>. Se verifica el contenido (no la igualdad exacta); la
        # representación impresa ya limpia las etiquetas vía _l10n_pe_ne_observacion_impresa().
        self.assertIn('PAGO EN 30 DIAS', m.narration or '')

    def test_ticket_adicional_usa_etiqueta_observacion(self):
        m = self._move(narration='REVISADO')
        self.assertIn('Observación: REVISADO', m._l10n_pe_ne_ticket_adicional())
        self.assertNotIn('Nota: REVISADO', m._l10n_pe_ne_ticket_adicional())

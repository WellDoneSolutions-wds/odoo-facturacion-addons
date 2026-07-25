from odoo.tests import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestPlaca(TransactionCase):
    """Placa del vehículo en factura de combustible → cac:AdditionalItemProperty cat-55 código 7000
    (Gastos Art. 37 Renta) en CADA línea. Solo factura. Vía la lista genérica adicionalDetalle."""

    def setUp(self):
        super().setUp()
        self.company = self.env.company
        self.igv = self.env['account.tax'].search([
            ('company_id', '=', self.company.id), ('type_tax_use', '=', 'sale'),
            ('l10n_pe_edi_tax_code', '=', '1000')], limit=1)
        IdType = self.env['l10n_latam.identification.type']
        ruc_type = IdType.search([('l10n_pe_vat_code', '=', '6')], limit=1)
        dni_type = IdType.search([('l10n_pe_vat_code', '=', '1')], limit=1)
        self.partner = self.env['res.partner'].create({
            'name': 'CLIENTE SAC', 'vat': '20100070970',
            'l10n_latam_identification_type_id': ruc_type.id})
        # Consumidor final con DNI: como en test_documents.py::test_boleta_dni, sin
        # l10n_latam_document_type_id elegido, _l10n_pe_document_type() cae al documento
        # de identidad y devuelve '03' de forma natural (no forzada).
        self.partner_dni = self.env['res.partner'].create({
            'name': 'CONSUMIDOR FINAL', 'vat': '12345678',
            'l10n_latam_identification_type_id': dni_type.id})
        self.product = self.env['product.product'].create({'name': 'DIESEL B5', 'default_code': 'D1'})

    def _move(self, **vals):
        base = {
            'move_type': 'out_invoice', 'partner_id': self.partner.id, 'invoice_date': '2026-06-20',
            'l10n_pe_serie': 'F001', 'l10n_pe_correlativo': '9',
            'invoice_line_ids': [(0, 0, {'product_id': self.product.id, 'quantity': 10.0,
                                         'price_unit': 18.0, 'tax_ids': [(6, 0, self.igv.ids)]})]}
        base.update(vals)
        move = self.env['account.move'].create(base)
        move.action_post()
        return move

    def test_placa_emite_propiedad_7000_por_linea(self):
        payload = self._move(l10n_pe_ne_placa='ABC-123')._l10n_pe_build_invoice_request()
        placas = [d for d in payload['adicionalDetalle'] if d.get('codPropiedad') == '7000']
        self.assertEqual(len(placas), 1)  # 1 línea
        self.assertEqual(placas[0]['valPropiedad'], 'ABC-123')
        self.assertEqual(placas[0]['codTipoVariable'], '-')
        self.assertEqual(placas[0]['idLinea'], '1')

    def test_sin_placa_no_emite_7000(self):
        payload = self._move()._l10n_pe_build_invoice_request()
        self.assertEqual([d for d in payload['adicionalDetalle'] if d.get('codPropiedad') == '7000'], [])

    def test_placa_no_se_emite_en_boleta(self):
        # Boleta REAL y aún no emitida: partner con DNI + serie B001, sin l10n_latam_document_type_id
        # elegido y SIN forzar l10n_pe_ne_tipo_doc (que en la vida real recién se congela al emitir,
        # ver _l10n_pe_apply_emission_response/_l10n_pe_apply_signed). _l10n_pe_document_type()
        # debe devolver '03' por su cuenta a partir del documento de identidad del partner.
        m = self._move(partner_id=self.partner_dni.id, l10n_pe_serie='B001',
                       l10n_pe_ne_placa='ABC-123')
        self.assertFalse(m.l10n_pe_ne_tipo_doc)  # todavía no se emitió: el campo sigue vacío
        self.assertEqual(m._l10n_pe_document_type(), '03')
        payload = m._l10n_pe_build_invoice_request()
        self.assertEqual([d for d in payload['adicionalDetalle'] if d.get('codPropiedad') == '7000'], [])

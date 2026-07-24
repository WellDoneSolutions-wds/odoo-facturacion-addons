import json

from odoo.tests import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestBillerDua(TransactionCase):
    """DUA/DAM de exportación (QA-023): se captura como dato del ERP y NO se envía a SUNAT en el
    XML de la factura. Aduanas numera la DUA DESPUÉS del comprobante comercial (por eso la
    exportación se emite sin ella, QA-024), así que el campo es opcional y queda editable con el
    comprobante ya emitido/posteado."""

    def setUp(self):
        super().setUp()
        self.company = self.env.company
        self.igv = self.env['account.tax'].search([
            ('company_id', '=', self.company.id), ('type_tax_use', '=', 'sale'),
            ('l10n_pe_edi_tax_code', '=', '1000')], limit=1)
        ruc_type = self.env['l10n_latam.identification.type'].search(
            [('l10n_pe_vat_code', '=', '6')], limit=1)
        self.partner = self.env['res.partner'].create({
            'name': 'IMPORTADORA GLOBAL INC', 'vat': '20100070970',
            'l10n_latam_identification_type_id': ruc_type.id})
        self.product = self.env['product.product'].create(
            {'name': 'CACAO EN GRANO', 'default_code': 'EXP1'})

    def _move(self):
        return self.env['account.move'].create({
            'move_type': 'out_invoice', 'partner_id': self.partner.id,
            'invoice_date': '2026-06-20', 'l10n_pe_serie': 'F001', 'l10n_pe_correlativo': '9',
            'invoice_line_ids': [(0, 0, {
                'product_id': self.product.id, 'quantity': 1, 'price_unit': 1000.0,
                'tax_ids': [(6, 0, self.igv.ids)]})]})

    def test_dua_se_guarda_desde_payload(self):
        """El nº de DUA del payload se persiste en el comprobante (con trim)."""
        move = self._move()
        move._l10n_pe_ne_quick_flags(move, {'dua': '  118-2026-10-123456  '})
        self.assertEqual(move.l10n_pe_ne_dua, '118-2026-10-123456')

    def test_sin_dua_no_rompe(self):
        """Sin DUA en el payload el campo queda vacío (la exportación se emite igual, QA-024)."""
        move = self._move()
        move._l10n_pe_ne_quick_flags(move, {})
        self.assertFalse(move.l10n_pe_ne_dua)

    def test_dua_no_va_al_xml(self):
        """La DUA NUNCA se filtra al payload que va al biller/SUNAT: es dato del ERP."""
        move = self._move()
        move.l10n_pe_ne_dua = '118-2026-10-123456'
        move.action_post()
        req = move._l10n_pe_build_invoice_request()
        self.assertNotIn('118-2026-10-123456', json.dumps(req, default=str))

    def test_dua_editable_tras_emitir(self):
        """Se puede asociar/cambiar la DUA con el comprobante ya posteado (QA-024)."""
        move = self._move()
        move.action_post()
        move.l10n_pe_ne_dua = '118-2026-10-999999'
        self.assertEqual(move.l10n_pe_ne_dua, '118-2026-10-999999')

    def test_dua_en_detalle(self):
        """El detalle del comprobante expone la DUA para que el front la muestre."""
        move = self._move()
        move.l10n_pe_ne_dua = '118-2026-10-123456'
        self.assertEqual(
            move.l10n_pe_ne_comprobante_detalle()['dua'], '118-2026-10-123456')

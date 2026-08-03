from odoo.tests import TransactionCase, tagged

from .common import EnvioSincronoMixin


@tagged('post_install', '-at_install')
class TestMaderaraTextil(EnvioSincronoMixin, TransactionCase):
    """Verticales «maderera / aserradero» (MTQ, volumen) y «textil / telas» (MTR + DZN, docena).

    Comparten con peso/ferretería la cantidad como MEDIDA fraccionaria; acá se fija el contrato de:
      * maderera — el volumen en m³ (MTQ) se emite y se descuenta del kardex a 3 decimales;
      * textil — la tela por metro (MTR) igual, y la docena emite con el código cat.03 **DZN**
        (no DPC): el front y el back deben coincidir en el mismo código (QA-021).
    """

    def setUp(self):
        super().setUp()
        self.company = self.env.company
        self.igv = self.env['account.tax'].search([
            ('company_id', '=', self.company.id), ('type_tax_use', '=', 'sale'),
            ('l10n_pe_edi_tax_code', '=', '1000')], limit=1)
        self.wh = self.env['stock.warehouse'].search([('company_id', '=', self.company.id)], limit=1)
        ruc_type = self.env['l10n_latam.identification.type'].search(
            [('l10n_pe_vat_code', '=', '6')], limit=1)
        self.partner = self.env['res.partner'].create({
            'name': 'CLIENTE SAC', 'vat': '20100070970',
            'l10n_latam_identification_type_id': ruc_type.id})

    def _bien(self, name):
        return self.env['product.product'].create({
            'name': name, 'type': 'consu', 'is_storable': True})

    def _abastecer(self, prod, qty):
        q = self.env['stock.quant'].with_context(inventory_mode=True).create({
            'product_id': prod.id, 'location_id': self.wh.lot_stock_id.id,
            'inventory_quantity': qty})
        q.action_apply_inventory()

    def _stock(self, prod):
        prod.invalidate_recordset()
        return prod.qty_available

    def _emitir(self, lineas, corr='9200'):
        move = self.env['account.move'].create({
            'move_type': 'out_invoice', 'partner_id': self.partner.id,
            'invoice_date': '2026-07-20', 'l10n_pe_serie': 'F001', 'l10n_pe_correlativo': corr,
            'invoice_line_ids': [
                (0, 0, {'product_id': p.id, 'quantity': q, 'price_unit': pr,
                        'l10n_pe_ne_unit_code': uc, 'tax_ids': [(6, 0, self.igv.ids)]})
                for (p, q, pr, uc) in lineas]})
        move.action_post()
        return move

    # -- maderera / aserradero ---------------------------------------------------------------
    def test_maderera_mtq_volumen_xml_y_kardex(self):
        """2.125 m³ de madera emiten ctdUnidadItem 2.125 con unitCode MTQ, y el kardex descuenta
        el volumen exacto."""
        madera = self._bien('MADERA TORNILLO')
        self._abastecer(madera, 5)
        move = self._emitir([(madera, 2.125, 1800.0, 'MTQ')])

        det = move._l10n_pe_build_invoice_request()['detalle'][0]
        self.assertEqual(det['ctdUnidadItem'], '2.125')
        self.assertEqual(det['codUnidadMedida'], 'MTQ')
        move._l10n_pe_ne_mover_stock()
        self.assertAlmostEqual(self._stock(madera), 2.875, places=3)   # 5 − 2.125

    # -- textil / telas ----------------------------------------------------------------------
    def test_textil_metro_y_docena_xml_y_kardex(self):
        """Tela por metro (15.75 m, MTR) y prendas por docena (2.5 DZN): la docena emite con
        unitCode DZN (no DPC) y el kardex descuenta la medida de cada línea."""
        tela = self._bien('TELA DRILL')
        polos = self._bien('POLOS')
        self._abastecer(tela, 100)
        self._abastecer(polos, 10)
        move = self._emitir([(tela, 15.75, 12.0, 'MTR'), (polos, 2.5, 180.0, 'DZN')])

        dets = move._l10n_pe_build_invoice_request()['detalle']
        by_unit = {d['codUnidadMedida']: d for d in dets}
        self.assertIn('DZN', by_unit)                 # docena = DZN, no DPC
        self.assertEqual(by_unit['MTR']['ctdUnidadItem'], '15.75')
        self.assertEqual(by_unit['DZN']['ctdUnidadItem'], '2.50')
        move._l10n_pe_ne_mover_stock()
        self.assertAlmostEqual(self._stock(tela), 84.25, places=3)     # 100 − 15.75
        self.assertAlmostEqual(self._stock(polos), 7.5, places=3)      # 10 − 2.5 (docenas)

    def test_docena_odoo_uom_mapea_a_dzn(self):
        """Un producto con la UoM 'Docena' de Odoo (sin override de línea) emite unitCode DZN."""
        dozen = self.env.ref('uom.product_uom_dozen', raise_if_not_found=False)
        if not dozen:
            self.skipTest('uom.product_uom_dozen no disponible')
        prod = self.env['product.product'].create({
            'name': 'MEDIAS', 'type': 'consu', 'uom_id': dozen.id})
        move = self.env['account.move'].create({
            'move_type': 'out_invoice', 'partner_id': self.partner.id,
            'invoice_date': '2026-07-20', 'l10n_pe_serie': 'F001', 'l10n_pe_correlativo': '9203',
            'invoice_line_ids': [(0, 0, {
                'product_id': prod.id, 'quantity': 3, 'price_unit': 60.0,
                'tax_ids': [(6, 0, self.igv.ids)]})]})
        move.action_post()
        self.assertEqual(move._l10n_pe_build_invoice_request()['detalle'][0]['codUnidadMedida'], 'DZN')

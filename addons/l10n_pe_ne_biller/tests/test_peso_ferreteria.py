from odoo.tests import TransactionCase, tagged

from .common import EnvioSincronoMixin


@tagged('post_install', '-at_install')
class TestPesoFerreteria(EnvioSincronoMixin, TransactionCase):
    """Verticales «venta al peso / balanza» y «ferretería» de punta a punta.

    Las dos comparten el mismo requisito que las separa del conteo de la bodega: la CANTIDAD es
    una MEDIDA fraccionaria (peso en KGM, longitud en MTR, área en MTK), no un entero. Este test
    fija el contrato de esa medida en los dos puntos donde se puede romper:

      * el XML a SUNAT: `ctdUnidadItem` conserva la medida exacta (3 decimales, ver QA-020) y el
        `unitCode` es el código cat.03 correcto — no NIU;
      * el kardex: el movimiento de stock descuenta la MISMA medida fraccionaria (vender 12.5 m de
        tubo baja 12.5 del inventario, no 12 ni 13).
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

    def _emitir(self, lineas, tipo='01', serie='F001', corr='9100'):
        """lineas = [(product, qty, price, unit_code), ...]"""
        move = self.env['account.move'].create({
            'move_type': 'out_invoice', 'partner_id': self.partner.id,
            'invoice_date': '2026-07-20', 'l10n_pe_serie': serie, 'l10n_pe_correlativo': corr,
            'invoice_line_ids': [
                (0, 0, {'product_id': p.id, 'quantity': q, 'price_unit': pr,
                        'l10n_pe_ne_unit_code': uc, 'tax_ids': [(6, 0, self.igv.ids)]})
                for (p, q, pr, uc) in lineas]})
        move.action_post()
        return move

    # -- venta al peso / balanza -------------------------------------------------------------
    def test_peso_kgm_xml_y_kardex(self):
        """Un pollo pesado en balanza (18.375 kg) emite `ctdUnidadItem` 18.375 con unitCode KGM,
        y el kardex descuenta exactamente 18.375 kg."""
        pollo = self._bien('POLLO')
        self._abastecer(pollo, 20)
        move = self._emitir([(pollo, 18.375, 9.80, 'KGM')])

        det = move._l10n_pe_build_invoice_request()['detalle'][0]
        self.assertEqual(det['ctdUnidadItem'], '18.375')      # no truncado a 18.38
        self.assertEqual(det['codUnidadMedida'], 'KGM')       # no NIU
        # El movimiento de stock nace de la emisión (como el POS de Odoo), aquí explícito.
        move._l10n_pe_ne_mover_stock()
        # 20 − 18.375 = 1.625 kg en existencia
        self.assertAlmostEqual(self._stock(pollo), 1.625, places=3)

    # -- ferretería --------------------------------------------------------------------------
    def test_ferreteria_mtr_mtk_xml_y_kardex(self):
        """Ferretería: un tubo por metro (12.5 m) y una malla por metro cuadrado (3.25 m²) emiten
        con unitCode MTR y MTK, y el kardex descuenta la medida fraccionaria de cada uno."""
        tubo = self._bien('TUBO PVC')
        malla = self._bien('MALLA')
        self._abastecer(tubo, 100)
        self._abastecer(malla, 50)
        move = self._emitir([(tubo, 12.5, 8.50, 'MTR'), (malla, 3.25, 24.0, 'MTK')])

        dets = move._l10n_pe_build_invoice_request()['detalle']
        by_unit = {d['codUnidadMedida']: d for d in dets}
        self.assertEqual(by_unit['MTR']['ctdUnidadItem'], '12.50')
        self.assertEqual(by_unit['MTK']['ctdUnidadItem'], '3.25')
        move._l10n_pe_ne_mover_stock()
        self.assertAlmostEqual(self._stock(tubo), 87.5, places=3)    # 100 − 12.5
        self.assertAlmostEqual(self._stock(malla), 46.75, places=3)  # 50 − 3.25

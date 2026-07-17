from odoo.tests import TransactionCase, tagged

from .common import EnvioSincronoMixin


@tagged('post_install', '-at_install')
class TestLoteSerie(EnvioSincronoMixin, TransactionCase):
    """Rastreo por lote (farmacia, alimentos) y por serie (celulares, equipos).

    El reparto sale de cómo funciona Odoo, verificado: la ENTRADA define el lote —la compra
    es la única que sabe cuál llegó— y la SALIDA lo asigna sola al reservar, por la estrategia
    de salida (con vencimiento, lo que caduca antes sale primero). Por eso vender no pide lote.
    """

    def setUp(self):
        super().setUp()
        self.Move = self.env['account.move']
        self.igv = self.env['account.tax'].search([
            ('company_id', '=', self.env.company.id), ('type_tax_use', '=', 'sale'),
            ('l10n_pe_edi_tax_code', '=', '1000')], limit=1)
        ruc = self.env['l10n_latam.identification.type'].search([('l10n_pe_vat_code', '=', '6')], limit=1)
        self.partner = self.env['res.partner'].create({
            'name': 'CLIENTE LOTE', 'vat': '20100070970',
            'l10n_latam_identification_type_id': ruc.id})
        self.med = self.env['product.product'].create({
            'name': 'PARACETAMOL LOTE TEST', 'type': 'consu', 'is_storable': True,
            'tracking': 'lot', 'use_expiration_date': True})

    def _stock(self, p=None):
        p = p or self.med
        p.invalidate_recordset()
        return p.qty_available

    def _compra(self, lineas, total, numero):
        return self.Move.l10n_pe_ne_create_compra({
            'proveedor': {'tipoDoc': '6', 'numDoc': '20100070970', 'razonSocial': 'DROGUERIA SAC'},
            'tipoComprobante': '01', 'serie': 'F001', 'numero': numero, 'fecha': '2026-07-16',
            'total': total, 'descripcion': 'COMPRA LOTE', 'lineas': lineas})

    # -- el rastreo es del producto -------------------------------------------------------
    def test_el_producto_declara_su_rastreo(self):
        d = self.Move.l10n_pe_ne_create_producto({
            'descripcion': 'AMOXICILINA TEST', 'precio': 10, 'tipo': 'bien',
            'llevaStock': True, 'rastreo': 'lote'})
        self.assertEqual(d['rastreo'], 'lote')
        s = self.Move.l10n_pe_ne_create_producto({
            'descripcion': 'CELULAR TEST', 'precio': 1500, 'tipo': 'bien',
            'llevaStock': True, 'rastreo': 'serie'})
        self.assertEqual(s['rastreo'], 'serie')

    def test_sin_rastreo_por_defecto(self):
        """Un tornillo no necesita lote: rastrear es opt-in."""
        d = self.Move.l10n_pe_ne_create_producto({
            'descripcion': 'TORNILLO TEST RASTREO', 'precio': 1, 'tipo': 'bien', 'llevaStock': True})
        self.assertEqual(d['rastreo'], 'ninguno')

    def test_activar_el_rastreo_a_un_existente(self):
        d = self.Move.l10n_pe_ne_create_producto({
            'descripcion': 'JARABE TEST', 'precio': 20, 'tipo': 'bien', 'llevaStock': True})
        self.assertEqual(d['rastreo'], 'ninguno')
        out = self.Move.l10n_pe_ne_update_producto({'id': d['id'], 'rastreo': 'lote', 'vence': True})
        self.assertEqual(out['rastreo'], 'lote')
        self.assertTrue(out['vence'])

    # -- la compra define el lote ---------------------------------------------------------
    def test_la_compra_ingresa_con_su_lote_y_vencimiento(self):
        self._compra([{'productId': self.med.id, 'cantidad': 50, 'precioUnitario': 2,
                       'lote': 'L-2026-0431', 'vence': '2027-12-31'}], total=100, numero='8001')
        self.assertEqual(self._stock(), 50)
        lote = self.env['stock.lot'].search([('name', '=', 'L-2026-0431'), ('product_id', '=', self.med.id)])
        self.assertEqual(len(lote), 1, 'la compra crea el lote')
        self.assertEqual(str(lote.expiration_date)[:10], '2027-12-31')

    def test_dos_compras_del_mismo_lote_no_lo_duplican(self):
        self._compra([{'productId': self.med.id, 'cantidad': 10, 'precioUnitario': 2,
                       'lote': 'L-MISMO'}], total=20, numero='8002')
        self._compra([{'productId': self.med.id, 'cantidad': 5, 'precioUnitario': 2,
                       'lote': 'L-MISMO'}], total=10, numero='8003')
        self.assertEqual(len(self.env['stock.lot'].search(
            [('name', '=', 'L-MISMO'), ('product_id', '=', self.med.id)])), 1)
        self.assertEqual(self._stock(), 15, 'ambas entradas suman al mismo lote')

    # -- la venta NO pide lote ------------------------------------------------------------
    def test_vender_no_pide_lote_odoo_lo_asigna(self):
        """Lo que hace innecesario tocar el POS ni Emitir: Odoo asigna el lote al reservar."""
        self._compra([{'productId': self.med.id, 'cantidad': 30, 'precioUnitario': 2,
                       'lote': 'L-VENTA'}], total=60, numero='8010')
        venta = self.env['account.move'].create({
            'move_type': 'out_invoice', 'partner_id': self.partner.id, 'invoice_date': '2026-07-16',
            'l10n_pe_serie': 'F001', 'l10n_pe_correlativo': '8011',
            'invoice_line_ids': [(0, 0, {'product_id': self.med.id, 'quantity': 12,
                                         'price_unit': 5, 'tax_ids': [(6, 0, self.igv.ids)]})]})
        venta.action_post()
        moves = venta._l10n_pe_ne_mover_stock()
        self.assertTrue(moves, 'la venta mueve sin que nadie indique el lote')
        self.assertEqual(self._stock(), 18, '30 − 12')
        self.assertEqual(moves.move_line_ids[:1].lot_id.name, 'L-VENTA', 'Odoo asignó el lote')

    # -- el fallo deja rastro, no silencio ------------------------------------------------
    def test_vender_un_rastreado_sin_existencias_avisa(self):
        """Sin stock en ningún lote, Odoo no puede inventar de dónde sale. El comprobante es
        válido igual —el stock nunca lo tumba— pero el documento queda con el aviso: un
        movimiento que no ocurre y nadie ve es un kardex mintiendo en silencio."""
        venta = self.env['account.move'].create({
            'move_type': 'out_invoice', 'partner_id': self.partner.id, 'invoice_date': '2026-07-16',
            'l10n_pe_serie': 'F001', 'l10n_pe_correlativo': '8020',
            'invoice_line_ids': [(0, 0, {'product_id': self.med.id, 'quantity': 5,
                                         'price_unit': 5, 'tax_ids': [(6, 0, self.igv.ids)]})]})
        venta.action_post()
        self.assertFalse(venta._l10n_pe_ne_mover_stock())
        self.assertTrue(venta.l10n_pe_ne_stock_aviso, 'debe quedar el aviso, no el silencio')
        self.assertIn('inventario', venta.l10n_pe_ne_stock_aviso)

    def test_un_movimiento_que_sale_bien_no_deja_aviso(self):
        self._compra([{'productId': self.med.id, 'cantidad': 8, 'precioUnitario': 2,
                       'lote': 'L-OK'}], total=16, numero='8030')
        c = self.env['account.move'].search([('move_type', '=', 'in_invoice')], order='id desc', limit=1)
        self.assertFalse(c.l10n_pe_ne_stock_aviso)

    # -- FEFO: sale primero lo que vence antes --------------------------------------------
    def test_sale_primero_el_lote_que_vence_antes(self):
        """El default de Odoo es FIFO —sale lo que entró primero— y para lo que caduca eso
        está MAL. Comprobado en el stack: con dos lotes, la venta se llevaba el que vence en
        2028 y dejaba el de 2026 pudriéndose. En farmacia es plata tirada y riesgo sanitario."""
        # El que vence TARDE entra primero: con FIFO saldría este.
        self._compra([{'productId': self.med.id, 'cantidad': 20, 'precioUnitario': 2,
                       'lote': 'L-TARDE', 'vence': '2028-06-30'}], total=40, numero='8040')
        self._compra([{'productId': self.med.id, 'cantidad': 10, 'precioUnitario': 2,
                       'lote': 'L-PRONTO', 'vence': '2026-09-30'}], total=20, numero='8041')
        venta = self.env['account.move'].create({
            'move_type': 'out_invoice', 'partner_id': self.partner.id, 'invoice_date': '2026-07-16',
            'l10n_pe_serie': 'F001', 'l10n_pe_correlativo': '8042',
            'invoice_line_ids': [(0, 0, {'product_id': self.med.id, 'quantity': 6,
                                         'price_unit': 5, 'tax_ids': [(6, 0, self.igv.ids)]})]})
        venta.action_post()
        moves = venta._l10n_pe_ne_mover_stock()
        self.assertEqual(moves.move_line_ids[:1].lot_id.name, 'L-PRONTO',
                         'debe salir el que vence antes, no el que entró primero')

    def test_fefo_se_configura_en_la_ubicacion(self):
        self._compra([{'productId': self.med.id, 'cantidad': 5, 'precioUnitario': 2,
                       'lote': 'L-FEFO'}], total=10, numero='8050')
        wh = self.env['stock.warehouse'].search([('company_id', '=', self.env.company.id)], limit=1)
        self.assertEqual(wh.lot_stock_id.removal_strategy_id.method, 'fefo')

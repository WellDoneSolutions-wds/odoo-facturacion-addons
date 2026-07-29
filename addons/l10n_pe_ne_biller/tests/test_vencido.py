from odoo.tests import TransactionCase, tagged

from .common import EnvioSincronoMixin, L10nPeSeedMixin


@tagged('post_install', '-at_install')
class TestVencido(L10nPeSeedMixin, EnvioSincronoMixin, TransactionCase):
    """F2 · Alerta de venta de producto VENCIDO (farma/perecibles).

    La regla lee el lote que la salida de stock reservó (FEFO) y avisa si ya venció. Es un
    aviso del pre-flight (control de negocio/DIGEMID), no bloquea la emisión.
    """

    def setUp(self):
        super().setUp()  # L10nPeSeedMixin: RUC + IGV
        self.Move = self.env['account.move']
        self.stock_loc = self.env['stock.warehouse'].search(
            [('company_id', '=', self.env.company.id)], limit=1).lot_stock_id

    def _producto_con_lote(self):
        return self.env['product.product'].create({
            'name': 'PARACETAMOL', 'default_code': 'FARMA1', 'type': 'consu',
            'is_storable': True, 'tracking': 'lot', 'use_expiration_date': True})

    def _sembrar_lote(self, prod, vence, qty=10):
        lot = self.env['stock.lot'].create({
            'name': 'L-%s' % vence[:10], 'product_id': prod.id,
            'company_id': self.env.company.id, 'expiration_date': vence})
        self.env['stock.quant']._update_available_quantity(prod, self.stock_loc, qty, lot_id=lot)
        return lot

    def _payload(self, prod):
        return {
            'tipoDoc': '01', 'moneda': 'PEN', 'serie': 'F001',
            'cliente': {'tipoDoc': '6', 'numDoc': '20100070970', 'razonSocial': 'CLIENTE SAC'},
            'lineas': [{'descripcion': prod.name, 'productId': prod.id, 'cantidad': 1,
                        'precioUnitario': 100, 'taxCode': '1000'}],
        }

    def test_lote_vencido_avisa(self):
        prod = self._producto_con_lote()
        self._sembrar_lote(prod, '2020-01-01 00:00:00')  # ya venció
        codes = {f['code'] for f in self.Move.l10n_pe_ne_preflight(self._payload(prod))}
        self.assertIn('vencido', codes, 'vender un lote vencido debe avisar')

    def test_lote_vigente_no_avisa(self):
        prod = self._producto_con_lote()
        self._sembrar_lote(prod, '2030-12-31 00:00:00')  # vigente
        codes = {f['code'] for f in self.Move.l10n_pe_ne_preflight(self._payload(prod))}
        self.assertNotIn('vencido', codes)

    def test_producto_sin_vencimiento_no_avisa(self):
        # Un producto normal (sin rastreo de vencimiento) nunca dispara la alerta.
        prod = self.env['product.product'].create({
            'name': 'CUADERNO', 'default_code': 'LIB1', 'type': 'consu', 'is_storable': True})
        codes = {f['code'] for f in self.Move.l10n_pe_ne_preflight(self._payload(prod))}
        self.assertNotIn('vencido', codes)

    # -- F1: lote + vencimiento en la descripción del ítem (XML + PDF) --------------------
    def _emitir(self, prod):
        from unittest.mock import patch
        ok = type('R', (), {'status_code': 200, 'text': '<Invoice/>', 'headers': {}})()
        target = 'odoo.addons.l10n_pe_ne_biller.models.account_move_biller.requests.post'
        with patch(target, return_value=ok):
            res = self.Move.l10n_pe_ne_quick_emit(self._payload(prod))
        return self.Move.browse(res['id'])

    def test_lote_y_vencimiento_van_en_la_descripcion(self):
        prod = self._producto_con_lote()
        self._sembrar_lote(prod, '2030-12-31 00:00:00')
        move = self._emitir(prod)
        des = move._l10n_pe_detalle()[0]['desItem']
        self.assertIn('Lote L-2030-12-31', des, 'el lote va en la descripción del ítem')
        self.assertIn('Vence 31/12/2030', des, 'el vencimiento va en la descripción del ítem')

    def test_producto_sin_vencimiento_no_anota_lote(self):
        prod = self.env['product.product'].create({
            'name': 'CUADERNO', 'default_code': 'LIB2', 'type': 'consu', 'is_storable': True})
        move = self._emitir(prod)
        self.assertNotIn('Lote', move._l10n_pe_detalle()[0]['desItem'])

from unittest.mock import patch

from odoo.exceptions import UserError
from odoo.tests import TransactionCase, tagged

from .common import EnvioSincronoMixin, L10nPeSeedMixin

_TARGET = 'odoo.addons.l10n_pe_ne_biller.models.account_move_biller.requests.post'


@tagged('post_install', '-at_install')
class TestFraccionamiento(L10nPeSeedMixin, EnvioSincronoMixin, TransactionCase):
    """F3 · Fraccionamiento: el producto se stockea por empaque (caja) pero se vende por unidad.
    La línea va en sub-unidades (código SUNAT de fracción) y el stock descuenta cantidad/factor
    del empaque. El precio por sub-unidad lo manda el front."""

    def setUp(self):
        super().setUp()  # L10nPeSeedMixin: RUC + IGV
        self.Move = self.env['account.move']
        self.stock_loc = self.env['stock.warehouse'].search(
            [('company_id', '=', self.env.company.id)], limit=1).lot_stock_id

    def _producto(self, factor=30.0, fraccion='NIU'):
        return self.env['product.product'].create({
            'name': 'PARACETAMOL CAJA x30', 'default_code': 'FRAC1', 'type': 'consu',
            'is_storable': True, 'l10n_pe_ne_unidades_por_empaque': factor,
            'l10n_pe_ne_unidad_fraccion': fraccion})

    def _emitir(self, prod, cantidad, fraccionar=True):
        ok = type('R', (), {'status_code': 200, 'text': '<Invoice/>', 'headers': {}})()
        with patch(_TARGET, return_value=ok):
            res = self.Move.l10n_pe_ne_quick_emit({
                'tipoDoc': '01', 'moneda': 'PEN', 'serie': 'F001',
                'cliente': {'tipoDoc': '6', 'numDoc': '20100070970', 'razonSocial': 'CLIENTE SAC'},
                'lineas': [{'descripcion': prod.name, 'productId': prod.id, 'cantidad': cantidad,
                            'precioUnitario': 2, 'taxCode': '1000',
                            **({'fraccionar': True} if fraccionar else {})}],
            })
        return self.Move.browse(res['id'])

    def test_venta_fraccionada_descuenta_por_factor(self):
        prod = self._producto(factor=30.0)
        self.env['stock.quant']._update_available_quantity(prod, self.stock_loc, 10)  # 10 cajas
        move = self._emitir(prod, 5, fraccionar=True)  # vende 5 unidades
        # La línea sale en la sub-unidad SUNAT, cantidad 5.
        d = move._l10n_pe_detalle()[0]
        self.assertEqual(d['codUnidadMedida'], 'NIU')
        self.assertEqual(float(d['ctdUnidadItem']), 5.0)
        # El stock descuenta una FRACCIÓN de caja (~5/30), NO 5 cajas. El valor exacto sigue la
        # precisión de la UoM del producto (por defecto 0.01: 0.1667 → 0.17); para inventario
        # fraccionado fino, configurar el redondeo de la UoM del producto más pequeño.
        prod.invalidate_recordset(['qty_available'])
        self.assertLess(prod.qty_available, 10.0)
        self.assertGreater(prod.qty_available, 9.5, 'descontó una fracción, no 5 cajas')
        self.assertAlmostEqual(prod.qty_available, 10 - 5.0 / 30.0, delta=0.02)

    def test_sin_fraccionar_descuenta_entero(self):
        prod = self._producto(factor=30.0)
        self.env['stock.quant']._update_available_quantity(prod, self.stock_loc, 10)
        self._emitir(prod, 2, fraccionar=False)  # 2 cajas normales
        prod.invalidate_recordset(['qty_available'])
        self.assertAlmostEqual(prod.qty_available, 8.0, delta=0.001)

    def test_fraccionar_sin_factor_falla(self):
        prod = self.env['product.product'].create({
            'name': 'SIN FACTOR', 'default_code': 'FRAC0', 'type': 'consu', 'is_storable': True})
        with self.assertRaises(UserError):
            self._emitir(prod, 5, fraccionar=True)

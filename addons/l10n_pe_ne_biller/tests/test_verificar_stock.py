# -*- coding: utf-8 -*-
"""Pre-emisión: aviso (no bloqueante) de stock negativo. l10n_pe_ne_verificar_stock informa qué
bienes con inventario quedarían negativos; agrega cantidades por producto e ignora servicios."""
from odoo.tests import TransactionCase, tagged

from .common import L10nPeSeedMixin


@tagged("post_install", "-at_install")
class TestVerificarStock(L10nPeSeedMixin, TransactionCase):
    def setUp(self):
        super().setUp()
        self.wh = self.env["stock.warehouse"].search(
            [("company_id", "=", self.env.company.id)], limit=1)
        self.AM = self.env["account.move"]

    def _producto(self, qty, storable=True):
        p = self.env["product.product"].create({
            "name": "Gaseosa", "type": "consu", "is_storable": storable, "lst_price": 10.0})
        if storable:
            self.env["stock.quant"]._update_available_quantity(p, self.wh.lot_stock_id, qty)
        return p

    def test_stock_suficiente_sin_aviso(self):
        p = self._producto(10)
        r = self.AM.l10n_pe_ne_verificar_stock([{"productId": p.id, "cantidad": 4}])
        self.assertEqual(r["avisos"], [])

    def test_stock_insuficiente_avisa_con_queda_en(self):
        p = self._producto(2)
        r = self.AM.l10n_pe_ne_verificar_stock([{"productId": p.id, "cantidad": 5}])
        self.assertEqual(len(r["avisos"]), 1)
        a = r["avisos"][0]
        self.assertEqual(a["productId"], p.id)
        self.assertEqual(a["stock"], 2.0)
        self.assertEqual(a["cantidad"], 5.0)
        self.assertEqual(a["quedaEn"], -3.0)

    def test_agrega_cantidad_del_mismo_producto(self):
        # El mismo producto en dos líneas se suma: 3 + 4 = 7 contra 5 → queda -2.
        p = self._producto(5)
        r = self.AM.l10n_pe_ne_verificar_stock([
            {"productId": p.id, "cantidad": 3},
            {"productId": p.id, "cantidad": 4},
        ])
        self.assertEqual(len(r["avisos"]), 1)
        self.assertEqual(r["avisos"][0]["quedaEn"], -2.0)

    def test_servicio_sin_inventario_nunca_avisa(self):
        p = self._producto(0, storable=False)
        r = self.AM.l10n_pe_ne_verificar_stock([{"productId": p.id, "cantidad": 99}])
        self.assertEqual(r["avisos"], [])

    def test_lineas_sin_producto_se_ignoran(self):
        # Conceptos libres (sin productId) no tienen stock que verificar.
        r = self.AM.l10n_pe_ne_verificar_stock([{"cantidad": 5}, {"productId": 0, "cantidad": 3}])
        self.assertEqual(r["avisos"], [])

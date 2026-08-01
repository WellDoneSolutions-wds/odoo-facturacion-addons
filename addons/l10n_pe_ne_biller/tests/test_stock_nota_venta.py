# -*- coding: utf-8 -*-
"""La nota de venta (venta sin comprobante) mueve stock igual que un comprobante:
registrar → salida; anular → repone; convertir → NO vuelve a descontar (Task 3)."""
from odoo.tests import TransactionCase, tagged

from .common import L10nPeSeedMixin


@tagged("post_install", "-at_install")
class TestStockNotaVenta(L10nPeSeedMixin, TransactionCase):
    def setUp(self):
        super().setUp()
        self.wh = self.env["stock.warehouse"].search(
            [("company_id", "=", self.env.company.id)], limit=1)
        self.NV = self.env["l10n_pe_ne.nota_venta"]

    def _producto(self, qty_inicial=10):
        p = self.env["product.product"].create({
            "name": "Gaseosa", "type": "consu", "is_storable": True, "lst_price": 10.0})
        self.env["stock.quant"]._update_available_quantity(p, self.wh.lot_stock_id, qty_inicial)
        return p

    def test_registrar_descuenta_y_anular_repone(self):
        p = self._producto(10)
        res = self.NV.l10n_pe_ne_quick_venta({
            "items": [{"productId": p.id, "cantidad": 3, "precio": 10.0, "afectoIgv": True}],
        })
        self.assertEqual(p.qty_available, 7.0, "registrar la nota debe descontar 3")
        nv = self.NV.browse(res["id"])
        nv.l10n_pe_ne_set_estado_nota_venta("anulada")
        self.assertEqual(p.qty_available, 10.0, "anular la nota debe reponer 3")

    def test_servicio_no_mueve_stock(self):
        # Un producto sin is_storable no debe generar movimientos ni romper el registro.
        serv = self.env["product.product"].create({
            "name": "Delivery", "type": "consu", "is_storable": False, "lst_price": 5.0})
        res = self.NV.l10n_pe_ne_quick_venta({
            "items": [{"productId": serv.id, "cantidad": 1, "precio": 5.0, "afectoIgv": True}],
        })
        self.assertTrue(res.get("id"))
        moves = self.env["stock.move"].search([("l10n_pe_ne_nota_venta_id", "=", res["id"])])
        self.assertFalse(moves, "un servicio no mueve stock")

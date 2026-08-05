# -*- coding: utf-8 -*-
"""Kardex por producto: movimientos done con saldo acumulado corriente."""
from odoo.tests import TransactionCase, tagged

from .common import L10nPeSeedMixin


@tagged("post_install", "-at_install")
class TestKardex(L10nPeSeedMixin, TransactionCase):
    def test_kardex_saldo_y_orden(self):
        AM = self.env["account.move"]
        p = self.env["product.product"].create({
            "name": "Clavo", "type": "consu", "is_storable": True})
        AM._l10n_pe_ne_ajustar_stock(p.id, "fijar", 100, "carga inicial")  # entrada 100
        AM._l10n_pe_ne_ajustar_stock(p.id, "restar", 3, "merma")           # salida 3
        k = AM._l10n_pe_ne_kardex(p.id)
        self.assertEqual(k["stock"], 97.0)
        self.assertEqual(len(k["movimientos"]), 2)
        m0, m1 = k["movimientos"]
        self.assertEqual((m0["entrada"], m0["salida"], m0["saldo"]), (100.0, 0.0, 100.0))
        self.assertEqual((m1["entrada"], m1["salida"], m1["saldo"]), (0.0, 3.0, 97.0))
        # Concepto: los ajustes de inventario se etiquetan 'ajuste' (no tienen comprobante enlazado).
        self.assertEqual(m0["concepto"], "ajuste")
        self.assertEqual(m1["concepto"], "ajuste")

    def test_kardex_atribuye_documento_nota(self):
        NV = self.env["l10n_pe_ne.nota_venta"]
        wh = self.env["stock.warehouse"].search([("company_id", "=", self.env.company.id)], limit=1)
        p = self.env["product.product"].create({
            "name": "Foco", "type": "consu", "is_storable": True})
        self.env["stock.quant"]._update_available_quantity(p, wh.lot_stock_id, 10)
        res = NV.l10n_pe_ne_quick_venta({
            "items": [{"productId": p.id, "cantidad": 2, "precio": 5.0, "afectoIgv": True}]})
        k = self.env["account.move"]._l10n_pe_ne_kardex(p.id)
        salida = [m for m in k["movimientos"] if m["salida"] > 0][-1]
        self.assertEqual(salida["salida"], 2.0)
        # el documento del movimiento es el número de la nota, y el concepto la clasifica
        nv = NV.browse(res["id"])
        self.assertEqual(salida["documento"], nv.name)
        self.assertEqual(salida["concepto"], "nota_venta")

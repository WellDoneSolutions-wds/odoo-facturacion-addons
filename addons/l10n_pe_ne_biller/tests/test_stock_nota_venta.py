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

    def test_conversion_no_doble_descuenta(self):
        p = self._producto(10)
        res = self.NV.l10n_pe_ne_quick_venta({
            "items": [{"productId": p.id, "cantidad": 3, "precio": 10.0, "afectoIgv": True}],
        })
        nv = self.NV.browse(res["id"])
        self.assertEqual(p.qty_available, 7.0)
        # Convertir a comprobante = emitir una boleta con notaVentaId. El stock NO debe volver a
        # bajar (la mercadería ya salió al registrar la nota); el movimiento se re-vincula.
        self.env["account.move"].l10n_pe_ne_quick_emit({
            "tipoDoc": "03", "moneda": "PEN", "notaVentaId": nv.id,
            "cliente": {"tipoDoc": "0", "numDoc": "", "razonSocial": "VARIOS"},
            "lineas": [{"descripcion": "Gaseosa", "cantidad": 3, "precioUnitario": 10.0,
                        "taxCode": "1000", "productId": p.id}],
        }, enviar=False)
        self.assertEqual(p.qty_available, 7.0, "convertir NO debe volver a descontar (sería 4)")
        nv.invalidate_recordset()
        self.assertEqual(nv.estado, "convertida")
        moves = self.env["stock.move"].search([("l10n_pe_ne_nota_venta_id", "=", nv.id)])
        self.assertTrue(moves, "debe existir el movimiento de la nota")
        self.assertTrue(all(m.l10n_pe_ne_move_id == nv.comprobante_id for m in moves),
                        "el movimiento de la nota debe re-vincularse al comprobante")

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

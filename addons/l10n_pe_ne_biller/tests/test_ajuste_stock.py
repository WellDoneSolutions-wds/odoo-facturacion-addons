# -*- coding: utf-8 -*-
"""Ajuste de inventario por producto (conteo físico / carga inicial / merma / corregir negativos)."""
from odoo.tests import TransactionCase, tagged

from .common import L10nPeSeedMixin


@tagged("post_install", "-at_install")
class TestAjusteStock(L10nPeSeedMixin, TransactionCase):
    def _prod(self):
        return self.env["product.product"].create({
            "name": "Tornillo", "type": "consu", "is_storable": True, "lst_price": 1.0})

    def test_fijar_sumar_restar(self):
        AM = self.env["account.move"]
        p = self._prod()
        AM._l10n_pe_ne_ajustar_stock(p.id, "fijar", 100, "carga inicial")
        self.assertEqual(p.qty_available, 100.0)
        AM._l10n_pe_ne_ajustar_stock(p.id, "restar", 5, "merma")
        self.assertEqual(p.qty_available, 95.0)
        AM._l10n_pe_ne_ajustar_stock(p.id, "sumar", 10, "correccion")
        self.assertEqual(p.qty_available, 105.0)
        AM._l10n_pe_ne_ajustar_stock(p.id, "fijar", 90, "conteo")
        self.assertEqual(p.qty_available, 90.0)

    def test_fijar_corrige_negativo(self):
        # La venta nunca bloquea → puede quedar negativo; "fijar" lo corrige a la cantidad real.
        AM = self.env["account.move"]
        p = self._prod()
        AM._l10n_pe_ne_ajustar_stock(p.id, "restar", 3, "salida sin stock")
        self.assertEqual(p.qty_available, -3.0)
        AM._l10n_pe_ne_ajustar_stock(p.id, "fijar", 0, "conteo")
        self.assertEqual(p.qty_available, 0.0)

    def test_servicio_no_ajusta(self):
        serv = self.env["product.product"].create({
            "name": "Servicio", "type": "consu", "is_storable": False})
        res = self.env["account.move"]._l10n_pe_ne_ajustar_stock(serv.id, "fijar", 10, "x")
        self.assertTrue(res.get("aviso"))  # no lleva inventario → aviso, no rompe

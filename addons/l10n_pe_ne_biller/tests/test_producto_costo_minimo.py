# -*- coding: utf-8 -*-
"""Precio de compra (costo) y stock mínimo en el producto del catálogo.

El costo ya lo guardaba el backend (standard_price) desde una línea de compra; aquí se verifica
que el ALTA y la EDICIÓN del catálogo también lo escriban y lo devuelvan. El stock mínimo es
nuevo: umbral de reposición que la lista usa para avisar "bajo el mínimo".
"""
from odoo.exceptions import ValidationError
from odoo.tests import TransactionCase, tagged

from .common import L10nPeSeedMixin


@tagged("post_install", "-at_install")
class TestProductoCostoStockMinimo(L10nPeSeedMixin, TransactionCase):
    def test_crear_con_costo_y_stock_minimo(self):
        res = self.env["account.move"].l10n_pe_ne_create_producto({
            "descripcion": "Arroz Costeño Extra 5 kg", "precio": 23.90, "costo": 18.50,
            "llevaStock": True, "tipo": "bien", "stockMinimo": 20,
        })
        p = self.env["product.product"].browse(res["id"])
        # Se persisten en el producto…
        self.assertEqual(p.standard_price, 18.50)
        self.assertEqual(p.l10n_pe_ne_stock_minimo, 20.0)
        # …y el read los expone al front.
        self.assertEqual(res["costo"], 18.50)
        self.assertEqual(res["stockMinimo"], 20.0)

    def test_actualizar_costo_y_stock_minimo(self):
        res = self.env["account.move"].l10n_pe_ne_create_producto({
            "descripcion": "Leche Gloria Entera 400 g", "precio": 4.20,
            "llevaStock": True, "tipo": "bien",
        })
        upd = self.env["account.move"].l10n_pe_ne_update_producto({
            "id": res["id"], "costo": 3.50, "stockMinimo": 50,
        })
        self.assertEqual(upd["costo"], 3.50)
        self.assertEqual(upd["stockMinimo"], 50.0)
        p = self.env["product.product"].browse(res["id"])
        self.assertEqual(p.standard_price, 3.50)
        self.assertEqual(p.l10n_pe_ne_stock_minimo, 50.0)

    def test_sin_costo_ni_minimo_quedan_en_cero(self):
        # Opcionales: sin ellos el producto se crea igual, en 0 (no rompe).
        res = self.env["account.move"].l10n_pe_ne_create_producto({
            "descripcion": "Bolsa", "precio": 1.0, "tipo": "bien",
        })
        self.assertEqual(res["costo"], 0.0)
        self.assertEqual(res["stockMinimo"], 0.0)

    def test_stock_minimo_negativo_bloquea(self):
        # Defensa en profundidad: un write directo (fuera de la API) con mínimo negativo no pasa.
        with self.assertRaises(ValidationError):
            self.env["product.product"].create({
                "name": "Producto X", "type": "consu", "l10n_pe_ne_stock_minimo": -1,
            })

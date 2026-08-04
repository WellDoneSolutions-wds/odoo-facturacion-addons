# -*- coding: utf-8 -*-
"""Marca comercial de producto (l10n_pe_ne.marca): crear/deduplicar, asignar, buscar, desasignar."""
from odoo.tests import TransactionCase, tagged

from .common import L10nPeSeedMixin


@tagged("post_install", "-at_install")
class TestProductoMarca(L10nPeSeedMixin, TransactionCase):
    def test_crear_marca_deduplica(self):
        AM = self.env["account.move"]
        a = AM.l10n_pe_ne_crear_marca({"nombre": "Gloria"})
        # Case-insensitive: 'gloria' reutiliza la misma marca, no crea otra.
        b = AM.l10n_pe_ne_crear_marca({"nombre": "  gloria "})
        self.assertEqual(a["id"], b["id"])
        nombres = [m["name"] for m in AM.l10n_pe_ne_list_marcas()]
        self.assertEqual(nombres.count("Gloria"), 1)

    def test_producto_con_marca(self):
        AM = self.env["account.move"]
        marca = AM.l10n_pe_ne_crear_marca({"nombre": "Laive"})
        res = AM.l10n_pe_ne_create_producto({
            "descripcion": "Leche evaporada", "precio": 4.5, "tipo": "bien", "marcaId": marca["id"],
        })
        p = self.env["product.product"].browse(res["id"])
        self.assertEqual(p.l10n_pe_ne_marca_id.id, marca["id"])
        self.assertEqual(res["marcaId"], marca["id"])
        self.assertEqual(res["marca"], "Laive")

    def test_actualizar_y_desasignar_marca(self):
        AM = self.env["account.move"]
        marca = AM.l10n_pe_ne_crear_marca({"nombre": "Pura Vida"})
        res = AM.l10n_pe_ne_create_producto({"descripcion": "Leche", "precio": 3, "tipo": "bien"})
        self.assertIsNone(res["marcaId"])
        # Asigna…
        upd = AM.l10n_pe_ne_update_producto({"id": res["id"], "marcaId": marca["id"]})
        self.assertEqual(upd["marcaId"], marca["id"])
        # …y desasigna con marcaId vacío.
        upd2 = AM.l10n_pe_ne_update_producto({"id": res["id"], "marcaId": 0})
        self.assertIsNone(upd2["marcaId"])
        self.assertEqual(upd2["marca"], "")

    def test_buscar_producto_por_marca(self):
        AM = self.env["account.move"]
        marca = AM.l10n_pe_ne_crear_marca({"nombre": "Gloria"})
        AM.l10n_pe_ne_create_producto({
            "descripcion": "Leche evaporada tarro", "precio": 4.5, "tipo": "bien", "marcaId": marca["id"],
        })
        # El buscador del catálogo matchea por nombre de la marca.
        hallados = AM.l10n_pe_ne_list_productos("Gloria")
        self.assertTrue(any("Leche evaporada tarro" == it["descripcion"] for it in hallados))

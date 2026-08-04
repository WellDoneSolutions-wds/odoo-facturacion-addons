# -*- coding: utf-8 -*-
"""Categorías de producto (jerárquicas) por empresa: seed, árbol, crear y producto categorizado."""
from odoo.tests import TransactionCase, tagged

from .common import L10nPeSeedMixin


@tagged("post_install", "-at_install")
class TestProductoCategorias(L10nPeSeedMixin, TransactionCase):
    def test_seed_y_arbol_idempotente(self):
        AM = self.env["account.move"]
        arbol = AM.l10n_pe_ne_categorias()
        nombres = {c["nombre"] for c in arbol}
        self.assertIn("Abarrotes", nombres)
        self.assertIn("Arroz", nombres)
        # La subcategoría cuelga de su categoría (parentId apunta a la de la misma empresa).
        arroz = next(c for c in arbol if c["nombre"] == "Arroz")
        abarrotes = next(c for c in arbol if c["nombre"] == "Abarrotes")
        self.assertEqual(arroz["parentId"], abarrotes["id"])
        # Idempotente: no re-siembra en la segunda llamada.
        self.assertEqual(len(AM.l10n_pe_ne_categorias()), len(arbol))

    def test_crear_categoria_y_subcategoria(self):
        AM = self.env["account.move"]
        cat = AM.l10n_pe_ne_crear_categoria({"nombre": "Ferretería"})
        sub = AM.l10n_pe_ne_crear_categoria({"nombre": "Tornillos", "parentId": cat["id"]})
        self.assertEqual(sub["parentId"], cat["id"])

    def test_producto_con_categoria(self):
        AM = self.env["account.move"]
        cat = AM.l10n_pe_ne_crear_categoria({"nombre": "AbarrotesX"})
        sub = AM.l10n_pe_ne_crear_categoria({"nombre": "ArrozX", "parentId": cat["id"]})
        res = AM.l10n_pe_ne_create_producto({
            "descripcion": "Arroz 5kg", "precio": 20, "tipo": "bien", "categId": sub["id"],
        })
        p = self.env["product.product"].browse(res["id"])
        self.assertEqual(p.categ_id.id, sub["id"])
        # El read arma los DOS selects + la etiqueta de la lista.
        self.assertEqual(res["categoriaId"], cat["id"])
        self.assertEqual(res["subcategoriaId"], sub["id"])
        self.assertEqual(res["categoriaLabel"], "AbarrotesX › ArrozX")

    def test_actualizar_categoria_del_producto(self):
        AM = self.env["account.move"]
        cat = AM.l10n_pe_ne_crear_categoria({"nombre": "Bebidas"})
        # Sin categoría al crear → cae en la nativa de Odoo, que no es del negocio.
        res = AM.l10n_pe_ne_create_producto({"descripcion": "Inca Kola", "precio": 5, "tipo": "bien"})
        self.assertEqual(res["categoriaId"], 0)
        # Se le asigna una categoría top (sin subcategoría).
        upd = AM.l10n_pe_ne_update_producto({"id": res["id"], "categId": cat["id"]})
        self.assertEqual(upd["categoriaId"], cat["id"])
        self.assertEqual(upd["subcategoriaId"], 0)

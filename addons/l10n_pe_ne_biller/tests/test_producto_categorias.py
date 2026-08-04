# -*- coding: utf-8 -*-
"""Árbol único de categorías bajo 'Supermercado': seed, conteos, crear y producto categorizado.

El MISMO árbol lo consumen el filtro del catálogo (l10n_pe_ne_list_categorias, con conteos) y el
form de producto (asignar categId). Se siembra la primera vez para no arrancar vacío.
"""
from odoo.tests import TransactionCase, tagged

from .common import L10nPeSeedMixin


@tagged("post_install", "-at_install")
class TestProductoCategorias(L10nPeSeedMixin, TransactionCase):
    def test_seed_supermercado_idempotente(self):
        AM = self.env["account.move"]
        arbol = AM.l10n_pe_ne_list_categorias()
        root_id = arbol["rootId"]
        self.assertTrue(root_id)
        items = arbol["items"]
        por_nombre = {c["name"]: c for c in items}
        # Departamentos y subcategorías sembrados de la taxonomía de retail.
        self.assertIn("Abarrotes", por_nombre)
        self.assertIn("Arroz", por_nombre)
        # El departamento cuelga de la raíz; la subcategoría, del departamento.
        self.assertEqual(por_nombre["Abarrotes"]["parentId"], root_id)
        self.assertEqual(por_nombre["Arroz"]["parentId"], por_nombre["Abarrotes"]["id"])
        # Idempotente: la segunda llamada no re-siembra (mismo root, mismo conteo de nodos).
        arbol2 = AM.l10n_pe_ne_list_categorias()
        self.assertEqual(arbol2["rootId"], root_id)
        self.assertEqual(len(arbol2["items"]), len(items))

    def test_crear_departamento_y_subcategoria(self):
        AM = self.env["account.move"]
        arbol = AM.l10n_pe_ne_list_categorias()
        root_id = arbol["rootId"]
        # Sin parentId → departamento bajo la raíz 'Supermercado'.
        depto = AM.l10n_pe_ne_crear_categoria({"nombre": "Ferretería"})
        self.assertEqual(depto["parentId"], root_id)
        # Con parentId → subcategoría colgando del departamento.
        sub = AM.l10n_pe_ne_crear_categoria({"nombre": "Tornillos", "parentId": depto["id"]})
        self.assertEqual(sub["parentId"], depto["id"])

    def test_producto_con_categoria(self):
        AM = self.env["account.move"]
        depto = AM.l10n_pe_ne_crear_categoria({"nombre": "AbarrotesX"})
        sub = AM.l10n_pe_ne_crear_categoria({"nombre": "ArrozX", "parentId": depto["id"]})
        res = AM.l10n_pe_ne_create_producto({
            "descripcion": "Arroz 5kg", "precio": 20, "tipo": "bien", "categId": sub["id"],
        })
        p = self.env["product.product"].browse(res["id"])
        self.assertEqual(p.categ_id.id, sub["id"])
        # El read del producto devuelve la hoja (categId) y su ruta completa para la lista.
        self.assertEqual(res["categId"], sub["id"])
        self.assertIn("AbarrotesX", res["categoria"])
        self.assertIn("ArrozX", res["categoria"])

    def test_actualizar_categoria_del_producto(self):
        AM = self.env["account.move"]
        depto = AM.l10n_pe_ne_crear_categoria({"nombre": "Bebidas"})
        sub = AM.l10n_pe_ne_crear_categoria({"nombre": "Gaseosas", "parentId": depto["id"]})
        # Se crea con el departamento; luego se reasigna a la subcategoría.
        res = AM.l10n_pe_ne_create_producto({
            "descripcion": "Inca Kola", "precio": 5, "tipo": "bien", "categId": depto["id"],
        })
        self.assertEqual(res["categId"], depto["id"])
        upd = AM.l10n_pe_ne_update_producto({"id": res["id"], "categId": sub["id"]})
        self.assertEqual(upd["categId"], sub["id"])
        self.assertIn("Gaseosas", upd["categoria"])

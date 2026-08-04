# -*- coding: utf-8 -*-
"""Árbol de categorías de producto bajo la raíz 'Supermercado' (departamento → subcategoría).

UN solo árbol, compartido: la taxonomía de supermercado es la misma para todos (cada negocio la
edita). Se reusa product.category de Odoo (jerárquico nativo). La raíz 'Supermercado' es el punto
de anclaje que usa tanto el FILTRO del catálogo (navegar por departamento, con conteos) como el
FORM (asignar categoría/subcategoría al producto): así los dos hablan del mismo árbol. Se siembra
completo la primera vez que se pide, para no arrancar vacío.
"""
from odoo import _, api, fields, models
from odoo.exceptions import UserError

ROOT_NAME = "Supermercado"

# Taxonomía completa de retail peruano (departamentos → subcategorías). Editable por el negocio:
# una ferretería o farmacia borra lo que no usa y arma lo suyo. Es un punto de partida generoso.
_SEED = {
    "Abarrotes": ["Arroz", "Azúcar", "Fideos", "Menestras", "Harinas", "Aceites", "Conservas", "Salsas y aderezos", "Café", "Té e infusiones", "Cereales"],
    "Lácteos y huevos": ["Leche", "Yogurt", "Queso", "Mantequilla", "Margarina", "Crema de leche", "Huevos"],
    "Carnes y aves": ["Res", "Cerdo", "Pollo", "Pavo", "Embutidos"],
    "Pescados y mariscos": ["Pescado fresco", "Conservas de pescado", "Mariscos"],
    "Frutas y verduras": ["Frutas", "Verduras", "Hortalizas"],
    "Panadería y pastelería": ["Pan", "Pan de molde", "Kekes y bizcochos", "Tortas y postres", "Galletas frescas"],
    "Bebidas": ["Agua", "Gaseosas", "Jugos", "Energizantes", "Bebidas rehidratantes", "Café y té listos"],
    "Licores": ["Cerveza", "Vino", "Pisco", "Ron", "Whisky"],
    "Snacks y golosinas": ["Papas y frituras", "Galletas", "Chocolates", "Caramelos", "Frutos secos"],
    "Congelados": ["Helados", "Nuggets y apanados", "Papas prefritas", "Verduras congeladas", "Pizzas"],
    "Comidas preparadas": ["Pollo a la brasa", "Almuerzos", "Ensaladas", "Sándwiches", "Sushi"],
    "Productos naturales": ["Quinua y granos andinos", "Avena", "Miel", "Granola", "Semillas"],
    "Limpieza del hogar": ["Detergentes", "Lavavajillas", "Lejía", "Suavizantes", "Desinfectantes", "Ambientadores"],
    "Cuidado personal": ["Shampoo y acondicionador", "Jabón", "Higiene dental", "Desodorantes", "Papel higiénico", "Cuidado femenino", "Afeitado"],
    "Bebés": ["Pañales", "Toallitas húmedas", "Fórmula infantil", "Papillas", "Biberones y accesorios"],
    "Mascotas": ["Alimento para perros", "Alimento para gatos", "Arena sanitaria", "Snacks y accesorios"],
    "Bazar y hogar": ["Focos y pilas", "Menaje", "Descartables", "Bolsas", "Ferretería básica"],
    "Otros": [],
}


class ProductCategory(models.Model):
    _inherit = "product.category"

    @api.model
    def _l10n_pe_ne_root(self):
        """Raíz 'Supermercado' (find-or-create), sembrada con la taxonomía completa la primera
        vez. Idempotente: si ya existe, no re-siembra. Es global (compartida)."""
        root = self.search([("name", "=", ROOT_NAME), ("parent_id", "=", False)], limit=1)
        if root:
            return root
        root = self.create({"name": ROOT_NAME})
        for depto, subs in _SEED.items():
            d = self.create({"name": depto, "parent_id": root.id})
            for s in subs:
                self.create({"name": s, "parent_id": d.id})
        return root

    @api.model
    def _l10n_pe_ne_crear_bajo_super(self, nombre, parent_id=None):
        """Crea un departamento (bajo la raíz) o una subcategoría (bajo un departamento) del árbol
        Supermercado. Creable al vuelo desde el form. Solo anida bajo el propio árbol."""
        nombre = (nombre or "").strip()
        if not nombre:
            raise UserError(_("El nombre de la categoría es requerido."))
        root = self._l10n_pe_ne_root()
        parent = root
        if parent_id:
            p = self.browse(int(parent_id)).exists()
            # Solo si el padre pedido cuelga de 'Supermercado' (no anida fuera del árbol).
            if p and self.search_count([("id", "=", p.id), ("id", "child_of", root.id)]):
                parent = p
        cat = self.create({"name": nombre, "parent_id": parent.id})
        return {"id": cat.id, "name": cat.name, "parentId": parent.id}

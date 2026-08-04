# -*- coding: utf-8 -*-
"""Categorías de producto (jerárquicas) por empresa.

Se reusa product.category de Odoo (que YA es jerárquico: parent_id/child_id), no un enum fijo:
son datos que cada negocio edita. Se aísla por RUC con l10n_pe_ne_company_id para que en un
Odoo multi-tenant una bodega no vea el árbol de una ferretería. La primera vez que un negocio
pide su árbol se siembra uno genérico razonable (editable), para no arrancar en blanco.
"""
from odoo import _, api, fields, models
from odoo.exceptions import UserError

# Árbol semilla COMPLETO y editable — una taxonomía de retail/supermercado peruano lista para
# usar desde el día uno. NO es un enum fijo: son datos (product.category) que cada negocio
# adapta: una ferretería o farmacia borra lo que no usa y arma lo suyo. Es un punto de partida
# generoso; el negocio lo recorta más que ampliarlo.
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

    # Empresa (RUC) dueña de la categoría: aísla el árbol por emisor. Las categorías nativas de
    # Odoo (ej. "All") van sin esta marca y no se listan a los negocios. ondelete cascade: si se
    # borra la empresa, se van sus categorías.
    l10n_pe_ne_company_id = fields.Many2one(
        "res.company", string="Empresa (Ekipu)", index=True, ondelete="cascade",
    )

    @api.model
    def _l10n_pe_ne_seed(self, company):
        """Siembra el árbol genérico para la empresa si aún no tiene categorías propias."""
        if self.search_count([("l10n_pe_ne_company_id", "=", company.id)]):
            return
        for top, subs in _SEED.items():
            padre = self.create({"name": top, "l10n_pe_ne_company_id": company.id})
            for s in subs:
                self.create({
                    "name": s, "parent_id": padre.id, "l10n_pe_ne_company_id": company.id,
                })

    @api.model
    def _l10n_pe_ne_arbol(self, company):
        """Lista plana del árbol de la empresa: [{id, nombre, parentId}]. parentId=0 = raíz.
        Siembra el genérico la primera vez (idempotente)."""
        self._l10n_pe_ne_seed(company)
        cats = self.search(
            [("l10n_pe_ne_company_id", "=", company.id)], order="parent_id, name"
        )
        return [
            {
                "id": c.id,
                "nombre": c.name,
                # Solo se reconoce como padre uno de la MISMA empresa (no encadena a "All").
                "parentId": c.parent_id.id
                if (c.parent_id and c.parent_id.l10n_pe_ne_company_id.id == company.id)
                else 0,
            }
            for c in cats
        ]

    @api.model
    def _l10n_pe_ne_crear(self, company, nombre, parent_id=None):
        """Crea una categoría/subcategoría para la empresa (creable al vuelo desde el form)."""
        nombre = (nombre or "").strip()
        if not nombre:
            raise UserError(_("El nombre de la categoría es requerido."))
        vals = {"name": nombre, "l10n_pe_ne_company_id": company.id}
        if parent_id:
            padre = self.browse(int(parent_id)).exists()
            # Solo permite anidar bajo una categoría de la misma empresa (no filtra entre RUCs).
            if padre and padre.l10n_pe_ne_company_id.id == company.id:
                vals["parent_id"] = padre.id
        cat = self.create(vals)
        return {
            "id": cat.id,
            "nombre": cat.name,
            "parentId": cat.parent_id.id if vals.get("parent_id") else 0,
        }

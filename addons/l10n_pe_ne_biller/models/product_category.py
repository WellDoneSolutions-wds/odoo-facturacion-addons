# -*- coding: utf-8 -*-
"""Categorías de producto (jerárquicas) por empresa.

Se reusa product.category de Odoo (que YA es jerárquico: parent_id/child_id), no un enum fijo:
son datos que cada negocio edita. Se aísla por RUC con l10n_pe_ne_company_id para que en un
Odoo multi-tenant una bodega no vea el árbol de una ferretería. La primera vez que un negocio
pide su árbol se siembra uno genérico razonable (editable), para no arrancar en blanco.
"""
from odoo import _, api, fields, models
from odoo.exceptions import UserError

# Árbol semilla genérico y editable. No pretende ser el catálogo de un supermercado (una
# ferretería o farmacia lo borra y arma el suyo): es un punto de partida útil para el retail
# peruano común. Cada negocio lo adapta.
_SEED = {
    "Abarrotes": ["Arroz", "Fideos", "Aceites", "Conservas", "Menestras", "Azúcar"],
    "Bebidas": ["Gaseosas", "Agua", "Jugos"],
    "Lácteos": ["Leche", "Yogurt", "Queso"],
    "Snacks": ["Galletas", "Chocolates", "Frituras"],
    "Limpieza": ["Detergentes", "Lejía", "Jabón de ropa"],
    "Cuidado personal": ["Shampoo", "Jabón", "Papel higiénico"],
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

# -*- coding: utf-8 -*-
"""Marca comercial de producto (Gloria, Coca-Cola…). Catálogo simple y compartido.

Modelo propio, plano (sin jerarquía). Se comparte entre productos: 'Gloria' es una sola marca para
todos, lo que evita duplicados por tipeo ('Coca Cola' vs 'Coca-Cola'). Creable al vuelo desde el
form de producto; el buscador del catálogo la matchea y puede anotarse en el comprobante.
"""
from odoo import _, api, fields, models
from odoo.exceptions import UserError


class L10nPeNeMarca(models.Model):
    _name = "l10n_pe_ne.marca"
    _description = "Marca comercial de producto"
    _order = "name"

    name = fields.Char(string="Marca", required=True, index=True)

    _sql_constraints = [
        ("name_uniq", "unique(name)", "Ya existe una marca con ese nombre."),
    ]

    @api.model
    def l10n_pe_ne_list_marcas(self):
        """Todas las marcas (id + nombre) para el select del form de producto."""
        return [{"id": m.id, "name": m.name} for m in self.search([])]

    @api.model
    def l10n_pe_ne_crear_marca(self, nombre):
        """Crea la marca por nombre, o reutiliza la existente (case-insensitive) para no duplicar.
        Creable al vuelo desde el form."""
        nombre = (nombre or "").strip()
        if not nombre:
            raise UserError(_("El nombre de la marca es requerido."))
        m = self.search([("name", "=ilike", nombre)], limit=1)
        if not m:
            m = self.create({"name": nombre})
        return {"id": m.id, "name": m.name}

# -*- coding: utf-8 -*-
"""Muros por rol sobre la fachada API de account.move (edición de datos maestros).

El ACL base `emisor` da write sobre product/res.company a TODOS los roles operativos (cajero,
vendedor, despacho, taller), y estos endpoints no revalidaban el rol: solo el menú los ocultaba.
Por API directa (Postman) un cajero podía editar el precio de un producto o los datos del negocio.
La matriz reserva ambas a supervisor/dueño (veProductos = «supervisor edita precios»; veNegocio =
supervisor). Espejo del patrón del gate de emisión masiva.

Se gatea SOLO la EDICIÓN de un producto existente, no la creación: el POS auto-crea productos al
vender (control anti-fraude estándar de retail — el cajero no toca precios maestros, pero sí puede
dar de alta un ítem nuevo en la venta).
"""
from odoo import _, models
from odoo.exceptions import AccessError

_GRUPO_SUPERVISOR = "l10n_pe_ne_roles.group_l10n_pe_ne_supervisor"


class AccountMove(models.Model):
    _inherit = "account.move"

    def _l10n_pe_ne_supervisor_gate(self, msg):
        """Reserva la acción a supervisor (el dueño lo implica) o admin. Los procesos automáticos
        (sudo) pasan por env.su."""
        if (self.env.su
                or self.env.user.has_group(_GRUPO_SUPERVISOR)
                or self.env.user.has_group("base.group_system")):
            return
        raise AccessError(msg)

    def l10n_pe_ne_update_producto(self, producto):
        self._l10n_pe_ne_supervisor_gate(_(
            "Editar productos y precios está reservado al supervisor o al dueño."))
        return super().l10n_pe_ne_update_producto(producto)

    def l10n_pe_ne_update_negocio(self, vals):
        self._l10n_pe_ne_supervisor_gate(_(
            "Editar los datos del negocio está reservado al supervisor o al dueño."))
        return super().l10n_pe_ne_update_negocio(vals)

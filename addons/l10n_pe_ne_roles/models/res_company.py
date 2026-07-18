# -*- coding: utf-8 -*-
"""res.company — tope de usuarios por RUC (H-4) y provisión del dueño del tenant."""
from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError

# Grupos que recibe el PRIMER usuario de un tenant (su dueño): duenio + los operativos, para que
# tenga el menú completo. duenio implica supervisor (que implica emisor); ventas/caja/despacho/
# taller son hermanos y hay que darlos explícitos. NO contador (es un rol externo de solo lectura).
_GRUPOS_DUENIO = (
    "l10n_pe_ne_roles.group_l10n_pe_ne_duenio",
    "l10n_pe_ne_roles.group_l10n_pe_ne_ventas",
    "l10n_pe_ne_roles.group_l10n_pe_ne_caja",
    "l10n_pe_ne_roles.group_l10n_pe_ne_despacho",
    "l10n_pe_ne_roles.group_l10n_pe_ne_taller",
)
_PROHIBIDOS = ("base.group_system", "base.group_erp_manager")


class ResCompany(models.Model):
    _inherit = "res.company"

    # V2: tope de usuarios por RUC (por plan/cobro). 0 = ilimitado (default). Lo fija el operador
    # del SaaS por plan. El cupo se comprueba al dar de alta Y al reactivar (no solo al alta).
    l10n_pe_ne_max_usuarios = fields.Integer(
        string="Máximo de usuarios (0 = ilimitado)", default=0,
        help="Tope de usuarios internos activos del RUC. 0 no limita.")

    def _l10n_pe_ne_check_cupo_usuarios(self):
        self.ensure_one()
        maximo = self.l10n_pe_ne_max_usuarios or 0
        if maximo <= 0:
            return
        actuales = self.env["res.users"].sudo().search_count([
            ("company_ids", "in", self.ids), ("share", "=", False), ("active", "=", True),
        ])
        if actuales >= maximo:
            raise UserError(_(
                "Alcanzaste el máximo de %d usuarios de tu plan. Desactiva uno o pide ampliar el "
                "plan.") % maximo)

    @api.model
    def l10n_pe_ne_provision_tenant(self, vals):
        """El primer usuario de un tenant es su DUEÑO: tras la provisión (admin-only, en el biller)
        se le dan el rol duenio + los operativos. V5: aunque provision escribe group_ids sin red
        anti-escalada nativa, aquí se comprueba que el usuario no quede con ningún prohibido."""
        res = super().l10n_pe_ne_provision_tenant(vals)
        user = self.env["res.users"].sudo().browse(res.get("userId"))
        if user.exists():
            grupos = self.env["res.groups"]
            for xmlid in _GRUPOS_DUENIO:
                g = self.env.ref(xmlid, raise_if_not_found=False)
                if g:
                    grupos |= g
            user.write({"group_ids": [(4, g.id) for g in grupos]})
            prohibidos = self.env["res.groups"]
            for xmlid in _PROHIBIDOS:
                g = self.env.ref(xmlid, raise_if_not_found=False)
                if g:
                    prohibidos |= g
            if user.all_group_ids & prohibidos:
                raise AccessError(_("Escalada de privilegios en la provisión."))
        return res

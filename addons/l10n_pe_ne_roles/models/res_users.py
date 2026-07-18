# -*- coding: utf-8 -*-
"""res.users — extensión de roles (iteración 3).

H-3: añade al perfil las capacidades por rol (has_group de los grupos de l10n_pe_ne_roles),
que la SPA usa para pintar el menú. La fuente base del perfil vive en el biller
(l10n_pe_ne_perfil); aquí solo se le SUMA, con super(). No se compara identidad de usuarios ni
se decide nada de negocio en el front: el addon calcula, la SPA pinta.

H-4 (alta de usuarios por el dueño) llega en el siguiente paso, con sus métodos sudo() y la
whitelist de grupos otorgables.
"""
from odoo import models

# Grupos cuya presencia expone una capacidad en el perfil. xmlid -> clave del perfil.
_ROL_CAP = {
    "l10n_pe_ne_roles.group_l10n_pe_ne_ventas": "puedeCotizar",
    "l10n_pe_ne_roles.group_l10n_pe_ne_caja": "puedeCobrar",
    "l10n_pe_ne_roles.group_l10n_pe_ne_despacho": "puedeDespachar",
    "l10n_pe_ne_roles.group_l10n_pe_ne_taller": "puedeTaller",
    "l10n_pe_ne_roles.group_l10n_pe_ne_supervisor": "puedeSupervisar",
    "l10n_pe_ne_roles.group_l10n_pe_ne_contador": "esContador",
    "l10n_pe_ne_roles.group_l10n_pe_ne_duenio": "esDuenio",
}


class ResUsers(models.Model):
    _inherit = "res.users"

    def l10n_pe_ne_perfil(self):
        perfil = super().l10n_pe_ne_perfil()
        self.ensure_one()
        for xmlid, clave in _ROL_CAP.items():
            perfil[clave] = self.has_group(xmlid)
        return perfil

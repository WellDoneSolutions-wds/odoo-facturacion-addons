# -*- coding: utf-8 -*-
"""Hook de instalación de l10n_pe_ne_roles.

Un emisor que YA existía podía hacer todo (el grupo Emisor era todo-o-nada). Al instalar el
modelo de roles se le dan todos los roles operativos + duenio, para que su menú (que ahora se
pinta desde el perfil por rol) NO pierda acceso. Restringir es decisión del dueño, no efecto
colateral del upgrade. Es la misma filosofía que la migración de anulación del biller.
"""
_ROLES_A_EMISORES = (
    "l10n_pe_ne_roles.group_l10n_pe_ne_ventas",
    "l10n_pe_ne_roles.group_l10n_pe_ne_caja",
    "l10n_pe_ne_roles.group_l10n_pe_ne_despacho",
    "l10n_pe_ne_roles.group_l10n_pe_ne_taller",
    "l10n_pe_ne_roles.group_l10n_pe_ne_supervisor",
    "l10n_pe_ne_roles.group_l10n_pe_ne_duenio",
)


def post_init_hook(env):
    emisor = env.ref("l10n_pe_ne_biller.group_l10n_pe_ne_emisor", raise_if_not_found=False)
    if not emisor:
        return
    # all_user_ids: incluye a quien tiene emisor por implicación (p.ej. anulación). Se lee ANTES
    # de escribir, porque otorgar los roles nuevos (que implican emisor) cambiaría el conjunto.
    usuarios = emisor.all_user_ids
    if not usuarios:
        return
    comando = [(4, uid) for uid in usuarios.ids]
    for xmlid in _ROLES_A_EMISORES:
        grupo = env.ref(xmlid, raise_if_not_found=False)
        if grupo:
            grupo.write({"user_ids": comando})

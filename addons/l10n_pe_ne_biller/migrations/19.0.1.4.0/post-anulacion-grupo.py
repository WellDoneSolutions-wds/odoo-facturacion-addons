from odoo import SUPERUSER_ID, api


def migrate(cr, version):
    """Anular salió del grupo Emisor a su propio grupo. Los emisores que ya existían podían
    anular, así que se les da el grupo nuevo: el upgrade no debe quitarle a nadie una
    capacidad que ya tenía en silencio. Restringir quién anula es una decisión del admin,
    que ahora puede tomarla quitando el grupo.

    Post (no pre): el grupo lo crea la carga de datos del módulo, que corre antes.
    """
    env = api.Environment(cr, SUPERUSER_ID, {})
    emisor = env.ref(
        "l10n_pe_ne_biller.group_l10n_pe_ne_emisor", raise_if_not_found=False
    )
    anulacion = env.ref(
        "l10n_pe_ne_biller.group_l10n_pe_ne_anulacion", raise_if_not_found=False
    )
    if not emisor or not anulacion:
        return
    # all_user_ids (no user_ids): incluye a quien tiene el grupo por implicación de otro,
    # que también podía anular. Se lee ANTES de escribir — anulacion implica emisor, así
    # que escribir cambiaría el conjunto que estamos recorriendo.
    usuarios = emisor.all_user_ids
    if usuarios:
        anulacion.write({"user_ids": [(4, uid) for uid in usuarios.ids]})

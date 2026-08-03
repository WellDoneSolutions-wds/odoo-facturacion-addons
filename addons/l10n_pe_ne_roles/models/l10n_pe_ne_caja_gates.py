# -*- coding: utf-8 -*-
"""Gates de rol sobre la CAJA y los GASTOS del biller (hallazgo de auditoría en vivo).

El biller no conoce los grupos de roles (es la base), así que sus métodos públicos de caja
solo tenían el ACL del emisor — que ventas/despacho/taller IMPLICAN. Resultado: un vendedor
o un contador podían registrar movimientos, gastos y CERRAR el arqueo del cajero (la SPA les
oculta el menú, pero el muro debe ser el backend). Este módulo envuelve los puntos de entrada
mutadores con el mismo criterio de la matriz (menu-por-rol.md): caja o supervisor.

RETROCOMPAT (misma decisión que H-3b): un usuario SIN ningún rol NE de la matriz —el legacy
solo-`emisor` de los tenants pre-roles, o el solo-`anulación`— NO se gatea: pre-roles ese
perfil operaba la caja y quitárselo sería regresión. El admin de plataforma tampoco. La
LECTURA (estado/arqueo/historial) queda libre a propósito: la doctrina del repo es
segregación por ACCIÓN, no por vista.
"""
from odoo import _, api, models
from odoo.exceptions import AccessError

from .res_users import _ROL_CAP

_G_CAJA = "l10n_pe_ne_roles.group_l10n_pe_ne_caja"
_G_SUP = "l10n_pe_ne_roles.group_l10n_pe_ne_supervisor"


def _es_admin_plataforma(user):
    return (user._is_admin() or user.has_group("base.group_system")
            or user.has_group("base.group_erp_manager"))


def _check_rol_caja(env, que):
    user = env.user
    if _es_admin_plataforma(user):
        return
    if not any(user.has_group(x) for x in _ROL_CAP):
        return  # legacy sin roles NE: opera como pre-roles
    if not (user.has_group(_G_CAJA) or user.has_group(_G_SUP)):
        raise AccessError(_(
            "%s es del CAJERO (o de quien supervisa). Tu rol no maneja el dinero de la "
            "caja.") % que)


def _check_rol_politica(env, que):
    """Como _check_rol_caja pero SIN el cajero: cambiar la regla que a uno le aplica no es
    operar la caja, es supervisarla. El dueño la tiene por implicación de supervisor."""
    user = env.user
    if _es_admin_plataforma(user):
        return
    if not any(user.has_group(x) for x in _ROL_CAP):
        return  # legacy sin roles NE: opera como pre-roles
    if not user.has_group(_G_SUP):
        raise AccessError(_(
            "%s la decide el dueño o quien supervisa: si el cajero pudiera moverla, el control "
            "no controlaría nada.") % que)


class CajaSesionGates(models.Model):
    _inherit = "l10n_pe_ne.caja.sesion"

    @api.model
    def l10n_pe_ne_abrir_caja(self, datos):
        _check_rol_caja(self.env, _("Abrir la caja"))
        return super().l10n_pe_ne_abrir_caja(datos)

    @api.model
    def l10n_pe_ne_caja_movimiento(self, datos):
        _check_rol_caja(self.env, _("Registrar movimientos de caja"))
        return super().l10n_pe_ne_caja_movimiento(datos)

    @api.model
    def l10n_pe_ne_cerrar_caja(self, datos):
        _check_rol_caja(self.env, _("Cerrar la caja"))
        return super().l10n_pe_ne_cerrar_caja(datos)


class NegocioGates(models.Model):
    """La tolerancia de descuadre (C2) es la regla que decide cuándo un cierre exige explicación
    y dispara el aviso. Sin este muro, el mismo cajero que descuadra podría subirla a S/ 999 999
    desde /ne/api/negocio, cerrar sin escribir nada y sin que se avise a nadie — el control
    quedaría apagado por quien está controlado.

    Se gatea SOLO ese campo, y solo si CAMBIA: el resto del endpoint (razón social, dirección,
    datos de pago) conserva la autorización que ya tenía. Endurecerlo entero es una decisión
    aparte, no un efecto colateral de esta rebanada."""
    _inherit = "account.move"

    @api.model
    def l10n_pe_ne_update_negocio(self, vals):
        crudo = (vals or {}).get("toleranciaDescuadre")
        bruto = "" if crudo is None else str(crudo).strip()
        if bruto:
            try:
                nueva = round(float(bruto), 2)
            except (TypeError, ValueError):
                nueva = None   # el biller ya lo rechaza con su propio mensaje
            actual = round(self.env.company.l10n_pe_ne_cierre_tolerancia or 0.0, 2)
            if nueva is not None and nueva != actual:
                _check_rol_politica(self.env, _("La tolerancia de descuadre de caja"))
        return super().l10n_pe_ne_update_negocio(vals)


class GastoGates(models.Model):
    _inherit = "l10n_pe_ne.gasto"

    @api.model
    def l10n_pe_ne_create_gasto(self, gasto):
        _check_rol_caja(self.env, _("Registrar gastos"))
        return super().l10n_pe_ne_create_gasto(gasto)

    @api.model
    def l10n_pe_ne_update_gasto(self, gasto):
        _check_rol_caja(self.env, _("Editar gastos"))
        return super().l10n_pe_ne_update_gasto(gasto)

    @api.model
    def l10n_pe_ne_delete_gasto(self, rec_id):
        _check_rol_caja(self.env, _("Eliminar gastos"))
        return super().l10n_pe_ne_delete_gasto(rec_id)

    @api.model
    def l10n_pe_ne_reversar_gasto(self, rec_id, motivo=None):
        _check_rol_caja(self.env, _("Reversar gastos"))
        return super().l10n_pe_ne_reversar_gasto(rec_id, motivo=motivo)

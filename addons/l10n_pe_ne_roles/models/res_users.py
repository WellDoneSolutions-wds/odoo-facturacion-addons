# -*- coding: utf-8 -*-
"""res.users — roles (H-3) y alta de usuarios por el dueño del RUC (H-4).

H-3: añade al perfil las capacidades por rol (has_group), que la SPA usa para pintar el menú.

H-4: el DUEÑO del RUC da de alta, edita, desactiva y asigna roles a la gente de SU RUC, y solo
de su RUC. Se implementa como métodos .sudo() con WHITELIST de grupos otorgables y CERO filas de
ACL sobre res.users — porque en Odoo la regla res_users_rule es GLOBAL y no aísla por compañía, y
la única ACL con write sobre res.users es la de group_erp_manager (que ve toda la BD). Ver
docs/procesos-negocio/decision-alta-usuarios.md. En el momento en que se hace .sudo().write sobre
group_ids, el addon es el ÚNICO control de escalada: Odoo no tiene freno nativo (no hay constrains
anti-escalada al escribir group_ids). La whitelist + la red anti-escalada son ese control.
"""
from odoo import SUPERUSER_ID, _, api, models
from odoo.exceptions import AccessDenied, AccessError, UserError

# Capacidades por rol expuestas en el perfil (H-3). xmlid -> clave del perfil.
_ROL_CAP = {
    "l10n_pe_ne_roles.group_l10n_pe_ne_ventas": "puedeCotizar",
    "l10n_pe_ne_roles.group_l10n_pe_ne_caja": "puedeCobrar",
    "l10n_pe_ne_roles.group_l10n_pe_ne_despacho": "puedeDespachar",
    "l10n_pe_ne_roles.group_l10n_pe_ne_taller": "puedeTaller",
    "l10n_pe_ne_roles.group_l10n_pe_ne_supervisor": "puedeSupervisar",
    "l10n_pe_ne_roles.group_l10n_pe_ne_contador": "esContador",
    "l10n_pe_ne_roles.group_l10n_pe_ne_duenio": "esDuenio",
}

# WHITELIST — los ÚNICOS grupos que un dueño puede otorgar. Vive en Python (cambiarla exige PR +
# review + deploy, no un UPDATE). NO contiene 'duenio' (así set_roles no puede clonar ni destituir
# a un dueño) ni 'emisor' (es la base implícita, no un rol asignable). clave de rol -> xmlid.
_ROLES = {
    "ventas": "l10n_pe_ne_roles.group_l10n_pe_ne_ventas",
    "caja": "l10n_pe_ne_roles.group_l10n_pe_ne_caja",
    "despacho": "l10n_pe_ne_roles.group_l10n_pe_ne_despacho",
    "taller": "l10n_pe_ne_roles.group_l10n_pe_ne_taller",
    "supervisor": "l10n_pe_ne_roles.group_l10n_pe_ne_supervisor",
    "contador": "l10n_pe_ne_roles.group_l10n_pe_ne_contador",
    "anulacion": "l10n_pe_ne_biller.group_l10n_pe_ne_anulacion",
}
_ROL_DUENIO = "l10n_pe_ne_roles.group_l10n_pe_ne_duenio"
# base.group_user NO es un rol: es el marcador de "empleado interno". Sin él, _compute_share pone
# share=True y el usuario nace PORTAL (invisible para la SPA). Se otorga SIEMPRE al crear.
_GRUPO_INTERNO = "base.group_user"
# Prohibidos: dárselos (o dar un grupo que los implique) = entregar toda la plataforma.
_PROHIBIDOS = ("base.group_system", "base.group_erp_manager")
_MAX_LOGIN = 64
_MAX_NAME = 128


class ResUsers(models.Model):
    _inherit = "res.users"

    # ───────────────────────────────────────────────────────────── H-3 · perfil
    def l10n_pe_ne_perfil(self):
        perfil = super().l10n_pe_ne_perfil()
        self.ensure_one()
        for xmlid, clave in _ROL_CAP.items():
            perfil[clave] = self.has_group(xmlid)
        # H-4: la SPA muestra la pantalla de Equipo solo al dueño.
        perfil["puedeGestionarEquipo"] = perfil.get("esDuenio", False)
        return perfil

    # ─────────────────────────────────────────────────── H-4 · choke points
    def _l10n_pe_ne_duenio_company(self):
        """CHOKE POINT 1: todo método de gestión empieza aquí. Devuelve LA compañía (exactamente
        una) sobre la que el dueño puede actuar. sudo() no cambia env.uid, así que este has_group
        evalúa al llamante real y no se auto-concede."""
        user = self.env.user
        if not user.has_group(_ROL_DUENIO):
            raise AccessError(_("Solo el dueño del negocio puede gestionar usuarios."))
        if len(user.company_ids) != 1:
            raise AccessError(_(
                "Tu usuario alcanza más de una empresa y por eso no puede gestionar usuarios. "
                "Contacta con soporte."))
        company = user.company_ids
        if user.company_id != company:
            raise AccessError(_("Configuración de empresa inconsistente."))
        return company

    def _l10n_pe_ne_duenio_target(self, target_id, company):
        """CHOKE POINT 2: resuelve y VALIDA el usuario objetivo. Si esto deja pasar algo, no hay
        segunda línea de defensa (el ACL ya está puenteado por sudo)."""
        target = self.sudo().with_context(active_test=False).browse(int(target_id or 0)).exists()
        if not target:
            raise UserError(_("Usuario no encontrado."))
        # INCLUSIÓN (no intersección): el objetivo debe pertenecer SOLO a la compañía del dueño.
        # Un usuario que además alcanza otro RUC no es "de mi empresa" — negar, no adivinar.
        if target.company_ids != company:
            raise AccessError(_("Ese usuario no pertenece a tu empresa."))
        # Nunca tocar a un administrador de la plataforma ni al superusuario.
        if (target.id == SUPERUSER_ID or target.has_group("base.group_system")
                or target.has_group("base.group_erp_manager")):
            raise AccessError(_("No puedes gestionar a un administrador de la plataforma."))
        return target

    # ─────────────────────────────────────────────────── H-4 · whitelist + red
    def _l10n_pe_ne_grupos_whitelist(self):
        grupos = self.env["res.groups"]
        for xmlid in _ROLES.values():
            g = self.env.ref(xmlid, raise_if_not_found=False)
            if g:
                grupos |= g
        return grupos

    def _l10n_pe_ne_roles_a_grupos(self, roles):
        """Mapea claves de rol -> grupos, rechazando desconocidos, y pasa la red anti-escalada."""
        roles = list(roles or [])
        desconocidos = set(roles) - set(_ROLES)
        if desconocidos:
            raise UserError(_("Rol no válido: %s") % ", ".join(sorted(desconocidos)))
        grupos = self.env["res.groups"]
        for r in roles:
            g = self.env.ref(_ROLES[r], raise_if_not_found=False)
            if g:
                grupos |= g
        self._l10n_pe_ne_assert_sin_escalada(grupos)
        return grupos

    def _l10n_pe_ne_prohibidos(self):
        prohibidos = self.env["res.groups"]
        for xmlid in _PROHIBIDOS:
            g = self.env.ref(xmlid, raise_if_not_found=False)
            if g:
                prohibidos |= g
        return prohibidos

    def _l10n_pe_ne_assert_sin_escalada(self, grupos):
        """RED ANTI-ESCALADA (V5): ningún grupo otorgado puede SER ni IMPLICAR (cierre transitivo)
        un grupo prohibido. Se comprueba en TODA ruta que otorga grupos, ANTES de escribir."""
        if grupos.all_implied_ids & self._l10n_pe_ne_prohibidos():
            raise AccessError(_("Operación no permitida (escalada de privilegios)."))

    def _l10n_pe_ne_assert_target_limpio(self, target):
        """Defensa en profundidad: tras escribir, el objetivo no debe tener ningún prohibido."""
        if target.all_group_ids & self._l10n_pe_ne_prohibidos():
            raise AccessError(_("Operación no permitida (escalada de privilegios)."))

    # ─────────────────────────────────────────────────── H-4 · serialización
    def _l10n_pe_ne_equipo_dict(self, user):
        roles = [clave for clave, xmlid in _ROLES.items()
                 if user.has_group(xmlid)]
        return {
            "id": user.id,
            "name": user.name,
            "login": user.login,
            "email": user.email or "",
            "activo": user.active,
            "roles": roles,
            "esDuenio": user.has_group(_ROL_DUENIO),
        }

    # ─────────────────────────────────────────────────── H-4 · métodos públicos
    @api.model
    def l10n_pe_ne_duenio_list_equipo(self):
        company = self._l10n_pe_ne_duenio_company()
        # Búsqueda sudo con filtro EXPLÍCITO por compañía (la ir.rule global no aísla, hecho 1).
        usuarios = self.sudo().with_context(active_test=False).search(
            [("company_ids", "in", company.ids), ("share", "=", False)], order="name")
        out = []
        for u in usuarios:
            # Nunca listar a un administrador de la plataforma (system o erp_manager) ni al
            # superusuario, aunque estuviera atado a este RUC.
            if (u.company_ids != company or u.id == SUPERUSER_ID
                    or u.has_group("base.group_system")
                    or u.has_group("base.group_erp_manager")):
                continue
            out.append(self._l10n_pe_ne_equipo_dict(u))
        return out

    @api.model
    def l10n_pe_ne_duenio_alta(self, name, login, roles=None, email=None):
        company = self._l10n_pe_ne_duenio_company()
        company._l10n_pe_ne_check_cupo_usuarios()   # V2: tope de asientos
        name = (name or "").strip()[:_MAX_NAME]
        login = (login or "").strip()[:_MAX_LOGIN]
        if not name or not login:
            raise UserError(_("Indica el nombre y el usuario (login)."))
        grupos = self._l10n_pe_ne_roles_a_grupos(roles)
        interno = self.env.ref(_GRUPO_INTERNO)      # base.group_user (hecho 5)
        # V1: NO revelar si el login ya existe en OTRO RUC (el login es único en toda la BD). Un
        # mensaje genérico evita el oráculo de enumeración cross-tenant. (Rate-limit: a nivel infra.)
        if self.sudo().with_context(active_test=False).search_count([("login", "=", login)]):
            raise UserError(_("No se pudo crear ese acceso. Prueba con otro usuario (login)."))
        temp = self._l10n_pe_ne_gen_password()
        user = self.sudo().create({
            "name": name,
            "login": login,
            "email": email or False,
            "password": temp,
            "tz": "America/Lima",
            "company_id": company.id,
            "company_ids": [(6, 0, company.ids)],
            "group_ids": [(6, 0, (interno | grupos).ids)],
            "l10n_pe_ne_must_change_password": True,
        })
        self._l10n_pe_ne_assert_target_limpio(user)
        return {**self._l10n_pe_ne_equipo_dict(user), "password": temp}

    @api.model
    def l10n_pe_ne_duenio_set_roles(self, target_id, roles=None):
        company = self._l10n_pe_ne_duenio_company()
        target = self._l10n_pe_ne_duenio_target(target_id, company)
        grupos = self._l10n_pe_ne_roles_a_grupos(roles)
        interno = self.env.ref(_GRUPO_INTERNO)
        whitelist = self._l10n_pe_ne_grupos_whitelist()
        # Reemplaza SOLO los grupos de la whitelist: quita los no elegidos, añade los elegidos, y
        # conserva base.group_user y cualquier grupo fuera de la whitelist (p.ej. duenio: set_roles
        # nunca lo quita, así un dueño no se destituye por esta vía).
        a_quitar = whitelist - grupos
        target.sudo().write({"group_ids":
            [(3, g.id) for g in a_quitar] + [(4, g.id) for g in grupos] + [(4, interno.id)]})
        self._l10n_pe_ne_assert_target_limpio(target)
        return self._l10n_pe_ne_equipo_dict(target)

    @api.model
    def l10n_pe_ne_duenio_set_activo(self, target_id, activo):
        company = self._l10n_pe_ne_duenio_company()
        target = self._l10n_pe_ne_duenio_target(target_id, company)
        activo = bool(activo)
        if not activo:
            if target.id == self.env.user.id:
                raise UserError(_("No puedes desactivarte a ti mismo."))
            self._l10n_pe_ne_check_no_ultimo_duenio(company, excluir=target)   # V3
        else:
            company._l10n_pe_ne_check_cupo_usuarios()   # V2: el cupo también aplica al reactivar
        target.sudo().write({"active": activo})
        if not activo:
            # A4 (revisión Fable): al desactivar, revocar sus API keys (el Bearer del BFF es una key
            # nativa). Un usuario dado de baja no debe seguir autenticando en /ne/api hasta el TTL.
            # Paridad con el path admin del biller.
            self.env["res.users.apikeys"].sudo().search([("user_id", "=", target.id)]).unlink()
        return self._l10n_pe_ne_equipo_dict(target)

    def _l10n_pe_ne_check_no_ultimo_duenio(self, company, excluir):
        """V3 (auto-lockout): no dejar el RUC sin ningún dueño activo. Se bloquea la fila de la
        compañía (FOR UPDATE) para SERIALIZAR las bajas concurrentes de dueños del mismo RUC: dos
        transacciones que intenten quitar los dos últimos dueños a la vez no corren en paralelo —
        la segunda espera a la primera y ve su efecto."""
        self.env.cr.execute("SELECT id FROM res_company WHERE id = %s FOR UPDATE", (company.id,))
        duenio = self.env.ref(_ROL_DUENIO)
        # El admin de plataforma (base.group_system) tiene el grupo dueño en todos los RUC, pero NO
        # cuenta como dueño DEL TENANT: si contara, el guard nunca dispararía y el tenant podría
        # dejarse sin ningún dueño de autoservicio. Se lo EXCLUYE en Python con has_group: el filtro
        # de dominio ('all_group_ids','not in',system.id) NO excluye bien un m2m COMPUTADO (both
        # scalar and list forms devuelven al admin igual — confirmado en el e2e con Odoo real).
        otros = self.sudo().search([
            ("id", "!=", excluir.id),
            ("active", "=", True),
            ("company_ids", "in", company.ids),
            ("all_group_ids", "in", duenio.id),
        ]).filtered(lambda u: not u.has_group("base.group_system"))
        if not otros:
            raise UserError(_("No puedes desactivar al último dueño del negocio."))

    @api.model
    def l10n_pe_ne_duenio_reset_password(self, target_id):
        company = self._l10n_pe_ne_duenio_company()
        target = self._l10n_pe_ne_duenio_target(target_id, company)
        temp = self._l10n_pe_ne_gen_password()
        target.sudo().write({"password": temp, "l10n_pe_ne_must_change_password": True})
        # A4 (revisión Fable): rotar la contraseña revoca también las API keys existentes; si se
        # resetea por sospecha de robo, el Bearer viejo deja de autenticar de inmediato.
        self.env["res.users.apikeys"].sudo().search([("user_id", "=", target.id)]).unlink()
        return {"login": target.login, "name": target.name, "password": temp}

    @api.model
    def l10n_pe_ne_duenio_add_codueno(self, target_id, password):
        """Otorga el rol DUEÑO a un usuario del RUC. Es la ÚNICA operación que amplía el conjunto de
        gestores, así que exige RE-AUTENTICACIÓN con la contraseña del propio dueño: una sesión con
        el Bearer robado no debe poder crear otro dueño. (V4: duenio es contagioso y reversible
        entre pares dentro del RUC; es poder legítimo del dueño, documentado.)"""
        company = self._l10n_pe_ne_duenio_company()
        target = self._l10n_pe_ne_duenio_target(target_id, company)
        try:
            self.env.user._check_credentials(
                {"type": "password", "password": password or ""}, {"interactive": False})
        except AccessDenied:
            raise AccessError(_("Contraseña incorrecta."))
        duenio = self.env.ref(_ROL_DUENIO)
        target.sudo().write({"group_ids": [(4, duenio.id)]})
        self._l10n_pe_ne_assert_target_limpio(target)
        return self._l10n_pe_ne_equipo_dict(target)

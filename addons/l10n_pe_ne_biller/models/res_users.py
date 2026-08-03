import logging
import re
import secrets
import string

import werkzeug.urls

from odoo import _, api, fields, models
from odoo.exceptions import AccessDenied, AccessError, UserError

_logger = logging.getLogger(__name__)

_MIN_LEN = 8

# Solo estos orígenes (subdominios del SPA) pueden recibir el link de reset.
# Esquema wildcard: <t>.app.comercioagil.com (antes <t>-app.comercioagil.com).
_L10N_PE_NE_SPA_ORIGIN_RE = re.compile(r'^https://[a-z0-9-]+\.app\.comercioagil\.com$')


class ResUsers(models.Model):
    _inherit = 'res.users'

    l10n_pe_ne_must_change_password = fields.Boolean(
        string='Debe cambiar contraseña',
        default=False, copy=False,
        help="La contraseña actual es temporal (seteada por un admin): se fuerza el "
             "cambio en el próximo ingreso al SPA.")

    def l10n_pe_ne_perfil(self):
        """Perfil y permisos efectivos del usuario para la SPA — FUENTE ÚNICA. La consumen
        /ne/api/login y /ne/api/whoami (antes el dict estaba duplicado inline en ambas, con
        riesgo de divergir). El addon l10n_pe_ne_roles la EXTIENDE con super() para añadir las
        capacidades por rol (puedeCotizar, puedeCobrar, …). La SPA pinta el menú desde esto."""
        self.ensure_one()
        return {
            "user": self.name,
            "login": self.login,
            "companyId": self.company_id.id,
            "company": self.company_id.name,
            "ruc": self.company_id.vat or "",
            "isAdmin": self.has_group("base.group_system"),
            "puedeAnular": self.has_group("l10n_pe_ne_biller.group_l10n_pe_ne_anulacion"),
            "puedeConfigSeries": self._l10n_pe_ne_puede_config_series(),
            "mustChangePassword": self.l10n_pe_ne_must_change_password,
        }

    def _l10n_pe_ne_puede_config_series(self):
        """Quién puede tocar el registro de series y el catálogo de establecimientos. Desde que
        el local determina la serie, eso es cambiar la numeración fiscal de la empresa: se separa
        del grupo Emisor igual que se separó la anulación, para que un cajero facture sin poder
        renumerar el negocio.

        El administrador de plataforma pasa igual: es quien aprovisiona el tenant y da soporte,
        ya puede todo por otras vías, y negárselo dejaría sin salida al RUC que se quedara sin
        supervisor. Mismo trato que le dan los choke points de l10n_pe_ne_roles."""
        self.ensure_one()
        return (self.has_group("l10n_pe_ne_biller.group_l10n_pe_ne_config_series")
                or self.has_group("base.group_system"))

    def _l10n_pe_ne_check_config_series(self):
        """Muro compartido por l10n_pe_ne.serie y l10n_pe_ne.establecimiento. Vive en el MODELO
        (no solo en el controller) para que ninguna vía —RPC, backend, un endpoint futuro— se lo
        pueda saltar; el controller solo lo refleja como 403."""
        if not self._l10n_pe_ne_puede_config_series():
            raise AccessError(_(
                "No tienes permiso para configurar las series de numeración ni los "
                "establecimientos: determinan la numeración fiscal del negocio. Pídeselo al "
                "dueño o al supervisor."))

    @api.model
    def _l10n_pe_ne_gen_password(self, length=14):
        """Contraseña temporal alfanumérica (sin símbolos ambiguos, para dictarla).
        Garantiza al menos una mayúscula, una minúscula y un número, así cumple la
        política (_l10n_pe_ne_check_password_policy) aun siendo aleatoria."""
        length = max(length, _MIN_LEN)
        chars = [secrets.choice(string.ascii_uppercase),
                 secrets.choice(string.ascii_lowercase),
                 secrets.choice(string.digits)]
        pool = string.ascii_letters + string.digits
        chars += [secrets.choice(pool) for _ in range(length - len(chars))]
        secrets.SystemRandom().shuffle(chars)  # que las obligatorias no queden siempre al inicio
        return ''.join(chars)

    @api.model
    def _l10n_pe_ne_check_password_policy(self, password):
        """Política de contraseña, alineada con el schema del front (passwordSchema):
        mínimo 8 caracteres, al menos una mayúscula y un número. Se valida en el SERVIDOR
        para que un POST directo (saltándose el front) no pueda fijar una clave débil."""
        pw = password or ''
        if len(pw) < _MIN_LEN:
            raise UserError(_("La contraseña debe tener al menos %d caracteres.") % _MIN_LEN)
        if not re.search(r'[A-Z]', pw):
            raise UserError(_("La contraseña debe incluir al menos una mayúscula."))
        if not re.search(r'\d', pw):
            raise UserError(_("La contraseña debe incluir al menos un número."))

    @api.model
    def l10n_pe_ne_admin_reset_password(self, target_id, new_password=None, force_change=True):
        """Un admin fija (o genera) la contraseña de otro usuario. Devuelve la clave
        UNA sola vez. Revoca las API keys del target (cierra sus sesiones)."""
        if not self.env.user.has_group('base.group_system'):
            raise AccessError(_("Solo un administrador puede resetear contraseñas."))
        target = self.sudo().browse(int(target_id)).exists()
        if not target or not target.active:
            raise UserError(_("Usuario no encontrado o inactivo."))
        if target.share:
            raise UserError(_("Solo se puede resetear usuarios internos."))
        # Scope por compañía: el target debe compartir alguna compañía con el admin.
        if not (target.company_ids & self.env.user.company_ids):
            raise AccessError(_("No puedes gestionar usuarios de otra empresa."))
        pw = (new_password or '').strip() or self._l10n_pe_ne_gen_password()
        self._l10n_pe_ne_check_password_policy(pw)
        target.write({'password': pw, 'l10n_pe_ne_must_change_password': bool(force_change)})
        # Revoca sesiones activas del target (una API key sobrevive al cambio de clave).
        self.env['res.users.apikeys'].sudo().search([('user_id', '=', target.id)]).unlink()
        _logger.info("NE admin reset: %s -> %s", self.env.user.login, target.login)
        return {'login': target.login, 'name': target.name, 'password': pw}

    @api.model
    def l10n_pe_ne_change_own_password(self, current_password, new_password):
        """El usuario logueado cambia su propia contraseña. Verifica la actual,
        valida la nueva, limpia el flag de cambio forzado y revoca TODAS sus API
        keys (cierra las demás sesiones; el controller mintea un token rotado
        para que la sesión actual continúe)."""
        user = self.env.user
        current = current_password or ''
        new = (new_password or '').strip()
        self._l10n_pe_ne_check_password_policy(new)
        try:
            user._check_credentials({'type': 'password', 'password': current}, {'interactive': False})
        except AccessDenied:
            raise UserError(_("La contraseña actual no es correcta."))
        if new == current:
            raise UserError(_("La nueva contraseña debe ser distinta de la actual."))
        user.sudo().write({'password': new, 'l10n_pe_ne_must_change_password': False})
        # Cerrar las demás sesiones: una API key sobrevive al cambio de clave, así
        # que se revocan TODAS; el controller entrega una fresca a la sesión actual.
        self.env['res.users.apikeys'].sudo().search([('user_id', '=', user.id)]).unlink()
        return {'ok': True}

    @api.model
    def l10n_pe_ne_list_manageable_users(self):
        """Usuarios internos activos de las compañías del admin (para el panel de reset)."""
        if not self.env.user.has_group('base.group_system'):
            raise AccessError(_("Solo un administrador puede ver los usuarios."))
        company_ids = self.env.user.company_ids.ids
        users = self.sudo().search([
            ('share', '=', False),
            ('active', '=', True),
            ('company_ids', 'in', company_ids),
        ], order='login')
        return [{
            'id': u.id,
            'login': u.login,
            'name': u.name,
            'email': u.email or '',
            'company': u.company_id.name,
            'companyId': u.company_id.id,
            'isAdmin': u.has_group('base.group_system'),
        } for u in users]

    @api.model
    def l10n_pe_ne_request_password_reset(self, login, origin):
        """Fase 2 self-service: si existe una cuenta activa CON correo, genera el token de
        reset (auth_signup) y envía el link al SPA.

        La respuesta es SIEMPRE genérica —exista o no la cuenta, tenga o no correo, esté o
        no en cooldown— para NO permitir enumerar usuarios/correos: un atacante no puede
        distinguir un login válido de uno inválido por la respuesta. (Antes lanzaba
        errores explícitos 'no existe la cuenta' / 'no tiene correo', que sí enumeraban.)"""
        origin = (origin or '').rstrip('/')
        ok_origin = bool(_L10N_PE_NE_SPA_ORIGIN_RE.match(origin)) or origin.startswith('http://localhost')
        if not ok_origin:
            raise UserError(_("Origen no permitido."))
        login = (login or '').strip()
        if not login:
            raise UserError(_("Indica tu usuario o correo."))
        # Acepta usuario (login exacto) o correo (case-insensitive), como el reset nativo.
        user = self.sudo().search([('active', '=', True), ('login', '=', login)], limit=1) \
            or self.sudo().search([('active', '=', True), ('email', '=ilike', login)], limit=1)
        # Solo se hace trabajo cuando hay una cuenta interna con correo; en cualquier otro
        # caso se cae directo a la respuesta genérica de abajo, sin revelar nada.
        if user and not user.share and user.email:
            icp = self.env['ir.config_parameter'].sudo()
            key = 'l10n_pe_ne.reset_cooldown.%d' % user.id
            last = icp.get_param(key)
            now = fields.Datetime.now()
            # Rate-limit: 1 correo por usuario cada 60s. En cooldown NO reenvía —y tampoco lo
            # revela: sale por la misma respuesta genérica que un login inexistente.
            if not (last and (now - fields.Datetime.to_datetime(last)).total_seconds() < 60):
                icp.set_param(key, fields.Datetime.to_string(now))
                user.partner_id.signup_prepare(signup_type='reset')
                token = user.partner_id._generate_signup_token()
                link = '%s/reset?token=%s' % (origin, werkzeug.urls.url_quote(token))
                self._l10n_pe_ne_send_reset_email(user, link)
        return {'ok': True}

    def _l10n_pe_ne_send_reset_email(self, user, link):
        company_name = user.company_id.name or 'NE Express'
        body = (
            '<div style="font-family:sans-serif;font-size:14px;color:#111">'
            '<p>Hola %s,</p>'
            '<p>Recibimos una solicitud para restablecer tu contrase&ntilde;a en '
            '<b>%s</b>. Haz clic en el bot&oacute;n para crear una nueva:</p>'
            '<p><a href="%s" style="background:#5046E4;color:#fff;padding:10px 18px;'
            'border-radius:8px;text-decoration:none;display:inline-block">'
            'Restablecer contrase&ntilde;a</a></p>'
            '<p style="color:#666;font-size:12px">Si el bot&oacute;n no funciona, copia este enlace:<br>%s</p>'
            '<p style="color:#666;font-size:12px">Si no fuiste t&uacute;, ignora este correo. '
            'El enlace vence en 4 horas.</p></div>'
        ) % (user.name or user.login, company_name, link, link)
        mail = self.env['mail.mail'].sudo().create({
            'subject': 'Restablece tu contraseña — NE Express',
            'email_from': user.company_id.email_formatted or user.email_formatted,
            'email_to': user.email,
            'body_html': body,
            'auto_delete': True,
            'message_type': 'user_notification',
        })
        mail.send()
        _logger.info("NE reset email enviado a %s (user %s)", user.email, user.login)

    @api.model
    def l10n_pe_ne_confirm_password_reset(self, token, password):
        """Fase 2: valida el token de reset y fija la contraseña nueva."""
        password = (password or '').strip()
        self._l10n_pe_ne_check_password_policy(password)
        Partner = self.env['res.partner'].sudo()
        try:
            partner = Partner._signup_retrieve_partner(token, check_validity=True, raise_exception=True)
        except Exception:
            raise UserError(_("El enlace no es válido o expiró. Solicita uno nuevo."))
        user = partner.user_ids[:1]
        if not user:
            raise UserError(_("El enlace no es válido."))
        user.sudo().write({'password': password})
        self.env['res.users.apikeys'].sudo().search([('user_id', '=', user.id)]).unlink()
        _logger.info("NE reset confirmado para user %s", user.login)
        return {'ok': True}

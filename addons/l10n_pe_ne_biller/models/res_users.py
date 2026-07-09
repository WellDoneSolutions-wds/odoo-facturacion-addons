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

    @api.model
    def _l10n_pe_ne_gen_password(self, length=14):
        """Contraseña temporal alfanumérica (sin ambigüedad de símbolos para dictarla)."""
        alphabet = string.ascii_letters + string.digits
        return ''.join(secrets.choice(alphabet) for _ in range(length))

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
        if len(pw) < _MIN_LEN:
            raise UserError(_("La contraseña debe tener al menos %d caracteres.") % _MIN_LEN)
        target.write({'password': pw, 'l10n_pe_ne_must_change_password': bool(force_change)})
        # Revoca sesiones activas del target (una API key sobrevive al cambio de clave).
        self.env['res.users.apikeys'].sudo().search([('user_id', '=', target.id)]).unlink()
        _logger.info("NE admin reset: %s -> %s", self.env.user.login, target.login)
        return {'login': target.login, 'name': target.name, 'password': pw}

    @api.model
    def l10n_pe_ne_change_own_password(self, current_password, new_password):
        """El usuario logueado cambia su propia contraseña. Verifica la actual,
        valida la nueva, limpia el flag de cambio forzado. Mantiene la sesión actual."""
        user = self.env.user
        current = current_password or ''
        new = (new_password or '').strip()
        if len(new) < _MIN_LEN:
            raise UserError(_("La nueva contraseña debe tener al menos %d caracteres.") % _MIN_LEN)
        try:
            user._check_credentials({'type': 'password', 'password': current}, {'interactive': False})
        except AccessDenied:
            raise UserError(_("La contraseña actual no es correcta."))
        if new == current:
            raise UserError(_("La nueva contraseña debe ser distinta de la actual."))
        user.sudo().write({'password': new, 'l10n_pe_ne_must_change_password': False})
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
        """Fase 2 self-service: valida la cuenta, genera el token de reset
        (auth_signup) y envía el correo con link al SPA. Lanza errores explícitos
        (como el reset nativo de Odoo): sin cuenta / sin correo. El correo va SIEMPRE
        al email de la ficha del usuario (no al texto ingresado)."""
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
        if not user or user.share:
            raise UserError(_("No se encontró una cuenta para este inicio de sesión."))
        if not user.email:
            raise UserError(_("No se puede enviar el correo electrónico: el usuario %s no tiene dirección de correo electrónico.") % user.name)
        # Rate-limit simple: 1 correo por usuario cada 60s.
        icp = self.env['ir.config_parameter'].sudo()
        key = 'l10n_pe_ne.reset_cooldown.%d' % user.id
        last = icp.get_param(key)
        now = fields.Datetime.now()
        if last and (now - fields.Datetime.to_datetime(last)).total_seconds() < 60:
            raise UserError(_("Ya te enviamos un enlace hace un momento. Revisa tu correo (y spam) o espera un minuto."))
        icp.set_param(key, fields.Datetime.to_string(now))
        user.partner_id.signup_prepare(signup_type='reset')
        token = user.partner_id._generate_signup_token()
        link = '%s/reset?token=%s' % (origin, werkzeug.urls.url_quote(token))
        self._l10n_pe_ne_send_reset_email(user, link)
        return {'ok': True, 'email': self._l10n_pe_ne_mask_email(user.email)}

    @api.model
    def _l10n_pe_ne_mask_email(self, email):
        """a***o@dominio.com — para confirmar el destino sin exponerlo entero."""
        try:
            local, domain = (email or '').split('@', 1)
        except ValueError:
            return email or ''
        shown = local[:1] + '*' if len(local) <= 2 else local[0] + ('*' * (len(local) - 2)) + local[-1]
        return '%s@%s' % (shown, domain)

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
        if len(password) < _MIN_LEN:
            raise UserError(_("La contraseña debe tener al menos %d caracteres.") % _MIN_LEN)
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

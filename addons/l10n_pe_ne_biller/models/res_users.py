import logging
import secrets
import string

from odoo import _, api, fields, models
from odoo.exceptions import AccessDenied, AccessError, UserError

_logger = logging.getLogger(__name__)

_MIN_LEN = 8


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

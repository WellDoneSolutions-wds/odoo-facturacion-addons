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

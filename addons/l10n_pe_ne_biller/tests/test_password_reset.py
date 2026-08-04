import json

from odoo.tests import TransactionCase, tagged
from odoo.exceptions import AccessError, UserError


@tagged('post_install', '-at_install')
class TestPasswordReset(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        Users = cls.env['res.users']
        cls.company_a = cls.env['res.company'].with_context(l10n_pe_ne_allow_company_create=True).create({'name': 'Co A', 'vat': '20000000001'})
        cls.company_b = cls.env['res.company'].with_context(l10n_pe_ne_allow_company_create=True).create({'name': 'Co B', 'vat': '20000000002'})
        cls.admin = Users.create({
            'name': 'Admin A', 'login': 'pr_admin_a',
            'company_id': cls.company_a.id, 'company_ids': [(6, 0, [cls.company_a.id])],
            'group_ids': [(4, cls.env.ref('base.group_system').id)],
        })
        cls.user_a = Users.create({
            'name': 'User A', 'login': 'pr_user_a', 'password': 'oldpass12',
            'company_id': cls.company_a.id, 'company_ids': [(6, 0, [cls.company_a.id])],
            'group_ids': [(4, cls.env.ref('base.group_user').id)],
        })
        cls.user_b = Users.create({
            'name': 'User B', 'login': 'pr_user_b',
            'company_id': cls.company_b.id, 'company_ids': [(6, 0, [cls.company_b.id])],
            'group_ids': [(4, cls.env.ref('base.group_user').id)],
        })

    def test_field_exists(self):
        self.assertIn('l10n_pe_ne_must_change_password', self.env['res.users']._fields)

    def test_non_admin_cannot_reset(self):
        with self.assertRaises(AccessError):
            self.env['res.users'].with_user(self.user_a).l10n_pe_ne_admin_reset_password(self.user_b.id)

    def test_admin_cannot_reset_cross_company(self):
        with self.assertRaises(AccessError):
            self.env['res.users'].with_user(self.admin).l10n_pe_ne_admin_reset_password(self.user_b.id)

    def test_admin_reset_generates_temp_and_sets_flag(self):
        res = self.env['res.users'].with_user(self.admin).l10n_pe_ne_admin_reset_password(self.user_a.id)
        self.assertEqual(res['login'], 'pr_user_a')
        self.assertGreaterEqual(len(res['password']), 8)
        self.assertTrue(self.user_a.l10n_pe_ne_must_change_password)

    def test_admin_reset_revokes_apikeys(self):
        key = self.env['res.users.apikeys'].with_user(self.user_a).sudo()._generate('l10n_pe_ne', 'test', False)
        self.assertTrue(self.env['res.users.apikeys'].sudo().search([('user_id', '=', self.user_a.id)]))
        self.env['res.users'].with_user(self.admin).l10n_pe_ne_admin_reset_password(self.user_a.id)
        self.assertFalse(self.env['res.users.apikeys'].sudo().search([('user_id', '=', self.user_a.id)]))
        del key

    def test_change_own_wrong_current_raises(self):
        with self.assertRaises(UserError):
            self.env['res.users'].with_user(self.user_a).l10n_pe_ne_change_own_password('mala', 'NuevaPass12')

    def test_change_own_too_short_raises(self):
        with self.assertRaises(UserError):
            self.env['res.users'].with_user(self.user_a).l10n_pe_ne_change_own_password('oldpass12', 'corta')

    def test_change_own_success_clears_flag(self):
        self.user_a.l10n_pe_ne_must_change_password = True
        res = self.env['res.users'].with_user(self.user_a).l10n_pe_ne_change_own_password('oldpass12', 'NuevaPass34')
        self.assertEqual(res, {'ok': True})
        self.assertFalse(self.user_a.l10n_pe_ne_must_change_password)

    def test_change_own_revokes_apikeys(self):
        self.env['res.users.apikeys'].with_user(self.user_a).sudo()._generate('l10n_pe_ne', 'test', False)
        self.assertTrue(self.env['res.users.apikeys'].sudo().search([('user_id', '=', self.user_a.id)]))
        self.env['res.users'].with_user(self.user_a).l10n_pe_ne_change_own_password('oldpass12', 'NuevaPass56')
        self.assertFalse(self.env['res.users.apikeys'].sudo().search([('user_id', '=', self.user_a.id)]))

    def test_confirm_reset_revokes_apikeys(self):
        self.env['res.users.apikeys'].with_user(self.user_a).sudo()._generate('l10n_pe_ne', 'test', False)
        partner = self.user_a.partner_id
        partner.signup_prepare(signup_type='reset')
        token = partner._generate_signup_token()
        res = self.env['res.users'].l10n_pe_ne_confirm_password_reset(token, 'ClaveNueva99')
        self.assertEqual(res, {'ok': True})
        self.assertFalse(self.env['res.users.apikeys'].sudo().search([('user_id', '=', self.user_a.id)]))

    # ---- Endurecimiento (issues #1 enumeración, #2 política de contraseña) ----
    def test_password_policy_requires_upper_and_digit(self):
        """La política se valida en el servidor: mínimo 8 + mayúscula + número (issue #2)."""
        U = self.env['res.users'].with_user(self.user_a)
        with self.assertRaises(UserError):  # falta mayúscula
            U.l10n_pe_ne_change_own_password('oldpass12', 'minusculas9')
        with self.assertRaises(UserError):  # falta número
            U.l10n_pe_ne_change_own_password('oldpass12', 'SinNumeroAqui')

    def test_admin_generated_password_meets_policy(self):
        """La clave temporal autogenerada cumple la política (mayúscula + número)."""
        pw = self.env['res.users']._l10n_pe_ne_gen_password()
        # No debe lanzar:
        self.env['res.users']._l10n_pe_ne_check_password_policy(pw)

    def test_request_reset_generic_no_enumeration(self):
        """La solicitud responde SIEMPRE {ok:True}, exista o no la cuenta (issue #1)."""
        R = self.env['res.users']
        O = 'https://demo.app.ekipu.pe'
        self.assertEqual(R.l10n_pe_ne_request_password_reset('no-existe-xyz@nadie.pe', O), {'ok': True})
        self.assertEqual(R.l10n_pe_ne_request_password_reset('pr_user_a', O), {'ok': True})  # existe, sin correo

    def test_list_users_non_admin_raises(self):
        with self.assertRaises(AccessError):
            self.env['res.users'].with_user(self.user_a).l10n_pe_ne_list_manageable_users()

    def test_list_users_scoped_to_company(self):
        rows = self.env['res.users'].with_user(self.admin).l10n_pe_ne_list_manageable_users()
        logins = {r['login'] for r in rows}
        self.assertIn('pr_user_a', logins)
        self.assertNotIn('pr_user_b', logins)
        self.assertTrue(all('id' in r and 'name' in r for r in rows))


from odoo.tests import HttpCase


@tagged('post_install', '-at_install')
class TestPasswordResetRoutes(HttpCase):
    def test_admin_users_requires_auth(self):
        r = self.url_open('/ne/api/admin/users')
        self.assertEqual(r.status_code, 401)

    def test_change_password_requires_auth(self):
        r = self.url_open('/ne/api/change-password', data='{}',
                          headers={'Content-Type': 'application/json'})
        self.assertEqual(r.status_code, 401)

    def test_change_password_rotates_token(self):
        user = self.env['res.users'].create({
            'name': 'Rot User', 'login': 'pr_rot_user', 'password': 'oldpass12',
            'group_ids': [(4, self.env.ref('base.group_user').id)],
        })
        old_token = self.env['res.users.apikeys'].with_user(user).sudo()._generate('l10n_pe_ne', 'test', False)
        r = self.url_open(
            '/ne/api/change-password',
            data=json.dumps({'current': 'oldpass12', 'new': 'NuevaPass99'}),
            headers={'Content-Type': 'application/json', 'Authorization': 'Bearer ' + old_token},
        )
        self.assertEqual(r.status_code, 200)
        d = r.json()
        self.assertTrue(d.get('ok'))
        self.assertTrue(d.get('token'), 'la respuesta debe traer el token rotado')
        self.assertTrue(d.get('expires'))
        self.assertNotEqual(d['token'], old_token)
        # El token viejo quedó revocado; el rotado autentica.
        r_old = self.url_open('/ne/api/whoami', headers={'Authorization': 'Bearer ' + old_token})
        self.assertEqual(r_old.status_code, 401)
        r_new = self.url_open('/ne/api/whoami', headers={'Authorization': 'Bearer ' + d['token']})
        self.assertEqual(r_new.status_code, 200)

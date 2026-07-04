from odoo.tests import TransactionCase, tagged
from odoo.exceptions import AccessError, UserError


@tagged('post_install', '-at_install')
class TestPasswordReset(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        Users = cls.env['res.users']
        cls.company_a = cls.env['res.company'].create({'name': 'Co A', 'vat': '20000000001'})
        cls.company_b = cls.env['res.company'].create({'name': 'Co B', 'vat': '20000000002'})
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
            self.env['res.users'].with_user(self.user_a).l10n_pe_ne_change_own_password('mala', 'nuevapass12')

    def test_change_own_too_short_raises(self):
        with self.assertRaises(UserError):
            self.env['res.users'].with_user(self.user_a).l10n_pe_ne_change_own_password('oldpass12', 'corta')

    def test_change_own_success_clears_flag(self):
        self.user_a.l10n_pe_ne_must_change_password = True
        res = self.env['res.users'].with_user(self.user_a).l10n_pe_ne_change_own_password('oldpass12', 'nuevapass34')
        self.assertEqual(res, {'ok': True})
        self.assertFalse(self.user_a.l10n_pe_ne_must_change_password)

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

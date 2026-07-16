from odoo.tests import TransactionCase, tagged
from odoo.addons.l10n_pe_ne_biller.hooks import post_init_hook


@tagged('post_install', '-at_install')
class TestAdminEmisorGroup(TransactionCase):
    def setUp(self):
        super().setUp()
        self.emisor = self.env.ref('l10n_pe_ne_biller.group_l10n_pe_ne_emisor')
        self.anulacion = self.env.ref('l10n_pe_ne_biller.group_l10n_pe_ne_anulacion')
        self.admin = self.env.ref('base.user_admin')

    def test_post_init_hook_deja_admin_pudiendo_emitir_y_anular(self):
        # Partimos de un estado conocido: admin sin ninguno de los dos grupos.
        self.admin.write({'group_ids': [(3, self.anulacion.id), (3, self.emisor.id)]})
        self.assertFalse(
            self.admin.has_group('l10n_pe_ne_biller.group_l10n_pe_ne_emisor'),
            "Precondición: el admin no debería poder emitir antes del hook")

        post_init_hook(self.env)

        # El hook asigna SOLO el grupo de anulación: como implica el de emisor, con
        # uno basta para que el admin quede pudiendo emitir Y anular.
        self.assertIn(self.anulacion, self.admin.group_ids)
        self.assertTrue(
            self.admin.has_group('l10n_pe_ne_biller.group_l10n_pe_ne_anulacion'),
            "El admin debe poder anular tras el hook")
        self.assertTrue(
            self.admin.has_group('l10n_pe_ne_biller.group_l10n_pe_ne_emisor'),
            "El grupo de anulación implica el de emisor: el admin debe poder emitir")

    def test_anulacion_implica_emisor(self):
        """La implicación es la que sostiene el hook, la migración y el check del
        controller: se afirma sola para que no se pierda en un refactor del XML."""
        self.assertIn(self.emisor, self.anulacion.implied_ids)

    def test_emisor_solo_no_puede_anular(self):
        """El punto de separar los grupos: un cajero factura pero no da de baja."""
        cajero = self.env['res.users'].create({
            'name': 'CAJERO', 'login': 'cajero_test_anulacion',
            'group_ids': [(6, 0, [self.emisor.id])]})
        self.assertTrue(cajero.has_group('l10n_pe_ne_biller.group_l10n_pe_ne_emisor'))
        self.assertFalse(
            cajero.has_group('l10n_pe_ne_biller.group_l10n_pe_ne_anulacion'),
            "Un emisor sin el grupo de anulación NO debe poder anular")

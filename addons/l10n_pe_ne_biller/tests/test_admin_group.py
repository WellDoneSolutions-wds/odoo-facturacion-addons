from odoo.tests import TransactionCase, tagged
from odoo.addons.l10n_pe_ne_biller.hooks import post_init_hook


@tagged('post_install', '-at_install')
class TestAdminEmisorGroup(TransactionCase):
    def test_post_init_hook_deja_admin_en_grupo_emisor(self):
        group = self.env.ref('l10n_pe_ne_biller.group_l10n_pe_ne_emisor')
        admin = self.env.ref('base.user_admin')

        # Partimos de un estado conocido: admin SIN el grupo emisor.
        admin.write({'group_ids': [(3, group.id)]})
        self.assertNotIn(
            group, admin.group_ids,
            "Precondición: el admin no debería tener el grupo antes del hook")

        # El hook (install-only en producción) debe dejarlo dentro del grupo.
        post_init_hook(self.env)
        self.assertIn(
            group, admin.group_ids,
            "El admin debe quedar en el grupo Emisor NE Express tras el hook")

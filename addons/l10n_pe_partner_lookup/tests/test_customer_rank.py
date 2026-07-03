from odoo.tests import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestPartnerLookupCustomerRank(TransactionCase):
    """El cliente creado desde la búsqueda por DNI/RUC debe aparecer en «Clientes»
    (customer_rank > 0), no solo en «Contactos». Regresión de un caso donde el
    partner se creaba con customer_rank = 0 y no salía en el listado de clientes.
    """

    def _data(self):
        return {
            'doc_number': '20605145648',
            'doc_type': 'RUC',
            'name': 'CLIENTE SAC',
            'address': 'AV. SIEMPRE VIVA 123',
            'state': 'ACTIVO',
        }

    def test_prepare_vals_sets_customer_rank(self):
        vals = self.env['res.partner']._l10n_pe_prepare_partner_vals(self._data())
        self.assertEqual(
            vals.get('customer_rank'), 1,
            "El partner debe crearse como cliente (customer_rank = 1) para "
            "aparecer en «Clientes», no solo en «Contactos».")

    def test_prepare_vals_sets_company(self):
        vals = self.env['res.partner']._l10n_pe_prepare_partner_vals(self._data())
        self.assertEqual(
            vals.get('company_id'), self.env.company.id,
            "El cliente debe quedar aislado en la compañía del emisor "
            "(company_id), no compartido entre tenants (company_id=False).")

    def test_created_partner_is_customer(self):
        Partner = self.env['res.partner']
        partner = Partner.create(Partner._l10n_pe_prepare_partner_vals(self._data()))
        self.assertGreater(
            partner.customer_rank, 0,
            "El cliente recién creado no tiene customer_rank > 0.")
        self.assertEqual(
            partner.company_id, self.env.company,
            "El cliente no quedó aislado en la compañía del emisor.")
        # Debe ser visible bajo el dominio del listado de «Clientes».
        self.assertIn(
            partner,
            Partner.search([('customer_rank', '>', 0), ('id', '=', partner.id)]),
            "El cliente no aparece en el listado de clientes (customer_rank > 0).")

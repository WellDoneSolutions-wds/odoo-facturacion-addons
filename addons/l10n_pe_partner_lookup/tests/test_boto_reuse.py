from unittest.mock import MagicMock, patch

from odoo.tests import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestLookupResourceReuse(TransactionCase):
    """El autocomplete RUC/DNI creaba un boto3.resource('dynamodb') por
    consulta — 100-400ms de CPU en cada tecleo. Se memoiza por región."""

    def setUp(self):
        super().setUp()
        icp = self.env['ir.config_parameter'].sudo()
        icp.set_param('l10n_pe_partner_lookup.dynamo_table', 'ruc-dni-cache')
        icp.set_param('l10n_pe_partner_lookup.aws_region', 'us-east-1')

    def test_resource_dynamo_se_memoiza(self):
        Partner = self.env['res.partner']
        fake = MagicMock()
        fake.resource.return_value.Table.return_value.get_item.return_value = {}
        with patch('odoo.addons.l10n_pe_partner_lookup.models.res_partner.boto3', fake):
            Partner._l10n_pe_query_dynamodb('20605145648')
            Partner._l10n_pe_query_dynamodb('20605145648')
        self.assertEqual(fake.resource.call_count, 1,
                         "dos lookups → un solo resource (memoizado)")
        self.assertEqual(fake.resource.return_value.Table.call_count, 2,
                         "Table() por llamada es barato y se mantiene por consulta")

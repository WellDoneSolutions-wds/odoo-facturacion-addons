from unittest.mock import patch

from odoo.tests import TransactionCase, tagged

_MOD = 'odoo.addons.l10n_pe_ne_biller.models.account_move_biller'


@tagged('post_install', '-at_install')
class TestBotoClientReuse(TransactionCase):
    """Crear un cliente boto3 cuesta 100-400ms de CPU (carga los modelos del
    servicio); se pagaba DOS veces en cada emisión async (dynamodb + sqs).
    El helper memoiza por (service, region) a nivel de worker."""

    def test_cliente_se_memoiza_por_servicio_y_region(self):
        Move = self.env['account.move']
        with patch(_MOD + '.boto3') as mb:
            c1 = Move._l10n_pe_boto_client('sqs', 'us-east-1')
            c2 = Move._l10n_pe_boto_client('sqs', 'us-east-1')
            self.assertIs(c1, c2, "misma clave → mismo cliente, sin recrear")
            self.assertEqual(mb.client.call_count, 1)
            # Servicio o región distintos → cliente propio.
            Move._l10n_pe_boto_client('dynamodb', 'us-east-1')
            Move._l10n_pe_boto_client('sqs', 'us-east-2')
            self.assertEqual(mb.client.call_count, 3)

    def test_cache_se_rehace_si_cambia_el_modulo_boto3(self):
        # Los tests parchean el módulo boto3; el cache no debe servir clientes
        # de un boto3 anterior (mocks de otro test) ni al revés.
        Move = self.env['account.move']
        with patch(_MOD + '.boto3') as _mb1:
            c1 = Move._l10n_pe_boto_client('sqs', 'us-east-1')
            self.assertIsNotNone(c1)
        with patch(_MOD + '.boto3') as mb2:
            c2 = Move._l10n_pe_boto_client('sqs', 'us-east-1')
            self.assertEqual(mb2.client.call_count, 1,
                             "boto3 distinto → se reconstruye, no sirve el mock viejo")
            self.assertIsNot(c1, c2)

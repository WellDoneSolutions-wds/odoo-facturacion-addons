import time as time_mod
from unittest.mock import MagicMock, patch

import requests

from odoo.tests import TransactionCase, tagged

RUTA = 'odoo.addons.l10n_pe_partner_lookup.models.res_partner'

# Lista de resultados por DNI: el ancla aRucs trae el RUC10 de la persona.
SUNAT_LIST_HTML = """<html><body>
<a class="aRucs" data-ruc="10737559761">
  <div class="list-group-item-heading">10737559761</div>
  <div class="list-group-item-heading">PEREZ QUISPE JUAN</div>
  <p class="list-group-item-text">Estado: ACTIVO</p>
</a></body></html>"""


@tagged('post_install', '-at_install')
class TestCacheWriteback(TransactionCase):
    """Write-back a la cache DynamoDB tras un scraping SUNAT exitoso, y retry
    corto ante Timeout. La cache nace vacía y nadie más la puebla: sin esto,
    toda consulta pega a SUNAT."""

    def setUp(self):
        super().setUp()
        icp = self.env['ir.config_parameter'].sudo()
        icp.set_param('l10n_pe_partner_lookup.mode', 'dynamodb')
        icp.set_param('l10n_pe_partner_lookup.dynamo_table', 'ruc-dni-cache')
        icp.set_param('l10n_pe_partner_lookup.aws_region', 'us-east-1')
        icp.set_param('l10n_pe_partner_lookup.sunat_enabled', 'True')

    # -- helpers ---------------------------------------------------------
    def _fake_session(self, get_side_effects=None):
        """Session mockeada: get/post OK por defecto; get acepta side_effects."""
        response = MagicMock()
        response.status_code = 200
        response.text = SUNAT_LIST_HTML
        response.raise_for_status.return_value = None
        session = MagicMock()
        if get_side_effects:
            session.get.side_effect = get_side_effects
        else:
            session.get.return_value = response
        session.post.return_value = response
        return session

    def _fake_boto(self):
        fake = MagicMock()
        return fake, fake.resource.return_value.Table.return_value

    # -- write-back ------------------------------------------------------
    def test_writeback_en_sunat_ok(self):
        fake_boto, table = self._fake_boto()
        session = self._fake_session()
        with patch(RUTA + '.boto3', fake_boto), \
             patch(RUTA + '.requests.Session', return_value=session):
            vals = self.env['res.partner']._l10n_pe_query_sunat('73755976')
        self.assertTrue(vals and vals.get('name'))
        table.put_item.assert_called_once()
        item = table.put_item.call_args.kwargs['Item']
        self.assertEqual(item['tipo_documento'], 'DNI')
        self.assertEqual(item['numero_documento'], '73755976')  # clave = consultado
        self.assertEqual(item['nroDocumento'], '10737559761')   # devuelto por SUNAT
        self.assertEqual(item['nombre_razon_social'], 'PEREZ QUISPE JUAN')
        ahora = int(time_mod.time())
        self.assertGreater(item['expires_at'], ahora + 89 * 24 * 3600)
        self.assertLess(item['expires_at'], ahora + 91 * 24 * 3600)

    def test_no_writeback_en_modo_api(self):
        self.env['ir.config_parameter'].sudo().set_param(
            'l10n_pe_partner_lookup.mode', 'api')
        fake_boto, table = self._fake_boto()
        with patch(RUTA + '.boto3', fake_boto), \
             patch(RUTA + '.requests.Session', return_value=self._fake_session()):
            vals = self.env['res.partner']._l10n_pe_query_sunat('73755976')
        self.assertTrue(vals)
        table.put_item.assert_not_called()

    def test_writeback_no_rompe_lookup(self):
        fake_boto, table = self._fake_boto()
        table.put_item.side_effect = RuntimeError('dynamo caído')
        with patch(RUTA + '.boto3', fake_boto), \
             patch(RUTA + '.requests.Session', return_value=self._fake_session()):
            vals = self.env['res.partner']._l10n_pe_query_sunat('73755976')
        self.assertTrue(vals and vals.get('name'), 'el lookup debe sobrevivir al fallo de cache')

    # -- retry -----------------------------------------------------------
    def test_retry_tras_timeout(self):
        ok = MagicMock(status_code=200, text=SUNAT_LIST_HTML)
        ok.raise_for_status.return_value = None
        session = self._fake_session(get_side_effects=[requests.Timeout('lento'), ok])
        fake_boto, _table = self._fake_boto()
        with patch(RUTA + '.boto3', fake_boto), \
             patch(RUTA + '.requests.Session', return_value=session):
            vals = self.env['res.partner']._l10n_pe_query_sunat('73755976')
        self.assertTrue(vals, 'el segundo intento debe entregar el resultado')
        self.assertEqual(session.get.call_count, 2)

    def test_sin_retry_en_error_no_timeout(self):
        session = self._fake_session(
            get_side_effects=[requests.ConnectionError('reset')])
        fake_boto, _table = self._fake_boto()
        with patch(RUTA + '.boto3', fake_boto), \
             patch(RUTA + '.requests.Session', return_value=session):
            vals = self.env['res.partner']._l10n_pe_query_sunat('73755976')
        self.assertIsNone(vals)
        self.assertEqual(session.get.call_count, 1, 'sin retry para errores no-Timeout')

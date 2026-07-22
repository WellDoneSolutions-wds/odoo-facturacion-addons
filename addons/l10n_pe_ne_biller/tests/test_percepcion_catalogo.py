from odoo.tests import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestPercepcionCatalogo(TransactionCase):
    """Percepción del IGV (Apéndice 1, Ley 29173) como dato de config + catálogo: el negocio
    declara si es AGENTE designado (gate de toda la detección en Emitir) y el producto lleva
    su tasa sugerida (2% general, 1% combustibles). La emisión no cambia."""

    def setUp(self):
        super().setUp()
        self.Move = self.env['account.move']

    def test_agente_percepcion_negocio_round_trip(self):
        self.assertFalse(self.Move.l10n_pe_ne_negocio()['agentePercepcion'])
        self.Move.l10n_pe_ne_update_negocio({'agentePercepcion': True})
        self.assertTrue(self.Move.l10n_pe_ne_negocio()['agentePercepcion'])
        self.assertTrue(self.env.company.l10n_pe_ne_agente_percepcion)

    def test_config_expone_agente(self):
        self.env.company.l10n_pe_ne_agente_percepcion = True
        self.assertTrue(self.Move.l10n_pe_ne_config()['agentePercepcion'])

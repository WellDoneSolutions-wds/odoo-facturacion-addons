from odoo.exceptions import UserError, ValidationError
from odoo.tests import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestPercepcionCatalogo(TransactionCase):
    """Percepción del IGV (Apéndice 1, Ley 29173) como dato de config + catálogo: el negocio
    declara si es AGENTE designado (gate de toda la detección en Emitir) y el producto lleva
    su tasa sugerida (2% general, 1% combustibles). La emisión no cambia.

    Nota: la percepción NO se importa por la plantilla de productos (se define en el
    producto o la API); por eso acá no hay tests de una columna PERCEPCION % del import."""

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

    def test_crear_producto_con_percepcion(self):
        d = self.Move.l10n_pe_ne_create_producto({
            'descripcion': 'GASEOSA 3L', 'precio': 10.0, 'taxCode': '1000', 'percepTasa': 2.0,
        })
        self.assertEqual(d['percepTasa'], 2.0)

    def test_producto_sin_percepcion_expone_cero(self):
        d = self.Move.l10n_pe_ne_create_producto({'descripcion': 'CLAVOS', 'precio': 5.0})
        self.assertEqual(d['percepTasa'], 0.0)

    def test_percep_tasa_no_numerica_da_error(self):
        """Un percepTasa no numérico debe dar un UserError claro, no un 500 críptico."""
        with self.assertRaises(UserError):
            self.Move.l10n_pe_ne_create_producto({'descripcion': 'X', 'percepTasa': 'abc'})

    def test_percep_tasa_coma_decimal_api(self):
        """La API de productos tolera coma decimal."""
        d = self.Move.l10n_pe_ne_create_producto({'descripcion': 'ACEITE COMA API', 'percepTasa': '1,5'})
        self.assertEqual(d['percepTasa'], 1.5)

    def test_percep_tasa_fuera_de_rango_constraint(self):
        """Defensa en profundidad: fuera de 0-10 debe fallar también a nivel de modelo,
        no solo en la API."""
        tmpl = self.env['product.template'].create({'name': 'PRODUCTO RANGO'})
        with self.assertRaises(ValidationError):
            tmpl.write({'l10n_pe_ne_percepcion_tasa': 50})

    def test_update_cambia_y_limpia_percepcion(self):
        d = self.Move.l10n_pe_ne_create_producto({'descripcion': 'CERVEZA', 'precio': 8.0, 'percepTasa': 2.0})
        d2 = self.Move.l10n_pe_ne_update_producto({'id': d['id'], 'percepTasa': 1.0})
        self.assertEqual(d2['percepTasa'], 1.0)
        d3 = self.Move.l10n_pe_ne_update_producto({'id': d['id'], 'percepTasa': 0})
        self.assertEqual(d3['percepTasa'], 0.0)

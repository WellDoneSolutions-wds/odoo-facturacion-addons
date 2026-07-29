from odoo.tests import TransactionCase, tagged

from .common import L10nPeSeedMixin


@tagged('post_install', '-at_install')
class TestGuiaProyecto(L10nPeSeedMixin, TransactionCase):
    """C5 · Vincular la guía de remisión (traslado de materiales) al proyecto/obra."""

    def setUp(self):
        super().setUp()  # L10nPeSeedMixin: RUC de la compañía
        self.Guia = self.env['l10n_pe_ne.guia_remision']
        self.cliente = self.env['res.partner'].create({'name': 'OBRA SAC', 'vat': '20601030013'})
        self.producto = self.env['product.product'].create({'name': 'CEMENTO'})
        self.proyecto = self.env['l10n_pe_ne.proyecto'].create(
            {'name': 'EDIFICIO A', 'valor_total': 500000.0})

    def _vals(self, **extra):
        vals = {
            'partner_id': self.cliente.id,
            'ubigeo_partida': '150101', 'dir_partida': 'Av. Uno 100',
            'ubigeo_llegada': '150102', 'dir_llegada': 'Obra Av. Dos 200',
            'num_placa': 'ABC123', 'conductor_num_doc': '12345678',
            'conductor_nombres': 'Juan', 'conductor_apellidos': 'Perez',
            'conductor_licencia': 'Q12345678',
            'line_ids': [(0, 0, {'descripcion': 'CEMENTO', 'cantidad': 50,
                                 'product_id': self.producto.id})],
        }
        vals.update(extra)
        return vals

    def test_guia_se_vincula_al_proyecto(self):
        g = self.Guia.create(self._vals(l10n_pe_ne_proyecto_id=self.proyecto.id))
        self.assertEqual(g.l10n_pe_ne_proyecto_id, self.proyecto)
        d = g._l10n_pe_ne_guia_dict()
        self.assertEqual(d['proyectoId'], self.proyecto.id)
        self.assertEqual(d['proyecto'], 'EDIFICIO A')

    def test_proyecto_cuenta_sus_guias(self):
        self.Guia.create(self._vals(l10n_pe_ne_proyecto_id=self.proyecto.id))
        self.Guia.create(self._vals(l10n_pe_ne_proyecto_id=self.proyecto.id))
        self.assertEqual(self.proyecto._l10n_pe_ne_dict()['guias'], 2)

    def test_header_vals_mapea_y_desvincula(self):
        g = self.Guia.create(self._vals())
        self.assertFalse(g.l10n_pe_ne_proyecto_id, 'sin proyectoId no se vincula')
        self.Guia.l10n_pe_ne_update_guia({'id': g.id, 'proyectoId': self.proyecto.id})
        self.assertEqual(g.l10n_pe_ne_proyecto_id, self.proyecto)
        self.Guia.l10n_pe_ne_update_guia({'id': g.id, 'proyectoId': 0})  # desvincular
        self.assertFalse(g.l10n_pe_ne_proyecto_id)

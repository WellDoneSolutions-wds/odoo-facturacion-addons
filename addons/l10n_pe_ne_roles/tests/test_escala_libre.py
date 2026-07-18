import inspect
from unittest.mock import patch

from odoo.addons.l10n_pe_ne_roles.models.l10n_pe_ne_flujo_mixin import L10nPeNeFlujoMixin
from odoo.tests import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestEscalaLibre(TransactionCase):
    """El invariante del producto, ejecutable: si algo de esto se pone rojo, alguien metió
    una compuerta que exige dos personas y el producto dejó de funcionar para el cliente
    modal (la PyME peruana de un solo dueño). Ver docs/procesos-negocio/decision-escala-libre.md.
    """

    def _modelos_con_flujo(self):
        # Se detecta por el MRO de la clase de registro, NO por `_inherit`: `_inherit`
        # refleja solo la última clase Python cargada para el modelo, así que si otro
        # módulo reabre un modelo de flujo (class X: _inherit='...cotizacion') el modelo
        # desaparecería del set y el invariante quedaría verde sin verificar nada. El MRO
        # sí conserva la clase base del mixin pase lo que pase.
        return [
            name for name in self.env.registry
            if name != 'l10n_pe_ne.flujo.mixin'
            and issubclass(type(self.env[name]), L10nPeNeFlujoMixin)
        ]

    # ── El motor de folds, ejercitado con un grafo de prueba ──────────────────
    def test_ruta_sigue_solo_las_cadenas(self):
        """_ruta encuentra el camino más corto por transiciones cadena=True y NUNCA pasa
        por una rama de excepción (sin cadena). Es lo que hace que "cobrar y entregar" no
        pueda rechazar ni anular por accidente."""
        grafo = {
            ('borrador', 'aceptada'): {'cadena': True},
            ('aceptada', 'convertida'): {'cadena': True},
            ('convertida', 'entregada'): {'cadena': True},
            ('aceptada', 'rechazada'): {'motivo': True},   # excepción: sin cadena
        }
        mixin = self.env['l10n_pe_ne.flujo.mixin']
        with patch.object(type(mixin), '_transiciones', lambda self: grafo):
            self.assertEqual(
                mixin._ruta('borrador', 'entregada'),
                ['aceptada', 'convertida', 'entregada'])
            # No hay cadena hacia 'rechazada': el fold no la alcanza.
            self.assertEqual(mixin._ruta('borrador', 'rechazada'), [])
            # Mismo origen y destino: ruta vacía.
            self.assertEqual(mixin._ruta('aceptada', 'aceptada'), [])

    # ── Los dos invariantes de largo plazo (vacuos hasta CN-01/CN-02) ─────────
    def test_el_dueno_nunca_se_atasca(self):
        """Para cada modelo de flujo, desde cada estado NO terminal, un usuario con TODOS
        los grupos debe poder avanzar a algún sitio. Hoy no hay modelos de flujo: el test
        queda armado para dispararse en cuanto la cotización (CN-01) o el pedido (CN-02)
        hereden el mixin con una transición que exija dos personas."""
        for nombre in self._modelos_con_flujo():
            Model = self.env[nombre]
            terminales = set(Model._estados_terminales())
            estados = dict(Model._fields['estado']._description_selection(self.env))
            trans = Model._transiciones()
            for origen in estados:
                if origen in terminales:
                    continue
                # "Todos los grupos" = el máximo del retículo: toda transición con grupo
                # la pasaría. Basta con que exista al menos una salida desde 'origen'.
                salidas = [d for (o, d) in trans if o == origen]
                self.assertTrue(
                    salidas,
                    "%s: desde el estado '%s' no hay ninguna transición de salida; con un "
                    "solo usuario con todos los roles el documento queda atascado." %
                    (nombre, origen))

    def test_ninguna_guarda_compara_identidades(self):
        """Sonda estructural: ninguna guarda de realidad puede comparar identidades de
        usuario. Grosera a propósito —atrapa el reflejo de 'quien registra no aprueba'
        antes de que llegue a un tenant."""
        prohibido = (
            'env.user !=', 'env.user.id !=', 'env.uid !=', '!= self.create_uid',
            'aprobador_id !=', 'solicitante_id !=', 'usuario_revision_id not in',
            'not in (self.cajero_id', 'not in {self.cajero_id',
        )
        for nombre in self._modelos_con_flujo():
            Model = self.env[nombre]
            for t in Model._transiciones().values():
                guarda = t.get('guarda')
                if not guarda:
                    continue
                src = inspect.getsource(getattr(type(Model), guarda))
                for patron in prohibido:
                    self.assertNotIn(
                        patron, src,
                        "%s.%s compara identidades de usuario: rompe la escala libre. El "
                        "control va al eje de revisión asíncrona, no a un raise." %
                        (nombre, guarda))

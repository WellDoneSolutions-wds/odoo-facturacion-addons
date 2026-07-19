from odoo.tests import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestInstall(TransactionCase):
    """El addon instala y el mixin queda registrado con su forma esperada."""

    def test_mixin_registrado(self):
        mixin = self.env['l10n_pe_ne.flujo.mixin']
        self.assertTrue(mixin._abstract, "el mixin debe ser un AbstractModel")
        for campo in ('user_id', 'priority'):
            self.assertIn(campo, mixin._fields, "falta el campo %s en el mixin" % campo)
        # Hereda mail.thread -> message_post disponible en los modelos concretos.
        self.assertTrue(hasattr(mixin, 'message_post'))

    def test_transiciones_por_defecto_vacias(self):
        """Sin modelos concretos aún, la tabla base es {} y no revienta."""
        mixin = self.env['l10n_pe_ne.flujo.mixin']
        self.assertEqual(mixin._transiciones(), {})
        self.assertEqual(mixin._estados_terminales(), ())

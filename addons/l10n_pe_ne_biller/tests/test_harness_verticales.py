from odoo.tests import TransactionCase, tagged

from .common import EnvioSincronoMixin, L10nPeSeedMixin


@tagged('post_install', '-at_install')
class TestHarnessVerticales(L10nPeSeedMixin, EnvioSincronoMixin, TransactionCase):
    """Runner por vertical (L2). Emite un CORPUS de casos representativos de cada segmento a
    través del pre-flight (arma el comprobante como en la emisión real, corre el motor L1 y
    revierte) y afirma dos cosas:

      * corpus VÁLIDO: el happy-path de cada vertical NO produce ningún finding 'error' —
        es una prueba de humo de que el segmento sigue emitiendo bien de punta a punta.
      * corpus de RECHAZOS: cada caso inválido conocido (F001-247 y familia) marca el código
        de error esperado — es el guardián que mantiene «maduro» lo maduro (que un fix no se
        vuelva a romper).

    Sumar un vertical/caso = agregar una fila a la lista. Fuente única: el mismo motor que
    corre en el guard de emisión y en el pre-flight de la SPA.
    """

    # -- clientes reutilizables --------------------------------------------------------------
    _RUC = {'tipoDoc': '6', 'numDoc': '20100070970', 'razonSocial': 'CLIENTE SAC'}
    _DNI = {'tipoDoc': '1', 'numDoc': '12345678', 'razonSocial': 'JUAN PEREZ'}
    _SINDOC = {'razonSocial': 'CONSUMIDOR FINAL'}
    _EXTRANJERO = {'tipoDoc': '6', 'numDoc': '20100070970', 'razonSocial': 'FOREIGN CO', 'pais': 'US'}

    def _pre(self, payload):
        return self.env['account.move'].l10n_pe_ne_preflight(payload)

    # -- CORPUS: happy-path por vertical -----------------------------------------------------
    def _corpus_valido(self):
        return [
            ('retail · boleta con ICBPER', {
                'tipoDoc': '03', 'moneda': 'PEN', 'serie': 'B001', 'cliente': self._DNI,
                'lineas': [
                    {'descripcion': 'GASEOSA', 'cantidad': 2, 'precioUnitario': 100, 'taxCode': '1000'},
                    {'descripcion': 'BOLSA', 'cantidad': 2, 'precioUnitario': 1, 'taxCode': '1000', 'icbper': True},
                ]}),
            ('mayorista · factura al crédito con detracción', {
                'tipoDoc': '01', 'moneda': 'PEN', 'serie': 'F001', 'cliente': self._RUC,
                'lineas': [{'descripcion': 'SERVICIO', 'cantidad': 1, 'precioUnitario': 1000, 'taxCode': '1000'}],
                'detraccion': {'codBien': '037', 'tasa': 12, 'cuentaBN': '00-123-456789'},
                'formaPago': {'tipo': 'Credito', 'cuotas': [{'fecha': '2026-12-31', 'monto': 1180}]}}),
            ('grifo · factura de combustible con placa', {
                'tipoDoc': '01', 'moneda': 'PEN', 'serie': 'F001', 'cliente': self._RUC,
                'placa': 'ABC-123',
                'lineas': [{'descripcion': 'DIESEL', 'cantidad': 10, 'precioUnitario': 15, 'taxCode': '1000'}]}),
            ('exportación · factura 0200 con país', {
                'tipoDoc': '01', 'moneda': 'USD', 'serie': 'F001', 'cliente': self._EXTRANJERO,
                'lineas': [{'descripcion': 'EXPORT GOODS', 'cantidad': 1, 'precioUnitario': 1000, 'taxCode': '9995'}]}),
            ('servicios · concepto libre', {
                'tipoDoc': '01', 'moneda': 'PEN', 'serie': 'F001', 'cliente': self._RUC,
                'lineas': [{'descripcion': 'CONSULTORIA MAYO', 'cantidad': 1, 'precioUnitario': 500,
                            'taxCode': '1000', 'conceptoLibre': True, 'unidad': 'ZZ'}]}),
            ('gratuito · factura al crédito con bonificación (F001-247 arreglado)', {
                'tipoDoc': '01', 'moneda': 'PEN', 'serie': 'F001', 'cliente': self._RUC,
                'lineas': [
                    {'descripcion': 'PRODUCTO', 'cantidad': 1, 'precioUnitario': 100, 'taxCode': '1000'},
                    {'descripcion': 'REGALO', 'cantidad': 1, 'precioUnitario': 50, 'taxCode': '9996'},
                ],
                'formaPago': {'tipo': 'Credito', 'cuotas': [{'fecha': '2026-12-31', 'monto': 118}]}}),
            ('boleta · mayor a S/700 con documento', {
                'tipoDoc': '03', 'moneda': 'PEN', 'serie': 'B001', 'cliente': self._DNI,
                'lineas': [{'descripcion': 'TELEVISOR', 'cantidad': 1, 'precioUnitario': 800, 'taxCode': '1000'}]}),
            ('peso · balanza en KGM con 3 decimales (QA-020)', {
                'tipoDoc': '03', 'moneda': 'PEN', 'serie': 'B001', 'cliente': self._DNI,
                'lineas': [{'descripcion': 'POLLO', 'cantidad': 18.375, 'precioUnitario': 9.80,
                            'taxCode': '1000', 'unidad': 'KGM'}]}),
            ('ferretería · venta por metro y metro cuadrado + kardex', {
                'tipoDoc': '01', 'moneda': 'PEN', 'serie': 'F001', 'cliente': self._RUC,
                'lineas': [
                    {'descripcion': 'TUBO PVC', 'cantidad': 12.5, 'precioUnitario': 8.50,
                     'taxCode': '1000', 'unidad': 'MTR'},
                    {'descripcion': 'MALLA', 'cantidad': 3.25, 'precioUnitario': 24, 'taxCode': '1000',
                     'unidad': 'MTK'},
                ]}),
        ]

    def test_corpus_valido(self):
        for nombre, payload in self._corpus_valido():
            with self.subTest(caso=nombre):
                errores = [f for f in self._pre(payload) if f['nivel'] == 'error']
                self.assertFalse(errores, '%s NO debería tener errores: %s' % (nombre, errores))

    # -- CORPUS: rechazos conocidos (guardián de regresión) ---------------------------------
    def _corpus_rechazos(self):
        return [
            ('boleta > 700 SIN documento', 'boleta-700-doc', {
                'tipoDoc': '03', 'moneda': 'PEN', 'serie': 'B001', 'cliente': self._SINDOC,
                'lineas': [{'descripcion': 'TELEVISOR', 'cantidad': 1, 'precioUnitario': 800, 'taxCode': '1000'}]}),
            ('detracción SIN cuenta del Banco de la Nación', 'detraccion-cuenta', {
                'tipoDoc': '01', 'moneda': 'PEN', 'serie': 'F001', 'cliente': self._RUC,
                'lineas': [{'descripcion': 'SERVICIO', 'cantidad': 1, 'precioUnitario': 1000, 'taxCode': '1000'}],
                'detraccion': {'codBien': '037', 'tasa': 12}}),
            ('exportación SIN país del cliente', 'exportacion-pais', {
                'tipoDoc': '01', 'moneda': 'USD', 'serie': 'F001',
                'cliente': {'tipoDoc': '6', 'numDoc': '20100070970', 'razonSocial': 'FOREIGN CO'},
                'lineas': [{'descripcion': 'EXPORT GOODS', 'cantidad': 1, 'precioUnitario': 1000, 'taxCode': '9995'}]}),
        ]

    def test_corpus_rechazos(self):
        for nombre, code, payload in self._corpus_rechazos():
            with self.subTest(caso=nombre):
                codes = {f['code'] for f in self._pre(payload)}
                self.assertIn(code, codes,
                              '%s debería marcar «%s»; salió %s' % (nombre, code, sorted(codes)))

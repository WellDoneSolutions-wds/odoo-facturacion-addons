import base64

from odoo.exceptions import UserError
from odoo.tests import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestPleCompras(TransactionCase):
    """PLE 8.1 (Registro de Compras).

    ⚠ La ESTRUCTURA está pendiente de validación contable: los anexos de SUNAT con el layout
    son PDF escaneados y no hubo de dónde extraerla. Estos tests fijan lo que SÍ se puede
    afirmar sin la norma —que el archivo se arma, que los importes salen del documento, que
    el periodo filtra, que el nombre sigue el patrón del 14.1 que ya está en producción— y
    dejan clavado el número de campos, para que si alguien lo corrige, el test lo diga.
    """

    def setUp(self):
        super().setUp()
        self.Move = self.env['account.move']

    # Periodo PROPIO y lejano: contar las compras de un mes real haría que el test dependiera
    # de lo que ya haya en la BD (pasó: la de dev tenía compras de julio de mis pruebas y el
    # test contaba 5 en vez de 2). Cada test siembra lo suyo en un mes que nadie más usa.
    PERIODO = '202001'
    FECHA = '2020-01-15'

    def _compra(self, total=118, numero='7001', fecha=None, afect='1000'):
        fecha = fecha or self.FECHA
        return self.Move.l10n_pe_ne_create_compra({
            'proveedor': {'tipoDoc': '6', 'numDoc': '20100070970', 'razonSocial': 'PROVEEDOR SAC'},
            'tipoComprobante': '01', 'serie': 'F001', 'numero': numero, 'fecha': fecha,
            'total': total, 'descripcion': 'COMPRA PLE', 'afectacion': afect})

    def _linea(self, periodo=None):
        periodo = periodo or self.PERIODO
        txt = base64.b64decode(self.Move.l10n_pe_ne_ple_compras(periodo)['contentB64']).decode('latin-1')
        return [l for l in txt.split('\r\n') if l]

    def test_arma_una_linea_por_compra(self):
        self._compra(numero='7001')
        self._compra(numero='7002')
        self.assertEqual(len(self._linea()), 2)

    def test_los_campos_son_posicionales_y_no_deben_correrse(self):
        """Cada campo va separado por '|': uno de más o de menos corre TODOS los siguientes.
        Se clava el número para que un cambio de layout no pase inadvertido."""
        self._compra(numero='7010')
        campos = self._linea()[0].split('|')
        # 40 campos + el palote final deja un elemento vacío al final.
        self.assertEqual(len(campos), 41, 'cambió el número de campos del 8.1')
        self.assertEqual(campos[-1], '')

    def test_los_importes_salen_del_documento(self):
        """Base e IGV son los que registró la compra — es el dato que sostiene el crédito
        fiscal, y lo único de este formato que no depende de la estructura."""
        self._compra(total=118, numero='7020')
        c = self._linea()[0].split('|')
        self.assertEqual(c[0], '20200100')      # 1 periodo AAAAMM00
        self.assertEqual(c[3], '15/01/2020')    # 4 fecha de emisión
        self.assertEqual(c[5], '01')            # 6 tipo de comprobante
        self.assertEqual(c[6], 'F001')          # 7 serie
        self.assertEqual(c[10], '6')            # 11 tipo doc proveedor (RUC)
        self.assertEqual(c[11], '20100070970')  # 12 nro doc proveedor
        self.assertEqual(c[13], '100.00')       # 14 base gravada
        self.assertEqual(c[14], '18.00')        # 15 IGV
        self.assertEqual(c[23], '118.00')       # 24 importe total

    def test_la_compra_exonerada_no_declara_igv(self):
        """Una exonerada no da crédito fiscal: su importe va a "no gravadas" y el IGV en 0."""
        self._compra(total=100, numero='7030', afect='9997')
        c = self._linea()[0].split('|')
        self.assertEqual(c[13], '0.00')    # 14 base gravada
        self.assertEqual(c[14], '0.00')    # 15 IGV
        self.assertEqual(c[19], '100.00')  # 20 adquisiciones no gravadas
        self.assertEqual(c[23], '100.00')  # 24 total

    def test_el_periodo_filtra(self):
        self._compra(numero='7040', fecha='2020-01-15')
        self._compra(numero='7041', fecha='2020-02-10')
        self.assertEqual(len(self._linea('202001')), 1)
        self.assertEqual(len(self._linea('202002')), 1)

    def test_el_nombre_del_archivo_sigue_el_patron_del_14_1(self):
        """LE + RUC + periodo + 00 + libro + indOper + indCont + moneda + libro. El 8.1 usa
        080100 donde el 14.1 usa 140100 — es lo único que cambia."""
        self._compra(numero='7050')
        r = self.Move.l10n_pe_ne_ple_compras(self.PERIODO)
        ruc = self.env.company.vat
        self.assertEqual(r['filename'], 'LE%s20200100080100%s11.txt' % (ruc, '11'))
        self.assertIn('080100', r['filename'])

    def test_sin_compras_el_archivo_va_vacio_con_indicador_0(self):
        """Un periodo sin compras se declara igual: el indicador de contenido va en 0."""
        r = self.Move.l10n_pe_ne_ple_compras('201912')   # otro mes vacío
        self.assertEqual(r['count'], 0)
        self.assertEqual(base64.b64decode(r['contentB64']), b'')
        self.assertIn('080100', r['filename'])
        self.assertIn('10', r['filename'])   # indOper=1, indCont=0

    def test_periodo_invalido_rechaza(self):
        for malo in ('', '2026', '20261', '202613', 'abcdef'):
            with self.assertRaises(UserError, msg='debería rechazar %r' % malo):
                self.Move.l10n_pe_ne_ple_compras(malo)

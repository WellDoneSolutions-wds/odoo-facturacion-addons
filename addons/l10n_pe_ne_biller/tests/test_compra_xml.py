import base64

from odoo.exceptions import UserError
from odoo.tests import TransactionCase, tagged

# Fixture recortado de un XML REAL emitido por este mismo stack y aceptado por SUNAT beta
# (F001-00000054). Se conserva la forma exacta: namespaces UBL 2.1 con prefijos, el precio
# con IGV en AlternativeConditionPrice/PriceTypeCode=01 y el código del proveedor en
# SellersItemIdentification. Inventar el XML habría probado el parser contra mi idea del
# formato, no contra el formato.
XML = '''<?xml version="1.0" encoding="ISO-8859-1"?>
<Invoice xmlns="urn:oasis:names:specification:ubl:schema:xsd:Invoice-2"
 xmlns:cac="urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2"
 xmlns:cbc="urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2">
  <cbc:ID>F001-00000054</cbc:ID>
  <cbc:IssueDate>2026-07-13</cbc:IssueDate>
  <cbc:InvoiceTypeCode listID="0101">01</cbc:InvoiceTypeCode>
  <cbc:DocumentCurrencyCode>PEN</cbc:DocumentCurrencyCode>
  <cac:AccountingSupplierParty>
    <cac:Party>
      <cac:PartyIdentification><cbc:ID schemeID="6">20321856145</cbc:ID></cac:PartyIdentification>
      <cac:PartyName><cbc:Name>FERRETERIA MAYORISTA</cbc:Name></cac:PartyName>
      <cac:PartyLegalEntity><cbc:RegistrationName>FERRETERIA MAYORISTA SAC</cbc:RegistrationName></cac:PartyLegalEntity>
    </cac:Party>
  </cac:AccountingSupplierParty>
  <cac:TaxTotal><cbc:TaxAmount currencyID="PEN">1.10</cbc:TaxAmount></cac:TaxTotal>
  <cac:LegalMonetaryTotal><cbc:PayableAmount currencyID="PEN">7.20</cbc:PayableAmount></cac:LegalMonetaryTotal>
  <cac:InvoiceLine>
    <cbc:ID>1</cbc:ID>
    <cbc:InvoicedQuantity unitCode="NIU">2.00</cbc:InvoicedQuantity>
    <cbc:LineExtensionAmount currencyID="PEN">6.10</cbc:LineExtensionAmount>
    <cac:PricingReference>
      <cac:AlternativeConditionPrice>
        <cbc:PriceAmount currencyID="PEN">3.60</cbc:PriceAmount>
        <cbc:PriceTypeCode listName="Tipo de Precio">01</cbc:PriceTypeCode>
      </cac:AlternativeConditionPrice>
    </cac:PricingReference>
    <cac:Item>
      <cbc:Description>DESARMADOR PLANO</cbc:Description>
      <cac:SellersItemIdentification><cbc:ID>P001</cbc:ID></cac:SellersItemIdentification>
      <cac:StandardItemIdentification><cbc:ID>7501234567890</cbc:ID></cac:StandardItemIdentification>
    </cac:Item>
    <cac:Price><cbc:PriceAmount currencyID="PEN">3.05</cbc:PriceAmount></cac:Price>
  </cac:InvoiceLine>
</Invoice>'''


@tagged('post_install', '-at_install')
class TestCompraXml(TransactionCase):
    """Importar el XML del proveedor: es el documento fiscal de verdad (el PDF es solo su
    representación impresa), así que leerlo evita teclear el dato que va al Registro de
    Compras — y trae el detalle, que es lo único que alimenta el kardex sin tipear."""

    def _parse(self, xml=None):
        return self.env['account.move']._l10n_pe_ne_parse_compra_xml((xml or XML).encode('utf-8'))

    def test_lee_la_cabecera(self):
        d = self._parse()
        self.assertEqual(d['proveedor']['numDoc'], '20321856145')
        self.assertEqual(d['proveedor']['razonSocial'], 'FERRETERIA MAYORISTA SAC')
        self.assertEqual(d['tipoComprobante'], '01')
        self.assertEqual(d['serie'], 'F001')
        self.assertEqual(d['numero'], '54')          # sin los ceros de relleno
        self.assertEqual(d['fecha'], '2026-07-13')
        self.assertEqual(d['total'], 7.20)
        self.assertEqual(d['igv'], 1.10)             # lo que pide el Registro de Compras

    def test_lee_el_detalle_con_el_precio_CON_igv(self):
        """El precio debe salir de AlternativeConditionPrice (con IGV), no de cac:Price (sin
        IGV): el detalle se compara contra el total del documento, que va con IGV."""
        ln = self._parse()['lineas'][0]
        self.assertEqual(ln['descripcion'], 'DESARMADOR PLANO')
        self.assertEqual(ln['cantidad'], 2.0)
        self.assertEqual(ln['precioUnitario'], 3.60)   # con IGV, NO 3.05
        self.assertEqual(ln['codigoProveedor'], 'P001')
        self.assertEqual(ln['barcode'], '7501234567890')

    def test_propone_el_producto_por_codigo_de_barras(self):
        """El GTIN es universal: si coincide, es el mismo producto."""
        p = self.env['product.product'].create({
            'name': 'DESARMADOR NUESTRO', 'barcode': '7501234567890', 'type': 'consu'})
        self.assertEqual(self._parse()['lineas'][0]['productId'], p.id)

    def test_propone_por_codigo_propio_si_no_hay_barcode(self):
        # Código propio del fixture, no 'P001': ese ya existe en cualquier BD sembrada y el
        # test encontraría OTRO producto. (La misma no-hermeticidad que se arregló en #46.)
        p = self.env['product.product'].create({
            'name': 'DESARMADOR POR CODIGO', 'default_code': 'COD-XML-TEST-1', 'type': 'consu'})
        xml = (XML
               .replace('<cac:StandardItemIdentification><cbc:ID>7501234567890</cbc:ID></cac:StandardItemIdentification>', '')
               .replace('<cbc:ID>P001</cbc:ID>', '<cbc:ID>COD-XML-TEST-1</cbc:ID>'))
        self.assertEqual(self._parse(xml)['lineas'][0]['productId'], p.id)

    def test_sin_coincidencia_lo_deja_al_usuario(self):
        """El proveedor nombra y codifica a su manera: inventar el mapeo ensuciaría el
        kardex. Sin coincidencia va None y elige el usuario."""
        xml = XML.replace('7501234567890', 'BC-QUE-NO-EXISTE-9').replace('<cbc:ID>P001</cbc:ID>', '<cbc:ID>ZZZ-NO-EXISTE</cbc:ID>')
        self.assertIsNone(self._parse(xml)['lineas'][0]['productId'])

    def test_el_detalle_cuadra_con_el_total_del_xml(self):
        """El XML es coherente consigo mismo: 2 x 3.60 = 7.20 = PayableAmount. O sea que la
        compra importada pasa la validación de cuadre sin tocar nada."""
        d = self._parse()
        suma = sum(l['cantidad'] * l['precioUnitario'] for l in d['lineas'])
        self.assertAlmostEqual(suma, d['total'], places=2)

    # -- lo que no es un XML de compra ----------------------------------------------------
    def test_xml_ilegible_rechaza(self):
        with self.assertRaises(UserError):
            self._parse('<esto no es xml')

    def test_xml_que_no_es_comprobante_rechaza(self):
        with self.assertRaises(UserError):
            self._parse('<?xml version="1.0"?><Cualquiera><a>1</a></Cualquiera>')

    def test_xml_sin_ruc_del_emisor_rechaza(self):
        xml = XML.replace('<cbc:ID schemeID="6">20321856145</cbc:ID>', '')
        with self.assertRaises(UserError):
            self._parse(xml)

    def test_base64_ilegible_rechaza(self):
        with self.assertRaises(UserError):
            self.env['account.move'].l10n_pe_ne_importar_compra_xml({'xml': '@@@no-es-base64@@@'})

    def test_importa_desde_base64(self):
        """El camino real: la SPA manda el archivo en base64."""
        d = self.env['account.move'].l10n_pe_ne_importar_compra_xml(
            {'xml': base64.b64encode(XML.encode('utf-8')).decode()})
        self.assertEqual(d['serie'], 'F001')

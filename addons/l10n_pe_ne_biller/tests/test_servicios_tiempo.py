from odoo.tests import TransactionCase, tagged

from .common import EnvioSincronoMixin, L10nPeSeedMixin


@tagged('post_install', '-at_install')
class TestServiciosTiempo(L10nPeSeedMixin, EnvioSincronoMixin, TransactionCase):
    """Servicios por hora / día (consultoría, alquiler de equipos, servicios técnicos).

    La cantidad es una MEDIDA de tiempo y la unidad SUNAT es HUR (hora) o DAY (día), no NIU. Este
    test fija que el comprobante emite esos códigos cat.03. El front deriva la cantidad de un rango
    (ver lib/tiempo.ts); acá se comprueba el otro extremo, el XML."""

    def setUp(self):
        super().setUp()
        ruc_type = self.env['l10n_latam.identification.type'].search(
            [('l10n_pe_vat_code', '=', '6')], limit=1)
        self.partner = self.env['res.partner'].create({
            'name': 'CLIENTE SAC', 'vat': '20100070970',
            'l10n_latam_identification_type_id': ruc_type.id})

    def test_hora_y_dia_emiten_su_unidad(self):
        move = self.env['account.move'].l10n_pe_ne_quick_emit({
            'tipoDoc': '01', 'moneda': 'PEN', 'serie': 'F001',
            'cliente': {'tipoDoc': '6', 'numDoc': '20100070970', 'razonSocial': 'CLIENTE SAC'},
            'lineas': [
                {'descripcion': 'CONSULTORIA', 'cantidad': 4.5, 'precioUnitario': 120,
                 'taxCode': '1000', 'unidad': 'HUR'},
                {'descripcion': 'ALQUILER EQUIPO', 'cantidad': 15, 'precioUnitario': 80,
                 'taxCode': '1000', 'unidad': 'DAY'},
            ]}, enviar=False)
        dets = move._l10n_pe_build_invoice_request()['detalle']
        by_unit = {d['codUnidadMedida']: d for d in dets}
        self.assertIn('HUR', by_unit)
        self.assertIn('DAY', by_unit)
        self.assertEqual(by_unit['HUR']['ctdUnidadItem'], '4.50')
        self.assertEqual(by_unit['DAY']['ctdUnidadItem'], '15.00')

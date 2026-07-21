"""Guía de Remisión Electrónica (GRE) — Remitente (tipo 09).

A diferencia de los comprobantes (factura/boleta/NC/ND), la GRE NO es un account.move:
es un documento de traslado propio, con su serie (T###) y su canal en el biller
(`POST /generator/guia`, REST/OAuth2 — ver ms-ne-biller). Este modelo arma el payload que
espera el biller (mismas claves que `GreCabeceraRequest`) y guarda el resultado.

El controller (`/ne/api/guias`), el PDF QWeb con el QR de SUNAT y la re-consulta del
ticket (botón + cron) viven también en este addon; ver controllers/main.py y
report/guia_report.xml.
"""
import base64
import io
import json
import logging
import re
import zipfile

import requests

from odoo import _, api, fields, models
from odoo.exceptions import UserError
from odoo.tools import config

_logger = logging.getLogger(__name__)

# Catálogo 20 SUNAT — motivo de traslado (los más comunes).
MOTIVOS_TRASLADO = [
    ('01', 'Venta'),
    ('02', 'Compra'),
    ('04', 'Traslado entre establecimientos de la misma empresa'),
    ('08', 'Importación'),
    ('09', 'Exportación'),
    ('13', 'Otros'),
    ('14', 'Venta sujeta a confirmación del comprador'),
    ('18', 'Traslado emisor itinerante de comprobantes de pago'),
    ('19', 'Traslado a zona primaria'),
]
# Catálogo 18 SUNAT — modalidad de traslado.
MODALIDADES_TRASLADO = [
    ('01', 'Transporte público'),
    ('02', 'Transporte privado'),
]
UNIDADES_PESO = [('KGM', 'Kilogramos'), ('TNE', 'Toneladas')]

# Motivos cuyo XML el biller sustenta completo hoy. 04 exige código de establecimiento
# propio en ambos puntos (ver _l10n_pe_ne_validar).
SUPPORTED_MOTIVOS = ('01', '02', '04', '08', '09', '13', '14', '18')
# Comercio exterior (catálogo 20): 08 = Importación, 09 = Exportación. Ambos emiten una
# DAM/DUA relacionada (DocumentTypeCode 50) + el indicador de traslado total DAM/DS, que
# relaja el nivel de línea. El N° de la DAM codifica el régimen (SUNAT 3441): 10 =
# importación, 40 = exportación definitiva. La importación se modela por el PUERTO de
# ingreso (no un establecimiento de tercero): el origen es el puerto/aeropuerto, cuyo
# ubigeo debe ser el punto de partida (SUNAT 3364/3365).
DAM_EXPORTACION_RE = r'^[0-9]{3}-[0-9]{4}-40-[1-9][0-9]{0,5}$'
DAM_IMPORTACION_RE = r'^[0-9]{3}-[0-9]{4}-10-[1-9][0-9]{0,5}$'
# DSI (Declaración Simplificada, DocumentTypeCode 52): alternativa a la DAM/DUA para
# envíos de bajo valor. Viaja como documento relacionado codTipDocRel 52 y su N° codifica
# el régimen (SUNAT 3441) igual que la DAM, pero con otros códigos: 18 = importación
# simplificada, 48 = exportación simplificada. Espejan DSI_*_RE de la SPA.
DSI_IMPORTACION_RE = r'^[0-9]{3}-[0-9]{4}-18-[1-9][0-9]{0,5}$'
DSI_EXPORTACION_RE = r'^[0-9]{3}-[0-9]{4}-48-[1-9][0-9]{0,5}$'
# Régimen aduanero por (motivo, tipo de declaración): la DAM (50) codifica 10/40; la DSI
# (52) codifica 18/48. Cada entrada trae el regex del N° + una etiqueta amigable (nombra el
# documento y el régimen) + el formato, para armar el mensaje de error (SUNAT 3441).
DECLARACION_REGIMEN = {
    ('08', '50'): (DAM_IMPORTACION_RE, 'DAM/DUA de importación (régimen 10)', 'NNN-AAAA-10-NNNNNN'),
    ('09', '50'): (DAM_EXPORTACION_RE, 'DAM/DUA de exportación (régimen 40)', 'NNN-AAAA-40-NNNNNN'),
    ('08', '52'): (DSI_IMPORTACION_RE, 'DSI de importación (régimen 18)', 'NNN-AAAA-18-NNNNNN'),
    ('09', '52'): (DSI_EXPORTACION_RE, 'DSI de exportación (régimen 48)', 'NNN-AAAA-48-NNNNNN'),
}

# Puertos (cat_63, listID 1) y aeropuertos (cat_64, listID 2) SUNAT: código -> (ubigeo,
# nombre). El biller los emite como cac:FirstArrivalPortLocation (codPuerto + locTypePuerto
# '1' puerto / '2' aeropuerto + nomPuerto). La regla SUNAT 3364 exige que el ubigeo del
# punto de partida (importación) o de llegada (exportación) sea el del puerto elegido.
PUERTOS = {
    "PUB": ("200801", "Bayóvar"), "CLL": ("070101", "Callao"), "CON": ("150119", "Conchán"),
    "CHY": ("150605", "Chancay"), "CHM": ("021801", "Chimbote"), "EEN": ("140113", "Eten"),
    "HCO": ("150801", "Huacho"), "HUY": ("021101", "Huarmey"), "ILQ": ("180301", "Ilo"),
    "IQT": ("160101", "Iquitos"), "MRI": ("040701", "Matarani"), "PAI": ("200501", "Paita"),
    "PIO": ("110505", "Pisco"), "PCL": ("250101", "Pucallpa"), "PUN": ("210101", "Puno"),
    "SVY": ("130109", "Salaverry"), "SNX": ("110304", "San Nicolas"), "SUP": ("150204", "Supe"),
    "TYL": ("200701", "Talara"), "YMS": ("160201", "Yurimaguas"), "ZOR": ("240103", "Zorritos"),
}
AEROPUERTOS = {
    "AQP": ("040104", "Rodríguez Ballón"), "ANS": ("030201", "Andahuaylas"),
    "ATA": ("020604", "Comandante FAP Germán Arias Graciani"), "AYP": ("050113", "Coronel FAP Alfredo Mendívil Duarte"),
    "CJA": ("060108", "Mayor Gral. FAP Armando Revoredo Iglesias"), "CHM": ("021809", "Tnte. FAP Jaime De Montruil M."),
    "CUZ": ("080108", "Alejandro Velazco Astete"), "CHH": ("010101", "Chachapoyas"),
    "CIX": ("140101", "Capitán FAP José Quiñones G."), "HUU": ("100101", "Alférez FAP David Figueroa Fernandini"),
    "ILO": ("180301", "Ilo"), "IQT": ("160101", "Coronel FAP Francisco Secada Vignetta"),
    "JAE": ("060802", "Jaén - Shumba"), "JJI": ("220601", "Juanjuí"), "JUL": ("211101", "Manco Cápac"),
    "JAU": ("120430", "Francisco Carlé"), "LIM": ("070101", "Internacional Jorge Chávez"),
    "MBP": ("220101", "Moyobamba"), "PIO": ("110506", "Capitán FAP Renán Elías Olivera"),
    "PIU": ("200104", "Capitán FAP Carlos Concha Iberico"), "PCL": ("250105", "Capitán FAP David Abensur Rengifo"),
    "PEM": ("170101", "Padre Aldamiz"), "RIJ": ("220801", "Juan Simons Vela - Rioja"),
    "TCQ": ("230101", "Coronel FAP Carlos Ciriani Santa Rosa"), "TYL": ("200701", "Capitán FAP Montes Arias"),
    "TPP": ("220901", "Cadete FAP Guillermo del Castillo Paredes"), "TIG": ("100601", "Tingo María"),
    "TRU": ("130104", "Capitán FAP Carlos Martínez Pinillos"), "TBP": ("240101", "Capitán FAP Pedro Canga Rodríguez"),
    "ATG": ("250201", "Atalaya - Tnte. Gral. Gerardo Pérez Pinedo"), "YMS": ("160201", "Moisés Benzaquen Rengifo"),
}

# Relación destinatario ↔ emisor según el motivo (SUNAT 2554/2555):
#   - traslado interno (compra, entre establecimientos propios, itinerante): el destinatario
#     DEBE ser la propia empresa (mismo RUC que el emisor).
#   - venta y afines: el destinatario NO puede ser la propia empresa (debe ser un tercero).
# Solo se listan los motivos soportados; el resto (p. ej. '13' Otros) no restringe el destinatario.
MOTIVOS_DEST_ES_EMISOR = ('02', '04', '18')
MOTIVOS_DEST_NO_EMISOR = ('01', '14')

# Estados desde los que se puede (re)emitir o editar: aún sin CDR aceptado.
ESTADOS_EMITIBLES = ('borrador', 'error', 'rechazado')


class L10nPeNeGuiaRemision(models.Model):
    _name = 'l10n_pe_ne.guia_remision'
    _description = 'Guía de Remisión Electrónica (Remitente)'
    _order = 'fecha_emision desc, id desc'

    name = fields.Char(string='Número', required=True, copy=False, readonly=True,
                       default=lambda s: _('Nueva'), index=True)
    # Tipo de GRE: 09 remitente (el emisor manda los bienes) vs 31 transportista (el emisor
    # es el carrier). Cambia qué partes lleva la cabecera, el endpoint del biller y el código
    # de tipo del nombre de archivo (RUC-09-... vs RUC-31-...).
    tipo_gre = fields.Selection([('09', 'Remitente'), ('31', 'Transportista')],
                                string='Tipo de guía', default='09', required=True)
    serie = fields.Char(string='Serie', default='T001', required=True)
    correlativo = fields.Char(string='Correlativo', copy=False, readonly=True)
    company_id = fields.Many2one('res.company', required=True, index=True,
                                 default=lambda s: s.env.company)
    # Estado espeja al del comprobante (para reusar la UI de estados en el front).
    estado = fields.Selection([
        ('borrador', 'Borrador'),
        ('en_proceso', 'En proceso'),
        ('enviado', 'Aceptado'),
        ('rechazado', 'Rechazado'),
        ('error', 'Error'),
        ('anulado', 'Anulado'),
    ], string='Estado', default='borrador', required=True, copy=False)

    # -------------------------------------------------------------- cabecera
    fecha_emision = fields.Date(string='Fecha de emisión', required=True,
                               default=fields.Date.context_today)
    hora_emision = fields.Char(string='Hora de emisión', default='08:00:00')
    obs_guia = fields.Char(string='Observación')

    # Destinatario (a quién se le entrega). El tipo/num doc se derivan del partner.
    partner_id = fields.Many2one('res.partner', string='Destinatario', required=True, index=True)

    # Remitente (quien ENVÍA los bienes): solo aplica a la GRE transportista (tipo 31), donde
    # el emisor es el carrier y el remitente es un tercero al que el carrier hace referencia.
    # En la GRE remitente (09) el emisor ES el remitente y este campo queda vacío.
    remitente_id = fields.Many2one('res.partner', string='Remitente (quien envía)')

    # Datos del traslado.
    motivo_traslado = fields.Selection(MOTIVOS_TRASLADO, string='Motivo de traslado',
                                       default='01', required=True)
    des_motivo_traslado = fields.Char(string='Descripción del motivo')
    peso_bruto = fields.Float(string='Peso bruto total', required=True, default=1.0)
    uni_medida_peso = fields.Selection(UNIDADES_PESO, string='Unidad de peso',
                                       default='KGM', required=True)
    num_bultos = fields.Integer(string='N° de bultos', default=1)
    modalidad_traslado = fields.Selection(MODALIDADES_TRASLADO, string='Modalidad',
                                          default='02', required=True)
    fecha_inicio_traslado = fields.Date(string='Fecha de inicio del traslado', required=True,
                                        default=fields.Date.context_today)

    # Transporte público (modalidad 01): datos del transportista.
    transportista_id = fields.Many2one('res.partner', string='Transportista')
    num_reg_mtc = fields.Char(string='Registro MTC')

    # Proveedor (motivo 02 Compra): SellerSupplierParty en el XML.
    proveedor_id = fields.Many2one('res.partner', string='Proveedor')

    # Transporte privado (modalidad 02): vehículo + conductor.
    num_placa = fields.Char(string='Placa del vehículo')
    # Tarjeta Única de Circulación del vehículo: solo GRE transportista (tipo 31). El MTC
    # propio del carrier reusa num_reg_mtc.
    num_tuc = fields.Char(string='TUC del vehículo')
    conductor_tipo_doc = fields.Selection([('1', 'DNI'), ('4', 'Carné ext.'), ('7', 'Pasaporte')],
                                          string='Tipo doc. conductor', default='1')
    conductor_num_doc = fields.Char(string='N° doc. conductor')
    conductor_nombres = fields.Char(string='Nombres del conductor')
    conductor_apellidos = fields.Char(string='Apellidos del conductor')
    conductor_licencia = fields.Char(string='Licencia de conducir')

    # Puntos de partida y llegada (ubigeo cat. 13 + dirección).
    ubigeo_partida = fields.Char(string='Ubigeo de partida', required=True)
    dir_partida = fields.Char(string='Dirección de partida', required=True)
    ubigeo_llegada = fields.Char(string='Ubigeo de llegada', required=True)
    dir_llegada = fields.Char(string='Dirección de llegada', required=True)

    # Comprobante relacionado (docRelacionado): la factura/boleta que origina el traslado.
    # comprobante_id se mantiene como "primer documento" por compat con guías/SPA viejas;
    # comprobante_ids es la lista completa (0..n) que alimenta el docRelacionado real.
    comprobante_id = fields.Many2one('account.move', string='Comprobante relacionado',
                                     copy=False, index=True)
    comprobante_ids = fields.Many2many('account.move', 'l10n_pe_ne_guia_move_rel',
                                       'guia_id', 'move_id', string='Comprobantes relacionados',
                                       copy=False)

    # Indicadores de traslado (booleanos GRE remitente 2.1): '1' en el payload si están
    # activos, ausentes si no (el biller no acepta 'false'/'0' — ver _l10n_pe_ne_build_gre_payload).
    ind_transbordo = fields.Boolean(string='Transbordo programado')
    ind_m1l = fields.Boolean(string='Vehículos categoría M1 o L')
    ind_retorno_envases = fields.Boolean(string='Retorno con envases vacíos')
    ind_retorno_vacio = fields.Boolean(string='Retorno de vehículo vacío')
    fecha_entrega_transportista = fields.Date(
        string='Entrega de bienes al transportista',
        help='Modalidad 01 (transporte público): SUNAT la exige (observación 3617).')

    # Establecimientos anexos PROPIOS del emisor como punto de partida/llegada (motivo 04:
    # traslado entre establecimientos de la misma empresa). El RUC que acompaña al código
    # en el payload SIEMPRE es el de esta compañía (son establecimientos propios, no de
    # terceros) — nunca se manda un codEstab* sin su rucEstab* gemelo.
    cod_estab_partida = fields.Char(string='Cód. establecimiento partida')
    cod_estab_llegada = fields.Char(string='Cód. establecimiento llegada')
    # Comercio exterior (08 Importación / 09 Exportación): N° de la declaración aduanera.
    # Viaja como documento relacionado; su formato codifica el régimen según el tipo — DAM/DUA
    # (50) 10 importación / 40 exportación, DSI (52) 18 / 48. Ver DECLARACION_REGIMEN.
    dam_numero = fields.Char(string='N° de declaración aduanera')
    # Tipo de declaración aduanera (DocumentTypeCode SUNAT del docRelacionado): 50 = DAM/DUA
    # (Declaración Aduanera de Mercancías) · 52 = DSI (Declaración Simplificada). El régimen
    # embebido en dam_numero depende de este tipo (ver DECLARACION_REGIMEN).
    dam_tipo = fields.Selection([('50', 'DAM/DUA'), ('52', 'DSI')],
                                string='Tipo de declaración', default='50')
    # Puerto/aeropuerto de embarque/desembarque (cat_63 puerto / cat_64 aeropuerto). El
    # nombre se deriva del catálogo (PUERTOS/AEROPUERTOS), no es un campo. En la importación
    # (08) el ubigeo del puerto es el punto de partida; en la exportación (09), el de llegada.
    puerto_codigo = fields.Char(string='Cód. puerto/aeropuerto (SUNAT)')
    puerto_tipo = fields.Selection([('1', 'Puerto'), ('2', 'Aeropuerto')],
                                   string='Tipo de punto de embarque')
    # Comercio exterior — contenedor y precinto (cac:TransportHandlingUnit/cac:Package). Con
    # contenedor + indicador total, el precinto (TraceID) es obligatorio (SUNAT 3422) y los
    # bultos quedan PROHIBIDOS (SUNAT 3621) — el biller los suprime con el sentinela "-".
    num_contenedor = fields.Char(string='N° de contenedor')
    num_precinto = fields.Char(string='N° de precinto')
    # Segundo contenedor (opcional): SUNAT admite un MÁXIMO de 2 contenedores por guía
    # (regla 3420). Requiere el primero (num_contenedor), su propio precinto (3422) y ser
    # distinto del primero en contenedor y precinto (3423). El biller lo emite en su propio
    # cac:Package con las claves planas numContenedor2/numPrecinto2.
    num_contenedor2 = fields.Char(string='N° de contenedor 2')
    num_precinto2 = fields.Char(string='N° de precinto 2')

    # Autorización de carga (permiso especial de transporte, catálogo D37 SUNAT).
    ent_autorizacion_carga = fields.Char(string='Entidad autorización de carga (D37)')
    num_autorizacion_carga = fields.Char(string='N° autorización de carga')

    # Vehículo(s)/conductor(es) del traslado (transporte privado): uno principal + hasta 2
    # secundarios de cada uno. Los campos num_placa/conductor_* de arriba siguen siendo el
    # camino legado de un único vehículo/conductor sin lista — ver _l10n_pe_ne_principal().
    vehiculo_ids = fields.One2many('l10n_pe_ne.guia_remision.vehiculo', 'guia_id',
                                   string='Vehículos', copy=True)
    conductor_ids = fields.One2many('l10n_pe_ne.guia_remision.conductor', 'guia_id',
                                    string='Conductores', copy=True)

    line_ids = fields.One2many('l10n_pe_ne.guia_remision.line', 'guia_id',
                               string='Bienes', copy=True)

    # Resultado del biller.
    l10n_pe_biller_xml = fields.Many2one('ir.attachment', string='XML firmado', copy=False)
    l10n_pe_biller_cdr = fields.Many2one('ir.attachment', string='CDR', copy=False)
    l10n_pe_biller_message = fields.Char(string='Mensaje del facturador', copy=False)
    num_ticket = fields.Char(string='N° de ticket SUNAT', copy=False)
    # Nº de re-consultas del ticket ya intentadas (lo incrementa el cron). Acota la
    # re-consulta: pasado un tope el ticket se da por muerto y deja de gastar la ventana
    # del cron (mismo espíritu que l10n_pe_ne_envio_intentos en account.move).
    consulta_intentos = fields.Integer(string='Intentos de re-consulta', default=0, copy=False)
    l10n_pe_ne_qr_url = fields.Char(string='URL del QR (SUNAT)', copy=False,
                                    help='Viene en el CDR aceptado; es el QR válido para sustentar el traslado.')

    def init(self):
        # Único parcial sobre las secuencias de guía: bajo REPEATABLE READ el lock
        # consultivo no basta (el snapshot de una transacción concurrente puede no ver
        # la secuencia recién commiteada y crearla de nuevo, duplicando correlativos).
        # El índice convierte esa carrera en IntegrityError: un request fallido,
        # nunca un correlativo duplicado ante SUNAT.
        self.env.cr.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS ir_sequence_gre_code_company_uniq
            ON ir_sequence (code, company_id)
            WHERE code LIKE 'l10n_pe.ne.guia_remision.%'
        """)

    @api.model
    def _l10n_pe_ne_next_correlativo(self, company, serie):
        """Correlativo por (compañía, serie): SUNAT exige numeración correlativa por serie y
        por RUC. Crea la secuencia al primer uso, sembrada tras el correlativo más alto ya
        emitido en esa serie (migración desde la secuencia global inicial)."""
        code = 'l10n_pe.ne.guia_remision.%s' % serie
        # Lock consultivo: serializa el primer uso de una (serie, compañía) en el caso
        # común. La garantía dura la pone el índice único parcial (ver init()): bajo
        # REPEATABLE READ una transacción concurrente puede no ver el commit ajeno.
        self.env.cr.execute(
            "SELECT pg_advisory_xact_lock(hashtext(%s))", ('%s/%s' % (code, company.id),))
        Seq = self.env['ir.sequence'].sudo()
        seq = Seq.search([('code', '=', code), ('company_id', '=', company.id)], limit=1)
        if not seq:
            ultimo = 0
            for g in self.sudo().search([('company_id', '=', company.id), ('serie', '=', serie)]):
                try:
                    ultimo = max(ultimo, int(g.correlativo or 0))
                except ValueError:
                    pass
            seq = Seq.create({
                'name': 'GRE %s (%s)' % (serie, company.display_name),
                'code': code,
                'company_id': company.id,
                'padding': 1,
                'number_increment': 1,
                'implementation': 'no_gap',
                'number_next': ultimo + 1,
            })
        return seq.next_by_id()

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if not vals.get('name') or vals.get('name') == _('Nueva'):
                # La serie codifica el tipo de GRE ante SUNAT: Remitente usa T###,
                # Transportista (tipo 31) exige V### — con T### SUNAT rechaza el
                # DespatchAdvice/cbc:ID (errorCode 1001). Se fija explícito en vals para
                # que el campo serie quede coherente con name/correlativo (no basta el
                # default del campo, que es T### para ambos).
                serie = vals.get('serie')
                if not serie:
                    serie = 'V001' if vals.get('tipo_gre') == '31' else 'T001'
                vals['serie'] = serie
                company = self.env['res.company'].browse(
                    vals.get('company_id') or self.env.company.id)
                corr = self._l10n_pe_ne_next_correlativo(company, serie)
                vals['correlativo'] = corr
                vals['name'] = '%s-%s' % (serie, corr)
        return super().create(vals_list)

    # -------------------------------------------------------- payload biller
    def _l10n_pe_ne_doc_tipo(self, partner):
        """Tipo de documento SUNAT (cat. 6): 6=RUC (11 dígitos), 1=DNI (8). Cualquier otra
        cosa se rechaza acá — mandarlo mal es rechazo seguro de SUNAT."""
        vat = (partner.vat or '').strip()
        if len(vat) == 11:
            return '6'
        if len(vat) == 8:
            return '1'
        raise UserError(_('El documento de "%s" debe ser RUC (11 dígitos) o DNI (8); tiene "%s".')
                        % (partner.display_name, vat or '—'))

    def _l10n_pe_ne_principal(self, recs):
        """El registro marcado `principal`, o el primero si ninguno lo está (recordset
        vacío si `recs` está vacío). Se usa igual en el payload y en la validación: una
        guía con un solo vehículo/conductor sin marcar lo trata como el principal —
        ambigüedad real solo hay si hay MÁS de uno marcado."""
        return recs.filtered('principal')[:1] or recs[:1]

    def _l10n_pe_ne_comprobante_numero(self, m):
        """'serie-correlativo' de un comprobante relacionado para la representación impresa.
        Misma cadena de respaldo que `L10nPeNeCotizacion._l10n_pe_ne_comprobante_numero`
        (l10n_pe_ne_cotizacion.py): serie/correlativo emitidos por este addon si existen,
        si no la numeración propia del asiento (l10n_pe_serie), y si tampoco hay eso,
        m.name — nunca un '-' vacío para un comprobante aún no emitido a SUNAT."""
        serie = m.l10n_pe_ne_serie_emit or m.l10n_pe_serie or ''
        corr = m.l10n_pe_ne_corr_emit or ''
        return ('%s-%s' % (serie, corr)) if (serie or corr) else (m.name or '')

    def _l10n_pe_ne_puerto_entry(self):
        """(ubigeo, nombre) del puerto/aeropuerto elegido según su tipo (cat_63 puerto /
        cat_64 aeropuerto), o None si el código no está en el catálogo. Tipo '2' =
        aeropuerto; cualquier otro (o vacío) se trata como puerto."""
        self.ensure_one()
        catalogo = AEROPUERTOS if self.puerto_tipo == '2' else PUERTOS
        return catalogo.get((self.puerto_codigo or '').strip().upper())

    def _l10n_pe_ne_puerto_ubigeo(self):
        """Ubigeo (cat.13) del puerto/aeropuerto elegido, o '' si el código no está en el
        catálogo. Es el ubigeo que la regla SUNAT 3364 exige en el punto de partida
        (importación 08) o de llegada (exportación 09)."""
        entry = self._l10n_pe_ne_puerto_entry()
        return entry[0] if entry else ''

    def _l10n_pe_ne_build_gre_payload(self):
        """Arma el JSON que espera el biller (`GreRequest`). Claves = campos de GreCabeceraRequest."""
        self.ensure_one()
        dest = self.partner_id
        cab = {
            'ublVersionId': '2.1',
            'customizationId': '2.0',
            'fecEmision': self.fecha_emision.strftime('%Y-%m-%d') if self.fecha_emision else '',
            'horEmision': self.hora_emision or '08:00:00',
            'obsGuia': self.obs_guia or '',
            'tipDocDestinatario': self._l10n_pe_ne_doc_tipo(dest),
            'numDocDestinatario': dest.vat or '',
            'rznSocialDestinatario': dest.name or '',
            'motTrasladoDatosEnvio': self.motivo_traslado,
            'desMotivoTrasladoDatosEnvio': self.des_motivo_traslado
                or dict(MOTIVOS_TRASLADO).get(self.motivo_traslado, ''),
            'psoBrutoTotalBienesDatosEnvio': '%.3f' % (self.peso_bruto or 0.0),
            'uniMedidaPesoBrutoDatosEnvio': self.uni_medida_peso or 'KGM',
            'numBultosDatosEnvio': str(self.num_bultos or 1),
            'modTrasladoDatosEnvio': self.modalidad_traslado,
            'fecInicioTrasladoDatosEnvio': self.fecha_inicio_traslado.strftime('%Y-%m-%d')
                if self.fecha_inicio_traslado else '',
            'ubiPartida': self.ubigeo_partida or '',
            'dirPartida': self.dir_partida or '',
            'ubiLlegada': self.ubigeo_llegada or '',
            'dirLlegada': self.dir_llegada or '',
        }
        if self.motivo_traslado == '02' and self.proveedor_id:
            prov = self.proveedor_id
            cab.update({
                'tipDocProveedor': self._l10n_pe_ne_doc_tipo(prov),
                'numDocProveedor': prov.vat or '',
                'rznSocialProveedor': prov.name or '',
            })
        # Indicadores: '1' cuando activos, ausentes cuando no (el biller rechaza 'false'/'0').
        if self.ind_transbordo:
            cab['indTransbordoProgDatosEnvio'] = '1'
        if self.ind_m1l:
            cab['indTrasladoVehiculoM1L'] = '1'
        if self.ind_retorno_envases:
            cab['indRetornoVehiculoEnvaseVacio'] = '1'
        if self.ind_retorno_vacio:
            cab['indRetornoVehiculoVacio'] = '1'
        # Comercio exterior (08 importación / 09 exportación): el indicador de traslado total
        # DAM/DS relaja el nivel de línea (así el detalle de bienes del baseline pasa el XSLT
        # sin exigir unidad cat.65 ni cantidad). La DAM viaja como documento relacionado
        # (ver _guia_doc_relacionado).
        if self.motivo_traslado in ('08', '09'):
            cab['indTrasladoTotalDAMoDS'] = '1'
        # Puerto/aeropuerto de embarque/desembarque (cac:FirstArrivalPortLocation): codPuerto
        # + locTypePuerto ('1' puerto cat_63 / '2' aeropuerto cat_64) + nomPuerto (del
        # catálogo). En la importación es el puerto de ingreso; en la exportación, el de salida.
        if self.puerto_codigo:
            entry = self._l10n_pe_ne_puerto_entry()
            cab['codPuerto'] = self.puerto_codigo
            cab['locTypePuerto'] = self.puerto_tipo or '1'
            cab['nomPuerto'] = entry[1] if entry else ''
        # Contenedor (comercio exterior): va como cac:Package. Con el indicador total presente,
        # SUNAT 3621 PROHÍBE TotalTransportHandlingUnitQuantity (bultos) — se suprime con el
        # sentinela "-" que el biller interpreta como "no emitir bultos"; el precinto (3422) es
        # obligatorio y lo garantiza la validación.
        if self.num_contenedor:
            cab['numContenedor'] = self.num_contenedor
            cab['numPrecinto'] = self.num_precinto or ''
            cab['numBultosDatosEnvio'] = '-'
        # Segundo contenedor (comercio exterior, máx 2 por SUNAT 3420): el biller emite un 2°
        # cac:Package con su precinto. El sentinela '-' de bultos ya lo puso el primer
        # contenedor (un 2° siempre exige el 1° — ver _l10n_pe_ne_validar), así que no se re-toca.
        if self.num_contenedor2:
            cab['numContenedor2'] = self.num_contenedor2
            cab['numPrecinto2'] = self.num_precinto2 or ''
        # Establecimientos propios (motivo 04): el RUC gemelo SIEMPRE es el de esta
        # compañía — nunca se manda un codEstab* sin su rucEstab* (el biller lo rechaza).
        # Sin RUC de compañía configurado no hay con qué llenar rucEstab*: mejor fallar acá
        # con un mensaje claro que mandar el campo vacío al biller (F4).
        if (self.cod_estab_partida or self.cod_estab_llegada) and not self.company_id.vat:
            raise UserError(_('Configure el RUC de la compañía antes de emitir con '
                              'establecimiento propio de partida/llegada.'))
        if self.cod_estab_partida:
            cab['codEstabPartida'] = self.cod_estab_partida
            cab['rucEstabPartida'] = self.company_id.vat
        if self.cod_estab_llegada:
            cab['codEstabLlegada'] = self.cod_estab_llegada
            cab['rucEstabLlegada'] = self.company_id.vat
        if self.fecha_entrega_transportista:
            cab['fecEntregaBienesTransportista'] = self.fecha_entrega_transportista.strftime('%Y-%m-%d')
        if self.ent_autorizacion_carga:
            cab['entAutorizacionCarga'] = self.ent_autorizacion_carga
        if self.num_autorizacion_carga:
            cab['numAutorizacionCarga'] = self.num_autorizacion_carga
        if self.modalidad_traslado == '01':  # transporte público
            t = self.transportista_id
            cab.update({
                'tipDocTransportista': self._l10n_pe_ne_doc_tipo(t) if t else '6',
                'numDocTransportista': (t.vat or '') if t else '',
                'nomTransportista': (t.name or '') if t else '',
                'numRegMtcTransportista': self.num_reg_mtc or '',
            })
        else:  # transporte privado: el vehículo/conductor PRINCIPAL alimenta las claves
               # legadas (numPlacaTransPrivado/conductor*); sin lista, valen num_placa/
               # conductor_* de siempre (compat guías viejas — ver test_compat_legado).
            veh = self._l10n_pe_ne_principal(self.vehiculo_ids)
            if veh:
                cab['numPlacaTransPrivado'] = veh.placa or ''
                if veh.ent_autorizacion:
                    cab['entAutorizacionVehiculoPrincipal'] = veh.ent_autorizacion
                if veh.num_autorizacion:
                    cab['numAutorizacionVehiculoPrincipal'] = veh.num_autorizacion
            else:
                cab['numPlacaTransPrivado'] = self.num_placa or ''
            cond = self._l10n_pe_ne_principal(self.conductor_ids)
            if cond:
                cab.update({
                    'tipDocIdeConductorTransPrivado': cond.tipo_doc or '1',
                    'numDocIdeConductorTransPrivado': cond.num_doc or '',
                    'nomConductorTransPrivado': cond.nombres or '',
                    'apeConductorTransPrivado': cond.apellidos or '',
                    'licConductorTransPrivado': cond.licencia or '',
                })
            else:
                cab.update({
                    'tipDocIdeConductorTransPrivado': self.conductor_tipo_doc or '1',
                    'numDocIdeConductorTransPrivado': self.conductor_num_doc or '',
                    'nomConductorTransPrivado': self.conductor_nombres or '',
                    'apeConductorTransPrivado': self.conductor_apellidos or '',
                    'licConductorTransPrivado': self.conductor_licencia or '',
                })
            secundarios_veh = self.vehiculo_ids - veh
            if secundarios_veh:
                cab['vehiculosSecundarios'] = [{
                    'numPlaca': v.placa or '',
                    'entAutorizacion': v.ent_autorizacion or '',
                    'numAutorizacion': v.num_autorizacion or '',
                } for v in secundarios_veh]
            secundarios_cond = self.conductor_ids - cond
            if secundarios_cond:
                cab['conductoresSecundarios'] = [{
                    'tipDoc': c.tipo_doc or '1',
                    'numDoc': c.num_doc or '',
                    'nombres': c.nombres or '',
                    'apellidos': c.apellidos or '',
                    'licencia': c.licencia or '',
                } for c in secundarios_cond]
        resp = {
            'id': {
                'ruc': self.company_id.vat or '',
                'serie': self.serie or 'T001',
                'correlativo': self.correlativo or '1',
            },
            'cabecera': cab,
            # detalle (bien normalizado cat.25 + GTIN) y docRelacionado son idénticos en
            # remitente y transportista → helpers compartidos.
            'detalle': self._l10n_pe_ne_guia_detalle_lines(),
            'docRelacionado': self._l10n_pe_ne_guia_doc_relacionado(),
        }
        _logger.info("--------------------- PAYLOAD GRE ---------------------")
        _logger.info('GRE payload: %s', resp)
        _logger.info("--------------------- FIN PAYLOAD GRE ---------------------")
        return resp

    def _l10n_pe_ne_guia_detalle_lines(self):
        """detalle (bienes) del payload — idéntico en remitente y transportista: código
        de producto SUNAT (cat.25) + GTIN del producto, vacíos si el bien es texto libre."""
        return [{
            'canItem': '%.2f' % (l.cantidad or 0.0),
            'uniMedidaItem': l.unidad or 'NIU',
            'desItem': l.descripcion or (l.product_id.display_name or ''),
            'codItem': (l.product_id.default_code or '') if l.product_id else '',
            'codProductoSUNAT': (l.product_id.l10n_pe_ne_cod_producto_sunat or '') if l.product_id else '',
            'gtin': (l.product_id.barcode or '') if l.product_id else '',
        } for l in self.line_ids]

    def _l10n_pe_ne_guia_doc_relacionado(self):
        """docRelacionado del payload — idéntico en remitente y transportista: itera
        comprobante_ids (lista nueva) y cae al comprobante_id legado; descarta los que aún
        no fueron emitidos por este addon (sin serie_emit — _l10n_pe_ne_validar los rechaza)."""
        docs = self.comprobante_ids or self.comprobante_id
        rel = [{
            'codTipDocRel': m.l10n_pe_ne_tipo_doc or '01',
            'numDocRel': '%s-%s' % (m.l10n_pe_ne_serie_emit, m.l10n_pe_ne_corr_emit or ''),
        } for m in docs if m.l10n_pe_ne_serie_emit]
        # Comercio exterior (08 importación / 09 exportación): la declaración aduanera como
        # documento relacionado — DAM/DUA (DocumentTypeCode 50) o DSI (52) según dam_tipo. No
        # lleva IssuerParty (el gate 3380/3382 no aplica a 50/52); el FTL del biller emite el
        # bloque idéntico para ambos códigos.
        if self.motivo_traslado in ('08', '09') and self.dam_numero:
            rel.append({'codTipDocRel': self.dam_tipo or '50', 'numDocRel': self.dam_numero})
        return rel

    def _l10n_pe_ne_build_gre_transportista_payload(self):
        """Arma el JSON de la GRE transportista (`GreTransportistaRequest`, tipo 31). El emisor
        es el transportista y lo inyecta el biller desde el TaxPayer (igual que el emisor de la
        remitente); acá van el remitente (quien envía), el destinatario, un único vehículo
        (placa + TUC) y un único conductor. Claves = campos de GreTransportistaCabeceraRequest."""
        self.ensure_one()
        rem = self.remitente_id
        dest = self.partner_id
        # El conductor principal de la lista alimenta las claves; sin lista, valen los
        # campos legados conductor_* (mismo patrón que el builder remitente).
        cond = self._l10n_pe_ne_principal(self.conductor_ids)
        cab = {
            'ublVersionId': '2.1',
            'customizationId': '2.0',
            'fecEmision': self.fecha_emision.strftime('%Y-%m-%d') if self.fecha_emision else '',
            'horEmision': self.hora_emision or '08:00:00',
            'obsGuia': self.obs_guia or '',
            'tipDocRemitente': self._l10n_pe_ne_doc_tipo(rem) if rem else '',
            'numDocRemitente': (rem.vat or '') if rem else '',
            'rznSocialRemitente': (rem.name or '') if rem else '',
            'tipDocDestinatario': self._l10n_pe_ne_doc_tipo(dest),
            'numDocDestinatario': dest.vat or '',
            'rznSocialDestinatario': dest.name or '',
            'psoBrutoTotalBienesDatosEnvio': '%.3f' % (self.peso_bruto or 0.0),
            'uniMedidaPesoBrutoDatosEnvio': self.uni_medida_peso or 'KGM',
            'numBultosDatosEnvio': str(self.num_bultos or 1),
            'fecInicioTrasladoDatosEnvio': self.fecha_inicio_traslado.strftime('%Y-%m-%d')
                if self.fecha_inicio_traslado else '',
            'numRegMtcTransportista': self.num_reg_mtc or '',
            'numPlacaVehiculoPrincipal': self.num_placa or '',
            'numTucVehiculoPrincipal': self.num_tuc or '',
            'tipDocConductor': (cond.tipo_doc if cond else self.conductor_tipo_doc) or '1',
            'numDocConductor': (cond.num_doc if cond else self.conductor_num_doc) or '',
            'nomConductor': (cond.nombres if cond else self.conductor_nombres) or '',
            'apeConductor': (cond.apellidos if cond else self.conductor_apellidos) or '',
            'licConductor': (cond.licencia if cond else self.conductor_licencia) or '',
            'ubiPartida': self.ubigeo_partida or '',
            'dirPartida': self.dir_partida or '',
            'ubiLlegada': self.ubigeo_llegada or '',
            'dirLlegada': self.dir_llegada or '',
        }
        return {
            'id': {
                'ruc': self.company_id.vat or '',
                'serie': self.serie or 'T001',
                'correlativo': self.correlativo or '1',
            },
            'cabecera': cab,
            'detalle': self._l10n_pe_ne_guia_detalle_lines(),
            'docRelacionado': self._l10n_pe_ne_guia_doc_relacionado(),
        }

    # ------------------------------------------------------------- emisión
    def _l10n_pe_ne_tipo_cod(self):
        """Código de tipo de documento SUNAT para el nombre de archivo: 31 transportista, 09
        remitente (RUC-09-serie-corr / RUC-31-serie-corr)."""
        return '31' if self.tipo_gre == '31' else '09'

    def _l10n_pe_ne_validar(self):
        self.ensure_one()
        if self.estado not in ESTADOS_EMITIBLES:
            raise UserError(_('La guía %s ya fue emitida (estado: %s).') % (self.name, self.estado))
        # La serie codifica el tipo ante SUNAT (T### remitente / V### transportista). El
        # selector de tipo se bloquea al editar, pero si por API se cambiara el tipo de una
        # guía ya numerada, serie y tipo quedarían desincronizados y SUNAT rechazaría el
        # cbc:ID (errorCode 1001). Se corta acá con un mensaje claro antes de emitir.
        prefijo = (self.serie or '')[:1].upper()
        if self.tipo_gre == '31' and prefijo != 'V':
            raise UserError(_('Una guía de transportista (31) necesita una serie V### (la '
                              'actual es %s). Crea una guía nueva con el tipo correcto.') % (self.serie or '—'))
        if self.tipo_gre == '09' and prefijo != 'T':
            raise UserError(_('Una guía de remitente (09) necesita una serie T### (la actual '
                              'es %s). Crea una guía nueva con el tipo correcto.') % (self.serie or '—'))
        if not self.line_ids:
            raise UserError(_('La guía necesita al menos un bien.'))
        if not self.peso_bruto or self.peso_bruto <= 0:
            raise UserError(_('El peso bruto debe ser mayor a 0.'))
        for campo, etiqueta in (('ubigeo_partida', 'partida'), ('ubigeo_llegada', 'llegada')):
            if not re.fullmatch(r'\d{6}', self[campo] or ''):
                raise UserError(_('El ubigeo de %s debe tener 6 dígitos.') % etiqueta)
        if self.fecha_inicio_traslado and self.fecha_emision \
                and self.fecha_inicio_traslado < self.fecha_emision:
            raise UserError(_('La fecha de inicio del traslado no puede ser anterior a la emisión.'))
        self._l10n_pe_ne_doc_tipo(self.partner_id)  # valida RUC/DNI del destinatario
        # Un comprobante vinculado sin l10n_pe_ne_serie_emit nunca fue emitido por este
        # addon: _l10n_pe_ne_build_gre_payload lo descartaría en silencio de docRelacionado
        # (SUNAT jamás lo vería), mientras el PDF ya mostraría algo para él. Mejor rechazar
        # acá con un mensaje claro que dejar pasar un documento a medias.
        for m in (self.comprobante_ids or self.comprobante_id):
            if not m.l10n_pe_ne_serie_emit:
                raise UserError(_('El comprobante relacionado %s aún no ha sido emitido a SUNAT.')
                                % (m.name or m.id))
        # GRE transportista (tipo 31): el emisor es el carrier; el remitente (quien envía) es
        # una parte nueva, y hay un solo vehículo (placa + TUC) y un solo conductor. Se salta
        # aquí (return) todo lo propio de la remitente: motivo/modalidad/establecimiento,
        # 2554/2555, exención M1L y topes de secundarios.
        if self.tipo_gre == '31':
            if not self.remitente_id:
                raise UserError(_('Indica el remitente (quien envía los bienes).'))
            self._l10n_pe_ne_doc_tipo(self.remitente_id)  # valida RUC/DNI del remitente
            # SUNAT 2560: el remitente (DespatchParty) no puede ser el propio transportista
            # (DespatchSupplierParty = emisor). Si el que transporta también envía, corresponde
            # una guía de remitente (09), no de transportista.
            if (self.remitente_id.vat or '').strip() and \
                    (self.remitente_id.vat or '').strip() == (self.company_id.vat or '').strip():
                raise UserError(_('El remitente no puede ser el mismo transportista (emisor). '
                                  'Si tú envías los bienes, emite una guía de remitente (09).'))
            # Un solo vehículo/conductor: placa desde num_placa; conductor desde el principal
            # de la lista o los campos legados (misma lógica de completitud del privado).
            cond = self._l10n_pe_ne_principal(self.conductor_ids)
            efectivos = (
                (_('la placa del vehículo'), self.num_placa or ''),
                (_('el documento del conductor'), (cond.num_doc if cond else self.conductor_num_doc) or ''),
                (_('los nombres del conductor'), (cond.nombres if cond else self.conductor_nombres) or ''),
                (_('los apellidos del conductor'), (cond.apellidos if cond else self.conductor_apellidos) or ''),
                (_('la licencia de conducir'), (cond.licencia if cond else self.conductor_licencia) or ''),
            )
            faltantes = [etiqueta for etiqueta, valor in efectivos if not valor.strip()]
            if faltantes:
                raise UserError(_('Guía transportista: falta %s.') % ', '.join(faltantes))
            # SUNAT 2567: la placa (cbc:ID del TransportEquipment) debe ser 6-8 alfanuméricos
            # en mayúscula, no todo ceros. Validarlo evita un rechazo confuso del biller.
            placa = (self.num_placa or '').strip()
            if not re.match(r'^(?!0+$)[0-9A-Z]{6,8}$', placa):
                raise UserError(_('La placa del vehículo debe tener de 6 a 8 caracteres '
                                  'alfanuméricos en mayúscula (SUNAT 2567).'))
            # La TUC es opcional, pero si va debe cumplir el formato SUNAT (cbc:Registration-
            # NationalityID, errorCode 3355): 10 a 15 alfanuméricos en mayúscula, no todo ceros.
            # Validarlo aquí evita un rechazo 3355 poco claro del biller/SUNAT.
            tuc = (self.num_tuc or '').strip()
            if tuc and not re.match(r'^(?!0+$)[0-9A-Z]{10,15}$', tuc):
                raise UserError(_('La TUC del vehículo debe tener de 10 a 15 caracteres '
                                  'alfanuméricos en mayúscula (SUNAT 3355).'))
            return
        if self.motivo_traslado not in SUPPORTED_MOTIVOS:
            raise UserError(_('El motivo de traslado %s aún no soportado para emisión.')
                            % self.motivo_traslado)
        if self.motivo_traslado == '13' and not (self.des_motivo_traslado or '').strip():
            raise UserError(_('El motivo "Otros" requiere describir el motivo del traslado.'))
        # El contenedor (cac:Package) solo aplica a comercio exterior: en otros motivos el XSLT
        # lo rechaza (el detalle de contenedor por línea 7024-7028 es exclusivo del motivo 19).
        if (self.num_contenedor or self.num_contenedor2) and self.motivo_traslado not in ('08', '09'):
            raise UserError(_('El contenedor solo aplica a comercio exterior (importación/exportación).'))
        if self.motivo_traslado in ('08', '09'):
            # Comercio exterior: la declaración aduanera (DAM/DUA o DSI) es obligatoria (SUNAT
            # 3440) y su N° codifica el régimen (SUNAT 3441) según el tipo — DAM 10/40, DSI 18/48.
            # El regex y la etiqueta (nombran el documento y el régimen) salen del lookup.
            dam = (self.dam_numero or '').strip()
            tipo = self.dam_tipo or '50'
            if not dam:
                raise UserError(_('El comercio exterior requiere el N° de %s.')
                                % ('DSI' if tipo == '52' else 'DAM/DUA'))
            regimen = DECLARACION_REGIMEN.get((self.motivo_traslado, tipo))
            if regimen and not re.match(regimen[0], dam):
                raise UserError(_('El N° de %s debe tener el formato %s (p. ej. %s).')
                                % (regimen[1], regimen[2], regimen[2].replace('NNN', '235', 1)
                                   .replace('AAAA', '2024').replace('NNNNNN', '123456')))
            # Contenedor: con el indicador total el precinto es obligatorio (SUNAT 3422); ambos
            # tienen formato acotado (contenedor 4071, precinto 4074).
            if self.num_contenedor:
                cont = (self.num_contenedor or '').strip()
                if not re.match(r'^[A-Z0-9\-/]{1,17}$', cont):
                    raise UserError(_('El N° de contenedor debe tener hasta 17 caracteres '
                                      'alfanuméricos en mayúscula (SUNAT 4071).'))
                prec = (self.num_precinto or '').strip()
                if not prec:
                    raise UserError(_('El contenedor requiere el N° de precinto (SUNAT 3422).'))
                if not re.match(r'^(?!0+$)[A-Z0-9]{1,100}$', prec):
                    raise UserError(_('El N° de precinto debe ser alfanumérico en mayúscula '
                                      '(SUNAT 4074).'))
            # Segundo contenedor (opcional, máx 2 por SUNAT 3420): requiere el PRIMERO (no puede
            # haber un 2° sin 1°), su propio precinto (3422), formatos válidos (4071/4074) y ser
            # DISTINTO del primero en contenedor y precinto (3423).
            if self.num_contenedor2:
                cont2 = (self.num_contenedor2 or '').strip()
                if not self.num_contenedor:
                    raise UserError(_('Indica primero el N° de contenedor antes de agregar un segundo.'))
                if not re.match(r'^[A-Z0-9\-/]{1,17}$', cont2):
                    raise UserError(_('El N° de contenedor 2 debe tener hasta 17 caracteres '
                                      'alfanuméricos en mayúscula (SUNAT 4071).'))
                prec2 = (self.num_precinto2 or '').strip()
                if not prec2:
                    raise UserError(_('El segundo contenedor requiere el N° de precinto (SUNAT 3422).'))
                if not re.match(r'^(?!0+$)[A-Z0-9]{1,100}$', prec2):
                    raise UserError(_('El N° de precinto 2 debe ser alfanumérico en mayúscula '
                                      '(SUNAT 4074).'))
                if cont2 == (self.num_contenedor or '').strip():
                    raise UserError(_('Los dos contenedores deben ser distintos (SUNAT 3423).'))
                if prec2 == (self.num_precinto or '').strip():
                    raise UserError(_('Los precintos de ambos contenedores deben ser distintos (SUNAT 3423).'))
        # Puerto/aeropuerto (cat_63/cat_64): si se declara, exige el tipo y que el código
        # exista en el catálogo — de ahí sale el ubigeo que la regla SUNAT 3364 obliga a
        # que coincida con el punto de partida (importación) o de llegada (exportación).
        if self.puerto_codigo:
            if not self.puerto_tipo:
                raise UserError(_('Indica si el punto de embarque es un puerto o un aeropuerto.'))
            if not self._l10n_pe_ne_puerto_ubigeo():
                raise UserError(_('El código de puerto/aeropuerto "%s" no está en el catálogo '
                                  'SUNAT (cat_63 puerto / cat_64 aeropuerto).') % self.puerto_codigo)
        if self.motivo_traslado == '08':
            # Importación vía puerto (SUNAT 3365): el origen es el puerto/aeropuerto de ingreso
            # y su ubigeo debe ser el punto de partida (SUNAT 3364). No lleva establecimiento
            # propio de partida (ese iría con RUC del agente de aduanas — no modelado).
            if not self.puerto_codigo:
                raise UserError(_('La importación requiere el puerto/aeropuerto de ingreso.'))
            if self.ubigeo_partida != self._l10n_pe_ne_puerto_ubigeo():
                raise UserError(_('El ubigeo de partida debe coincidir con el del puerto elegido.'))
        elif self.motivo_traslado == '09':
            # Exportación: con puerto, el ubigeo de llegada debe ser el del puerto (SUNAT 3364)
            # y no se exige establecimiento de llegada; sin puerto, la llegada debe ser un
            # establecimiento propio (SUNAT 3369 exige AddressTypeCode de llegada presente).
            if self.puerto_codigo:
                # SUNAT 3369 es ASIMÉTRICO: en exportación solo un puerto MARÍTIMO (tipo 1)
                # exime el establecimiento de llegada; un aeropuerto (tipo 2) NO lo exime y el
                # XSLT rechaza (a diferencia de la importación, que sí admite tipo 2). La
                # exportación aérea va por el establecimiento de llegada, sin puerto.
                if self.puerto_tipo != '1':
                    raise UserError(_('La exportación por punto de embarque solo admite puertos '
                                      'marítimos. Para exportación aérea, deja el puerto vacío e '
                                      'indica el establecimiento de llegada.'))
                if self.ubigeo_llegada != self._l10n_pe_ne_puerto_ubigeo():
                    raise UserError(_('El ubigeo de llegada debe coincidir con el del puerto elegido.'))
            elif not self.cod_estab_llegada:
                raise UserError(_('La exportación sin puerto requiere indicar el establecimiento '
                                  'de llegada (elige un punto de llegada registrado).'))
        if self.motivo_traslado == '02':
            if not self.proveedor_id:
                raise UserError(_('El motivo "Compra" requiere indicar el proveedor.'))
            if self.cod_estab_partida:
                # SUNAT 3411: en Compra la partida es del proveedor, no un establecimiento
                # propio del emisor.
                raise UserError(_('El motivo "Compra" no admite establecimiento de partida '
                                  '(la partida es del proveedor, no del emisor).'))
        if self.motivo_traslado == '04' and not (self.cod_estab_partida and self.cod_estab_llegada):
            raise UserError(_('El motivo "Traslado entre establecimientos de la misma empresa" '
                              'requiere el código de establecimiento en partida y llegada.'))
        # SUNAT 2554/2555: el destinatario, según el motivo, debe (o no debe) ser el propio
        # emisor. Se valida acá con un mensaje claro para que el usuario no llegue al rechazo.
        motivo_txt = dict(MOTIVOS_TRASLADO).get(self.motivo_traslado, self.motivo_traslado)
        ruc_emisor = (self.company_id.vat or '').strip()
        ruc_dest = (self.partner_id.vat or '').strip()
        if self.motivo_traslado in MOTIVOS_DEST_ES_EMISOR:
            if not (len(ruc_dest) == 11 and ruc_dest == ruc_emisor):
                raise UserError(_('Para el motivo "%s" el destinatario debe ser tu propia empresa '
                                  '(el mismo RUC del emisor: %s).') % (motivo_txt, ruc_emisor or '—'))
        elif self.motivo_traslado in MOTIVOS_DEST_NO_EMISOR:
            if len(ruc_dest) == 11 and ruc_dest == ruc_emisor:
                raise UserError(_('Para el motivo "%s" el destinatario no puede ser tu propia empresa '
                                  '(debe ser un tercero distinto al emisor).') % motivo_txt)
        # Tope SUNAT: máximo 2 vehículos/conductores secundarios por guía (el biller no
        # lo limita — se valida acá). El principal (marcado o el primero) no cuenta.
        if self.vehiculo_ids and len(self.vehiculo_ids - self._l10n_pe_ne_principal(self.vehiculo_ids)) > 2:
            raise UserError(_('La guía admite máximo 2 vehículos secundarios.'))
        if self.conductor_ids and len(self.conductor_ids - self._l10n_pe_ne_principal(self.conductor_ids)) > 2:
            raise UserError(_('La guía admite máximo 2 conductores secundarios.'))
        if self.modalidad_traslado == '02':
            if self.ind_m1l:
                pass  # RS SUNAT: vehículos categoría M1/L no exigen vehículo/conductor
            else:
                if len(self.vehiculo_ids.filtered('principal')) > 1:
                    raise UserError(_('Transporte privado: solo puede haber un vehículo principal.'))
                if len(self.conductor_ids.filtered('principal')) > 1:
                    raise UserError(_('Transporte privado: solo puede haber un conductor principal.'))
                # Completitud exigida en AMBOS lados (vehículo Y conductor), sin importar
                # qué representación use cada uno (lista nueva o campos legados): antes,
                # que UN lado tuviera datos (en cualquier representación) bastaba para
                # saltarse la validación del otro, colando conductor/vehículo vacíos hasta
                # el biller (F1).
                veh = self._l10n_pe_ne_principal(self.vehiculo_ids)
                cond = self._l10n_pe_ne_principal(self.conductor_ids)
                efectivos = (
                    (_('la placa del vehículo'), (veh.placa if veh else self.num_placa) or ''),
                    (_('el documento del conductor'), (cond.num_doc if cond else self.conductor_num_doc) or ''),
                    (_('los nombres del conductor'), (cond.nombres if cond else self.conductor_nombres) or ''),
                    (_('los apellidos del conductor'), (cond.apellidos if cond else self.conductor_apellidos) or ''),
                    (_('la licencia de conducir'), (cond.licencia if cond else self.conductor_licencia) or ''),
                )
                faltantes = [etiqueta for etiqueta, valor in efectivos if not valor.strip()]
                if faltantes:
                    raise UserError(_('Transporte privado: falta %s.') % ', '.join(faltantes))
        else:
            if not self.transportista_id:
                raise UserError(_('Transporte público: indica el transportista.'))
            if len((self.transportista_id.vat or '').strip()) != 11:
                raise UserError(_('El transportista debe tener RUC (11 dígitos).'))
            if not self.fecha_entrega_transportista:
                # SUNAT 3617: obligatoria en modalidad 01 (el biller también la exige;
                # Odoo debe fallar primero con un mensaje amigable).
                raise UserError(_('Transporte público: indica la fecha de entrega de los '
                                  'bienes al transportista.'))

    def _l10n_pe_ne_extraer_qr_url(self, cdr_bytes):
        """URL del código QR que SUNAT incluye en el CDR aceptado de la GRE. El QR de la
        representación impresa DEBE ser este (RS 123-2022) — no uno generado localmente.
        Busca la primera URL http(s) en los cbc:Note / cbc:DocumentDescription del
        ApplicationResponse (regex sobre bytes, tolerante a namespaces)."""
        try:
            with zipfile.ZipFile(io.BytesIO(cdr_bytes)) as zf:
                xml_name = next((n for n in zf.namelist() if n.lower().endswith('.xml')), None)
                content = zf.read(xml_name) if xml_name else b''
        except Exception:  # noqa: BLE001
            return ''
        for m in re.finditer(rb'<cbc:(?:Note|DocumentDescription)>([^<]*)</cbc:(?:Note|DocumentDescription)>', content):
            um = re.search(r'https?://[^\s|<>"]+', m.group(1).decode('utf-8', 'replace'))
            if um:
                return um.group(0)
        return ''

    def _l10n_pe_ne_store_cdr(self, cdr_b64):
        """Guarda el CDR (zip base64 del header X-Sunat-Cdr) como adjunto en l10n_pe_biller_cdr
        y devuelve (responseCode, description). Reusa el parser del CDR de account.move."""
        self.ensure_one()
        try:
            cdr_bytes = base64.b64decode(cdr_b64)
        except Exception:  # noqa: BLE001
            return '', ''
        att = self.env['ir.attachment'].create({
            'name': 'R%s-%s-%s.zip' % (self.company_id.vat or '', self._l10n_pe_ne_tipo_cod(), self.name),
            'res_model': 'l10n_pe_ne.guia_remision',
            'res_id': self.id,
            'mimetype': 'application/zip',
            'raw': cdr_bytes,
        })
        self.l10n_pe_biller_cdr = att.id
        self.l10n_pe_ne_qr_url = self._l10n_pe_ne_extraer_qr_url(cdr_bytes) or self.l10n_pe_ne_qr_url
        return self.env['account.move']._l10n_pe_parse_cdr_codes(cdr_bytes)

    def _l10n_pe_ne_aplicar_cdr(self, cdr_b64):
        """Guarda el CDR y fija estado/mensaje según su ResponseCode (0 = aceptada).
        Camino común de la emisión síncrona y de la re-consulta del ticket."""
        code, desc = self._l10n_pe_ne_store_cdr(cdr_b64)
        if code == '0':
            self.estado = 'enviado'
            self.l10n_pe_biller_message = _('Aceptada por SUNAT — CDR ResponseCode 0. %s') % (desc or '')
            # Automatización (opt-in): al aceptarse, enviar la guía (XML + PDF + CDR) al correo
            # del destinatario. Gateado por config para no mandar correos sin querer; nunca rompe
            # la emisión (un fallo de correo se loguea y sigue). Espeja email_on_accept de factura.
            if self.env['ir.config_parameter'].sudo().get_param(
                    'l10n_pe_ne_biller.email_guia_on_accept', '').strip().lower() in ('1', 'true'):
                try:
                    self._l10n_pe_ne_email_guia()
                except Exception as e:  # noqa: BLE001
                    _logger.warning('email guía %s: %s', self.name, e)
        elif not code:
            # CDR ilegible (base64/zip corrupto o sin ResponseCode): NO es un rechazo
            # de SUNAT — queda en_proceso para que el botón/cron reintenten con el ticket.
            self.estado = 'en_proceso'
            self.l10n_pe_biller_message = _('CDR recibido pero ilegible; se reintentará la consulta del ticket.')
        else:
            self.estado = 'rechazado'
            self.l10n_pe_biller_message = _('Rechazada por SUNAT (ResponseCode %s). %s') % (code or '—', desc or '')

    def _l10n_pe_ne_email_guia(self):
        """Envía la guía aceptada (XML firmado + PDF + CDR) al correo del destinatario, con copia
        al remitente en el tipo 31. Automatiza la entrega manual. No-op si no hay correo; nunca
        lanza (el llamador la envuelve, pero igual usamos send sin excepción). Espeja
        _l10n_pe_ne_email_comprobante de la factura."""
        self.ensure_one()
        email = (self.partner_id.email or '').strip()
        cc = (self.remitente_id.email or '').strip() if (self.tipo_gre == '31' and self.remitente_id) else ''
        if not email and not cc:
            _logger.info('email guía %s: sin correo de destinatario/remitente, se omite', self.name)
            return False
        atts = self.env['ir.attachment']
        if self.l10n_pe_biller_xml:
            atts |= self.l10n_pe_biller_xml
        try:
            pdf, _ct = self.env['ir.actions.report'].sudo()._render_qweb_pdf(
                'l10n_pe_ne_biller.action_report_guia', res_ids=self.ids)
            if pdf:
                atts |= self.env['ir.attachment'].sudo().create({
                    'name': '%s.pdf' % self.name,
                    'res_model': 'l10n_pe_ne.guia_remision', 'res_id': self.id,
                    'mimetype': 'application/pdf', 'raw': pdf,
                })
        except Exception:  # noqa: BLE001 — el PDF es deseable pero no bloquea el correo
            pass
        if self.l10n_pe_biller_cdr:
            atts |= self.l10n_pe_biller_cdr
        subject = _('Guía de remisión electrónica %s') % self.name
        body = _(
            '<p>Estimado,</p>'
            '<p>Adjuntamos la guía de remisión electrónica <b>%(num)s</b> emitida por '
            '<b>%(emisor)s</b> y aceptada por SUNAT.</p>'
            '<p>Se incluyen el XML firmado, la representación impresa (PDF) y el CDR.</p>'
        ) % {'num': self.name, 'emisor': self.company_id.name or ''}
        mail = self.env['mail.mail'].sudo().create({
            'subject': subject,
            'body_html': body,
            'email_to': email or cc,
            'email_cc': cc if (email and cc) else '',
            'email_from': self.company_id.email or self.env.user.email_formatted,
            'attachment_ids': [(6, 0, atts.ids)],
            'auto_delete': False,
        })
        mail.send(raise_exception=False)
        _logger.info('email guía %s enviado a %s (%d adjuntos)', self.name, email or cc, len(atts))
        return True

    def l10n_pe_ne_emitir_guia(self):
        """Emite la GRE al biller: firma, envía a SUNAT y recoge el CDR. Según el tipo, va al
        endpoint remitente (`POST /generator/guia`, tipo 09) o transportista
        (`POST /generator/guiaTransportista`, tipo 31); solo cambian URL, payload y el código de
        tipo del nombre de archivo. El biller devuelve el XML firmado en el body y el CDR (zip
        base64) en el header `X-Sunat-Cdr` (igual que la factura). Guarda ambos y fija el estado
        según el ResponseCode del CDR (0 = aceptado)."""
        self.ensure_one()
        # SUNAT valida fecEmision/horEmision contra el momento del envío: se estampan al
        # emitir (hora de Lima), no al crear el borrador. Solo en estados emitibles — una
        # guía ya aceptada no debe ver su fecha pisada ni siquiera antes del UserError.
        if self.estado in ESTADOS_EMITIBLES:
            ahora_lima = fields.Datetime.context_timestamp(
                self.with_context(tz='America/Lima'), fields.Datetime.now())
            self.fecha_emision = ahora_lima.date()
            self.hora_emision = ahora_lima.strftime('%H:%M:%S')
        self._l10n_pe_ne_validar()
        icp = self.env['ir.config_parameter'].sudo()
        base = icp.get_param('l10n_pe_ne_biller.url', 'http://localhost:8090').rstrip('/')
        timeout = int(icp.get_param('l10n_pe_ne_biller.timeout', '240'))
        headers = {'X-Api-Key': self.company_id.sudo().l10n_pe_ne_api_key or ''}
        _logger.info("----------------------- Guia %s ---------------------", self.name)
        _logger.info('tipo_gre: %s', self.tipo_gre)
        if self.tipo_gre == '31':
            endpoint, payload = '/generator/guiaTransportista', self._l10n_pe_ne_build_gre_transportista_payload()
        else:
            endpoint, payload = '/generator/guia', self._l10n_pe_ne_build_gre_payload()
        try:
            resp = requests.post(base + endpoint, json=payload, headers=headers,
                                 timeout=(5, timeout))
        except requests.RequestException as exc:
            self.estado = 'error'
            self.l10n_pe_biller_message = _('Error de conexión con el facturador: %s') % exc
            return self._l10n_pe_ne_guia_dict()
        body = resp.text or ''
        if resp.status_code == 200 and any(t in body for t in ('<DespatchAdvice', '<ext:UBLExtensions')):
            att = self.env['ir.attachment'].create({
                'name': '%s-%s-%s.xml' % (self.company_id.vat, self._l10n_pe_ne_tipo_cod(), self.name),
                'res_model': 'l10n_pe_ne.guia_remision',
                'res_id': self.id,
                'mimetype': 'application/xml',
                'raw': body.encode('utf-8'),
            })
            self.l10n_pe_biller_xml = att.id
            self.num_ticket = resp.headers.get('X-Sunat-Ticket') or self.num_ticket
            cdr_b64 = resp.headers.get('X-Sunat-Cdr')
            if cdr_b64:
                self._l10n_pe_ne_aplicar_cdr(cdr_b64)
            else:
                # Firmada y enviada, pero SUNAT aún no devolvió el CDR: queda el ticket
                # para re-consultar (botón en la SPA + cron cada 10 min).
                self.estado = 'en_proceso'
                self.l10n_pe_biller_message = _('Firmada y enviada; SUNAT aún no devolvió el CDR.')
                # Re-consulta inmediata (best-effort): SUNAT suele resolver el ticket GRE en
                # segundos; así el usuario recibe el CDR sin esperar los 10 min del cron. Si
                # falla, el cron reintenta. No debe romper la emisión ya realizada.
                if self.num_ticket:
                    try:
                        self.l10n_pe_ne_consultar_ticket()
                    except Exception as exc:  # noqa: BLE001
                        _logger.info('GRE %s: re-consulta inmediata falló (reintenta el cron): %s',
                                     self.name, exc)
        else:
            self.estado = 'rechazado' if resp.status_code == 400 else 'error'
            self.l10n_pe_biller_message = ('HTTP %s: %s' % (resp.status_code, body))[:2000]
        return self._l10n_pe_ne_guia_dict()

    def l10n_pe_ne_consultar_ticket(self):
        """Re-consulta al biller el ticket de una guía en_proceso
        (GET /generator/guia/ticket/{numTicket}) y aplica el CDR si ya está."""
        self.ensure_one()
        if self.estado != 'en_proceso' or not self.num_ticket:
            raise UserError(_('Solo se puede consultar una guía en proceso con ticket de SUNAT.'))
        icp = self.env['ir.config_parameter'].sudo()
        base = icp.get_param('l10n_pe_ne_biller.url', 'http://localhost:8090').rstrip('/')
        headers = {'X-Api-Key': self.company_id.sudo().l10n_pe_ne_api_key or ''}
        try:
            resp = requests.get('%s/generator/guia/ticket/%s' % (base, self.num_ticket),
                                headers=headers, timeout=(5, 60))
        except requests.RequestException as exc:
            raise UserError(_('Error de conexión con el facturador: %s') % exc)
        cdr_b64 = resp.headers.get('X-Sunat-Cdr')
        if resp.status_code == 200 and cdr_b64:
            self._l10n_pe_ne_aplicar_cdr(cdr_b64)
        elif resp.status_code != 200:
            self.l10n_pe_biller_message = ('HTTP %s: %s' % (resp.status_code, resp.text or ''))[:2000]
        return self._l10n_pe_ne_guia_dict()

    # Tope de re-consultas: a 10 min/cron, ~288 intentos ≈ 2 días. SUNAT resuelve un ticket
    # GRE en minutos; pasado eso es un ticket muerto que no debe seguir gastando la ventana.
    _MAX_CONSULTA_INTENTOS = 288

    @api.model
    def _cron_consultar_en_proceso(self):
        """Cron: re-consulta las guías en_proceso con ticket y aplica el CDR si ya está.
        Acotado (limit + tope de intentos + commit por guía) para no exceder limit_time_real ni
        perder el progreso ante un SIGKILL — mismo patrón que los crons de account.move."""
        guias = self.search([
            ('estado', '=', 'en_proceso'),
            ('num_ticket', '!=', False),
            ('consulta_intentos', '<', self._MAX_CONSULTA_INTENTOS),
        ], limit=50)
        # Odoo prohíbe cr.commit() dentro de un test; en producción sí commiteamos por guía.
        test_mode = config['test_enable']
        for g in guias:
            try:
                g.l10n_pe_ne_consultar_ticket()
                # Solo cuenta el intento cuando HUBO respuesta del biller (la consulta no lanzó):
                # un corte de red transitorio no debe gastar el tope y sacar la guía del cron —
                # esos días de outage no son "tickets muertos". Un HTTP no-200 sí cuenta (no lanza).
                g.consulta_intentos = (g.consulta_intentos or 0) + 1
            except Exception as exc:  # noqa: BLE001 — reintenta al próximo cron (sin gastar intento)
                _logger.warning('GRE %s: re-consulta falló: %s', g.name, exc)
            # Commit por guía: una re-consulta lenta no debe descartar el progreso previo.
            if not test_mode:
                self.env.cr.commit()

    # ------------------------------------------------------- serialización
    def _l10n_pe_ne_guia_dict(self):
        self.ensure_one()
        resp = {
            'id': self.id,
            'numero': self.name,
            'destinatario': self.partner_id.name or '',
            'destinatarioDoc': self.partner_id.vat or '',
            'fecha': self.fecha_emision.strftime('%Y-%m-%d') if self.fecha_emision else '',
            'estado': self.estado,
            'motivo': dict(MOTIVOS_TRASLADO).get(self.motivo_traslado, self.motivo_traslado),
            'modalidad': dict(MODALIDADES_TRASLADO).get(self.modalidad_traslado, ''),
            'items': len(self.line_ids),
            'mensaje': self.l10n_pe_biller_message or '',
        }
        _logger.info("----------------------- Guia %s ---------------------", self.name)
        _logger.info('guia_dict: %s', resp)
        _logger.info('estado: %s', self.estado)
        return resp

    def l10n_pe_ne_guia_detalle(self):
        """Detalle completo para el formulario/PDF: cabecera + bienes."""
        self.ensure_one()
        c = self
        return {
            **self._l10n_pe_ne_guia_dict(),
            'serie': c.serie, 'correlativo': c.correlativo or '',
            'tipoGre': c.tipo_gre,
            'destinatarioId': c.partner_id.id,
            # Remitente y TUC solo tienen sentido en la GRE transportista (tipo 31); en la
            # remitente quedan vacíos. El id acompaña al nombre para el round-trip de la SPA.
            'remitenteId': c.remitente_id.id if c.remitente_id else None,
            'remitente': c.remitente_id.name if c.remitente_id else '',
            'remitenteDoc': c.remitente_id.vat if c.remitente_id else '',
            'numTuc': c.num_tuc or '',
            'horaEmision': c.hora_emision or '',
            'obsGuia': c.obs_guia or '',
            'motivoTraslado': c.motivo_traslado, 'desMotivoTraslado': c.des_motivo_traslado or '',
            'modalidadTraslado': c.modalidad_traslado,
            'pesoBruto': c.peso_bruto, 'uniMedidaPeso': c.uni_medida_peso, 'numBultos': c.num_bultos,
            'fechaInicioTraslado': c.fecha_inicio_traslado.strftime('%Y-%m-%d') if c.fecha_inicio_traslado else '',
            'ubigeoPartida': c.ubigeo_partida or '', 'dirPartida': c.dir_partida or '',
            'ubigeoLlegada': c.ubigeo_llegada or '', 'dirLlegada': c.dir_llegada or '',
            'damNumero': c.dam_numero or '', 'damTipo': c.dam_tipo or '50',
            'puertoCodigo': c.puerto_codigo or '', 'puertoTipo': c.puerto_tipo or '',
            'puertoNombre': (c._l10n_pe_ne_puerto_entry() or (None, ''))[1],
            'numContenedor': c.num_contenedor or '', 'numPrecinto': c.num_precinto or '',
            'numContenedor2': c.num_contenedor2 or '', 'numPrecinto2': c.num_precinto2 or '',
            'numPlaca': c.num_placa or '',
            'conductorTipoDoc': c.conductor_tipo_doc or '1', 'conductorNumDoc': c.conductor_num_doc or '',
            'conductorNombres': c.conductor_nombres or '', 'conductorApellidos': c.conductor_apellidos or '',
            'conductorLicencia': c.conductor_licencia or '',
            'transportistaId': c.transportista_id.id if c.transportista_id else None,
            'transportista': c.transportista_id.name if c.transportista_id else '',
            'numRegMtc': c.num_reg_mtc or '',
            'proveedorId': c.proveedor_id.id if c.proveedor_id else None,
            'proveedor': c.proveedor_id.name if c.proveedor_id else '',
            'comprobanteId': c.comprobante_id.id if c.comprobante_id else None,
            'comprobanteIds': c.comprobante_ids.ids,
            # Sibling con número (serie-correlativo) para que el front no tenga que resolver
            # cada id de comprobanteIds con una consulta aparte (mata el N+1 de la SPA).
            # comprobanteIds se mantiene tal cual (frozen) — esto es un agregado, no un
            # reemplazo.
            'comprobantes': [{'id': m.id, 'numero': c._l10n_pe_ne_comprobante_numero(m)}
                             for m in (c.comprobante_ids or c.comprobante_id)],
            'indTransbordo': c.ind_transbordo, 'indM1L': c.ind_m1l,
            'indRetornoEnvases': c.ind_retorno_envases, 'indRetornoVacio': c.ind_retorno_vacio,
            'fechaEntregaTransportista': c.fecha_entrega_transportista.strftime('%Y-%m-%d')
                if c.fecha_entrega_transportista else '',
            'codEstabPartida': c.cod_estab_partida or '', 'codEstabLlegada': c.cod_estab_llegada or '',
            'entAutorizacionCarga': c.ent_autorizacion_carga or '',
            'numAutorizacionCarga': c.num_autorizacion_carga or '',
            'vehiculos': [{
                'id': v.id, 'placa': v.placa, 'entAutorizacion': v.ent_autorizacion or '',
                'numAutorizacion': v.num_autorizacion or '', 'principal': v.principal,
            } for v in c.vehiculo_ids],
            'conductores': [{
                'id': d.id, 'tipoDoc': d.tipo_doc, 'numDoc': d.num_doc, 'nombres': d.nombres,
                'apellidos': d.apellidos, 'licencia': d.licencia, 'principal': d.principal,
            } for d in c.conductor_ids],
            'bienes': [{
                'descripcion': l.descripcion, 'cantidad': l.cantidad, 'unidad': l.unidad or 'NIU',
                'productId': l.product_id.id or None, 'codigo': l.product_id.default_code or '',
                # Bien normalizado: SPA muestra el código SUNAT (cat.25) y el GTIN del producto.
                'codProductoSUNAT': (l.product_id.l10n_pe_ne_cod_producto_sunat or '') if l.product_id else '',
                'gtin': (l.product_id.barcode or '') if l.product_id else '',
            } for l in c.line_ids],
        }

    def l10n_pe_ne_get_files(self, kind=None):
        """Devuelve {xml, cdr, pdf} en base64, para que el controller los sirva. El PDF (QWeb) se
        renderiza a demanda; xml/cdr salen de los adjuntos guardados al emitir."""
        self.ensure_one()
        def b64(att):
            v = att.datas
            return v.decode('ascii') if isinstance(v, (bytes, bytearray)) else (v or '')
        out = {}
        if self.l10n_pe_biller_xml:
            out['xml'] = b64(self.l10n_pe_biller_xml)
        if self.l10n_pe_biller_cdr:
            out['cdr'] = b64(self.l10n_pe_biller_cdr)
        if kind in (None, 'pdf'):
            try:
                pdf, _ct = self.env['ir.actions.report']._render_qweb_pdf(
                    'l10n_pe_ne_biller.action_report_guia', res_ids=self.ids)
                out['pdf'] = base64.b64encode(pdf).decode()
            except Exception:  # noqa: BLE001
                if kind == 'pdf':
                    raise
        return out

    # --------------------------------------------------- representación impresa
    def l10n_pe_ne_qr_data(self):
        """Contenido del QR de la representación impresa: la URL que SUNAT devolvió en el
        CDR aceptado. Sin CDR aceptado no hay QR (la guía aún no sustenta el traslado)."""
        self.ensure_one()
        return self.l10n_pe_ne_qr_url or ''

    def l10n_pe_ne_qr_datauri(self):
        """QR como data-URI PNG para la representación impresa. '' si no se puede generar."""
        self.ensure_one()
        if not self.l10n_pe_ne_qr_data():
            return ''
        try:
            png = self.env['ir.actions.report'].barcode('QR', self.l10n_pe_ne_qr_data(), width=220, height=220)
            return 'data:image/png;base64,' + base64.b64encode(png).decode()
        except Exception:  # noqa: BLE001
            return ''

    # ------------------------------------------------------------- API React
    @api.model
    def l10n_pe_ne_list_guias(self, query=None, limit=100, offset=None):
        """Lista de guías (para la UI). Paginación opt-in: con `offset` devuelve {items,total}."""
        domain = [('company_id', '=', self.env.company.id)]
        if query:
            q = query.strip()
            domain += ['|', ('name', 'ilike', q), ('partner_id.name', 'ilike', q)]
        recs = self.search(domain, limit=limit, offset=offset or 0)
        items = [g._l10n_pe_ne_guia_dict() for g in recs]
        if offset is None:
            return items
        return {'items': items, 'total': self.search_count(domain)}

    def _l10n_pe_ne_resolve_destinatario(self, payload):
        partner = False
        if payload.get('destinatarioId'):
            partner = self.env['res.partner'].browse(int(payload['destinatarioId'])).exists()
        if not partner and payload.get('destinatario'):
            partner = self.env['account.move']._l10n_pe_ne_quick_partner(payload['destinatario'])
        if not partner:
            raise UserError(_('Indica el destinatario de la guía.'))
        return partner

    def _l10n_pe_ne_build_guia_lines(self, items):
        vals = []
        for it in (items or []):
            desc = (it.get('descripcion') or '').strip()
            prod = False
            if it.get('productId'):
                prod = self.env['product.product'].browse(int(it['productId'])).exists()
                if prod and not desc:
                    desc = prod.display_name
            # Una guía traslada bienes, no servicios (main introdujo bien/servicio en el
            # producto — ver `_l10n_pe_ne_tipo_producto` en account_move_biller.py). Se
            # valida acá, al capturar la línea, para no descubrirlo recién al emitir.
            if prod and prod.type == 'service':
                raise UserError(_('"%s" es un servicio — una guía de remisión solo traslada bienes.')
                                % prod.display_name)
            if not desc:
                raise UserError(_('Cada bien necesita una descripción (o un producto).'))
            vals.append((0, 0, {
                'product_id': prod.id if prod else False,
                'descripcion': desc,
                'cantidad': float(it.get('cantidad') or 1),
                'unidad': it.get('unidad') or (prod.l10n_pe_ne_unit_code if prod else '') or 'NIU',
            }))
        return vals

    def _l10n_pe_ne_guia_header_vals(self, payload):
        """Traduce la cabecera del payload de React a vals de escritura (sin partner ni líneas)."""
        vals = {}
        strmap = {
            'serie': 'serie', 'obsGuia': 'obs_guia', 'horaEmision': 'hora_emision',
            'tipoGre': 'tipo_gre', 'numTuc': 'num_tuc',
            'motivoTraslado': 'motivo_traslado', 'desMotivoTraslado': 'des_motivo_traslado',
            'modalidadTraslado': 'modalidad_traslado', 'uniMedidaPeso': 'uni_medida_peso',
            'numPlaca': 'num_placa', 'conductorTipoDoc': 'conductor_tipo_doc',
            'conductorNumDoc': 'conductor_num_doc', 'conductorNombres': 'conductor_nombres',
            'conductorApellidos': 'conductor_apellidos', 'conductorLicencia': 'conductor_licencia',
            'numRegMtc': 'num_reg_mtc',
            'ubigeoPartida': 'ubigeo_partida', 'dirPartida': 'dir_partida',
            'ubigeoLlegada': 'ubigeo_llegada', 'dirLlegada': 'dir_llegada',
            'codEstabPartida': 'cod_estab_partida', 'codEstabLlegada': 'cod_estab_llegada',
            'damNumero': 'dam_numero', 'damTipo': 'dam_tipo',
            'puertoCodigo': 'puerto_codigo', 'puertoTipo': 'puerto_tipo',
            'numContenedor': 'num_contenedor', 'numPrecinto': 'num_precinto',
            'numContenedor2': 'num_contenedor2', 'numPrecinto2': 'num_precinto2',
            'entAutorizacionCarga': 'ent_autorizacion_carga',
            'numAutorizacionCarga': 'num_autorizacion_carga',
        }
        for k, f in strmap.items():
            if k in payload:
                vals[f] = payload.get(k) or False
        boolmap = {
            'indTransbordo': 'ind_transbordo', 'indM1L': 'ind_m1l',
            'indRetornoEnvases': 'ind_retorno_envases', 'indRetornoVacio': 'ind_retorno_vacio',
        }
        for k, f in boolmap.items():
            if k in payload:
                vals[f] = bool(payload.get(k))
        if payload.get('pesoBruto') is not None:
            vals['peso_bruto'] = float(payload['pesoBruto'] or 0)
        if payload.get('numBultos') is not None:
            vals['num_bultos'] = int(payload['numBultos'] or 1)
        if payload.get('fecha'):
            vals['fecha_emision'] = payload['fecha']
        if payload.get('fechaInicioTraslado'):
            vals['fecha_inicio_traslado'] = payload['fechaInicioTraslado']
        if 'fechaEntregaTransportista' in payload:
            # Partial-PUT: la SPA en modalidad privada manda '' para BORRAR una fecha ya
            # guardada (antes solo se seteaba cuando venía truthy, y no había forma de
            # limpiarla). Clave ausente = no tocar (el contrato del resto del método).
            vals['fecha_entrega_transportista'] = payload.get('fechaEntregaTransportista') or False
        if 'transportistaId' in payload:
            vals['transportista_id'] = int(payload['transportistaId']) if payload.get('transportistaId') else False
        if 'proveedorId' in payload:
            vals['proveedor_id'] = int(payload['proveedorId']) if payload.get('proveedorId') else False
        if 'remitenteId' in payload:
            vals['remitente_id'] = int(payload['remitenteId']) if payload.get('remitenteId') else False
        if 'vehiculos' in payload:
            # placa es required= en el modelo línea, pero eso solo garantiza NOT NULL en
            # SQL: un Char required acepta '' sin quejarse. Sin este guard, un vehículo sin
            # placa se cuela silenciosamente (F2) y llega vacío al biller.
            veh_vals = []
            for v in (payload.get('vehiculos') or []):
                placa = (v.get('placa') or '').strip()
                if not placa:
                    raise UserError(_('Cada vehículo necesita la placa.'))
                veh_vals.append((0, 0, {
                    'placa': placa,
                    'ent_autorizacion': v.get('entAutorizacion') or False,
                    'num_autorizacion': v.get('numAutorizacion') or False,
                    'principal': bool(v.get('principal')),
                }))
            vals['vehiculo_ids'] = [(5, 0, 0)] + veh_vals
        if 'conductores' in payload:
            cond_vals = []
            for c in (payload.get('conductores') or []):
                num_doc = (c.get('numDoc') or '').strip()
                nombres = (c.get('nombres') or '').strip()
                apellidos = (c.get('apellidos') or '').strip()
                licencia = (c.get('licencia') or '').strip()
                faltantes = [etiqueta for etiqueta, valor in (
                    (_('N° de documento'), num_doc), (_('nombres'), nombres),
                    (_('apellidos'), apellidos), (_('licencia de conducir'), licencia),
                ) if not valor]
                if faltantes:
                    raise UserError(_('Cada conductor necesita %s.') % ', '.join(faltantes))
                cond_vals.append((0, 0, {
                    'tipo_doc': c.get('tipoDoc') or '1',
                    'num_doc': num_doc,
                    'nombres': nombres,
                    'apellidos': apellidos,
                    'licencia': licencia,
                    'principal': bool(c.get('principal')),
                }))
            vals['conductor_ids'] = [(5, 0, 0)] + cond_vals
        if 'comprobanteIds' in payload:
            ids = [int(x) for x in (payload.get('comprobanteIds') or [])]
            vals['comprobante_ids'] = [(6, 0, ids)]
            # Espejo en el legado comprobante_id (primer documento): guías/SPA viejas
            # que solo leen comprobanteId siguen funcionando.
            vals['comprobante_id'] = ids[0] if ids else False
        elif 'comprobanteId' in payload:
            vals['comprobante_id'] = int(payload['comprobanteId']) if payload.get('comprobanteId') else False
        return vals

    @api.model
    def l10n_pe_ne_quick_guia(self, payload):
        """Crea una guía (borrador) desde el payload de React."""
        payload = payload or {}
        partner = self._l10n_pe_ne_resolve_destinatario(payload)
        lines = self._l10n_pe_ne_build_guia_lines(payload.get('items') or payload.get('bienes'))
        if not lines:
            raise UserError(_('La guía necesita al menos un bien.'))
        vals = self._l10n_pe_ne_guia_header_vals(payload)
        vals.update({'company_id': self.env.company.id, 'partner_id': partner.id, 'line_ids': lines})
        g = self.create(vals)
        return g._l10n_pe_ne_guia_dict()

    @api.model
    def l10n_pe_ne_update_guia(self, payload):
        """Reemplaza cabecera + bienes de una guía en borrador."""
        payload = payload or {}
        g = self.browse(int(payload.get('id') or 0)).exists()
        if not g:
            raise UserError(_('Guía no encontrada.'))
        if g.estado not in ESTADOS_EMITIBLES:
            raise UserError(_('Solo se puede editar una guía en borrador.'))
        vals = g._l10n_pe_ne_guia_header_vals(payload)
        # La serie es inmutable una vez numerada la guía (el correlativo depende de
        # ella): honrarla aquí emitiría p.ej. T002-1 duplicando el de otra guía.
        vals.pop('serie', None)
        if payload.get('destinatarioId') or payload.get('destinatario'):
            vals['partner_id'] = g._l10n_pe_ne_resolve_destinatario(payload).id
        if payload.get('items') is not None or payload.get('bienes') is not None:
            vals['line_ids'] = [(5, 0, 0)] + g._l10n_pe_ne_build_guia_lines(
                payload.get('items') or payload.get('bienes'))
        g.write(vals)
        return g._l10n_pe_ne_guia_dict()

    @api.model
    def l10n_pe_ne_guia_prefill(self, move_id):
        """Datos para precargar una guía desde un comprobante (factura/boleta): destinatario +
        bienes (líneas del comprobante) + el comprobante como documento relacionado. NO crea nada;
        el front abre el formulario de guía con esto y el usuario completa el traslado/transporte."""
        move = self.env['account.move'].browse(int(move_id or 0)).exists()
        if not move:
            raise UserError(_('Comprobante no encontrado.'))
        p = move.partner_id
        bienes = [{
            'descripcion': ln.name or (ln.product_id.display_name or ''),
            'cantidad': ln.quantity or 1.0,
            'unidad': move._l10n_pe_unit_code(ln),
            'productId': ln.product_id.id or None,
            'codigo': ln.product_id.default_code or '',
        } for ln in move._l10n_pe_product_lines()]
        part = move.company_id.partner_id
        return {
            'comprobanteId': move.id,
            'comprobanteNumero': '%s-%s' % (move.l10n_pe_ne_serie_emit or '', move.l10n_pe_ne_corr_emit or ''),
            'destinatario': {'id': p.id, 'razonSocial': p.name or '', 'numDoc': p.vat or ''},
            'bienes': bienes,
            'ubigeoPartida': (part.l10n_pe_district.code or '') if part.l10n_pe_district else '',
            'dirPartida': part.street or '',
            'dirLlegada': p.street or '',
        }

    @api.model
    def l10n_pe_ne_delete_guia(self, rec_id):
        g = self.browse(int(rec_id or 0)).exists()
        if g:
            if g.estado == 'enviado':
                raise UserError(_('No se puede eliminar una guía ya aceptada por SUNAT.'))
            g.unlink()
        return {'ok': True, 'modo': 'eliminado'}


class L10nPeNeGuiaRemisionLine(models.Model):
    _name = 'l10n_pe_ne.guia_remision.line'
    _description = 'Bien de la guía de remisión'
    _order = 'id'

    guia_id = fields.Many2one('l10n_pe_ne.guia_remision', string='Guía',
                              required=True, ondelete='cascade', index=True)
    product_id = fields.Many2one('product.product', string='Producto')
    descripcion = fields.Char(string='Descripción', required=True)
    cantidad = fields.Float(string='Cantidad', default=1.0)
    unidad = fields.Char(string='Unidad (cat. 03)', default='NIU')
    company_id = fields.Many2one(related='guia_id.company_id', store=True, index=True)


class L10nPeNeGuiaRemisionVehiculo(models.Model):
    _name = 'l10n_pe_ne.guia_remision.vehiculo'
    _description = 'Vehículo de la guía de remisión'
    _order = 'principal desc, id'

    guia_id = fields.Many2one('l10n_pe_ne.guia_remision', string='Guía',
                              required=True, ondelete='cascade', index=True)
    placa = fields.Char(string='Placa', required=True)
    ent_autorizacion = fields.Char(string='Entidad de la autorización (cat. D37)')
    num_autorizacion = fields.Char(string='N° de autorización')
    principal = fields.Boolean(string='Principal')
    company_id = fields.Many2one(related='guia_id.company_id', store=True, index=True)


class L10nPeNeGuiaRemisionConductor(models.Model):
    _name = 'l10n_pe_ne.guia_remision.conductor'
    _description = 'Conductor de la guía de remisión'
    _order = 'principal desc, id'

    guia_id = fields.Many2one('l10n_pe_ne.guia_remision', string='Guía',
                              required=True, ondelete='cascade', index=True)
    tipo_doc = fields.Selection([('1', 'DNI'), ('4', 'Carné ext.'), ('7', 'Pasaporte')],
                                string='Tipo doc.', default='1', required=True)
    num_doc = fields.Char(string='N° documento', required=True)
    nombres = fields.Char(string='Nombres', required=True)
    apellidos = fields.Char(string='Apellidos', required=True)
    licencia = fields.Char(string='Licencia de conducir', required=True)
    principal = fields.Boolean(string='Principal')
    company_id = fields.Many2one(related='guia_id.company_id', store=True, index=True)

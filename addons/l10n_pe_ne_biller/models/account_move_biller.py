import base64
import io
import json
import logging
import re
import zipfile
from datetime import timedelta

import pytz
import requests

try:  # SQS para el modo asíncrono (l10n_pe_ne_biller.async_enabled); si falta
    import boto3  # boto3, el modo síncrono sigue funcionando igual.
except ImportError:  # pragma: no cover
    boto3 = None

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError
from odoo.tools import float_round, html2plaintext

from ..tools.amount_to_words import leyenda_monto

_logger = logging.getLogger(__name__)

# Cache de clientes boto3 por (service, region) — ver _l10n_pe_boto_client.
# Guarda (módulo_boto3, cliente) para invalidarse solo si boto3 fue parcheado.
_BOTO_CLIENTS = {}

# Descuento global que NO afecta la base imponible del IGV (AllowanceChargeReasonCode, cat. 53).
# CONFIRMADO contra el validador SUNAT (ValidaExprRegFactura-2.0.1.xsl) y beta (spike 2026-07-21):
# el código "03" es el que el validador cuenta en `descuentosGlobalesNOAfectaBI` (línea 335) y NO
# resta de la base del IGV; el "02" cae en `MontoDescuentoAfectoBI` (SÍ afecta la base) → daba
# error 3291 (esperaba el IGV sobre la base ya descontada). Con "03" el IGV queda sobre el precio
# lleno y baja solo el MtoImpVenta.
DESC_GLOBAL_NO_AFECTA_COD = "03"

# Catálogo SUNAT de descripciones de motivo para Nota de Débito (08).
ND_MOTIVO_DESC = {
    "01": "Intereses por mora",
    "02": "Aumento en el valor",
    "03": "Penalidades/otros conceptos",
    "11": "Ajustes de operaciones de exportación",
    "12": "Ajustes afectos al IVAP",
}

# Mapeo del código de tributo de la tax de Odoo (`account.tax.l10n_pe_edi_tax_code`, cat. 05 de
# SUNAT, provisto por la localización community l10n_pe) a la tupla que exige el contrato SFS por
# línea/tributo:  (tipAfeIGV cat.07, codTriIGV cat.05, nomTributo, codTipTributo UN/ECE 5153,
# codCatTributo UN/ECE 5305). Valores tomados de los mocks ya aceptados E2E por el microservicio.
TAX_CODE_MAP = {
    "1000": ("10", "1000", "IGV", "VAT", "S"),  # Gravado - operación onerosa
    "1016": ("17", "1016", "IVAP", "VAT", "S"),  # IVAP (arroz pilado)
    "9997": ("20", "9997", "EXO", "VAT", "E"),  # Exonerado
    "9998": ("30", "9998", "INA", "FRE", "O"),  # Inafecto
    "9995": ("40", "9995", "EXP", "FRE", "G"),  # Exportación
    "9996": (
        "11",
        "9996",
        "GRA",
        "FRE",
        "Z",
    ),  # Gratuita (retiro/transferencia gratuita)
}
DEFAULT_TAX_CODE = "1000"  # Sin tax reconocida -> gravado IGV (caso más común).

# Tasas OFICIALES de detracción (SPOT) por código de bien/servicio (cat. 54). Guard autoritativo
# del backend: el front (detracciones.ts) las valida al capturar, pero esto cubre masiva/API/bypass.
# Ej.: contratos de CONSTRUCCIÓN = 030 (4%), NO 037 (12% "demás servicios"). Código 028 (transporte
# de pasajeros) no lleva tasa fija → se omite. Regulatorio: cambia por resolución SUNAT (mantener en
# sync con el front). Un código fuera de esta tabla no dispara el aviso.
DETRACCION_TASAS = {
    # Servicios (Anexo 3)
    "012": 12.0, "019": 10.0, "020": 12.0, "021": 10.0, "022": 12.0, "024": 10.0,
    "025": 10.0, "026": 10.0, "027": 4.0, "030": 4.0, "037": 12.0, "099": 8.0,
    # Bienes (Anexo 2)
    "001": 10.0, "002": 4.0, "003": 4.0, "004": 4.0, "005": 4.0, "007": 10.0,
    "008": 4.0, "009": 10.0, "010": 15.0, "011": 10.0, "014": 4.0, "016": 10.0,
    "017": 4.0, "023": 4.0, "031": 10.0, "032": 10.0, "034": 10.0, "035": 1.0,
    "036": 1.0, "039": 10.0, "040": 4.0, "041": 15.0,
}

# Código de unidad de medida de SUNAT (cat. 03 / UN-ECE Rec. 20) por XMLID de la unidad estándar
# de Odoo. Mapeo replicado de l10n_pe_edi (enterprise). Se resuelve en runtime porque las UoM base
# son `noupdate` y un data file de otro módulo no las actualiza; para unidades personalizadas, el
# usuario fija el override en `uom.uom.l10n_pe_ne_unit_code`.
UOM_CODE_BY_XMLID = {
    "uom.product_uom_unit": "NIU",
    "uom.product_uom_dozen": "DZN",
    "uom.product_uom_kgm": "KGM",
    "uom.product_uom_gram": "GRM",
    "uom.product_uom_day": "DAY",
    "uom.product_uom_hour": "HUR",
    "uom.product_uom_ton": "TNE",
    "uom.product_uom_meter": "MTR",
    "uom.product_uom_km": "KTM",
    "uom.product_uom_cm": "CMT",
    "uom.product_uom_litre": "LTR",
    "uom.product_uom_lb": "LBR",
    "uom.product_uom_oz": "ONZ",
    "uom.product_uom_inch": "INH",
    "uom.product_uom_foot": "FOT",
    "uom.product_uom_mile": "M52",
    "uom.product_uom_floz": "OZ",
    "uom.product_uom_qt": "QTI",
    "uom.product_uom_gal": "GLL",
}
DEFAULT_UNIT_CODE = "NIU"

# Importación de productos por Excel: mapeo tolerante de TEXTO en español (o el propio código
# cat.03) → código SUNAT cat.03. Clave = texto normalizado (minúsculas, sin tildes). Espejo del
# catálogo del front (lib/unidades.ts) más sinónimos comunes de ferretería/bodega.
UNIDAD_IMPORT = {
    "unidad": "NIU", "unidades": "NIU", "und": "NIU", "unid": "NIU", "niu": "NIU", "u": "NIU",
    "servicio": "ZZ", "servicios": "ZZ", "serv": "ZZ", "zz": "ZZ",
    "kilogramo": "KGM", "kilogramos": "KGM", "kilo": "KGM", "kilos": "KGM", "kg": "KGM", "kgm": "KGM",
    "gramo": "GRM", "gramos": "GRM", "gr": "GRM", "grm": "GRM",
    "libra": "LBR", "libras": "LBR", "lb": "LBR", "lbr": "LBR",
    "tonelada": "TNE", "toneladas": "TNE", "tonelada metrica": "TNE", "ton": "TNE", "tne": "TNE",
    "litro": "LTR", "litros": "LTR", "lt": "LTR", "ltr": "LTR",
    "galon": "GLL", "galones": "GLL", "gln": "GLL", "gll": "GLL",
    "barril": "BLL", "barriles": "BLL", "bll": "BLL",
    "lata": "CA", "latas": "CA", "ca": "CA",
    "caja": "BX", "cajas": "BX", "bx": "BX",
    "millar": "MLL", "millares": "MLL", "mll": "MLL",
    "metro": "MTR", "metros": "MTR", "mt": "MTR", "mtr": "MTR", "m": "MTR",
    "centimetro": "CMT", "centimetros": "CMT", "cm": "CMT", "cmt": "CMT",
    "metro cuadrado": "MTK", "m2": "MTK", "mtk": "MTK",
    "metro cubico": "MTQ", "m3": "MTQ", "mtq": "MTQ",
    "dia": "DAY", "dias": "DAY", "day": "DAY",
    "hora": "HUR", "horas": "HUR", "hr": "HUR", "hur": "HUR",
    "juego": "SET", "juegos": "SET", "set": "SET",
    # Docena = DZN ("dozen" en cat.03), igual que el front (QA-021). DPC ("dozen piece") también
    # es válido en cat.03 y se respeta si el usuario lo teclea explícito.
    "docena": "DZN", "docenas": "DZN", "dzn": "DZN", "dpc": "DPC",
    "onza": "ONZ", "onzas": "ONZ", "onz": "ONZ",
}
# Afectación IGV: texto (cat.07 humano) → código cat.07 que espera el producto.
AFECT_IMPORT = {
    "gravado": "1000", "gravada": "1000",
    "exonerado": "9997", "exonerada": "9997",
    "inafecto": "9998", "inafecta": "9998",
    "exportacion": "9995",
    "gratuito": "9996", "gratuita": "9996",
}
# Bien/servicio (tipo Odoo → stock) desde el Excel. Vacío = deducir de la unidad (ZZ → servicio).
TIPO_IMPORT = {
    "bien": "bien", "bienes": "bien", "producto": "bien", "b": "bien",
    "servicio": "servicio", "servicios": "servicio", "serv": "servicio", "s": "servicio",
}
# Códigos cat.03 válidos (para aceptar el código directo en el Excel, ej. "KGM").
_UNIDAD_CODES = set(UNIDAD_IMPORT.values())


def _percep_float(v):
    """float() de percepTasa que no revienta con un 500 críptico: tolera coma decimal (igual
    que el import masivo) y, si no es numérico, da un UserError legible en vez de un
    ValueError sin traducir. Vacío/None/False → 0.0 (limpia el campo, mismo criterio de
    siempre)."""
    if v in (None, "", False):
        return 0.0
    try:
        return float(str(v).replace(",", "."))
    except (TypeError, ValueError):
        raise UserError(_("La percepción sugerida debe ser un número (ej. 2 o 1.5)."))


class AccountMoveLine(models.Model):
    _inherit = "account.move.line"

    # Cantidad con 3 decimales (SUNAT admite hasta 10 en ctdUnidadItem). Por defecto la precisión
    # de UoM de Odoo es 2 y truncaba la venta al peso de balanza (18.375 kg -> 18.38). Ver QA-020.
    quantity = fields.Float(digits=(16, 3))

    # Overrides SUNAT por línea para el flujo rápido (sin depender de la UoM/producto de Odoo,
    # evitando el problema de categorías de unidad de medida).
    l10n_pe_ne_unit_code = fields.Char(
        string="Unidad SUNAT (cat.03)",
        copy=False,
        help="Código de unidad de medida SUNAT de la línea (ej. NIU, KGM, ZZ). "
        "Si está vacío se deriva de la unidad de medida del producto.",
    )
    l10n_pe_ne_fraccionado = fields.Boolean(
        string="Vendido fraccionado", copy=False,
        help="Farma: esta línea se vende por la sub-unidad del producto (fraccionamiento). La "
        "cantidad va en sub-unidades y el stock descuenta cantidad/unidades_por_empaque del empaque.")
    l10n_pe_ne_cod_producto_sunat = fields.Char(
        string="Cód. producto SUNAT (cat.25)",
        copy=False,
        help="Código de producto SUNAT (UNSPSC, catálogo 25) de la línea, si aplica.",
    )
    l10n_pe_ne_placa = fields.Char(
        string="Placa del vehículo (combustible)",
        copy=False,
        help="Número de placa del vehículo de ESTA línea (grifo/combustible). Va como "
        "cac:AdditionalItemProperty cat-55 código 7000 (Gastos Art. 37 Renta) en la línea. "
        "Cada línea de combustible puede tener su propia placa; las demás no la llevan.",
    )
    # Lote/serie de una línea de COMPRA. El lote entra con la mercadería, así que se captura
    # en la compra y viaja con la línea hasta que _l10n_pe_ne_mover_stock_compra crea el
    # movimiento. En la VENTA no se pide: Odoo reserva y asigna el lote solo, por su
    # estrategia de salida (lo que vence antes sale primero). Verificado.
    l10n_pe_ne_lote = fields.Char(
        string="Lote / serie",
        copy=False,
        help="Número de lote o serie de la mercadería que ingresa por esta línea.",
    )
    l10n_pe_ne_vence = fields.Date(
        string="Vencimiento del lote",
        copy=False,
        help="Fecha de vencimiento del lote que ingresa por esta línea.",
    )
    # Sub-tipo de operación gratuita (cat. 07 SUNAT). Solo aplica a líneas gratuitas (9996):
    # afina el genérico "11" al motivo real (retiro, bonificación, donación…). Vacío = 11.
    l10n_pe_ne_afectacion_gratuita = fields.Selection(
        [
            ("11", "Retiro por premio"),
            ("12", "Retiro por donación"),
            ("13", "Retiro de bienes"),
            ("14", "Retiro por publicidad"),
            ("15", "Bonificación"),
            ("16", "Retiro por entrega a trabajadores"),
        ],
        string="Tipo de operación gratuita",
        copy=False,
        help="Solo para líneas gratuitas: precisa el motivo (catálogo 07 de SUNAT). "
        "Si se deja vacío se usa 'Retiro por premio' (11).",
    )


class AccountMove(models.Model):
    _inherit = "account.move"

    def init(self):
        super().init()
        # Único parcial sobre las secuencias de comprobante: el pg_advisory_xact_lock de
        # _l10n_pe_ne_next_correlativo no basta bajo REPEATABLE READ (una transacción
        # concurrente puede no ver la secuencia recién commiteada y crear otra, con lo que dos
        # comprobantes se llevan el mismo número). Las guías ya tenían este índice; a la ruta
        # CPE le faltaba, y con dos sucursales emitiendo a la vez la carrera deja de ser teórica.
        # Si la base ya la sufrió y arrastra secuencias duplicadas, se LOGUEA y no se crea el
        # índice: tumbar el upgrade de un tenant por un dato viejo sería peor que la deuda, y la
        # limpieza es manual porque hay que decidir qué contador sobrevive.
        self.env.cr.execute("""
            SELECT code, company_id, count(*) FROM ir_sequence
             WHERE code LIKE 'l10n_pe.ne.cpe.%'
             GROUP BY code, company_id HAVING count(*) > 1
        """)
        duplicadas = self.env.cr.fetchall()
        if duplicadas:
            _logger.warning(
                "Secuencias de comprobante duplicadas (%s): no se crea "
                "ir_sequence_cpe_code_company_uniq. Revisa cuál contador conservar: %s",
                len(duplicadas), duplicadas[:10],
            )
            return
        self.env.cr.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS ir_sequence_cpe_code_company_uniq
            ON ir_sequence (code, company_id)
            WHERE code LIKE 'l10n_pe.ne.cpe.%'
        """)

    l10n_pe_biller_state = fields.Selection(
        selection=[
            ("por_enviar", "Por enviar"),
            ("en_proceso", "En proceso"),
            ("enviado", "Enviado"),
            ("anulado", "Anulado"),
            ("rechazado", "Rechazado"),
            ("error", "Error"),
        ],
        string="Estado Facturador",
        default="por_enviar",
        copy=False,
        tracking=True,  # cambios visibles en el chatter (que sí refresca en vivo)
    )
    l10n_pe_ne_baja_motivo = fields.Char(
        string="Motivo de baja",
        copy=False,
        help="Motivo de la comunicación de baja (RA) del comprobante ante SUNAT.",
    )
    l10n_pe_ne_baja_correlativo = fields.Char(
        string="Correlativo RA", copy=False, readonly=True
    )
    l10n_pe_ne_baja_fecha = fields.Date(string="Fecha RA", copy=False, readonly=True)
    l10n_pe_ne_baja_doc = fields.Char(
        string="Comunicación de baja",
        compute="_compute_l10n_pe_ne_baja_doc",
        store=True,
        help="Identificador de la comunicación de baja: RA-AAAAMMDD-correlativo.",
    )
    l10n_pe_ne_baja_cdr = fields.Many2one(
        "ir.attachment", string="CDR de baja", copy=False
    )
    # Identidad realmente emitida, congelada al enviar: la baja debe referenciar EXACTAMENTE lo emitido,
    # no recomputar del partner/nombre (que podrían cambiar y anular el comprobante equivocado).
    l10n_pe_ne_tipo_doc = fields.Char(
        string="Tipo doc. emitido", copy=False, readonly=True
    )
    # Liquidación de compra (comprobante tipo 04): la emite el COMPRADOR (con RUC) cuando le
    # compra a un productor/vendedor SIN RUC (agropecuario, recolección, artesanía). Es una
    # COMPRA (in_invoice): la mercadería ENTRA al stock y va al PLE de compras; pero además se
    # emite electrónicamente a SUNAT como SelfBilledInvoice. El flag marca ese cruce.
    l10n_pe_ne_liquidacion = fields.Boolean(
        string="Liquidación de compra", copy=False,
        help="Marca esta compra como Liquidación de compra electrónica (tipo 04): se emite a "
             "SUNAT como comprobante, con el proveedor sin RUC (DNI) como vendedor.")
    l10n_pe_ne_serie_emit = fields.Char(
        string="Serie emitida", copy=False, readonly=True
    )
    l10n_pe_ne_corr_emit = fields.Char(
        string="Correlativo emitido", copy=False, readonly=True
    )
    l10n_pe_serie = fields.Char(
        string="Serie",
        compute="_compute_l10n_pe_serie",
        store=True,
        readonly=False,
        copy=False,
        help="Serie del comprobante. Por defecto, la del diario (l10n_pe_ne_serie) con la "
        "letra ajustada a la familia del comprobante (F factura / B boleta).",
    )
    l10n_pe_correlativo = fields.Char(
        string="Correlativo",
        copy=False,
        help="Correlativo del comprobante. Si se deja vacío, se auto-incrementa del número del "
        "asiento (folio gestionado por Odoo por diario).",
    )

    l10n_pe_ne_detraccion = fields.Boolean(string="Sujeto a detracción", copy=False)
    l10n_pe_ne_detraccion_code = fields.Char(
        string="Código detracción",
        copy=False,
        help="Código del bien/servicio sujeto a detracción (catálogo 54 de SUNAT, ej. 037).",
    )
    l10n_pe_ne_detraccion_rate = fields.Float(
        string="% Detracción", digits=(5, 2), copy=False
    )
    l10n_pe_ne_detraccion_medio_pago = fields.Char(
        string="Medio de pago detracción",
        default="001",
        copy=False,
        help="Catálogo 59 (001 = depósito en cuenta del Banco de la Nación).",
    )
    l10n_pe_ne_detraccion_cuenta = fields.Char(
        string="Cuenta de detracción (Banco de la Nación)",
        copy=False,
        help="Cuenta del Banco de la Nación de ESTE comprobante. Si se deja vacía, "
        "se usa la cuenta de detracciones configurada en la empresa.",
    )
    l10n_pe_ne_percepcion = fields.Boolean(string="Aplica percepción", copy=False)
    l10n_pe_ne_percepcion_rate = fields.Float(
        string="% Percepción", digits=(5, 2), default=2.0, copy=False
    )
    l10n_pe_ne_anticipos = fields.Json(
        string="Anticipos regularizados",
        copy=False,
        help="Lista de anticipos (doc. A) que ESTA venta final regulariza/deduce. Cada elemento: "
        "{doc: serie-correlativo, monto: importe con IGV, tipo: '02'/'03' (cat. 12), "
        "origenId: id del anticipo local enlazado o null si se emitió por fuera}. SUNAT permite "
        "varios (pagos escalonados): se emiten como N documentos relacionados con numIdeAnticipo 1..N.",
    )
    l10n_pe_ne_es_anticipo = fields.Boolean(
        string="Es pago anticipado",
        copy=False,
        help="Marca que ESTE comprobante se emite por un pago anticipado (doc. A del ciclo de "
        "anticipos, equivale al 'Sí' de SEE-SOL). Es una venta interna normal (0101) por el monto "
        "anticipado; su descripción lleva 'PAGO ANTICIPADO' y queda disponible para regularizarse "
        "luego en la factura de venta final. No confundir con el anticipo aplicado, que descuenta "
        "un anticipo ya emitido (doc. B).",
    )
    l10n_pe_ne_anticipo_aplicado = fields.Monetary(
        string="Anticipo ya aplicado",
        compute="_compute_l10n_pe_ne_anticipo_saldo",
        help="Suma de las regularizaciones vivas que apuntan a este anticipo (doc. A).",
    )
    l10n_pe_ne_anticipo_saldo = fields.Monetary(
        string="Saldo del anticipo",
        compute="_compute_l10n_pe_ne_anticipo_saldo",
        help="Importe del anticipo aún disponible para regularizar = total − aplicado. Solo "
        "aplica a comprobantes marcados como pago anticipado (doc. A).",
    )
    l10n_pe_ne_desc_no_afecta = fields.Monetary(
        string="Descuento que no afecta el IGV",
        copy=False,
        help="Descuento (S/, CON IGV incluido en el sentido de que baja el total a pagar) que NO "
        "afecta la base imponible del IGV (cat. 53 'no afecta'): la gravada y el IGV se calculan "
        "sobre el precio lleno; este importe solo reduce el total (MtoImpVenta). Agrega el "
        "descuento por ítem 'no afecta' y el descuento global 'no afecta' del comprobante.",
    )

    @api.depends("l10n_pe_ne_es_anticipo", "amount_total")
    def _compute_l10n_pe_ne_anticipo_saldo(self):
        """Saldo por anticipo (doc. A) = total − regularizaciones vivas que lo aplican. Mismo
        criterio de 'vivo' que las NC (posteadas y no rechazadas/anuladas/con error; las en cola
        cuentan, para que dos regularizaciones simultáneas no consuman más que el total).
        El dato vive en una lista JSON (`l10n_pe_ne_anticipos`): no se puede agrupar por
        contenido JSON con `_read_group`, así que se busca las regularizaciones vivas y se
        agrega en Python sumando el `monto` de cada anticipo cuyo `origenId` matchee."""
        anticipos = self.filtered("l10n_pe_ne_es_anticipo")
        aplicado = {a.id: 0.0 for a in anticipos}
        if anticipos:
            # Nota de escala: search + loop Python sobre TODAS las regularizaciones vivas del
            # sistema; para volúmenes altos se podría filtrar por partner/JSONB, pero YAGNI.
            regs = self.env["account.move"].search([
                ("l10n_pe_ne_anticipos", "!=", False),
                ("state", "=", "posted"),
                ("l10n_pe_biller_state", "not in", ("rechazado", "error", "anulado")),
            ])
            for reg in regs:
                for a in reg._l10n_pe_ne_anticipos_list():
                    oid = a["origenId"]
                    if oid in aplicado:
                        aplicado[oid] += a["monto"]
        for move in self:
            ap = round(aplicado.get(move.id, 0.0), 2) if move.l10n_pe_ne_es_anticipo else 0.0
            move.l10n_pe_ne_anticipo_aplicado = ap
            move.l10n_pe_ne_anticipo_saldo = (
                round(move.amount_total - ap, 2) if move.l10n_pe_ne_es_anticipo else 0.0
            )

    def l10n_pe_ne_anticipos_pendientes(self, ruc=None, partner_id=None, moneda=None):
        """Anticipos (doc. A) ACEPTADOS por SUNAT y con saldo pendiente, para autocompletar la
        regularización en la venta final. Filtra por cliente (id o RUC/DNI) y, opcionalmente,
        moneda. Devuelve serie-correlativo, tipo (cat. 12: 02 factura / 03 boleta) y saldo."""
        domain = [
            ("l10n_pe_ne_es_anticipo", "=", True),
            ("l10n_pe_biller_state", "=", "enviado"),
            ("move_type", "=", "out_invoice"),
        ]
        if partner_id:
            domain.append(("partner_id", "=", int(partner_id)))
        elif ruc:
            domain.append(("partner_id.vat", "=", (ruc or "").strip()))
        if moneda:
            domain.append(("currency_id.name", "=", moneda))
        out = []
        for m in self.search(domain, order="id desc", limit=100):
            if round(m.l10n_pe_ne_anticipo_saldo, 2) <= 0:
                continue
            out.append(
                {
                    "id": m.id,
                    "doc": "%s-%s" % (m.l10n_pe_ne_serie_emit or "", m.l10n_pe_ne_corr_emit or ""),
                    "tipo": "02" if (m.l10n_pe_ne_tipo_doc or "01") == "01" else "03",
                    "total": m.amount_total,
                    "aplicado": m.l10n_pe_ne_anticipo_aplicado,
                    "saldo": m.l10n_pe_ne_anticipo_saldo,
                    "moneda": m.currency_id.name or "PEN",
                    "cliente": m.partner_id.name or "",
                    "fechaEmision": m.invoice_date.strftime("%Y-%m-%d") if m.invoice_date else "",
                }
            )
        return out

    # ==================================================== MODELO DE DINERO (L3)
    # Cuatro magnitudes, una definición autoritativa cada una. Confundirlas fue la raíz del
    # rechazo 3265 (el neto pendiente incluía gratuitos). Las invariantes se fijan por test
    # en test_modelo_dinero.py.
    #
    #   amount_total            total contable (Odoo). INCLUYE los bienes gratuitos.
    #   _l10n_pe_importe_cobrar total − anticipo − desc. que NO afecta IGV − gratuitos.
    #                           = lo que el cliente PAGA = mtoImpVenta (PayableAmount).
    #   _l10n_pe_detraccion_base total − desc. que NO afecta IGV. Importe de la OPERACIÓN para
    #                           el SPOT (incluye gratuitos y NO resta anticipo; distinto criterio).
    #   _l10n_pe_neto_pendiente importe_cobrar − detracción − inicial al contado − retención de
    #                           garantía − amortización de adelanto − penalidad − monto cubierto.
    #                           = saldo a CRÉDITO (lo que suman las cuotas) / copago del paciente.
    #
    # Invariantes (SUNAT + negocio):  neto_pendiente ≤ importe_cobrar ≤ amount_total ;
    #   importe_cobrar ≥ 0 ; detracción ≥ 0 ; sum(cuotas) == neto pendiente del crédito.
    def _l10n_pe_importe_cobrar(self):
        """Importe neto a cobrar = total − anticipo aplicado − descuento que no afecta el IGV −
        bienes gratuitos (lo que el cliente paga). Ver «MODELO DE DINERO» arriba."""
        self.ensure_one()
        ant = self._l10n_pe_anticipo()
        return round(
            self.amount_total
            - (ant[2] if ant else 0.0)
            - self._l10n_pe_desc_no_afecta()
            - self._l10n_pe_gratuito_base(),
            2,
        )

    def _l10n_pe_percepcion_monto(self):
        self.ensure_one()
        # La percepción se calcula sobre el neto a cobrar (descontado el anticipo): así la base de la
        # percepción nunca supera el PayableAmount (evita rechazo SUNAT 2797 al combinar con anticipo).
        return round(
            self._l10n_pe_importe_cobrar()
            * (self.l10n_pe_ne_percepcion_rate or 0.0)
            / 100.0,
            2,
        )

    def _l10n_pe_anticipo_gravado(self):
        """Categoría y tasa de la operación gravada del anticipo: (cod_tri, tasa, motivo_no_soportado).
        El anticipo solo es representable como descuento global código 04 sobre una operación gravada
        homogénea (un único régimen IGV '1000' o IVAP '1016'). Devuelve un motivo si no lo es."""
        self.ensure_one()
        lines = self._l10n_pe_product_lines()
        codes = {self._l10n_pe_tax_info(l)[0][1] for l in lines}
        gravado = codes & {"1000", "1016"}
        if not gravado:
            return None, 0.0, "no_gravado"
        if len(gravado) > 1 or (codes - {"1000", "1016"}):
            return None, 0.0, "mixto"
        cod_tri = next(iter(gravado))
        tasa = next(
            (
                self._l10n_pe_tax_info(l)[1]
                for l in lines
                if self._l10n_pe_tax_info(l)[0][1] == cod_tri
            ),
            18.0,
        )
        return cod_tri, tasa, None

    def _l10n_pe_ne_anticipos_list(self):
        """Lista normalizada de anticipos de esta factura: [{doc, monto, tipo, origenId}]. Vacía
        si no aplica (no out_invoice, NC/ND o sin anticipos).
        `origenId` se coerciona con seguridad (nunca explota con basura tipo "abc"): esta lista
        la recorre también `_compute_l10n_pe_ne_anticipo_saldo` para TODAS las regularizaciones
        vivas del sistema, así que una fila envenenada en OTRA factura no debe romper el
        saldo/pendientes de todas las demás — se trata como sin origen local (None)."""
        self.ensure_one()
        if self.move_type != "out_invoice" or self.debit_origin_id:
            return []
        out = []
        for a in (self.l10n_pe_ne_anticipos or []):
            monto = round(float(a.get("monto") or 0.0), 2)
            if monto <= 0:
                continue
            origen_raw = a.get("origenId")
            try:
                origen_id = int(origen_raw) if origen_raw not in (None, "", False) else None
            except (TypeError, ValueError):
                origen_id = None
            out.append({
                "doc": (a.get("doc") or "").strip(),
                "monto": monto,
                "tipo": a.get("tipo") or "02",
                "origenId": origen_id,
            })
        return out

    def _l10n_pe_anticipos_montos(self):
        """(valor, igv, total) AGREGADO de los anticipos: (0.0, 0.0, 0.0) si no aplica (no
        out_invoice, NC/ND o sin anticipos). El valor de CADA anticipo se separa con la tasa
        gravada homogénea real de la factura (no asume 18%) y el agregado es la SUMA de los
        valores/igv por anticipo (no el total dividido una sola vez), así que con montos que no
        son fracción redonda de la base el agregado no arrastra un desvío de redondeo distinto al
        que vería cada `AdditionalDocumentReference` individual — de ahí que el loop por ítem se
        mantenga aunque solo se devuelva el agregado (nadie más consume el desglose por ítem)."""
        self.ensure_one()
        lst = self._l10n_pe_ne_anticipos_list()
        if not lst:
            return (0.0, 0.0, 0.0)
        _cod, tasa, _m = self._l10n_pe_anticipo_gravado()
        vt = it = tt = 0.0
        for a in lst:
            total = round(a["monto"], 2)
            valor = round(total / (1.0 + (tasa or 0.0) / 100.0), 2)
            igv = round(total - valor, 2)
            vt += valor
            it += igv
            tt += total
        return round(vt, 2), round(it, 2), round(tt, 2)

    def _l10n_pe_anticipo(self):
        """(valor, igv, total) AGREGADO de los anticipos, o None si no hay ninguno. Wrapper de
        `_l10n_pe_anticipos_montos()` para no romper a los llamadores previos (percepción, importe
        a cobrar, cabecera, tributos, variable global 04) que solo necesitan el agregado."""
        self.ensure_one()
        v, i, t = self._l10n_pe_anticipos_montos()
        return (v, i, t) if t > 0 else None

    def _l10n_pe_desc_no_afecta(self):
        """Monto del descuento global que NO afecta la base del IGV, topeado para no dejar el total
        negativo. Es un ajuste SOLO de emisión (como el anticipo): no agrega una línea a Odoo, así
        que la gravada/IGV quedan sobre el precio lleno; solo baja el MtoImpVenta (total a pagar).
        Devuelve 0.0 si no aplica (notas, o sin descuento)."""
        self.ensure_one()
        monto = self.l10n_pe_ne_desc_no_afecta or 0.0
        if monto <= 0 or self.move_type not in ("out_invoice",) or self.debit_origin_id:
            return 0.0
        # Tope: no puede superar el total menos lo ya deducido por anticipo (dejaría MtoImpVenta < 0).
        ant = self._l10n_pe_anticipo()
        tope = round((self.amount_total or 0.0) - (ant[2] if ant else 0.0), 2)
        return round(min(monto, max(0.0, tope)), 2)

    def _l10n_pe_check_anticipo(self):
        """Valida que los anticipos sean representables (N documentos relacionados + un descuento
        global código 04 AGREGADO) antes de emitir el XML. Rechaza con un mensaje claro los casos no
        soportados, en vez de generar un comprobante inválido. Cada anticipo de la lista se valida
        individualmente (doc, origen, partner, moneda y saldo propio); la SUMA se valida contra el
        total de la factura."""
        self.ensure_one()
        if self.l10n_pe_ne_es_anticipo and self._l10n_pe_anticipo():
            raise UserError(
                _(
                    "Un comprobante que se emite por un pago anticipado no puede a la vez regularizar "
                    "otro anticipo. Desmarque una de las dos opciones."
                )
            )
        if not self._l10n_pe_anticipo():
            return
        lst = self._l10n_pe_ne_anticipos_list()
        total_aplicado = round(sum(a["monto"] for a in lst), 2)
        if total_aplicado > self.amount_total + 0.01:
            raise UserError(
                _(
                    "El total de anticipos (%.2f) no puede exceder el total de la factura (%.2f)."
                )
                % (total_aplicado, self.amount_total)
            )
        # Otras regularizaciones vivas que ya consumen anticipos (excluye esta factura). El dato vive
        # en la lista JSON: se busca ampliamente UNA sola vez y se filtra/agrega en Python por origen
        # (mismo patrón que `_compute_l10n_pe_ne_anticipo_saldo`).
        otras = self.env["account.move"].search([
            ("id", "!=", self.id),
            ("l10n_pe_ne_anticipos", "!=", False),
            ("state", "=", "posted"),
            ("l10n_pe_biller_state", "not in", ("rechazado", "error", "anulado")),
        ])
        aplicado_en_esta = {}  # origenId -> suma de monto ya visto en ESTA factura (mismo origen 2x).
        for idx, a in enumerate(lst, start=1):
            if a["tipo"] not in ("02", "03"):
                raise UserError(
                    _(
                        "Tipo de documento de anticipo inválido (debe ser 02 factura o 03 boleta)."
                    )
                )
            if not a["doc"]:
                raise UserError(
                    _(
                        "Indique el comprobante del anticipo #%d (serie-correlativo, ej. F001-00000100)."
                    )
                    % idx
                )
            # Si la regularización enlaza un anticipo local (doc. A), valida moneda y saldo
            # disponible: el importe aplicado no puede exceder lo que le queda al anticipo (evita
            # doble consumo), sea desde otra factura o desde otra línea de esta misma lista.
            origen = (
                self.env["account.move"].browse(a["origenId"])
                if a["origenId"]
                else self.env["account.move"]
            )
            if not origen:
                continue
            if not origen.l10n_pe_ne_es_anticipo:
                raise UserError(
                    _("El documento enlazado (%s) no está marcado como pago anticipado.")
                    % origen.display_name
                )
            if origen.partner_id != self.partner_id:
                raise UserError(
                    _("El anticipo (%s) pertenece a otro cliente: solo puede regularizarlo su titular.")
                    % origen.display_name
                )
            if origen.currency_id != self.currency_id:
                raise UserError(
                    _(
                        "El anticipo (%s) y la factura deben estar en la misma moneda: "
                        "regularice el anticipo en un comprobante de su misma moneda."
                    )
                    % origen.display_name
                )
            aplicado_otras = round(
                sum(
                    oa["monto"]
                    for m in otras
                    for oa in m._l10n_pe_ne_anticipos_list()
                    if oa["origenId"] == origen.id
                ),
                2,
            )
            aplicado_en_esta[origen.id] = round(
                aplicado_en_esta.get(origen.id, 0.0) + a["monto"], 2
            )
            disponible = round(origen.amount_total - aplicado_otras, 2)
            if aplicado_en_esta[origen.id] > disponible + 0.01:
                raise UserError(
                    _(
                        "El anticipo %s ya no tiene saldo suficiente: disponible %.2f, "
                        "intentas aplicar %.2f."
                    )
                    % (origen.display_name, disponible, aplicado_en_esta[origen.id])
                )
        _cod, _tasa, motivo = self._l10n_pe_anticipo_gravado()
        if motivo:
            raise UserError(
                _(
                    "El anticipo solo se soporta sobre una operación gravada homogénea (IGV o IVAP). "
                    "No es aplicable a operaciones exoneradas/inafectas/exportación ni a facturas con "
                    "regímenes mixtos: regularice el anticipo en un comprobante separado."
                )
            )

    def _l10n_pe_check_lineas_impuesto(self):
        """Ninguna línea con importe llega al XML sin su tax cat-05: `_l10n_pe_tax_info`
        la clasificaría con el default 'gravado (1000)' a tasa 0 y SUNAT rechaza con 3111
        (TaxableAmount>0 + TaxAmount=0.00), un mensaje críptico que además llega recién
        del validador. Se corta aquí, con el dato que el usuario sí puede arreglar.
        Las líneas de importe 0 (p.ej. NC de corrección de texto) no tienen base imponible
        —no hay 3111 posible— y pasan."""
        self.ensure_one()
        for line in self._l10n_pe_product_lines():
            if not line.price_subtotal:
                continue
            if not any(t.l10n_pe_edi_tax_code in TAX_CODE_MAP for t in line.tax_ids):
                raise UserError(
                    _(
                        "La línea «%s» no tiene impuesto SUNAT asignado (IGV, exonerado, "
                        "inafecto…). Asigna la afectación IGV en el producto o en la línea "
                        "y vuelve a emitir."
                    )
                    % (line.name or line.product_id.display_name or "?")
                )


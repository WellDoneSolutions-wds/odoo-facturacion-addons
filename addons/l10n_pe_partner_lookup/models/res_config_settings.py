from odoo import fields, models


class ResConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'

    l10n_pe_lookup_mode = fields.Selection(
        selection=[
            ('api', "API HTTP"),
            ('dynamodb', "DynamoDB (directo)"),
        ],
        string="Fuente de datos",
        default='api',
        config_parameter='l10n_pe_partner_lookup.mode',
        help="De dónde se obtienen los clientes al buscar por DNI/RUC.",
    )

    # --- Modo API HTTP --------------------------------------------------------
    l10n_pe_lookup_api_url = fields.Char(
        string="URL de la API de consulta DNI/RUC",
        config_parameter='l10n_pe_partner_lookup.api_url',
        help="URL base del servicio que consulta el documento, sin el número. "
             "Se llamará como GET {url}/{documento}.",
    )
    l10n_pe_lookup_api_key = fields.Char(
        string="API Key",
        config_parameter='l10n_pe_partner_lookup.api_key',
        help="Se envía en la cabecera 'x-api-key'. Déjalo vacío si tu API no la requiere.",
    )

    # --- Modo DynamoDB directo ------------------------------------------------
    l10n_pe_dynamo_region = fields.Char(
        string="Región AWS",
        config_parameter='l10n_pe_partner_lookup.aws_region',
        help="Región de la tabla, p. ej. us-east-1. Opcional si en «Tabla» pegas el ARN "
             "(la región se toma del ARN).",
    )
    l10n_pe_dynamo_table = fields.Char(
        string="Tabla DynamoDB",
        config_parameter='l10n_pe_partner_lookup.dynamo_table',
        help="Nombre de la tabla, o su ARN completo "
             "(arn:aws:dynamodb:region:cuenta:table/Nombre). Con el ARN se deduce "
             "también la región.",
    )
    l10n_pe_dynamo_hash_key = fields.Char(
        string="Clave de partición (hash)",
        config_parameter='l10n_pe_partner_lookup.dynamo_hash_key',
        help="Atributo de partición. Su valor es el tipo de documento (RUC/DNI), "
             "que se deduce de la longitud del número. Por defecto: tipo_documento.",
    )
    l10n_pe_dynamo_range_key = fields.Char(
        string="Clave de ordenación (range)",
        config_parameter='l10n_pe_partner_lookup.dynamo_range_key',
        help="Atributo de ordenación que guarda el número de documento. "
             "Por defecto: numero_documento.",
    )
    # Sin campos de access/secret key A PROPÓSITO: boto3 usa su cadena estándar
    # (rol IAM de instancia en producción; env vars / ~/.aws en local). Guardar
    # llaves en ir_config_parameter era un footgun: terminó una key de admin en
    # la BD, legible por cualquier admin y presente en los backups.

    # --- SUNAT (último recurso, para cualquier modo) --------------------------
    l10n_pe_sunat_enabled = fields.Boolean(
        string="Usar SUNAT como respaldo",
        config_parameter='l10n_pe_partner_lookup.sunat_enabled',
        help="Si el documento no aparece en Odoo ni en la fuente principal, "
             "se consulta SUNAT (e-consultaruc) como último recurso.",
    )
    l10n_pe_sunat_token = fields.Char(
        string="Token SUNAT",
        config_parameter='l10n_pe_partner_lookup.sunat_token',
        help="Token de la consulta de SUNAT. Si se deja vacío se usa uno por defecto.",
    )

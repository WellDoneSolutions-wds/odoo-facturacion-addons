import logging

import requests

try:
    import boto3
except ImportError:  # boto3 es opcional: solo se usa en modo DynamoDB directo
    boto3 = None

from odoo import _, api, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

# Tiempo máximo (segundos) que esperamos a la API antes de rendirnos. Corto a
# propósito: si el servicio externo está caído, la facturación no debe colgarse.
LOOKUP_TIMEOUT = 8

# SUNAT (consulta pública e-consultaruc), usada como último recurso.
SUNAT_URL = "https://e-consultaruc.sunat.gob.pe/cl-ti-itmrconsruc/jcrS00Alias"
SUNAT_WARMUP_URL = "https://e-consultaruc.sunat.gob.pe/cl-ti-itmrconsruc/FrameCriterioBusquedaWeb.jsp"


class ResPartner(models.Model):
    _inherit = 'res.partner'

    # ------------------------------------------------------------------
    # Configuración
    # ------------------------------------------------------------------
    @api.model
    def _l10n_pe_get_lookup_config(self):
        """Devuelve (url_base, api_key) configurados en Ajustes."""
        icp = self.env['ir.config_parameter'].sudo()
        return (
            icp.get_param('l10n_pe_partner_lookup.api_url'),
            icp.get_param('l10n_pe_partner_lookup.api_key'),
        )

    # ------------------------------------------------------------------
    # Consulta a la fuente externa (despachador según el modo configurado)
    # ------------------------------------------------------------------
    @api.model
    def _l10n_pe_query_external_db(self, doc_number):
        """Consulta el documento según el modo configurado (API o DynamoDB).

        Devuelve un diccionario normalizado con los datos del cliente, o
        ``None`` si no se encuentra / la fuente falla (para degradar a creación
        manual). Lanza ``UserError`` solo en casos accionables por el usuario
        (falta de configuración o timeout).
        """
        mode = self.env['ir.config_parameter'].sudo().get_param(
            'l10n_pe_partner_lookup.mode') or 'api'
        if mode == 'dynamodb':
            return self._l10n_pe_query_dynamodb(doc_number)
        return self._l10n_pe_query_api(doc_number)

    @api.model
    def _l10n_pe_query_api(self, doc_number):
        """Modo API: GET {url}/{documento} contra un servicio HTTP."""
        base_url, api_key = self._l10n_pe_get_lookup_config()
        if not base_url:
            raise UserError(_(
                "Configura la URL de la API de consulta en "
                "Ajustes → Facturación → «Búsqueda de cliente por DNI/RUC»."
            ))

        try:
            response = requests.get(
                url="%s/%s" % (base_url.rstrip('/'), doc_number),
                headers={'x-api-key': api_key} if api_key else {},
                timeout=LOOKUP_TIMEOUT,
            )
            response.raise_for_status()
            payload = response.json()
        except requests.Timeout:
            raise UserError(_(
                "El servicio de consulta no respondió a tiempo. "
                "Inténtalo de nuevo o crea el cliente manualmente."
            ))
        except (requests.RequestException, ValueError):
            # Error de red / HTTP / JSON inválido: lo registramos y devolvemos
            # None para que el flujo siga con creación manual.
            _logger.warning(
                "l10n_pe_partner_lookup: fallo consultando el documento %s",
                doc_number, exc_info=True,
            )
            return None

        return self._l10n_pe_normalize_payload(payload)

    @api.model
    def _l10n_pe_parse_dynamo_table(self, value):
        """Acepta el NOMBRE de la tabla o su ARN. Devuelve (region_or_None, table_name).
        ARN DynamoDB: arn:aws:dynamodb:<region>:<cuenta>:table/<NombreTabla>."""
        value = (value or '').strip()
        if value.startswith('arn:'):
            parts = value.split(':')
            # 0:arn 1:aws 2:dynamodb 3:<region> 4:<cuenta> 5:table/<Nombre>
            if len(parts) >= 6 and parts[2] == 'dynamodb':
                resource = parts[5]
                name = resource.split('/', 1)[1] if '/' in resource else resource
                return (parts[3] or None), name
            return None, value  # ARN no reconocido → se usa tal cual (best-effort)
        return None, value

    @api.model
    def _l10n_pe_query_dynamodb(self, doc_number):
        """Modo DynamoDB: get_item directo por la clave de partición.

        Requiere el paquete ``boto3`` y, en Ajustes, la región y la tabla.
        Las credenciales se toman de Ajustes o, mejor, de un rol IAM / variables
        de entorno (boto3 las descubre solo si se dejan vacías).
        """
        if boto3 is None:
            raise UserError(_(
                "El modo DynamoDB requiere el paquete 'boto3'. Instálalo con:\n"
                "    pip install boto3"
            ))

        icp = self.env['ir.config_parameter'].sudo()
        # La "Tabla" acepta el nombre o el ARN completo; si es un ARN, de él sale
        # también la región (y prevalece sobre el campo Región).
        arn_region, table_name = self._l10n_pe_parse_dynamo_table(
            icp.get_param('l10n_pe_partner_lookup.dynamo_table'))
        region = arn_region or icp.get_param('l10n_pe_partner_lookup.aws_region')
        hash_key = icp.get_param('l10n_pe_partner_lookup.dynamo_hash_key') or 'tipo_documento'
        range_key = icp.get_param('l10n_pe_partner_lookup.dynamo_range_key') or 'numero_documento'
        if not (region and table_name):
            raise UserError(_(
                "Configura la tabla de DynamoDB (nombre o ARN) y la región en "
                "Ajustes → Facturación → «Búsqueda de cliente por DNI/RUC». "
                "Si pegas el ARN de la tabla, la región se toma de él."
            ))

        # Credenciales: SIEMPRE la cadena estándar de boto3 — rol IAM de la
        # instancia en producción; variables de entorno o ~/.aws en local.
        # NUNCA guardar access/secret keys en la BD: cualquier admin las lee y
        # viajan en los backups (ya pasó una vez con una key de admin).
        kwargs = {'region_name': region}

        # Clave primaria compuesta: la partición es el tipo de documento
        # (RUC/DNI, deducido de la longitud) y el rango es el número.
        doc_type = 'RUC' if len(doc_number) == 11 else 'DNI'
        try:
            table = boto3.resource('dynamodb', **kwargs).Table(table_name)
            response = table.get_item(Key={
                hash_key: doc_type,
                range_key: doc_number,
            })
        except Exception:
            # Cualquier fallo de AWS/red: lo registramos y degradamos a manual.
            _logger.warning(
                "l10n_pe_partner_lookup: fallo en DynamoDB para el documento %s",
                doc_number, exc_info=True,
            )
            return None

        item = response.get('Item')
        if not item:
            return None
        return self._l10n_pe_normalize_payload(item)

    @api.model
    def _l10n_pe_normalize_payload(self, payload):
        """Mapea la respuesta cruda de la API a un dict interno estable.

        AJUSTA LAS CLAVES de la izquierda (``data.get(...)``) a los nombres
        EXACTOS que devuelve tu API. La API de este proyecto devuelve:
        nroDocumento, tipoDocumento, nombre/razonSocial, direccion (opcional),
        estado.
        """
        if not isinstance(payload, dict):
            return None
        # Algunas APIs envuelven el objeto en {"data": {...}} o {"result": {...}}.
        data = payload.get('data') or payload.get('result') or payload
        if not isinstance(data, dict):
            return None

        doc_number = (
            data.get('nroDocumento')
            or data.get('numeroDocumento')
            or data.get('numero_documento')
        )
        name = (
            data.get('nombre_razon_social')
            or data.get('nombre')
            or data.get('razonSocial')
            or data.get('razon_social')
            or data.get('nombreCompleto')
            or data.get('nombre_completo')
        )
        if not doc_number or not name:
            return None

        doc_type = data.get('tipoDocumento') or data.get('tipo_documento') or ''
        return {
            'doc_number': str(doc_number).strip(),
            'doc_type': str(doc_type).strip(),
            'name': str(name).strip(),
            'address': str(data.get('direccion') or '').strip() or False,
            'state': str(data.get('estado') or '').strip() or False,
        }

    # ------------------------------------------------------------------
    # Mapeo y creación del partner
    # ------------------------------------------------------------------
    @api.model
    def _l10n_pe_map_identification_type(self, doc_type, doc_number):
        """Determina el tipo de identificación (DNI/RUC) por tipo o longitud."""
        doc_type_norm = (doc_type or '').strip().upper()
        number = (doc_number or '').strip()
        if doc_type_norm in ('RUC', '6') or len(number) == 11:
            xmlid = 'l10n_pe.it_RUC'
        elif doc_type_norm in ('DNI', '1') or len(number) == 8:
            xmlid = 'l10n_pe.it_DNI'
        else:
            xmlid = 'l10n_latam_base.it_vat'  # genérico de respaldo
        return self.env.ref(xmlid, raise_if_not_found=False)

    @api.model
    def _l10n_pe_prepare_partner_vals(self, data):
        """Construye los valores de ``res.partner`` a partir del dict normalizado."""
        id_type = self._l10n_pe_map_identification_type(
            data.get('doc_type'), data['doc_number'])
        ruc_type = self.env.ref('l10n_pe.it_RUC', raise_if_not_found=False)
        # Solo el RUC de persona jurídica (empieza por 20) es empresa; el RUC de
        # persona natural (10/15/17) y el DNI son personas.
        is_company = (
            bool(id_type) and id_type == ruc_type
            and data['doc_number'].startswith('20')
        )

        vals = {
            'name': data['name'],
            'vat': data['doc_number'],
            'country_id': self.env.ref('base.pe').id,
            'company_type': 'company' if is_company else 'person',
            # customer_rank > 0 hace que el partner aparezca en «Clientes», no solo
            # en «Contactos». Sin esto se crea como contacto suelto (rank 0) y no
            # sale en el listado/buscador de clientes. Mismo criterio que
            # l10n_pe_ne_biller al dar de alta un cliente.
            'customer_rank': 1,
            # company_id del emisor actual: aísla el cliente por RUC (multi-tenant).
            # Sin esto quedaría company_id=False = visible/editable por TODOS los
            # tenants. Igual que l10n_pe_ne_biller._l10n_pe_ne_quick_partner.
            'company_id': self.env.company.id,
        }
        if id_type:
            vals['l10n_latam_identification_type_id'] = id_type.id
        if data.get('address'):
            vals['street'] = data['address']
        return vals

    @api.model
    def _l10n_pe_find_partner(self, doc_number):
        """Busca un partner existente por número de documento (anti-duplicado)."""
        if not doc_number:
            return self.browse()
        return self.search([('vat', '=', doc_number.strip())], limit=1)

    @api.model
    def _l10n_pe_find_or_create_from_external(self, doc_number):
        """Orquesta: si existe lo devuelve; si no, lo busca en la API y lo crea.

        Devuelve un recordset (vacío si no se encontró en ningún lado).
        """
        doc_number = (doc_number or '').strip()
        partner = self._l10n_pe_find_partner(doc_number)
        if partner:
            return partner
        data = self._l10n_pe_query_external_db(doc_number)
        if not data:
            data = self._l10n_pe_query_sunat(doc_number)  # último recurso
        if not data:
            return self.browse()
        # SUNAT puede resolver un DNI a su RUC: vuelve a verificar duplicado por
        # el documento devuelto antes de crear.
        partner = self._l10n_pe_find_partner(data['doc_number'])
        if partner:
            return partner
        return self.create(self._l10n_pe_prepare_partner_vals(data))

    # ------------------------------------------------------------------
    # Integración directa en el campo Customer (sin botón)
    # ------------------------------------------------------------------
    @api.model
    def _l10n_pe_is_document_number(self, value):
        """True si el texto parece un DNI (8 dígitos) o RUC (11): solo dígitos."""
        value = (value or '').strip()
        return value.isdigit() and len(value) in (8, 11)

    @api.model
    def name_create(self, name):
        """Permite buscar por DNI/RUC escribiendo en el propio campo Customer.

        Cuando escribes un DNI/RUC que no existe en Odoo y pulsas «Crear …»,
        intentamos traerlo de la fuente externa y crearlo con sus datos reales,
        en lugar de crear un contacto cuyo nombre sea el número. Si no se
        encuentra, se mantiene el comportamiento estándar de Odoo.
        """
        cleaned = (name or '').strip()
        if self._l10n_pe_is_document_number(cleaned):
            partner = self._l10n_pe_find_or_create_from_external(cleaned)
            if partner:
                return partner.id, partner.display_name
        return super().name_create(name)

    @api.model
    def l10n_pe_get_field_suggestions(self, name):
        """Sugerencias en vivo para el desplegable del campo Customer.

        Solo actúa si el texto es un DNI/RUC (8/11 dígitos) que aún NO existe en
        Odoo; entonces consulta la fuente externa y devuelve una sugerencia para
        crear. La búsqueda por nombre la cubre el name_search normal de Odoo.
        Degrada en silencio ante cualquier error (es búsqueda mientras tecleas).
        """
        cleaned = (name or '').strip()
        if not self._l10n_pe_is_document_number(cleaned):
            return []
        if self._l10n_pe_find_partner(cleaned):
            return []
        try:
            data = self._l10n_pe_query_external_db(cleaned)
            if not data:
                data = self._l10n_pe_query_sunat(cleaned)  # último recurso
        except Exception:
            _logger.warning(
                "l10n_pe_partner_lookup: sugerencia falló para %s",
                cleaned, exc_info=True)
            return []
        if not data:
            return []
        return [{
            "doc_number": data["doc_number"],
            "name": data["name"],
            "label": "%s — %s" % (data["doc_number"], data["name"]),
        }]

    @api.model
    def l10n_pe_create_partner_from_document(self, doc_number):
        """Crea (o reutiliza) el partner del documento; devuelve id y nombre."""
        partner = self._l10n_pe_find_or_create_from_external((doc_number or '').strip())
        if not partner:
            return False
        return {"id": partner.id, "display_name": partner.display_name}

    @api.model
    def l10n_pe_lookup_partner_data(self, doc_number):
        """Devuelve los datos de un documento para AUTOCOMPLETAR un formulario,
        SIN crear el partner. Si ya existe en Odoo, devuelve sus datos con
        exists=True (y su id) para avisar que al guardar se reusará. Si no existe,
        consulta la fuente externa (y SUNAT como respaldo). Degrada a {} ante
        cualquier fallo o si no se encuentra (para completar a mano)."""
        doc_number = (doc_number or '').strip()
        if not self._l10n_pe_is_document_number(doc_number):
            return {}
        partner = self._l10n_pe_find_partner(doc_number)
        if partner:
            return {
                'exists': True,
                'id': partner.id,
                'doc_number': partner.vat or doc_number,
                'doc_type': partner.l10n_latam_identification_type_id.l10n_pe_vat_code or '',
                'name': partner.name or '',
                'address': partner.street or '',
                'email': partner.email or '',
                'phone': partner.phone or '',
            }
        try:
            data = self._l10n_pe_query_external_db(doc_number)
            if not data:
                data = self._l10n_pe_query_sunat(doc_number)
        except Exception:  # noqa: BLE001
            _logger.warning(
                "l10n_pe_partner_lookup: autocompletar falló para %s",
                doc_number, exc_info=True)
            return {}
        if not data:
            return {}
        id_type = self._l10n_pe_map_identification_type(data.get('doc_type'), data['doc_number'])
        return {
            'exists': False,
            'doc_number': data['doc_number'],
            'doc_type': (id_type.l10n_pe_vat_code if id_type else '') or '',
            'name': data['name'],
            'address': data.get('address') or '',
            'email': '',
            'phone': '',
        }

    # ------------------------------------------------------------------
    # SUNAT (último recurso): scraping de e-consultaruc
    # ------------------------------------------------------------------
    @api.model
    def _l10n_pe_query_sunat(self, doc_number):
        """Consulta SUNAT como último recurso. Devuelve dict normalizado o None.

        Por DNI usa ``consPorTipdoc`` (página de lista) y por RUC ``consPorRuc``
        (ficha de detalle). Requiere el toggle activo en Ajustes. Mantiene una
        sesión con GET de calentamiento para obtener las cookies (sin ellas
        SUNAT no devuelve datos). Degrada en silencio ante cualquier error.
        """
        icp = self.env['ir.config_parameter'].sudo()
        enabled = (icp.get_param('l10n_pe_partner_lookup.sunat_enabled') or '').strip().lower()
        if enabled not in ('true', '1'):
            return None
        doc_number = (doc_number or '').strip()
        if not self._l10n_pe_is_document_number(doc_number):
            return None

        token = icp.get_param('l10n_pe_partner_lookup.sunat_token') or 'aaaaaaaaaaaaaaaaaaaa'
        payload = {
            'razSoc': '', 'nroRuc': '', 'nrodoc': '', 'token': token,
            'contexto': 'ti-it', 'modo': '1', 'search1': '',
            'tipdoc': '1', 'search2': '', 'search3': '', 'codigo': '',
        }
        if len(doc_number) == 11:
            payload.update({'accion': 'consPorRuc', 'nroRuc': doc_number, 'rbtnTipo': '1'})
        else:
            payload.update({'accion': 'consPorTipdoc', 'nrodoc': doc_number, 'rbtnTipo': '2'})

        try:
            session = requests.Session()
            session.headers.update({
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)',
                'Content-Type': 'application/x-www-form-urlencoded',
                'Referer': SUNAT_WARMUP_URL,
            })
            session.get(SUNAT_WARMUP_URL, timeout=LOOKUP_TIMEOUT)  # cookies de sesión
            response = session.post(SUNAT_URL, data=payload, timeout=LOOKUP_TIMEOUT)
            response.raise_for_status()
        except requests.RequestException:
            _logger.warning(
                "l10n_pe_partner_lookup: SUNAT no respondió para %s",
                doc_number, exc_info=True)
            return None
        return self._l10n_pe_parse_sunat_html(response.text, doc_number)

    @api.model
    def _l10n_pe_parse_sunat_html(self, html_text, queried_doc):
        """Parsea la respuesta de SUNAT (lista o ficha) al dict normalizado."""
        from lxml import html as lxml_html

        def has_class(css):
            return ("contains(concat(' ', normalize-space(@class), ' '), ' %s ')"
                    % css)

        try:
            tree = lxml_html.fromstring(html_text)
        except Exception:
            return None

        # Página de LISTA (consPorTipdoc, por DNI): <a class="aRucs" data-ruc="…">
        for anchor in tree.xpath("//a[%s]" % has_class("aRucs")):
            ruc = (anchor.get('data-ruc') or '').strip()
            headings = anchor.xpath(".//*[%s]" % has_class("list-group-item-heading"))
            name = " ".join(headings[1].text_content().split()) if len(headings) >= 2 else ''
            estado = ''
            for para in anchor.xpath(".//*[%s]" % has_class("list-group-item-text")):
                text = " ".join(para.text_content().split())
                if text.lower().startswith('estado'):
                    estado = text.split(':', 1)[-1].strip()
            if ruc and name:
                return self._l10n_pe_sunat_vals(ruc, name, False, estado)
            return None

        # Ficha de DETALLE (consPorRuc, por RUC): filas col-sm-5 / col-sm-7
        pairs = {}
        for item in tree.xpath("//*[%s]" % has_class("list-group-item")):
            labels = item.xpath(".//*[%s]" % has_class("col-sm-5"))
            values = item.xpath(".//*[%s]" % has_class("col-sm-7"))
            if labels and values:
                label = " ".join(labels[0].text_content().split()).rstrip(':')
                pairs[label] = " ".join(values[0].text_content().split())
        if pairs:
            ruc, _sep, name = pairs.get('Número de RUC', '').partition(' - ')
            ruc = ruc.strip() or queried_doc
            name = name.strip()
            address = pairs.get('Domicilio Fiscal', '')
            if address.strip() == '-':
                address = ''
            estado = pairs.get('Estado del Contribuyente', '')
            if name:
                return self._l10n_pe_sunat_vals(ruc, name, address, estado)
        return None

    @api.model
    def _l10n_pe_sunat_vals(self, doc_number, name, address, state):
        """Empaqueta los datos de SUNAT en el mismo formato que el resto."""
        doc_number = (doc_number or '').strip()
        addr = address.strip() if isinstance(address, str) else ''
        return {
            'doc_number': doc_number,
            'doc_type': 'RUC' if len(doc_number) == 11 else 'DNI',
            'name': (name or '').strip(),
            'address': addr or False,
            'state': (state or '').strip() or False,
        }

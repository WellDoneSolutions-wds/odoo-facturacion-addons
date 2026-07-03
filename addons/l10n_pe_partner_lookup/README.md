# l10n_pe_partner_lookup

Al registrar una factura de cliente, busca el contacto por **DNI/RUC** en una API
externa (respaldada, por ejemplo, por DynamoDB en AWS). Si lo encuentra, **crea el
cliente automáticamente** y lo selecciona en la factura. Si ya existe en Odoo, lo
reutiliza (anti-duplicado por número de documento).

## Instalación

El módulo vive en `addons/`, ya incluido en el `addons_path`. Instálalo con:

```bash
venv\Scripts\python odoo-bin -c odoo.conf -d odoo_dev -i l10n_pe_partner_lookup --stop-after-init
```

(Requiere la localización peruana `l10n_pe` instalada.)

## Configuración

**Ajustes → Facturación** (en Community la app `account` se llama *Facturación*) →
bloque **«Búsqueda de cliente por DNI/RUC»**. Elige la **Fuente de datos**:

### Modo «API HTTP»
- **URL de la API**: URL base SIN el número. Se llama como `GET {url}/{documento}`.
- **API Key**: se envía en la cabecera `x-api-key`. Déjala vacía si tu API no la usa.

### Modo «DynamoDB (directo)»
Consulta la tabla con `boto3` (`get_item` por **clave primaria compuesta**).
Requiere `pip install boto3`.
- **Región AWS** (p. ej. `us-east-1`) y **Tabla**. En **Tabla** puedes poner el
  *nombre* o pegar el **ARN completo**
  (`arn:aws:dynamodb:REGION:CUENTA:table/NOMBRE`); con el ARN se deduce también la
  región, y el campo Región queda opcional.
- **Clave de partición (hash)**: por defecto `tipo_documento`. Su valor (`RUC`/`DNI`)
  se deduce de la longitud del número (11 = RUC, si no = DNI).
- **Clave de ordenación (range)**: por defecto `numero_documento` (el número).

#### Autenticación en AWS: rol IAM, sin llaves (único método)
El addon **no tiene campos de Access/Secret Key a propósito** (guardar llaves en
`ir_config_parameter` es un footgun: cualquier admin las lee y viajan en los
backups — pasó con una key de admin). `boto3` usa su **cadena estándar**:
- **En AWS** (EC2 / ECS / EKS / Lambda): adjunta un **rol IAM** al cómputo
  (instance profile / task role / IRSA / execution role) y boto3 toma
  credenciales temporales automáticamente (rotación incluida).
- **En desarrollo local**: variables de entorno (`AWS_ACCESS_KEY_ID`, etc.) o
  `~/.aws/credentials` — boto3 las descubre solo, sin tocar Odoo.

Ojo: **un ARN por sí solo no autentica**; el ARN de la tabla se usa para acotar
el permiso del rol (mínimo privilegio):

```json
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Action": "dynamodb:GetItem",
    "Resource": "arn:aws:dynamodb:us-east-1:123456789012:table/clientes"
  }]
}
```

Fuera de AWS (on-prem/otra nube) no hay rol de instancia: usa **IAM Roles Anywhere**
(certificado X.509, keyless) o `AssumeRole` con credenciales base mínimas; como
último recurso, llaves de un usuario dedicado con solo ese `GetItem`.

### SUNAT (último recurso, opcional)
Toggle **"Usar SUNAT como respaldo"** + **Token**. Si el documento no aparece en
Odoo ni en la fuente principal (Dynamo/API), se consulta SUNAT (e-consultaruc)
mediante scraping: por DNI (`consPorTipdoc`, página de lista) o por RUC
(`consPorRuc`, ficha). Usa `lxml` (ya incluido en Odoo). Si buscas por **DNI** y
SUNAT resuelve el **RUC** de persona natural, el contacto se crea con el **RUC**.
Notas: el scraping es frágil (SUNAT puede cambiar el HTML) y depende de cookies
de sesión; por eso es el último recurso.

Internamente todo se guarda en `ir.config_parameter`
(`l10n_pe_partner_lookup.mode`, `.api_url`, `.api_key`, `.aws_region`,
`.dynamo_table`, `.dynamo_hash_key`, `.dynamo_range_key`, `.aws_access_key_id`,
`.aws_secret_access_key`, `.sunat_enabled`, `.sunat_token`).

## Uso

En una factura de cliente (borrador), pulsa el botón **«Buscar cliente por DNI/RUC»**
en la cabecera, escribe el documento y pulsa **Buscar**:

- Si ya existe en Odoo → lo selecciona.
- Si lo devuelve la API → muestra los datos y, al confirmar, **crea y selecciona** el cliente.
- Si no se encuentra → lo creas manualmente como siempre.

## Ajustar a TU API (importante)

El contrato esperado es un JSON con estos campos (acepta el objeto plano o envuelto
en `data`/`result`). Si tu API usa otros nombres de clave, edítalos en
`models/res_partner.py → _l10n_pe_normalize_payload`:

| Campo interno | Claves que se leen de la API           |
|---------------|----------------------------------------|
| documento     | `nroDocumento`, `numeroDocumento`      |
| nombre        | `nombre`, `razonSocial`, `nombreCompleto` |
| tipo doc.     | `tipoDocumento`                        |
| dirección     | `direccion` (opcional)                 |
| estado        | `estado` (opcional)                    |

El tipo de identificación (DNI/RUC) se resuelve por `tipoDocumento` y, como
respaldo, por la longitud del número (8 = DNI, 11 = RUC).

## Notas técnicas

- La consulta usa `requests` con timeout corto (8 s). Si la API cae, el flujo
  degrada a creación manual sin bloquear la factura.
- DynamoDB queda **detrás de tu API**: Odoo solo conoce la URL y la clave; no
  maneja credenciales de AWS. Si tu API Gateway usa autorización IAM (SigV4) o
  Cognito en lugar de API Key, ajusta la cabecera/firma en
  `_l10n_pe_query_external_db`.

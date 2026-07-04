# Diseño — Envío de comprobante por correo electrónico

- **Fecha:** 2026-07-03
- **Addon:** `l10n_pe_ne_biller` (sin addons nuevos)
- **Estado:** Aprobado (pendiente de plan de implementación)
- **Consumidor previsto:** `ne-express` (vía la superficie REST `/ne/api/*`). *No se toca `ne-express` en este trabajo.*

## 1. Motivación

Hoy el addon **no** tiene forma de enviar un comprobante al cliente por correo. La entrega
de archivos es solo por descarga HTTP (endpoints `GET /ne/api/comprobantes/<id>/<kind>` y
acciones `action_l10n_pe_download_*`). Se necesita una acción explícita "enviar por correo"
que `ne-express` pueda disparar por la misma superficie REST que ya usa.

El "Enviar y Imprimir" nativo de Odoo (`account.move.send`) **no** sirve: adjunta el PDF QWeb
nativo de Odoo, no la representación impresa SFS que genera el microservicio `ms-ne-biller` y
que es la legalmente relevante en Perú. Por eso se construye un envío propio que adjunta el
artefacto correcto.

## 2. Alcance

**Incluye (v1):**
- Solo `account.move` (factura, boleta, nota de crédito, nota de débito).
- Adjuntos: **PDF + XML firmado**.
- Solo se permite enviar cuando el comprobante está **aceptado por SUNAT (CDR ResponseCode 0)**.
- Un método de modelo, una plantilla de correo (`mail.template`), un endpoint REST y sus tests.

**No incluye (v1):**
- Otro-CPE (retención/percepción, `account.payment`, tipoDoc 20/40) — tienen su propio PDF
  (`_l10n_pe_otrocpe_pdf`) y requerirían método + endpoint paralelos. Segunda iteración.
- Adjuntar el CDR (decisión: solo PDF + XML por defecto).
- Botón "Enviar por correo" en la vista backend (trivial de añadir después; no es el objetivo,
  que es el consumo desde `ne-express`).
- Cualquier cambio en `ne-express`.

## 3. Decisiones tomadas

| Decisión | Elección | Motivo |
|---|---|---|
| Addon nuevo vs. extender | **Extender `l10n_pe_ne_biller`** | El PDF, el XML/CDR, el email del partner y el patrón `mail.mail` ya viven aquí. |
| Mecanismo del correo | **Approach A — `mail.template`** | Es un correo al cliente final; las empresas querrán personalizar asunto/cuerpo/firma sin un deploy. |
| Documentos | Solo `account.move` | Menos superficie; otro-CPE luego. |
| Adjuntos | PDF + XML | Lo que el receptor peruano suele necesitar; CDR queda opcional/descartado en v1. |
| Precondición | CDR aceptado (ResponseCode 0) | No enviar al cliente comprobantes rechazados o en proceso. |

## 4. Arquitectura y componentes

Todo dentro de `addons/l10n_pe_ne_biller/`.

### 4.1 Plantilla de correo — `data/l10n_pe_ne_mail_template.xml`

Un registro `mail.template` sobre `account.move`, con xml_id
`l10n_pe_ne_biller.mail_template_comprobante`:

- `subject`: `Comprobante {{ object.l10n_pe_ne_serie_emit }}-{{ object.l10n_pe_ne_corr_emit }} — {{ object.company_id.name }}`
- `email_from`: `{{ object.company_id.email_formatted }}`
- `email_to`: `{{ object.partner_id.email }}`
- `body_html`: saludo + serie/correlativo + total del comprobante.
- `auto_delete`: `False` (dejar traza del `mail.mail`).
- `lang`: `{{ object.partner_id.lang }}` (opcional).

Se registra en `data` del manifiesto.

### 4.2 Método de modelo — `models/account_move_biller.py`

```
def l10n_pe_ne_email_comprobante(self, to=None, cc=None):
    self.ensure_one()
    # 1. Precondición: aceptado por SUNAT
    if not self._l10n_pe_ne_is_aceptado():
        raise UserError(_("El comprobante no está aceptado por SUNAT; no se puede enviar."))
    # 2. Destinatario
    to = (to or self.partner_id.email or "").strip()
    if not to:
        raise UserError(_("El cliente no tiene correo configurado; indica un destinatario."))
    # 3. Adjuntos: PDF (reusa caché) + XML firmado
    pdf = self._l10n_pe_get_pdf_attachment()
    xml = self.l10n_pe_biller_xml
    att_ids = [pdf.id, xml.id]
    # 4. Envío por plantilla
    template = self.env.ref("l10n_pe_ne_biller.mail_template_comprobante")
    template.send_mail(self.id, force_send=True, email_values={
        "email_to": to,
        "email_cc": cc or "",
        "attachment_ids": [(6, 0, att_ids)],
    })
    # 5. Traza en el chatter
    self.message_post(body=_("Comprobante enviado por correo a %s") % to)
    return {"ok": True, "to": to}
```

Helper nuevo para la precondición (re-deriva la aceptación del CDR guardado, no depende del
texto de `l10n_pe_biller_message`):

```
def _l10n_pe_ne_is_aceptado(self):
    self.ensure_one()
    if self.l10n_pe_biller_state != "enviado" or not self.l10n_pe_biller_cdr:
        return False
    code, _desc = self._l10n_pe_parse_cdr_codes(self.l10n_pe_biller_cdr.raw or b"")
    return code == "0"
```

Reutiliza:
- `_l10n_pe_get_pdf_attachment()` (`account_move_biller.py:2712`) — genera/cachea el PDF SFS.
- `_l10n_pe_parse_cdr_codes()` (`account_move_biller.py:987`) — extrae ResponseCode del CDR.
- `l10n_pe_biller_xml` / `l10n_pe_biller_cdr` / `l10n_pe_biller_state`
  (`account_move_biller.py:418` / `:421` / `:82`).
- Patrón de correo tomado de `res_users._l10n_pe_ne_send_reset_email` (`res_users.py:120`).

### 4.3 Endpoint REST — `controllers/main.py`

Thin, delegando al modelo, con el estilo `_run(...)` del resto:

```
@http.route("/ne/api/comprobantes/<int:rec_id>/email", **_POST)
def email_comprobante(self, rec_id, **kw):
    uid = self._identify()
    if not uid:
        return self._unauth()
    def op():
        b = self._body()
        return self._move(uid).browse(rec_id).l10n_pe_ne_email_comprobante(
            to=b.get("to"), cc=b.get("cc"))
    return self._run(op)
```

- Auth: Bearer/API-key vía `_identify()` (`main.py:97`), 401 si falta (`_unauth`).
- Scope por RUC: `_move(uid)` (`main.py:126`) ya aplica `with_user` + `with_company`.
- Body opcional `{ "to": "...", "cc": "..." }`; default = `partner_id.email`.
- Errores: el `UserError` del modelo lo convierte `_run()`/`_fail()` en JSON con status.

### 4.4 Manifiesto — `__manifest__.py`

- `depends`: agregar `'mail'` explícito (hoy es transitivo vía `account`; al usar
  `mail.template` conviene declararlo).
- `data`: agregar `'data/l10n_pe_ne_mail_template.xml'`.

## 5. Flujo de datos

```
ne-express ──POST /ne/api/comprobantes/<id>/email {to?, cc?}──► controllers/main.py
                                                                     │ _identify(), _move(uid).browse(id)
                                                                     ▼
                                         account.move.l10n_pe_ne_email_comprobante(to, cc)
                                            ├─ _l10n_pe_ne_is_aceptado()  (CDR ResponseCode 0)
                                            ├─ _l10n_pe_get_pdf_attachment() ──► ms-ne-biller POST /report/pdf (cacheado)
                                            ├─ adjunta PDF + l10n_pe_biller_xml
                                            └─ mail.template.send_mail(force_send=True) ──► ir.mail_server (SMTP)
```

## 6. Manejo de errores

- Comprobante no aceptado → `UserError` ("…no se puede enviar").
- Sin destinatario (partner sin email y sin `to`) → `UserError`.
- Micro no devuelve PDF → `UserError` (ya lo lanza `_l10n_pe_get_pdf_attachment`).
- SMTP no configurado / fallo de envío → error propagado (prerrequisito de infra).

Todos se serializan a JSON por el `_run()`/`_fail()` existente del controller.

## 7. Prerrequisitos de infraestructura (no son código)

- **Servidor SMTP saliente** (`ir.mail_server`) configurado en Odoo. En dev (docker-compose)
  probablemente no existe; se necesita para probar el envío real. El reset de contraseña ya lo
  asume.
- **Email por compañía**: `email_from` sale de `company_id.email_formatted`; cada RUC/compañía
  debe tener correo configurado.

## 8. Plan de pruebas (TDD)

Archivo nuevo `tests/test_email.py`, siguiendo el patrón de `tests/test_send.py`
(mockea `...account_move_biller.requests.post` para el PDF; helper `_fake_cdr_b64` para el CDR).
Los correos no se envían de verdad en tests: se afirma sobre los registros `mail.mail`/`mail.message`.

**Modelo (`TransactionCase`)** — `setUp` deja un `account.move` posteado en estado `enviado`,
con `l10n_pe_biller_xml` (adjunto XML firmado) y `l10n_pe_biller_cdr` (CDR con ResponseCode 0):

1. `test_rechaza_sin_cdr_aceptado` — sin CDR o CDR con code≠0 → `UserError`.
2. `test_rechaza_sin_destinatario` — partner sin email y sin `to` → `UserError`.
3. `test_usa_email_del_partner` — sin `to` → usa `partner_id.email`.
4. `test_override_destinatario` — `to="x@y.com"` gana sobre el del partner.
5. `test_adjunta_pdf_y_xml` — mockea `/report/pdf` (`%PDF…`); el mail sale con 2 adjuntos
   (pdf + xml); la 2ª llamada reusa el PDF cacheado (no vuelve a llamar al micro).
6. `test_envia_mail` — se crea el `mail.mail`/`mail.message` con el asunto esperado.

**Controlador (`HttpCase`)**, como `tests/test_password_reset.py`:

7. `test_email_requiere_auth` — `POST /ne/api/comprobantes/1/email` sin Bearer → 401.

**Ciclo:** rojo (tests) → verde (helper `_l10n_pe_ne_is_aceptado` + método
`l10n_pe_ne_email_comprobante` + template + endpoint + manifest) → refactor.

## 9. Decisiones diferidas / notas

- **CDR con observaciones (ResponseCode 4xxx = "aceptada con observaciones")**: v1 usa la
  definición estricta `code == "0"`. Si el negocio quiere permitir 4xxx, es un cambio de una
  línea en `_l10n_pe_ne_is_aceptado`.
- **Otro-CPE (retención/percepción)**: fuera de v1; misma forma con
  `account.payment._l10n_pe_otrocpe_pdf` y un endpoint `/ne/api/otrocpe/<id>/email`.
- **Email en el endpoint `detalle`**: hoy `l10n_pe_ne_comprobante_detalle`
  (`account_move_biller.py:2630`) no devuelve el email del cliente. Si `ne-express` quiere
  prellenar el destinatario en su UI, o el endpoint `/email` lo recibe en el body (ya soportado),
  o se añade `email` al detalle. Decisión de `ne-express`, fuera de este trabajo.
- **Botón backend**: un `action_l10n_pe_email_comprobante` + botón en
  `views/account_move_views.xml` es trivial de sumar después.

## 10. Referencias de código

- Patrón de correo existente: `models/res_users.py:120` (`_l10n_pe_ne_send_reset_email`).
- PDF SFS on-demand + caché: `models/account_move_biller.py:2712`.
- Parseo de CDR / aceptación: `models/account_move_biller.py:987`, `:1035`.
- Campos: `models/account_move_biller.py:82` (state), `:418` (xml), `:421` (cdr), `:424` (pdf).
- Helpers de controller: `controllers/main.py:97` (`_identify`), `:126` (`_move`), `:142`
  (`_body`), `:179` (`_run`), `:232` (`_serve_file`), `:624` (endpoint de descarga análogo).
- Manifiesto: `__manifest__.py:6` (`depends`), `:7-19` (`data`).
- Tests de referencia: `tests/test_send.py`, `tests/test_report_pdf.py`, `tests/test_password_reset.py`.

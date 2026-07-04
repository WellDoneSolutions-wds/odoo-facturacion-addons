# Envío de comprobante por correo — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Añadir al addon `l10n_pe_ne_biller` la capacidad de enviar un comprobante aceptado por correo (PDF + XML), disparable desde `ne-express` vía `POST /ne/api/comprobantes/<id>/email`.

**Architecture:** Se extiende el addon existente (cero addons nuevos). Un `mail.template` provee asunto/cuerpo; un método de modelo `account.move.l10n_pe_ne_email_comprobante` valida la precondición (CDR aceptado), resuelve el destinatario, adjunta el PDF SFS (reusando `_l10n_pe_get_pdf_attachment`) + el XML firmado, y envía por la plantilla; un endpoint REST thin delega al método siguiendo el patrón `_run(...)` del resto del controller.

**Tech Stack:** Python, Odoo 19, framework de tests de Odoo (`TransactionCase` / `HttpCase`), `mail.template`/`mail.mail`, `unittest.mock`.

## Global Constraints

- Odoo **19.0**; todo el trabajo ocurre dentro de `addons/l10n_pe_ne_biller/`. Cero addons nuevos, cero cambios en `ne-express`.
- El PDF adjunto debe ser la **representación impresa SFS** que genera `ms-ne-biller` (`POST /report/pdf`, vía `_l10n_pe_get_pdf_attachment`). **Nunca** el reporte QWeb nativo de Odoo ni `account.move.send`.
- Alcance: **solo `account.move`** (factura/boleta/NC/ND). Sin otro-CPE (retención/percepción).
- Adjuntos por defecto: **PDF + XML firmado**. No se adjunta el CDR.
- Precondición estricta para enviar: `l10n_pe_biller_state == "enviado"` **y** existe `l10n_pe_biller_cdr` **y** su `ResponseCode == "0"`.
- Los controllers permanecen **thin**: delegan al modelo (lógica de negocio en el modelo), auth por API-key Bearer vía `_identify()`, aislamiento por RUC vía `_move(uid)` (`with_user` + `with_company`).
- Términos de dominio en español, sin traducir (comprobante, factura, boleta, CDR, RUC, SUNAT). Mensajes de error legibles con `UserError` + `_()`.

## Cómo correr los tests (referencia)

Todos los tests de este plan están en `addons/l10n_pe_ne_biller/tests/test_email.py`, tag `post_install`. Comando base (desde la raíz del repo `odoo-facturacion-addons`, con Odoo en el `$PATH` y la DB `odoo_ne_biller` ya inicializada):

```bash
odoo-bin -c config/odoo-community.conf -d odoo_ne_biller \
  -u l10n_pe_ne_biller --test-enable --stop-after-init --no-http \
  --test-tags '/l10n_pe_ne_biller:<Clase>.<metodo>'
```

- `-u l10n_pe_ne_biller` reinstala/actualiza el módulo para recargar código y data XML nuevos.
- **FAIL esperado (rojo):** el log termina con `FAILED` / traceback / `1 failed, 0 error`.
- **PASS esperado (verde):** el log muestra `0 failed, 0 error(s)` para los tests filtrados.

---

### Task 1: Precondición `_l10n_pe_ne_is_aceptado` + andamiaje de tests

Helper puro de modelo que re-deriva la aceptación desde el CDR guardado (no depende del texto de `l10n_pe_biller_message`). Este task crea el archivo de tests con su `setUp` (que emite un comprobante aceptado mockeando la red) — base para los tasks 3 y 4.

**Files:**
- Create: `addons/l10n_pe_ne_biller/tests/test_email.py`
- Modify: `addons/l10n_pe_ne_biller/tests/__init__.py` (registrar el nuevo test module)
- Modify: `addons/l10n_pe_ne_biller/models/account_move_biller.py` (añadir el helper; junto a los métodos de descarga/PDF, cerca de `_l10n_pe_get_pdf_attachment` en `:2712`)

**Interfaces:**
- Consumes: `action_l10n_pe_send_to_biller()`, `_l10n_pe_parse_cdr_codes(bytes) -> (code, desc)` (`:987`), campos `l10n_pe_biller_state` (`:82`), `l10n_pe_biller_cdr` (`:421`), `l10n_pe_biller_xml` (`:418`), `l10n_pe_ne_serie_emit` (`:119`).
- Produces: `account.move._l10n_pe_ne_is_aceptado() -> bool`. El `setUp` de `TestBillerEmail` deja `self.move` en estado `enviado` con XML + CDR (ResponseCode 0) y `self.move.company_id.email` seteado; helpers de test `_fake_cdr_b64(code, desc)` y `_pdf_resp()`.

- [ ] **Step 1: Registrar el test module**

En `addons/l10n_pe_ne_biller/tests/__init__.py`, añadir al final:

```python
from . import test_email
```

- [ ] **Step 2: Escribir el test file con setUp y los tests del helper (fallará)**

Crear `addons/l10n_pe_ne_biller/tests/test_email.py`:

```python
import base64
import io
import zipfile
from unittest.mock import patch

from odoo.tests import TransactionCase, tagged
from odoo.exceptions import UserError

_TARGET = 'odoo.addons.l10n_pe_ne_biller.models.account_move_biller.requests.post'


def _fake_cdr_b64(code='0', desc='La Factura F001-1 ha sido aceptada'):
    xml = ('<?xml version="1.0"?><ApplicationResponse '
           'xmlns:cbc="urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2">'
           '<cbc:ResponseCode>%s</cbc:ResponseCode><cbc:Description>%s</cbc:Description>'
           '</ApplicationResponse>' % (code, desc))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as zf:
        zf.writestr('R20605145648-01-F001-1.xml', xml)
    return base64.b64encode(buf.getvalue()).decode()


@tagged('post_install', '-at_install')
class TestBillerEmail(TransactionCase):
    def setUp(self):
        super().setUp()
        igv = self.env['account.tax'].search([
            ('company_id', '=', self.env.company.id), ('type_tax_use', '=', 'sale'),
            ('l10n_pe_edi_tax_code', '=', '1000')], limit=1)
        ruc_type = self.env['l10n_latam.identification.type'].search(
            [('l10n_pe_vat_code', '=', '6')], limit=1)
        partner = self.env['res.partner'].create({
            'name': 'CLIENTE SAC', 'vat': '20605145648',
            'l10n_latam_identification_type_id': ruc_type.id})
        product = self.env['product.product'].create(
            {'name': 'DESARMADOR', 'default_code': 'P001'})
        self.move = self.env['account.move'].create({
            'move_type': 'out_invoice', 'partner_id': partner.id, 'invoice_date': '2026-06-19',
            'l10n_pe_serie': 'F001', 'l10n_pe_correlativo': '1',
            'invoice_line_ids': [(0, 0, {'product_id': product.id, 'quantity': 1.0,
                                         'price_unit': 7.20, 'tax_ids': [(6, 0, igv.ids)]})]})
        self.move.action_post()
        # email del emisor válido para email_from = company_id.email_formatted
        self.move.company_id.email = 'emisor@example.com'
        # Emitir aceptado: mockea la red (XML firmado + CDR ResponseCode 0 en header).
        signed = self._resp(
            200, '<?xml version="1.0"?><Invoice xmlns="urn:x"><ext:UBLExtensions/></Invoice>',
            headers={'X-Sunat-Cdr': _fake_cdr_b64('0', 'aceptada')})
        with patch(_TARGET, return_value=signed):
            self.move.action_l10n_pe_send_to_biller()

    def _resp(self, code, text, headers=None):
        return type('R', (), {'status_code': code, 'text': text, 'headers': headers or {}})()

    def _pdf_resp(self):
        return type('R', (), {'status_code': 200, 'content': b'%PDF-1.4 test', 'text': ''})()

    # ---- helper de precondición
    def test_is_aceptado_true_con_cdr_0(self):
        self.assertEqual(self.move.l10n_pe_biller_state, 'enviado')
        self.assertTrue(self.move._l10n_pe_ne_is_aceptado())

    def test_is_aceptado_false_sin_cdr(self):
        self.move.l10n_pe_biller_cdr = False
        self.assertFalse(self.move._l10n_pe_ne_is_aceptado())

    def test_is_aceptado_false_estado_no_enviado(self):
        self.move.l10n_pe_biller_state = 'rechazado'
        self.assertFalse(self.move._l10n_pe_ne_is_aceptado())
```

- [ ] **Step 3: Correr los tests para verificar que fallan**

Run:
```bash
odoo-bin -c config/odoo-community.conf -d odoo_ne_biller \
  -u l10n_pe_ne_biller --test-enable --stop-after-init --no-http \
  --test-tags '/l10n_pe_ne_biller:TestBillerEmail'
```
Expected: FAIL — `AttributeError: 'account.move' object has no attribute '_l10n_pe_ne_is_aceptado'`.

- [ ] **Step 4: Implementar el helper**

En `addons/l10n_pe_ne_biller/models/account_move_biller.py`, junto a `_l10n_pe_get_pdf_attachment` (después de la línea `:2756`, dentro de la clase `account.move`):

```python
    def _l10n_pe_ne_is_aceptado(self):
        """True solo si el comprobante fue aceptado por SUNAT: estado 'enviado',
        con CDR guardado y ResponseCode 0. Re-parsea el CDR (no confía en el texto
        de l10n_pe_biller_message)."""
        self.ensure_one()
        if self.l10n_pe_biller_state != "enviado" or not self.l10n_pe_biller_cdr:
            return False
        code, _desc = self._l10n_pe_parse_cdr_codes(self.l10n_pe_biller_cdr.raw or b"")
        return code == "0"
```

- [ ] **Step 5: Correr los tests para verificar que pasan**

Run:
```bash
odoo-bin -c config/odoo-community.conf -d odoo_ne_biller \
  -u l10n_pe_ne_biller --test-enable --stop-after-init --no-http \
  --test-tags '/l10n_pe_ne_biller:TestBillerEmail'
```
Expected: PASS — `0 failed, 0 error(s)`.

- [ ] **Step 6: Commit**

```bash
git add addons/l10n_pe_ne_biller/tests/test_email.py \
        addons/l10n_pe_ne_biller/tests/__init__.py \
        addons/l10n_pe_ne_biller/models/account_move_biller.py
git commit -m "feat(l10n_pe_ne_biller): helper _l10n_pe_ne_is_aceptado (CDR ResponseCode 0)"
```

---

### Task 2: Plantilla de correo `mail.template` + manifest

Registra la plantilla y declara `mail` como dependencia explícita.

**Files:**
- Create: `addons/l10n_pe_ne_biller/data/l10n_pe_ne_mail_template.xml`
- Modify: `addons/l10n_pe_ne_biller/__manifest__.py:6` (`depends`) y `:7-19` (`data`)
- Modify: `addons/l10n_pe_ne_biller/tests/test_email.py` (añadir test de existencia)

**Interfaces:**
- Consumes: modelo `account.move`.
- Produces: registro `mail.template` con xml_id `l10n_pe_ne_biller.mail_template_comprobante` (model `account.move`), referenciable con `self.env.ref(...)`.

- [ ] **Step 1: Escribir el test de existencia (fallará)**

Añadir al final de `addons/l10n_pe_ne_biller/tests/test_email.py`:

```python
    def test_mail_template_existe(self):
        tmpl = self.env.ref(
            'l10n_pe_ne_biller.mail_template_comprobante', raise_if_not_found=False)
        self.assertTrue(tmpl, "La plantilla de correo debe existir")
        self.assertEqual(tmpl.model_id.model, 'account.move')
```

- [ ] **Step 2: Correr el test para verificar que falla**

Run:
```bash
odoo-bin -c config/odoo-community.conf -d odoo_ne_biller \
  -u l10n_pe_ne_biller --test-enable --stop-after-init --no-http \
  --test-tags '/l10n_pe_ne_biller:TestBillerEmail.test_mail_template_existe'
```
Expected: FAIL — el `assertTrue(tmpl)` falla (la ref no existe).

- [ ] **Step 3: Crear la plantilla**

Crear `addons/l10n_pe_ne_biller/data/l10n_pe_ne_mail_template.xml`:

```xml
<?xml version="1.0" encoding="utf-8"?>
<odoo>
    <record id="mail_template_comprobante" model="mail.template">
        <field name="name">Comprobante electrónico — envío al cliente</field>
        <field name="model_id" ref="account.model_account_move"/>
        <field name="subject">Comprobante {{ object.l10n_pe_ne_serie_emit }}-{{ object.l10n_pe_ne_corr_emit }} — {{ object.company_id.name }}</field>
        <field name="email_from">{{ object.company_id.email_formatted }}</field>
        <field name="email_to">{{ object.partner_id.email }}</field>
        <field name="auto_delete" eval="False"/>
        <field name="body_html" type="html">
            <div style="font-family:sans-serif;font-size:14px;color:#111">
                <p>Estimado(a) <t t-out="object.partner_id.name or ''"/>,</p>
                <p>Adjuntamos su comprobante electrónico
                    <b><t t-out="object.l10n_pe_ne_serie_emit or ''"/>-<t t-out="object.l10n_pe_ne_corr_emit or ''"/></b>
                    por un total de
                    <b><t t-out="object.currency_id.symbol or ''"/> <t t-out="'%.2f' % (object.amount_total or 0.0)"/></b>.</p>
                <p>Encontrará el PDF (representación impresa) y el XML firmado como archivos adjuntos.</p>
                <p style="color:#666;font-size:12px">Este es un mensaje automático de <t t-out="object.company_id.name or ''"/>.</p>
            </div>
        </field>
    </record>
</odoo>
```

- [ ] **Step 4: Declarar `mail` en depends y el data file en el manifest**

En `addons/l10n_pe_ne_biller/__manifest__.py`, cambiar la línea `depends`:

```python
    'depends': ['l10n_pe', 'account', 'uom', 'mail'],
```

y añadir el data file dentro de la lista `data` (después de `'data/l10n_pe_ne_cron.xml',`):

```python
        'data/l10n_pe_ne_mail_template.xml',
```

- [ ] **Step 5: Correr el test para verificar que pasa**

Run:
```bash
odoo-bin -c config/odoo-community.conf -d odoo_ne_biller \
  -u l10n_pe_ne_biller --test-enable --stop-after-init --no-http \
  --test-tags '/l10n_pe_ne_biller:TestBillerEmail.test_mail_template_existe'
```
Expected: PASS — `0 failed, 0 error(s)`.

- [ ] **Step 6: Commit**

```bash
git add addons/l10n_pe_ne_biller/data/l10n_pe_ne_mail_template.xml \
        addons/l10n_pe_ne_biller/__manifest__.py \
        addons/l10n_pe_ne_biller/tests/test_email.py
git commit -m "feat(l10n_pe_ne_biller): mail.template para envío de comprobante + depends mail"
```

---

### Task 3: Método `l10n_pe_ne_email_comprobante`

El corazón de la feature: valida precondición, resuelve destinatario, adjunta PDF+XML y envía por la plantilla.

**Files:**
- Modify: `addons/l10n_pe_ne_biller/models/account_move_biller.py` (añadir el método junto al helper de Task 1)
- Modify: `addons/l10n_pe_ne_biller/tests/test_email.py` (añadir los tests del método)

**Interfaces:**
- Consumes: `_l10n_pe_ne_is_aceptado()` (Task 1), `_l10n_pe_get_pdf_attachment() -> ir.attachment` (`:2712`), campo `l10n_pe_biller_xml`, `self.env.ref('l10n_pe_ne_biller.mail_template_comprobante')` (Task 2), `message_post` (de `mail.thread`).
- Produces: `account.move.l10n_pe_ne_email_comprobante(to=None, cc=None) -> {"ok": True, "to": <str>}`. Crea un `mail.mail` (via `template.send_mail`, `force_send=True`) con 2 `attachment_ids` (pdf `application/pdf`, xml `application/xml`).

- [ ] **Step 1: Escribir los tests del método (fallarán)**

Añadir al final de `addons/l10n_pe_ne_biller/tests/test_email.py`:

```python
    def _find_mail(self):
        return self.env['mail.mail'].search(
            [('model', '=', 'account.move'), ('res_id', '=', self.move.id)],
            order='id desc', limit=1)

    def test_rechaza_sin_cdr_aceptado(self):
        self.move.l10n_pe_biller_cdr = False
        with self.assertRaises(UserError):
            self.move.l10n_pe_ne_email_comprobante(to='a@b.com')

    def test_rechaza_sin_destinatario(self):
        self.move.partner_id.email = False
        with self.assertRaises(UserError):
            self.move.l10n_pe_ne_email_comprobante()

    def test_usa_email_del_partner(self):
        self.move.partner_id.email = 'cliente@example.com'
        with patch(_TARGET, return_value=self._pdf_resp()):
            res = self.move.l10n_pe_ne_email_comprobante()
        self.assertEqual(res, {'ok': True, 'to': 'cliente@example.com'})
        self.assertIn('cliente@example.com', self._find_mail().email_to or '')

    def test_override_destinatario(self):
        self.move.partner_id.email = 'cliente@example.com'
        with patch(_TARGET, return_value=self._pdf_resp()):
            res = self.move.l10n_pe_ne_email_comprobante(to='otro@dest.com')
        self.assertEqual(res['to'], 'otro@dest.com')

    def test_adjunta_pdf_y_xml(self):
        self.move.partner_id.email = 'cliente@example.com'
        with patch(_TARGET, return_value=self._pdf_resp()) as mp:
            self.move.l10n_pe_ne_email_comprobante()
            self.move.l10n_pe_ne_email_comprobante()  # 2a vez: reusa PDF cacheado
        self.assertEqual(mp.call_count, 1, "El PDF cacheado no debe volver a pedirse al micro")
        mail = self._find_mail()
        mimetypes = mail.attachment_ids.mapped('mimetype')
        self.assertEqual(len(mail.attachment_ids), 2)
        self.assertIn('application/pdf', mimetypes)
        self.assertIn('application/xml', mimetypes)

    def test_envia_mail_con_asunto(self):
        self.move.partner_id.email = 'cliente@example.com'
        with patch(_TARGET, return_value=self._pdf_resp()):
            self.move.l10n_pe_ne_email_comprobante()
        mail = self._find_mail()
        self.assertTrue(mail)
        self.assertIn(self.move.l10n_pe_ne_serie_emit, mail.subject or '')
```

- [ ] **Step 2: Correr los tests para verificar que fallan**

Run:
```bash
odoo-bin -c config/odoo-community.conf -d odoo_ne_biller \
  -u l10n_pe_ne_biller --test-enable --stop-after-init --no-http \
  --test-tags '/l10n_pe_ne_biller:TestBillerEmail'
```
Expected: FAIL — `AttributeError: 'account.move' object has no attribute 'l10n_pe_ne_email_comprobante'`.

- [ ] **Step 3: Implementar el método**

En `addons/l10n_pe_ne_biller/models/account_move_biller.py`, inmediatamente después de `_l10n_pe_ne_is_aceptado` (Task 1):

```python
    def l10n_pe_ne_email_comprobante(self, to=None, cc=None):
        """Envía el comprobante aceptado al cliente por correo, adjuntando el PDF
        (representación impresa SFS) y el XML firmado, vía la plantilla
        l10n_pe_ne_biller.mail_template_comprobante."""
        self.ensure_one()
        if not self._l10n_pe_ne_is_aceptado():
            raise UserError(
                _("El comprobante no está aceptado por SUNAT; no se puede enviar.")
            )
        to = (to or self.partner_id.email or "").strip()
        if not to:
            raise UserError(
                _("El cliente no tiene correo configurado; indica un destinatario.")
            )
        pdf = self._l10n_pe_get_pdf_attachment()
        xml = self.l10n_pe_biller_xml
        template = self.env.ref("l10n_pe_ne_biller.mail_template_comprobante")
        template.send_mail(
            self.id,
            force_send=True,
            email_values={
                "email_to": to,
                "email_cc": cc or "",
                "attachment_ids": [(6, 0, [pdf.id, xml.id])],
            },
        )
        self.message_post(body=_("Comprobante enviado por correo a %s") % to)
        return {"ok": True, "to": to}
```

- [ ] **Step 4: Correr los tests para verificar que pasan**

Run:
```bash
odoo-bin -c config/odoo-community.conf -d odoo_ne_biller \
  -u l10n_pe_ne_biller --test-enable --stop-after-init --no-http \
  --test-tags '/l10n_pe_ne_biller:TestBillerEmail'
```
Expected: PASS — `0 failed, 0 error(s)` (los 3 tests del helper + el de plantilla + los 6 del método).

- [ ] **Step 5: Commit**

```bash
git add addons/l10n_pe_ne_biller/models/account_move_biller.py \
        addons/l10n_pe_ne_biller/tests/test_email.py
git commit -m "feat(l10n_pe_ne_biller): l10n_pe_ne_email_comprobante (envía PDF+XML por correo)"
```

---

### Task 4: Endpoint REST `POST /ne/api/comprobantes/<id>/email`

Wrapper thin que expone el método a `ne-express`, con auth Bearer y aislamiento por RUC.

**Files:**
- Modify: `addons/l10n_pe_ne_biller/controllers/main.py` (añadir el endpoint junto a `file` en `:624`)
- Modify: `addons/l10n_pe_ne_biller/tests/test_email.py` (añadir la clase de tests de rutas)

**Interfaces:**
- Consumes: `_identify()` (`:97`), `_unauth()`, `_body()` (`:142`), `_move(uid)` (`:126`), `_run(func)` (`:179`), decorador `_POST` (`:57`), y `account.move.l10n_pe_ne_email_comprobante(to, cc)` (Task 3).
- Produces: ruta HTTP `POST /ne/api/comprobantes/<int:rec_id>/email` → JSON `{ok, to}`; 401 sin Bearer.

- [ ] **Step 1: Escribir el test de ruta (fallará)**

Añadir al final de `addons/l10n_pe_ne_biller/tests/test_email.py`:

```python
from odoo.tests import HttpCase


@tagged('post_install', '-at_install')
class TestBillerEmailRoutes(HttpCase):
    def test_email_requiere_auth(self):
        r = self.url_open('/ne/api/comprobantes/1/email', data='{}',
                          headers={'Content-Type': 'application/json'})
        self.assertEqual(r.status_code, 401)
```

- [ ] **Step 2: Correr el test para verificar que falla**

Run:
```bash
odoo-bin -c config/odoo-community.conf -d odoo_ne_biller \
  -u l10n_pe_ne_biller --test-enable --stop-after-init --no-http \
  --test-tags '/l10n_pe_ne_biller:TestBillerEmailRoutes'
```
Expected: FAIL — la ruta no existe todavía → responde 404 en vez de 401.

- [ ] **Step 3: Implementar el endpoint**

En `addons/l10n_pe_ne_biller/controllers/main.py`, después del método `file` (termina en `:634`) y antes de `otrocpe_file` (`:636`):

```python
    @http.route("/ne/api/comprobantes/<int:rec_id>/email", **_POST)
    def email_comprobante(self, rec_id, **kw):
        uid = self._identify()
        if not uid:
            return self._unauth()

        def op():
            b = self._body()
            return self._move(uid).browse(rec_id).l10n_pe_ne_email_comprobante(
                to=b.get("to"), cc=b.get("cc")
            )

        return self._run(op)
```

- [ ] **Step 4: Correr el test para verificar que pasa**

Run:
```bash
odoo-bin -c config/odoo-community.conf -d odoo_ne_biller \
  -u l10n_pe_ne_biller --test-enable --stop-after-init --no-http \
  --test-tags '/l10n_pe_ne_biller:TestBillerEmailRoutes'
```
Expected: PASS — `0 failed, 0 error(s)`.

- [ ] **Step 5: Correr TODA la suite del addon para confirmar que no se rompió nada**

Run:
```bash
odoo-bin -c config/odoo-community.conf -d odoo_ne_biller \
  -u l10n_pe_ne_biller --test-enable --stop-after-init --no-http \
  --test-tags '/l10n_pe_ne_biller'
```
Expected: PASS — `0 failed, 0 error(s)` en toda la suite (incluye `test_send`, `test_report_pdf`, `test_email`, etc.).

- [ ] **Step 6: Commit**

```bash
git add addons/l10n_pe_ne_biller/controllers/main.py \
        addons/l10n_pe_ne_biller/tests/test_email.py
git commit -m "feat(l10n_pe_ne_biller): endpoint POST /ne/api/comprobantes/<id>/email"
```

---

## Notas de integración (post-plan, fuera de código)

- **SMTP saliente**: para envío real se requiere un `ir.mail_server` configurado en Odoo (en dev/docker probablemente no existe). Los tests no envían de verdad (el framework de Odoo captura el correo), así que pasan sin SMTP.
- **Email por compañía**: `email_from` sale de `company_id.email_formatted`; cada RUC/compañía debe tener correo.
- **Consumo desde `ne-express`** (siguiente trabajo, no aquí): `POST /ne/api/comprobantes/<id>/email` con Bearer y body opcional `{ "to": "...", "cc": "..." }`.

## Self-Review (hecho al escribir el plan)

- **Cobertura del spec:** §4.1 plantilla → Task 2; §4.2 método + helper → Tasks 1 y 3; §4.3 endpoint → Task 4; §4.4 manifest → Task 2; §8 los 7 tests → repartidos (helper 3 + plantilla 1 + método 6 + ruta 1; nota: se añadieron 2 tests de helper extra sobre los del spec, mayor cobertura). Precondición estricta `code == "0"` → Task 1. Adjuntos PDF+XML → Task 3. Solo `account.move` → sin tocar `account.payment`.
- **Placeholders:** ninguno; todo el código está completo.
- **Consistencia de tipos/nombres:** `_l10n_pe_ne_is_aceptado()` (Task 1) usado en Task 3; `l10n_pe_ne_email_comprobante(to, cc)` (Task 3) usado en Task 4; xml_id `l10n_pe_ne_biller.mail_template_comprobante` idéntico en Tasks 2 y 3; helpers de test `_pdf_resp`/`_fake_cdr_b64`/`_find_mail` definidos antes de usarse.

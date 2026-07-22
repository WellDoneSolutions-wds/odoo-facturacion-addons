# -*- coding: utf-8 -*-
"""Harness E2E (Odoo -> ms-ne-biller -> SUNAT beta) para el addon l10n_pe_ne_biller.

Se ejecuta dentro de `odoo-bin shell`. Lee una lista de casos en formato canónico desde
E2E_CASES_FILE (JSON) y, por cada caso: construye el comprobante en el addon, dispara la acción de
envío real (que llama al microservicio y a SUNAT) y captura el resultado (estado + ResponseCode +
mensaje). Escribe los resultados en E2E_RESULTS_FILE. Cada caso corre en su propio savepoint para que
un fallo no contamine a los demás; al final hace rollback (los envíos a SUNAT son efecto externo, ya
ocurrieron; los registros Odoo no se persisten).

Formato canónico de un caso (ver e2e-test-plan-builder):
  {id, doc, title, partner, lines:[{tax,price,qty,discount,uom,bags}], flags:{...}, expected, source}
"""
import json
import os
import random

CASES_FILE = os.environ.get('E2E_CASES_FILE', '/tmp/e2e_cases.json')
RESULTS_FILE = os.environ.get('E2E_RESULTS_FILE', '/tmp/e2e_results.json')
# QW08: formatos de representación impresa a verificar contra el biller-pdf real (p.ej. 'TICKET').
PDF_FORMATS = [f.strip().upper() for f in os.environ.get('E2E_PDF_FORMATS', '').split(',') if f.strip()]

env = env  # provisto por odoo shell  # noqa: F821
company = env.company
API_KEY = 'dev-biller-key-20321856145'

# El emisor debe ser el tenant registrado; asegura RUC + api key.
if company.vat != '20321856145':
    company.write({'vat': '20321856145'})
company.sudo().l10n_pe_ne_api_key = API_KEY
# El emisor opera en zona horaria de Perú: las fechas (emisión, IssueDate de la baja/resumen) deben
# coincidir con la fecha de recepción de SUNAT (Perú, UTC-5). Sin esto, context_today puede adelantarse
# un día y SUNAT rechaza la baja/RC con 2301/2236 ("IssueDate mayor a la fecha de recepción").
env.user.tz = 'America/Lima'

# --- catálogo de taxes por categoría (cat_05) ---
TAX_CODE = {'gravado': '1000', 'exonerado': '9997', 'inafecto': '9998',
            'exportacion': '9995', 'gratuito': '9996', 'ivap': '1016'}


def _tax(code):
    return env['account.tax'].search([
        ('company_id', '=', company.id), ('type_tax_use', '=', 'sale'),
        ('l10n_pe_edi_tax_code', '=', code)], limit=1)


def _ensure_tax(code, name, amount):
    """Crea la tax (cat_05) si falta, con la tasa indicada. Usado para IVAP (1016) que no viene de fábrica."""
    t = _tax(code)
    if t:
        return t
    return env['account.tax'].create({
        'name': name, 'amount_type': 'percent', 'amount': amount,
        'type_tax_use': 'sale', 'company_id': company.id, 'l10n_pe_edi_tax_code': code})


def _ensure_isc_tax(sistema='01'):
    """Configura la tax ISC (2000) con tasa real, sistema ISC e include_base_amount (el IGV se computa
    sobre valor+ISC). sistema '01' = al valor (10%); '02' = monto fijo (S/ por unidad)."""
    t = _tax('2000')
    vals = {'l10n_pe_edi_isc_type': sistema, 'include_base_amount': True, 'type_tax_use': 'sale'}
    if sistema == '02':
        vals.update({'amount_type': 'fixed', 'amount': 5.0})
    else:
        vals.update({'amount_type': 'percent', 'amount': 10.0})
    if t:
        t.write(vals)
        return t
    return env['account.tax'].create(dict(vals, name='ISC', l10n_pe_edi_tax_code='2000', company_id=company.id))


def _partner(kind):
    Type = env['l10n_latam.identification.type']
    if kind == 'ruc':
        t = Type.search([('l10n_pe_vat_code', '=', '6')], limit=1)
        return env['res.partner'].create({'name': 'CLIENTE SAC', 'vat': '20100070970',
                                           'l10n_latam_identification_type_id': t.id})
    if kind == 'dni':
        t = Type.search([('l10n_pe_vat_code', '=', '1')], limit=1)
        return env['res.partner'].create({'name': 'CLIENTE DNI', 'vat': '12345678',
                                           'l10n_latam_identification_type_id': t.id})
    if kind == 'extranjero':
        # Exportación: el adquirente NO puede ser RUC (schemeID != 6). Catálogo 06: 4 carné ext / 7 pasaporte / 0 sin doc.
        t = (Type.search([('l10n_pe_vat_code', '=', '4')], limit=1)
             or Type.search([('l10n_pe_vat_code', '=', '7')], limit=1)
             or Type.search([('l10n_pe_vat_code', '=', '0')], limit=1))
        # País del adquirente (no domiciliado): alimenta codPaisCliente en la cabecera 0200.
        us = env.ref('base.us', raise_if_not_found=False)
        return env['res.partner'].create({'name': 'FOREIGN BUYER INC', 'vat': 'EXT0001',
                                           'country_id': us.id if us else False,
                                           'l10n_latam_identification_type_id': t.id if t else False})
    return env['res.partner'].create({'name': 'VARIOS'})  # consumidor final


def _uom(code):
    xmlid = {'NIU': 'uom.product_uom_unit', 'KGM': 'uom.product_uom_kgm',
             'ZZ': 'uom.product_uom_unit', 'MTR': 'uom.product_uom_meter',
             'LTR': 'uom.product_uom_litre'}.get(code or 'NIU', 'uom.product_uom_unit')
    try:
        return env.ref(xmlid)
    except Exception:
        return env.ref('uom.product_uom_unit')


def _line_vals(line):
    prod = env['product.product'].create({'name': 'ITEM ' + (line.get('tax') or 'gravado'),
                                          'default_code': 'P' + str(random.randint(1000, 9999))})
    taxes = []
    tax_kind = line.get('tax', 'gravado')
    if tax_kind == 'ivap':
        t = _ensure_tax('1016', 'IVAP 4%', 4.0)   # IVAP arroz pilado: no viene de fábrica
        if t:
            taxes.append(t.id)
    elif tax_kind in TAX_CODE:
        t = _tax(TAX_CODE[tax_kind])
        if t:
            taxes.append(t.id)
    elif tax_kind in ('isc', 'icbper'):
        igv = _tax('1000')
        if igv:
            taxes.append(igv.id)
        if tax_kind == 'isc':
            extra = _ensure_isc_tax('02' if line.get('isc_type') == '02' else '01')
        else:
            extra = _tax('7152')
        if extra:
            taxes.append(extra.id)
    vals = {'product_id': prod.id, 'quantity': float(line.get('qty', 1.0)),
            'price_unit': float(line.get('price', 100.0)),
            'discount': float(line.get('discount', 0) or 0),
            'tax_ids': [(6, 0, taxes)]}
    if line.get('uom'):
        vals['product_uom_id'] = _uom(line['uom']).id
    if tax_kind == 'icbper' and line.get('bags'):
        vals['quantity'] = float(line['bags'])
    return (0, 0, vals)


def _apply_flags(move, flags):
    f = flags or {}
    if f.get('serie'):
        move.l10n_pe_serie = f['serie']
    move.l10n_pe_correlativo = str(random.randint(40000, 99000))
    if f.get('detraccion'):
        d = f['detraccion']
        company.l10n_pe_ne_cuenta_detraccion = company.l10n_pe_ne_cuenta_detraccion or '00-000-000000'
        move.l10n_pe_ne_detraccion = True
        move.l10n_pe_ne_detraccion_code = d.get('code', '037')
        move.l10n_pe_ne_detraccion_rate = float(d.get('rate', 12.0))
        if d.get('medio_pago'):
            move.l10n_pe_ne_detraccion_medio_pago = d['medio_pago']
    if f.get('percepcion'):
        move.l10n_pe_ne_percepcion = True
    if f.get('es_anticipo'):
        move.l10n_pe_ne_es_anticipo = True   # doc. A: comprobante emitido POR un pago anticipado
    if f.get('anticipo'):
        a = f['anticipo']
        move.l10n_pe_ne_anticipo_total = float(a.get('total', 0))
        move.l10n_pe_ne_anticipo_doc = a.get('doc', 'F001-00000100')
        if a.get('tipo'):
            move.l10n_pe_ne_anticipo_tipo = a['tipo']
    if f.get('motivo'):
        move.l10n_pe_motivo_code = f['motivo']


def _send_move(move):
    move.action_l10n_pe_send_to_biller()
    return move.l10n_pe_biller_state, (move.l10n_pe_biller_message or '')


def _build_factura(case):
    partner = _partner(case.get('partner', 'ruc'))
    move = env['account.move'].create({
        'move_type': 'out_invoice', 'partner_id': partner.id,
        'invoice_date': fields_today(),  # noqa: F821
        'invoice_line_ids': [_line_vals(l) for l in case['lines']]})
    _apply_flags(move, case.get('flags'))
    move.action_post()
    return move


def _build_nota(case, refund):
    """NC (refund=True) o ND a partir de una factura origen enviada."""
    origin = _build_factura({'partner': case.get('partner', 'ruc'), 'lines': case['lines'], 'flags': {}})
    _send_move(origin)
    if refund:
        move = env['account.move'].create({
            'move_type': 'out_refund', 'partner_id': origin.partner_id.id,
            'invoice_date': fields_today(), 'reversed_entry_id': origin.id,  # noqa: F821
            'invoice_line_ids': [_line_vals(l) for l in case['lines']]})
    else:
        move = env['account.move'].create({
            'move_type': 'out_invoice', 'partner_id': origin.partner_id.id,
            'invoice_date': fields_today(), 'debit_origin_id': origin.id,  # noqa: F821
            'invoice_line_ids': [_line_vals(l) for l in case['lines']]})
    _apply_flags(move, case.get('flags'))
    move.action_post()
    return move


def _emitir_doc(case):
    """Emite el comprobante que luego se anula (RA/RC). Infiere el tipo del id del caso."""
    cid = case['id'].upper()
    f = case.get('flags', {})
    if case['doc'] == 'rc':
        c = {'doc': 'boleta', 'partner': ('none' if case.get('partner') == 'none' else 'dni'),
             'lines': case['lines'], 'flags': {'serie': f.get('serie', 'B001')}}
        return _build_factura(c)
    if 'NC' in cid:
        return _build_nota({'partner': 'ruc', 'lines': case['lines'],
                            'flags': {'serie': f.get('serie', 'FC01'), 'motivo': f.get('motivo', '01')}}, refund=True)
    if 'ND' in cid:
        return _build_nota({'partner': 'ruc', 'lines': case['lines'],
                            'flags': {'serie': f.get('serie', 'FD01'), 'motivo': f.get('motivo', '02')}}, refund=False)
    return _build_factura({'partner': 'ruc', 'lines': case['lines'], 'flags': {'serie': f.get('serie', 'F001')}})


def _build_and_anular(case):
    """Anulación: emite (y envía salvo no_enviado) el comprobante, luego dispara action_l10n_pe_send_baja
    (despacha RA factura/NC/ND o RC boleta). Soporta los negativos: sin motivo, fuera de plazo, no enviado."""
    from datetime import timedelta
    f = case.get('flags', {})
    move = _emitir_doc(case)
    if not f.get('no_enviado'):
        _send_move(move)
    if f.get('fuera_plazo'):
        move.invoice_date = fields_today() - timedelta(days=10)
    move.l10n_pe_ne_baja_motivo = f.get('baja_motivo', '')   # vacío => dispara la guarda de motivo
    move.action_l10n_pe_send_baja()
    return move.l10n_pe_biller_state, (move.l10n_pe_biller_message or '')


def _build_payment(case, kind):
    """Retención (pago saliente a proveedor con factura/s) o percepción (cobro entrante de cliente)."""
    Type = env['l10n_latam.identification.type']
    ruc_t = Type.search([('l10n_pe_vat_code', '=', '6')], limit=1)
    partner = env['res.partner'].create({'name': 'CONTRAPARTE SAC', 'vat': '20100070970',
                                         'l10n_latam_identification_type_id': ruc_t.id})
    prod = env['product.product'].create({'name': 'SERVICIO', 'default_code': 'SVC' + str(random.randint(100, 999))})
    mt = 'in_invoice' if kind == 'retencion' else 'out_invoice'
    doc_t = env['l10n_latam.document.type'].search(
        [('country_id.code', '=', 'PE'), ('code', '=', '01')], limit=1)
    docs = []
    for i, line in enumerate(case['lines']):
        vals = {'move_type': mt, 'partner_id': partner.id, 'invoice_date': fields_today(),
                'invoice_line_ids': [(0, 0, {'product_id': prod.id, 'quantity': 1.0,
                                             'price_unit': float(line.get('price', 1000.0)),
                                             'tax_ids': [(6, 0, _tax('1000').ids)]})]}
        if mt == 'in_invoice':   # factura de proveedor: exige tipo y número de documento
            if doc_t:
                vals['l10n_latam_document_type_id'] = doc_t.id
            vals['l10n_latam_document_number'] = 'F%03d-%08d' % (i + 1, random.randint(1, 99999999))
        b = env['account.move'].create(vals)
        b.action_post()
        docs.append(b)
    register = env['account.payment.register'].with_context(
        active_model='account.move', active_ids=[b.id for b in docs]).create({})
    pay = register._create_payments()[0]
    if kind == 'retencion':
        pay.l10n_pe_ret_correlativo = str(random.randint(100, 9999))
        pay.action_l10n_pe_send_retencion()
        return pay.l10n_pe_ret_state, (pay.l10n_pe_ret_message or '')
    pay.l10n_pe_per_correlativo = str(random.randint(100, 9999))
    pay.action_l10n_pe_send_percepcion()
    return pay.l10n_pe_per_state, (pay.l10n_pe_per_message or '')


def _run_anticipo_ciclo(case):
    """Ciclo real de pago por anticipo contra SUNAT beta: (A) emite el comprobante POR el pago
    anticipado ('PAGO ANTICIPADO', venta interna 0101) y (B) la venta final que lo regulariza,
    enlazada por origen_id (descuento global 04 + relacionado). Ambos deben salir aceptados
    (CDR ResponseCode 0), y el saldo del anticipo debe bajar tras aplicarlo."""
    import re as _re
    f = case.get('flags', {})
    partner = _partner(case.get('partner', 'ruc'))

    def _code(msg):
        m = _re.search(r'ResponseCode (\d+)', msg or '')
        return m.group(1) if m else ''

    def _acc(state, msg):
        return state in ('enviado', 'anulado') and (_code(msg) == '0' or 'aceptad' in (msg or '').lower())

    # (A) comprobante de anticipo
    a = env['account.move'].create({
        'move_type': 'out_invoice', 'partner_id': partner.id, 'invoice_date': fields_today(),
        'invoice_line_ids': [_line_vals(l) for l in case['lines']]})
    a.l10n_pe_serie = f.get('serie', 'F001')
    a.l10n_pe_correlativo = str(random.randint(40000, 99000))
    a.l10n_pe_ne_es_anticipo = True
    a.action_post()
    req_a = a._l10n_pe_build_invoice_request()
    des_ok = req_a['detalle'][0]['desItem'].startswith('PAGO ANTICIPADO')
    op_ok = req_a['cabecera']['tipOperacion'] == '0101'
    sa, ma = _send_move(a)
    saldo0 = a.l10n_pe_ne_anticipo_saldo

    # (B) venta final que regulariza el anticipo A (enlazada por origen_id)
    b = env['account.move'].create({
        'move_type': 'out_invoice', 'partner_id': partner.id, 'invoice_date': fields_today(),
        'invoice_line_ids': [_line_vals(l) for l in case.get('final_lines', case['lines'])]})
    b.l10n_pe_serie = f.get('serie_final', 'F001')
    b.l10n_pe_correlativo = str(random.randint(40000, 99000))
    b.l10n_pe_ne_anticipo_origen_id = a.id
    b.l10n_pe_ne_anticipo_total = float(f.get('aplicar') or a.amount_total)
    b.l10n_pe_ne_anticipo_doc = '%s-%s' % (a.l10n_pe_ne_serie_emit or a.l10n_pe_serie,
                                           a.l10n_pe_ne_corr_emit or '')
    b.l10n_pe_ne_anticipo_tipo = '02'
    b.action_post()
    sb, mb = _send_move(b)
    a.invalidate_recordset(['l10n_pe_ne_anticipo_saldo', 'l10n_pe_ne_anticipo_aplicado'])
    saldo1 = a.l10n_pe_ne_anticipo_saldo

    acc_a, acc_b = _acc(sa, ma), _acc(sb, mb)
    return {'id': case['id'], 'doc': 'anticipo_ciclo', 'title': case.get('title', ''),
            'docA': {'state': sa, 'code': _code(ma), 'accepted': bool(acc_a),
                     'desItem_pago_anticipado': des_ok, 'tipOperacion_0101': op_ok,
                     'msg': (ma or '')[:120]},
            'docB': {'state': sb, 'code': _code(mb), 'accepted': bool(acc_b),
                     'aplicado': b.l10n_pe_ne_anticipo_total, 'msg': (mb or '')[:120]},
            'saldo_antes': round(saldo0, 2), 'saldo_despues': round(saldo1, 2),
            'expected': case.get('expected', 'accepted'),
            'ok': bool(acc_a and acc_b and des_ok and op_ok and saldo1 < saldo0)}


def run_case(case):
    from odoo.exceptions import UserError, ValidationError
    if case.get('doc') == 'anticipo_ciclo':
        return _run_anticipo_ciclo(case)
    expected = case.get('expected', 'accepted')
    expect_reject = expected.startswith('rejected')
    doc = case['doc']
    _mv = None   # ref al move para verificar el ticket 80mm (solo factura/boleta; QW08)
    # Negativos de "nota con descuento": inyectar el descuento que el caso espera que sea rechazado.
    if 'DESCUENTO' in case['id'].upper():
        for ln in case['lines']:
            ln['discount'] = ln.get('discount') or 10
    try:
        if doc in ('factura', 'boleta'):
            if any(l.get('tax') == 'exportacion' for l in case['lines']):
                case['partner'] = 'extranjero'   # exportación exige adquirente no-RUC
            if doc == 'boleta' and case.get('partner', 'dni') == 'ruc':
                case['partner'] = 'dni'
            _mv = _build_factura(case)
            state, msg = _send_move(_mv)
        elif doc in ('nc', 'nd'):
            state, msg = _send_move(_build_nota(case, refund=(doc == 'nc')))
        elif doc in ('ra', 'rc'):
            state, msg = _build_and_anular(case)
        elif doc in ('retencion', 'percepcion'):
            state, msg = _build_payment(case, doc)
        else:
            return {'id': case['id'], 'doc': doc, 'skipped': 'no soportado'}
    except (UserError, ValidationError) as e:
        # Rechazo por una guarda del addon: para un caso negativo es el resultado esperado (ok).
        return {'id': case['id'], 'doc': doc, 'title': case.get('title', ''),
                'ok': bool(expect_reject), 'rejected': str(e)[:160], 'expected': expected,
                'state': 'guarda-addon'}
    import re as _re
    m = _re.search(r'ResponseCode (\d+)', msg or '')
    code = m.group(1) if m else ''
    accepted = (state in ('enviado', 'anulado')) and (code == '0' or 'aceptad' in (msg or '').lower())
    ok = (not accepted) if expect_reject else accepted   # negativo: ok si NO fue aceptado
    # QW08: ticket 80mm contra el biller-pdf real (solo boleta/factura aceptada). Diferible a staging
    # con E2E_PDF_FORMATS=TICKET; usa el XML firmado real → valida el pipeline XSL/QR/plantilla completo.
    ticket_ok = None
    if 'TICKET' in PDF_FORMATS and accepted and _mv is not None and doc in ('factura', 'boleta'):
        try:
            att = _mv._l10n_pe_get_pdf_attachment(formato='TICKET')
            ticket_ok = bool(att) and (att.raw or b'').startswith(b'%PDF') and att.name.endswith('-ticket.pdf')
        except Exception as exc:   # noqa: BLE001
            ticket_ok = 'error: %s' % exc
    return {'id': case['id'], 'doc': doc, 'title': case.get('title', ''),
            'state': state, 'code': code, 'ok': bool(ok), 'ticket_ok': ticket_ok,
            'expected': expected, 'msg': (msg or '')[:160]}


# odoo shell: fecha de hoy
from odoo import fields as _f


def fields_today():
    return _f.Date.context_today(env.user)


import time as _time
cases = json.load(open(CASES_FILE))
results = []
for _i, case in enumerate(cases):
    # SUNAT beta limita resúmenes (sendSummary: RA/RC) consecutivos rápidos con 401; espaciarlos.
    if _i and case.get('doc') in ('ra', 'rc'):
        _time.sleep(int(os.environ.get('E2E_SUMMARY_DELAY', '30')))
    try:
        with env.cr.savepoint():
            results.append(run_case(case))
    except Exception as e:
        results.append({'id': case.get('id', '?'), 'doc': case.get('doc'), 'ok': False,
                        'error': type(e).__name__ + ': ' + str(e)[:200]})
json.dump(results, open(RESULTS_FILE, 'w'), ensure_ascii=False)
print('E2E_DONE', len(results), 'casos')
env.cr.rollback()

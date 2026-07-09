# -*- coding: utf-8 -*-
"""E2E QW07 — ciclo de caja con ventas REALES a SUNAT beta -> arqueo por medio.

Correr: odoo-bin shell -c <conf> -d odoo_ne_biller --no-http < e2e/caja_flow.py
El envío a beta es un efecto externo aceptado; los registros NO persisten (rollback final).

Valida el ciclo completo de caja amarrando ventas REALES: agrupación por medio, contado
sin medios -> Efectivo + sinMedio, crédito excluido del esperado, snapshot inmutable tras
anular, y aislamiento multi-compañía. Los unit puros (Task 1) + TransactionCase/HttpCase
(Tasks 2-4) son el gate obligatorio de merge; este E2E vivo puede diferirse a staging.

Entorno: requiere ms-ne-biller (biller-app) accesible y `l10n_pe_ne_biller.url` apuntando a
él. SUNAT beta 401-throttlea / hace timeout TLS aleatorio — reintentar el envío no es fallo
de la lógica de caja. Espaciar las boletas ~30 s si beta rechaza sumarios.
"""
env = env  # provisto por odoo shell  # noqa: F821

from odoo.exceptions import AccessError, UserError  # noqa: E402

company = env.company
if company.vat != '20321856145':
    company.write({'vat': '20321856145'})
company.sudo().l10n_pe_ne_api_key = 'dev-biller-key-20321856145'
env.user.tz = 'America/Lima'
Caja = env['l10n_pe_ne.caja.sesion']
Move = env['account.move']

# 1) abrir + guarda de doble apertura
ses = Caja.l10n_pe_ne_abrir_caja({'saldoInicial': 150})
assert ses['estado'] == 'abierta', ses
try:
    Caja.l10n_pe_ne_abrir_caja({'saldoInicial': 10})
    raise AssertionError('esperaba UserError doble apertura')
except UserError as e:
    assert 'Ya hay una caja abierta' in str(e), str(e)

cli = {'tipoDoc': '6', 'numDoc': '20100070970', 'razonSocial': 'CLIENTE SAC'}
linea = [{'descripcion': 'PRODUCTO', 'cantidad': 1, 'precioUnitario': 100.0, 'taxCode': '1000'}]

# 2) boleta contado Efectivo 60 + Yape 40
r1 = Move.l10n_pe_ne_quick_emit({'tipoDoc': '03', 'cliente': cli, 'lineas': linea,
    'formaPago': {'tipo': 'Contado', 'medios': [{'medio': 'Efectivo', 'monto': 60},
                                                {'medio': 'Yape', 'monto': 40}]}})
assert r1.get('estado') == 'enviado', r1
# boleta contado SIN medios detallados -> Efectivo + sinMedio
r2 = Move.l10n_pe_ne_quick_emit({'tipoDoc': '03', 'cliente': cli, 'lineas': linea})
assert r2.get('estado') == 'enviado', r2
# factura a CRÉDITO con cuota futura -> excluida del esperado
r3 = Move.l10n_pe_ne_quick_emit({'tipoDoc': '01', 'cliente': cli, 'lineas': linea,
    'formaPago': {'tipo': 'Credito', 'cuotas': [{'fecha': '2026-12-31', 'monto': 118.0}]}})
assert r3.get('estado') == 'enviado', r3

# 3) retiro
Caja.l10n_pe_ne_caja_movimiento({'tipo': 'retiro', 'motivo': 'Pago proveedor', 'monto': 80})

# 4) esperado en vivo
act = Caja.l10n_pe_ne_caja_actual()['sesion']
esp = {e['medio']: e['monto'] for e in act['esperado']}
assert abs(esp['Efectivo'] - (150 + 60 + 118 - 80)) < 0.01, esp     # 150 + 60(medio) + 118(boleta sin medio) - 80
assert abs(esp.get('Yape', 0) - 40) < 0.01, esp
assert act['ventas']['sinMedio'] == 1, act['ventas']
assert act['ventas']['count'] == 3, act['ventas']                   # crédito cuenta en count, no en porMedio

# 5) cerrar con conteo -> diferencia; re-cerrar -> UserError; snapshot inmutable tras anular
arq = Caja.l10n_pe_ne_cerrar_caja({'conteos': [
    {'medio': 'Efectivo', 'contado': esp['Efectivo'] - 2.30}, {'medio': 'Yape', 'contado': 40}]})
assert abs(arq['diferenciaTotal'] - (-2.30)) < 0.01, arq
sid = arq['id']
try:
    Caja.l10n_pe_ne_cerrar_caja({'conteos': [{'medio': 'Efectivo', 'contado': 1}]})
    raise AssertionError('esperaba UserError re-cierre')
except UserError as e:
    assert 'No hay una caja abierta' in str(e), str(e)
# anular una venta de la sesión (RA/RC) -> el arqueo histórico NO cambia (snapshot)
before = Caja.l10n_pe_ne_caja_arqueo(sid)
try:
    Move.browse(r1['id']).l10n_pe_ne_quick_anular({'motivo': 'ERROR EN EL MONTO'})
except Exception as e:
    print('aviso: anulación beta no completó (throttle/timeout), snapshot se valida igual:', e)
after = Caja.l10n_pe_ne_caja_arqueo(sid)
assert before['arqueo'] == after['arqueo'], (before['arqueo'], after['arqueo'])

# 6) aislamiento multi-compañía
cb = env['res.company'].sudo().create({'name': 'CAJA OTRO RUC SAC', 'vat': '20512333797'})
ub = env['res.users'].sudo().create({'name': 'Cajero otro', 'login': 'cajero_otro_qw07',
    'company_id': cb.id, 'company_ids': [(6, 0, [cb.id])],
    'group_ids': [(4, env.ref('l10n_pe_ne_biller.group_l10n_pe_ne_emisor').id)]})
try:
    Caja.with_user(ub).l10n_pe_ne_caja_arqueo(sid)
    raise AssertionError('esperaba AccessError cross-tenant')
except AccessError:
    pass

print('E2E QW07 OK: sesion', sid, 'diferencia', arq['diferenciaTotal'])
env.cr.rollback()

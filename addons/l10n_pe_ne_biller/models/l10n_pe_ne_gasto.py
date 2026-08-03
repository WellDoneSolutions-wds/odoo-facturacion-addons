# -*- coding: utf-8 -*-
"""Gasto simple (NE Express) — egreso del negocio (efectivo/Yape/banco), estilo POS.

Modelo propio de Odoo: TODA la lógica (CRUD + serialización) vive en el addon; React
solo llama. Aislado por compañía (regla multi-compañía en security). Alimenta la
utilidad neta del dashboard ('Con gastos').

C3 — el gasto que se paga DEL CAJÓN mueve la caja. Hasta aquí el gasto y la caja eran dos
libros que no se hablaban: el cajero pagaba S/ 50 de gaseosas con la plata del cajón, lo
registraba como gasto, y el arqueo seguía esperando esos S/ 50 — al cerrar le faltaba dinero
sin que faltara nada. El único egreso que la caja veía era el 'retiro' de su propia pantalla,
así que el cajero honesto tenía que registrar DOS veces la misma plata (gasto + retiro) y
acordarse de hacerlo. Ahora lo declara UNA vez y el sistema mueve los dos libros."""
import calendar
import logging

from odoo import _, api, fields, models
from odoo.exceptions import UserError

from ..tools.caja_arqueo import normalizar_medio

_logger = logging.getLogger(__name__)


class L10nPeNeGasto(models.Model):
    _name = 'l10n_pe_ne.gasto'
    _description = 'Gasto (NE Express)'
    _order = 'fecha desc, id desc'

    fecha = fields.Date(string='Fecha', required=True, default=fields.Date.context_today)
    descripcion = fields.Char(string='Descripción', required=True)
    cuenta = fields.Char(string='Cuenta / Medio', default='Efectivo',
                         help="Medio del egreso: Efectivo, Yape, Plin, BCP, Interbank, etc.")
    monto = fields.Monetary(string='Monto', required=True, currency_field='currency_id')
    currency_id = fields.Many2one('res.currency', required=True,
                                  default=lambda s: s.env.company.currency_id)
    company_id = fields.Many2one('res.company', required=True, index=True,
                                 default=lambda s: s.env.company)
    # D-3 (integridad): quién lo registró. Se puebla solo al crear; los gastos históricos
    # caen al create_uid en el dict (ver _l10n_pe_ne_gasto_dict).
    usuario_id = fields.Many2one('res.users', string='Usuario', index=True,
                                 default=lambda s: s.env.user)
    # D-2 (integridad): el gasto es APPEND-ONLY. Corregir = registrar un contra-asiento
    # (un gasto en negativo que apunta al original vía este campo), nunca editar/borrar.
    gasto_reversado_id = fields.Many2one('l10n_pe_ne.gasto', string='Reversa a',
                                         index=True, ondelete='set null', copy=False)
    # C3: ¿esta plata salió del cajón (de la caja abierta) o por otra vía (banco, tarjeta
    # personal, cuenta del dueño)? Lo elige el usuario, gasto por gasto, porque el sistema no
    # tiene forma de saberlo: el mismo negocio paga el agua por transferencia y las gaseosas con
    # el efectivo del mostrador. Default False (ver l10n_pe_ne_gasto_de_caja en res.company):
    # los gastos de hoy no tocan caja y no pueden empezar a tocarla sin que nadie lo pida.
    paga_caja = fields.Boolean(string='Se pagó del cajón (sale de la caja)')
    # Movimiento(s) de caja que este gasto generó. Es One2many y no un M2o al revés para no
    # duplicar la relación: el movimiento ya apunta al gasto (l10n_pe_ne.caja.movimiento.gasto_id),
    # que es quien tiene que explicar de dónde salió. En la práctica es 0 o 1.
    movimiento_ids = fields.One2many('l10n_pe_ne.caja.movimiento', 'gasto_id',
                                     string='Movimientos de caja')

    # Campos de negocio que, una vez creado el gasto, no se pueden reescribir (D-2). paga_caja
    # entra: cambiarlo después dejaría un gasto diciendo que salió del cajón sin su movimiento
    # (o al revés), que es justamente el descuadre que esta rebanada vino a cerrar.
    _CAMPOS_INMUTABLES = ('monto', 'descripcion', 'fecha', 'cuenta', 'currency_id', 'paga_caja')

    def _l10n_pe_ne_gasto_dict(self):
        self.ensure_one()
        mv = self.movimiento_ids[:1]
        return {
            'id': self.id,
            'fecha': self.fecha.strftime('%Y-%m-%d') if self.fecha else '',
            'descripcion': self.descripcion or '',
            'cuenta': self.cuenta or '',
            'monto': self.monto or 0.0,
            'moneda': self.currency_id.name or 'PEN',
            'usuario': (self.usuario_id or self.create_uid).name or '',
            'esReversa': bool(self.gasto_reversado_id),
            'reversaDe': self.gasto_reversado_id.id or None,
            # C3: la lista tiene que distinguir el gasto que movió la caja del que no; si no, el
            # cajero no sabe cuáles de sus gastos ya están descontados de su arqueo.
            'pagaCaja': bool(self.paga_caja),
            'movimientoCajaId': mv.id or None,
            'sesionCajaId': mv.sesion_id.id or None,
        }

    # ------------------------------------------------------ enganche con la caja (C3)
    def _l10n_pe_ne_sesion_egreso(self):
        """Sesión de caja donde cae el movimiento de ESTE gasto, ya bloqueada (FOR UPDATE), o
        vacío si no corresponde tocar la caja. Lanza si hace falta caja y no la hay.

        Dos casos, y son distintos a propósito:

          * gasto NUEVO -> la caja abierta del usuario (el helper del biller resuelve el local y
            lanza si hay varias y ninguna es suya: adivinar descuadraría dos arqueos). Si no hay
            caja abierta se lanza con un mensaje que ofrece la salida — a diferencia de una venta,
            un gasto SÍ se puede detener: no hay un cliente esperando en el mostrador, y registrar
            un egreso del cajón sin cajón abierto es dinero que no entra en ningún arqueo.

          * REVERSA -> la sesión del movimiento ORIGINAL, que es de donde salió la plata. Nunca
            «la caja de hoy»: si el gasto salió del turno de ayer, devolverlo al de hoy le mete a
            un arqueo un dinero que nadie puso en ese cajón."""
        self.ensure_one()
        Sesion = self.env['l10n_pe_ne.caja.sesion']
        if self.gasto_reversado_id:
            mv = self.gasto_reversado_id.movimiento_ids[:1]
            if not mv:
                return Sesion.browse()      # el original no salió del cajón: la reversa tampoco
            sesion = mv.sesion_id
            # D-2: la sesión cerrada es INMUTABLE. No se le mete un movimiento nuevo ni se le
            # reescribe el arqueo congelado; el llamador avisa por escrito qué hacer con el
            # dinero (ver _l10n_pe_ne_aviso_reversa_caja).
            return sesion if sesion._l10n_pe_ne_bloquear() else Sesion.browse()
        if not Sesion._l10n_pe_ne_abiertas():
            raise UserError(_(
                "Este gasto se pagó del cajón, pero no hay ninguna caja abierta que lo descuente: "
                "quedaría fuera de todo arqueo. Abre tu caja y vuelve a registrarlo —o desmarca "
                "«se pagó del cajón» si salió por banco, tarjeta o de tu bolsillo—."))
        return Sesion._l10n_pe_ne_sesion_abierta()

    def _l10n_pe_ne_enganchar_caja(self):
        """Crea el movimiento de caja del gasto: RETIRO si es un gasto (monto > 0), INGRESO si es
        una reversa (monto < 0, la plata vuelve al cajón). Devuelve el movimiento o vacío.

        Una sola acción del usuario mueve los dos libros. Antes tenía que registrar el gasto y
        además acordarse de teclear el retiro; el día que se olvidaba, su arqueo cerraba con un
        faltante que nadie sabía explicar."""
        self.ensure_one()
        Movimiento = self.env['l10n_pe_ne.caja.movimiento']
        monto = round(abs(self.monto or 0.0), 2)
        if not monto:
            return Movimiento           # un gasto de S/ 0 no mueve dinero
        sesion = self._l10n_pe_ne_sesion_egreso()
        if not sesion:
            return Movimiento
        medio = normalizar_medio(self.cuenta)
        es_retiro = (self.monto or 0.0) > 0
        if es_retiro:
            # Mismo guard que el retiro manual: no se saca de un bolsillo más de lo que tiene.
            # D-4 (voucher sobre umbral) NO se exige aquí a propósito: la contraparte documental
            # de este egreso ES el gasto —fecha, descripción, autor, append-only (D-2) y encima
            # golpea la utilidad del mes—, que es más rastro del que deja un número de voucher
            # tecleado a mano. Exigir además un voucher solo lograría que el cajero volviera a
            # registrar el gasto por fuera de la caja, que es el agujero que estamos tapando.
            sesion._l10n_pe_ne_check_egreso(medio, monto)
        return Movimiento.create({
            'sesion_id': sesion.id,
            'tipo': 'retiro' if es_retiro else 'ingreso',
            'motivo': _("Gasto: %s", self.descripcion or ''),
            'monto': monto,
            'medio': medio,
            'gasto_id': self.id,
            'company_id': self.company_id.id,
            'currency_id': self.currency_id.id,
        })

    def _l10n_pe_ne_aviso_reversa_caja(self):
        """Texto honesto para la reversa que NO pudo devolver la plata a la caja porque su turno
        ya cerró. El arqueo cerrado no se reescribe (D-2): aquel cierre contó lo que había en el
        cajón y esa foto es la evidencia. Callarse el caso sería peor que el problema: el usuario
        creería que su caja ya está corregida."""
        self.ensure_one()
        sesion = self.gasto_reversado_id.movimiento_ids[:1].sesion_id
        return _(
            "El gasto se reversó, pero su egreso pertenece a la caja N° %(sid)s, que ya está "
            "cerrada: un arqueo cerrado no se reescribe. Si el dinero vuelve al cajón, "
            "regístralo como ingreso en la caja de hoy indicando este motivo.", sid=sesion.id)

    @api.model_create_multi
    def create(self, vals_list):
        """El enganche con la caja va en el ORM y no en el método público: así el gasto creado
        por cualquier vía (API, reversa, un flujo futuro) mueve la caja igual. Un gasto sin
        paga_caja no toca nada — que es todo lo que existía antes de C3."""
        if not any((v or {}).get('paga_caja') for v in vals_list):
            return super().create(vals_list)
        # savepoint: gasto y movimiento son UN hecho. Si el egreso no cabe (no hay caja abierta,
        # o el bolsillo no tiene tanto), la fila del gasto ya está insertada y una excepción a
        # pelo no la deshace: por HTTP la salva el rollback del controlador, pero por RPC o desde
        # un flujo interno quedaría vivo un gasto que dice «salió del cajón» sin haber salido de
        # ningún lado. Eso es exactamente el descuadre que esta rebanada vino a cerrar.
        with self.env.cr.savepoint():
            gastos = super().create(vals_list)
            for g in gastos:
                if g.paga_caja:
                    g._l10n_pe_ne_enganchar_caja()
        return gastos

    # -------------------------------------------------------- inmutabilidad (D-2)
    def write(self, vals):
        """Append-only: no se reescriben los campos de negocio de un gasto ya registrado.
        El contexto l10n_pe_ne_bypass_lock deja pasar migraciones/mantenimiento del sistema."""
        if not self.env.context.get('l10n_pe_ne_bypass_lock') and \
                any(c in vals for c in self._CAMPOS_INMUTABLES):
            raise UserError(_(
                "Un gasto no se puede editar una vez registrado. Para corregirlo, regístralo "
                "de nuevo con el monto en negativo (reversa)."))
        return super().write(vals)

    def unlink(self):
        if not self.env.context.get('l10n_pe_ne_bypass_lock'):
            raise UserError(_(
                "Un gasto no se puede eliminar. Para anularlo, regístralo de nuevo con el "
                "monto en negativo (reversa)."))
        return super().unlink()

    @api.model
    def l10n_pe_ne_list_gastos(self, query=None, periodo=None, limit=300, offset=None):
        """Lista de gastos (opcional por texto o periodo YYYYMM).

        Paginación opt-in: con `offset` devuelve {items, total}; sin él, lista plana
        (así l10n_pe_ne_total_gastos sigue sumando sobre el array completo)."""
        domain = []
        if query:
            domain += ['|', ('descripcion', 'ilike', query), ('cuenta', 'ilike', query)]
        if periodo and len(str(periodo)) == 6 and str(periodo).isdigit():
            y, m = int(periodo[:4]), int(periodo[4:6])
            last = calendar.monthrange(y, m)[1]
            domain += [('fecha', '>=', '%04d-%02d-01' % (y, m)),
                       ('fecha', '<=', '%04d-%02d-%02d' % (y, m, last))]
        recs = self.search(domain, limit=limit, offset=offset or 0)
        items = [g._l10n_pe_ne_gasto_dict() for g in recs]
        if offset is None:
            return items
        return {"items": items, "total": self.search_count(domain)}

    @api.model
    def l10n_pe_ne_total_gastos(self, periodo):
        """Total de gastos del periodo YYYYMM (para la utilidad neta del dashboard)."""
        gastos = self.l10n_pe_ne_list_gastos(periodo=periodo, limit=100000)
        return round(sum(g['monto'] for g in gastos), 2)

    @api.model
    def l10n_pe_ne_create_gasto(self, gasto):
        gasto = gasto or {}
        if not (gasto.get('descripcion') or '').strip():
            raise UserError(_("El gasto necesita una descripción."))
        # C3: `pagaCaja` ausente NO es False, es «lo que diga el negocio»: el cliente viejo (y
        # cualquier integración que ya exista) sigue creando gastos sin tocar la caja mientras el
        # RUC no encienda el default, y la bodega que paga todo del cajón no tiene que marcar la
        # casilla doscientas veces al mes.
        paga = gasto.get('pagaCaja')
        vals = {
            'descripcion': gasto['descripcion'].strip(),
            'monto': float(gasto.get('monto') or 0),
            'cuenta': (gasto.get('cuenta') or 'Efectivo').strip(),
            'paga_caja': (bool(self.env.company.l10n_pe_ne_gasto_de_caja) if paga is None
                          else bool(paga)),
        }
        if gasto.get('fecha'):
            vals['fecha'] = gasto['fecha']
        return self.create(vals)._l10n_pe_ne_gasto_dict()

    @api.model
    def l10n_pe_ne_update_gasto(self, gasto):
        """Append-only (D-2): un gasto ya registrado no se edita. Se conserva el endpoint
        para dar un error claro a los clientes que aún llamen a editar."""
        raise UserError(_(
            "Un gasto no se puede editar una vez registrado. Para corregirlo, usa la reversa "
            "(un contra-asiento con el monto en negativo)."))

    @api.model
    def l10n_pe_ne_delete_gasto(self, rec_id):
        """Append-only (D-2): un gasto no se borra; se reversa. Delega en la reversa para no
        romper a un cliente que aún llame a eliminar (el resultado ahora es la reversa creada)."""
        return self.l10n_pe_ne_reversar_gasto(rec_id)

    @api.model
    def l10n_pe_ne_reversar_gasto(self, rec_id, motivo=None):
        """Contra-asiento (D-2): crea un gasto en negativo que anula al original y lo referencia.
        Es la única forma de corregir un gasto. Idempotente por original: no se reversa dos veces."""
        orig = self.browse(int(rec_id or 0)).exists()
        if not orig:
            raise UserError(_("Gasto no encontrado."))
        if orig.gasto_reversado_id:
            raise UserError(_("Este movimiento ya es una reversa; no se reversa una reversa."))
        if self.search_count([('gasto_reversado_id', '=', orig.id)]):
            raise UserError(_("Este gasto ya fue reversado."))
        # C3: reversar un gasto que salió del cajón tiene que devolver la plata a la caja, o el
        # arqueo seguiría descontando un egreso que ya no existe. Se decide ANTES de crear —y
        # bloqueando la sesión (FOR UPDATE, que se sostiene hasta el commit)— para que la reversa
        # no diga «volvió al cajón» si entre la comprobación y la escritura el turno se cerró.
        mv = orig.movimiento_ids[:1]
        caja_ok = bool(mv) and mv.sesion_id._l10n_pe_ne_bloquear()
        rev = self.create({
            'descripcion': (motivo or '').strip() or _("Reversa de: %s", orig.descripcion or ''),
            'monto': -orig.monto,
            'cuenta': orig.cuenta,
            'currency_id': orig.currency_id.id,
            'fecha': fields.Date.context_today(self),
            'gasto_reversado_id': orig.id,
            'paga_caja': caja_ok,
        })
        d = rev._l10n_pe_ne_gasto_dict()
        if mv and not caja_ok:
            # D-2: el turno del que salió la plata ya cerró y su arqueo es inmutable. La reversa
            # contable se hace igual (el gasto no puede quedar vivo), pero hay que DECIRLO: el
            # dinero físico no volvió solo al cajón y el usuario tiene que decidir qué hace.
            d['avisoCaja'] = rev._l10n_pe_ne_aviso_reversa_caja()
            _logger.info(
                "Gasto %s reversado: su egreso quedó en la caja %s, ya cerrada (no se reescribe).",
                orig.id, mv.sesion_id.id)
        return d

# -*- coding: utf-8 -*-
"""Registro de series de numeración, opcionalmente atadas a un establecimiento.

El dueño con dos locales necesita declarar «F002 es de San Isidro». Ese dato no existía en
ninguna tabla: la serie vivía en `account.journal.l10n_pe_ne_serie` con ámbito compañía y el
establecimiento no tiene ningún campo de serie.

Vive en un modelo propio y no como campo del establecimiento porque el domicilio fiscal
('0000') es SINTÉTICO —lo fabrica `l10n_pe_ne_list` desde el partner de la compañía— y por
tanto no tiene fila donde colgar un campo. Aquí es `establecimiento_id = NULL`, sin
materializar nada. Tampoco cuelga del diario: eso obligaría a crear ocho diarios de venta por
tenant (uno por serie), cada uno con su secuencia contable y su libro, ensuciando el plan de
cuentas para modelar numeración fiscal.

El registro arranca VACÍO y ninguna migración lo siembra: la retrocompatibilidad va en el
código. Mientras no haya filas, la emisión se comporta exactamente igual que antes.

Contrato de numeración: este modelo describe la CONFIGURACIÓN (qué serie usa cada local),
nunca la numeración. El correlativo se sigue llaveando por (compañía, serie) — ver el
comentario de `_l10n_pe_ne_next_correlativo`.
"""
import re

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError

# Serie SUNAT: letra de familia + 3 alfanuméricos (F001, FC01, B002…). Misma familia que
# resuelve `_l10n_pe_serie_prefix`: F para factura y sus notas, B para boleta y las suyas.
_RE_SERIE = re.compile(r'^[FB][A-Z0-9]{3}$')

# Letra admitida por tipo de documento. Una NC/ND hereda la familia del documento afectado,
# así que 07/08 aceptan las dos: FC01 es nota de una factura y BC01 de una boleta, y ambas
# son legales.
_FAMILIA = {'01': ('F',), '03': ('B',), '07': ('F', 'B'), '08': ('F', 'B')}

_TIPO_DOC = [
    ('01', 'Factura'),
    ('03', 'Boleta'),
    ('07', 'Nota de crédito'),
    ('08', 'Nota de débito'),
]
_TIPO_NOMBRE = dict(_TIPO_DOC)


class L10nPeNeSerie(models.Model):
    _name = 'l10n_pe_ne.serie'
    _description = 'Serie de numeración por establecimiento'
    _order = 'tipo_doc, codigo'

    codigo = fields.Char(
        required=True, string='Serie',
        help='Serie de 4 caracteres: la letra de familia (F factura, B boleta) y 3 '
             'alfanuméricos. Ej.: F001, F002, BC01.')
    tipo_doc = fields.Selection(_TIPO_DOC, required=True, string='Tipo de comprobante')
    # Nullable A PROPÓSITO: vacío = domicilio fiscal ('0000'), que no tiene fila propia.
    # ondelete='restrict' porque borrar el local dejaría una serie huérfana emitiendo con un
    # codLocalEmisor inexistente; el borrado pasa por el archivado del establecimiento.
    establecimiento_id = fields.Many2one(
        'l10n_pe_ne.establecimiento', string='Establecimiento', index=True,
        ondelete='restrict',
        help='Vacío = domicilio fiscal (código 0000).')
    activa = fields.Boolean(default=True, string='Activa')
    predeterminada = fields.Boolean(
        string='Predeterminada',
        help='La que se propone al emitir ese tipo de comprobante desde ese local.')
    company_id = fields.Many2one('res.company', required=True, index=True,
                                 default=lambda s: s.env.company)

    # Unicidad de la serie dentro del RUC: SUNAT numera por (RUC, serie), de modo que la MISMA
    # serie en dos locales compartiría correlativo. La guarda amigable —la que EXPLICA la regla—
    # vive en l10n_pe_ne_serie_upsert; esta constraint es la defensa de última línea contra la
    # carrera (mismo reparto que el índice de sesión única de la caja).
    _codigo_company_uniq = models.Constraint(
        'unique(codigo, company_id)',
        'Esa serie ya está registrada para tu RUC.',
    )

    def init(self):
        # Una sola serie PREDETERMINADA por (compañía, tipo, local). El COALESCE es obligatorio:
        # en Postgres NULL != NULL, y sin él el domicilio fiscal (establecimiento_id NULL)
        # admitiría dos predeterminadas del mismo tipo — justo el caso más común.
        self.env.cr.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS l10n_pe_ne_serie_predeterminada_uniq
            ON l10n_pe_ne_serie (company_id, tipo_doc, COALESCE(establecimiento_id, 0))
            WHERE predeterminada
        """)

    # ------------------------------------------------------------- normalización
    def _l10n_pe_ne_norm(self, vals):
        """La serie es identidad fiscal: se guarda SIEMPRE en mayúsculas y sin espacios, así
        'f002 ' y 'F002' no conviven como dos filas que la unicidad no detecta."""
        if vals.get('codigo'):
            vals['codigo'] = vals['codigo'].strip().upper()
        return vals

    @api.model_create_multi
    def create(self, vals_list):
        return super().create([self._l10n_pe_ne_norm(dict(v)) for v in vals_list])

    def write(self, vals):
        return super().write(self._l10n_pe_ne_norm(dict(vals)))

    # ------------------------------------------------------------- validaciones
    @api.constrains('codigo', 'tipo_doc')
    def _check_codigo_familia(self):
        for s in self:
            codigo = (s.codigo or '').strip().upper()
            if not _RE_SERIE.match(codigo):
                raise ValidationError(_(
                    "La serie '%(serie)s' no tiene el formato de SUNAT: son 4 caracteres que "
                    "empiezan por F (factura y sus notas) o B (boleta y las suyas), seguidos de "
                    "3 letras o números. Ej.: F001, F002, BC01.") % {'serie': s.codigo or ''})
            letras = _FAMILIA.get(s.tipo_doc, ())
            if codigo[0] not in letras:
                raise ValidationError(_(
                    "La serie '%(serie)s' no corresponde a %(tipo)s: debe empezar por "
                    "%(letras)s. SUNAT exige que las series de factura (y sus notas) empiecen "
                    "por F, y las de boleta por B.") % {
                        'serie': codigo, 'tipo': _TIPO_NOMBRE.get(s.tipo_doc, s.tipo_doc),
                        'letras': ' o '.join(letras) or 'F o B'})

    @api.constrains('establecimiento_id', 'company_id')
    def _check_establecimiento_company(self):
        for s in self:
            if s.establecimiento_id and s.establecimiento_id.company_id != s.company_id:
                raise ValidationError(_("El establecimiento pertenece a otra empresa."))

    # ------------------------------------------------------------- resolución
    @api.model
    def _l10n_pe_ne_serie_de(self, company, tipo_doc, estab, familia=None):
        """Serie declarada para (local, tipo): la PREDETERMINADA si la hay y, si no, la activa
        de menor código —criterio estable, para que la misma configuración numere siempre igual—.
        `estab` vacío = domicilio fiscal (D3: el '0000' es la ausencia de FK, y el domain tiene
        que acordarse de ella o las series del domicilio desaparecen del resultado).

        `familia` acota a F o B, y solo lo usan las notas: una serie 07 puede ser FC02 (nota de
        factura) o BC02 (nota de boleta) y elegir la familia equivocada es rechazo seguro.

        Devuelve '' si ese local no declaró serie para ese tipo: el llamador cae al default de
        siempre y el tenant sin registro no nota nada."""
        dominio = [('company_id', '=', company.id),
                   ('tipo_doc', '=', tipo_doc),
                   ('activa', '=', True),
                   ('establecimiento_id', '=', estab.id if estab else False)]
        # sudo: elegir la serie es lógica de emisión, no una lectura del usuario. El emisor
        # tiene el registro en solo-lectura por ACL, pero la emisión también corre desde el cron
        # y el lote, y ahí no hay grupo que valga; la compañía va explícita en el domain.
        for s in self.sudo().search(dominio, order='predeterminada desc, codigo'):
            if familia and (s.codigo or '')[:1] != familia:
                continue
            return s.codigo
        return ''

    @api.model
    def _l10n_pe_ne_local_de_serie(self, company, codigo):
        """Establecimiento al que está atada una serie ACTIVA del registro; recordset vacío si
        la serie no está declarada, está apagada o se declaró sin local.

        Sin local no ata a nadie A PROPÓSITO: la serie del domicilio fiscal es la del tenant de
        una sola serie, que hoy emite desde donde quiera. Solo lo que el dueño ató a un anexo
        concreto se veta fuera de ese anexo."""
        serie = self.sudo().search(
            [('company_id', '=', company.id), ('activa', '=', True),
             ('codigo', '=', (codigo or '').strip().upper())], limit=1)
        return serie.establecimiento_id

    # ------------------------------------------------------------- serialización
    def _l10n_pe_ne_dict(self):
        self.ensure_one()
        e = self.establecimiento_id
        return {
            'id': self.id,
            'serie': self.codigo,
            'tipoDoc': self.tipo_doc,
            'tipo': _TIPO_NOMBRE.get(self.tipo_doc, self.tipo_doc),
            # establecimientoId None + codigo '0000' = domicilio fiscal (fila sintética: el
            # '0000' no existe como registro, ver l10n_pe_ne_establecimiento.l10n_pe_ne_list).
            'establecimientoId': e.id or None,
            'establecimiento': e.codigo or '0000',
            'establecimientoDireccion': e.direccion or '',
            'activa': self.activa,
            'predeterminada': self.predeterminada,
        }

    # ------------------------------------------------------------- API del SPA
    @api.model
    def l10n_pe_ne_serie_list(self):
        """Registro de series del RUC. SIN muro a propósito: leer la configuración propia no
        cambia nada, y exigir el grupo aquí dejaría sin la pantalla de Series a los tenants
        pre-roles, que hoy la ven con solo el grupo Emisor. El muro está en la escritura."""
        return [s._l10n_pe_ne_dict()
                for s in self.search([('company_id', '=', self.env.company.id)])]

    def _l10n_pe_ne_resolver_establecimiento(self, payload):
        """Local de la serie desde el payload. Ausente / 0 / '0000' = domicilio fiscal (False):
        el '0000' no se materializa, es la ausencia de FK (D3)."""
        estab_id = payload.get('establecimientoId')
        if estab_id in (None, '', 0, '0'):
            codigo = (payload.get('establecimiento') or '').strip()
            if not codigo or codigo == '0000':
                return self.env['l10n_pe_ne.establecimiento']
            estab = self.env['l10n_pe_ne.establecimiento'].search(
                [('codigo', '=', codigo), ('company_id', '=', self.env.company.id)], limit=1)
        else:
            estab = self.env['l10n_pe_ne.establecimiento'].browse(int(estab_id)).exists()
        if not estab or estab.company_id != self.env.company:
            raise UserError(_("El establecimiento indicado no existe en tu catálogo."))
        return estab

    @api.model
    def l10n_pe_ne_serie_upsert(self, payload):
        """Alta/edición de una serie del registro. El has_group vive DENTRO del método (y no
        solo en el controller) porque desde que el local determina la serie, tocar esto es
        cambiar la numeración fiscal de la empresa: ninguna vía —backend, RPC, un endpoint
        futuro— debe poder saltárselo."""
        self.env.user._l10n_pe_ne_check_config_series()
        payload = payload or {}
        codigo = (payload.get('serie') or payload.get('codigo') or '').strip().upper()
        tipo_doc = (payload.get('tipoDoc') or '').strip()
        if not codigo:
            raise UserError(_("Indica la serie."))
        if tipo_doc not in _TIPO_NOMBRE:
            raise UserError(_("Indica el tipo de comprobante de la serie."))
        estab = self._l10n_pe_ne_resolver_establecimiento(payload)
        vals = {
            'codigo': codigo,
            'tipo_doc': tipo_doc,
            'establecimiento_id': estab.id or False,
            'predeterminada': bool(payload.get('predeterminada')),
        }
        ocupada = self.search([('codigo', '=', codigo),
                               ('company_id', '=', self.env.company.id)], limit=1)
        rec_id = payload.get('id')
        if rec_id:
            rec = self.search([('id', '=', int(rec_id)),
                               ('company_id', '=', self.env.company.id)], limit=1)
            if not rec:
                raise UserError(_("La serie indicada no existe."))
            # Renombrar una fila hacia un código que ya es de otra: mismo choque de unicidad.
            if ocupada and ocupada != rec:
                self._l10n_pe_ne_error_serie_ocupada(codigo, ocupada)
        else:
            # ALTA. Si el código ya existe en el RUC solo es idempotente cuando apunta al MISMO
            # local; hacia otro local se corta. Mover la serie en silencio cambiaría la
            # numeración del primer local sin que nadie lo haya pedido.
            if ocupada and ocupada.establecimiento_id != estab:
                self._l10n_pe_ne_error_serie_ocupada(codigo, ocupada)
            rec = ocupada
        # 'activa' NO se toca al editar salvo que lo pidan: el editor cambia serie/tipo/local/
        # predeterminada, y una serie apagada no debe revivir por corregirle el local. Encenderla
        # o apagarla es la acción explícita de l10n_pe_ne_serie_toggle.
        if 'activa' in payload:
            vals['activa'] = bool(payload.get('activa'))
        elif not rec:
            vals['activa'] = True
        if vals['predeterminada']:
            self._l10n_pe_ne_liberar_predeterminada(vals, rec)
        if rec:
            rec.write(vals)
        else:
            rec = self.create(dict(vals, company_id=self.env.company.id))
        return rec._l10n_pe_ne_dict()

    def _l10n_pe_ne_error_serie_ocupada(self, codigo, ocupada):
        """«Quiero F001 en mis dos locales» es la intuición del dueño y choca con una regla dura
        de SUNAT. El error la EXPLICA en vez de solo negarla: si no, entra por soporte como bug."""
        donde = (ocupada.establecimiento_id.codigo
                 if ocupada.establecimiento_id else '0000 (domicilio fiscal)')
        raise ValidationError(_(
            "La serie '%(serie)s' ya está asignada al establecimiento %(donde)s. Una serie "
            "pertenece a UN solo local: SUNAT numera por RUC y serie, así que si dos locales "
            "compartieran '%(serie)s' los dos emitirían el mismo número y tendrías comprobantes "
            "duplicados (solo se corrigen con comunicación de baja ante SUNAT). Dale otra serie "
            "al segundo local, p. ej. F002.") % {'serie': codigo, 'donde': donde})

    def _l10n_pe_ne_liberar_predeterminada(self, vals, excluir):
        """Marcar una predeterminada DESMARCA la anterior del mismo (local, tipo). Es lo que el
        dueño espera al pulsar 'usar esta por defecto'; el índice único parcial queda como
        garantía contra la carrera, no como error que el usuario tenga que resolver."""
        dominio = [('company_id', '=', self.env.company.id),
                   ('tipo_doc', '=', vals['tipo_doc']),
                   ('establecimiento_id', '=', vals['establecimiento_id'] or False),
                   ('predeterminada', '=', True)]
        if excluir:
            dominio.append(('id', '!=', excluir.id))
        previas = self.search(dominio)
        if previas:
            previas.write({'predeterminada': False})
            # El índice es de BD y Odoo difiere los writes: sin bajar la bandera ANTES del
            # INSERT/UPDATE de la nueva, Postgres ve las dos marcadas a la vez y rechaza.
            previas.flush_recordset(['predeterminada'])

    @api.model
    def l10n_pe_ne_serie_toggle(self, rec_id, activa=None):
        """Activa/desactiva una serie. NUNCA borra: una serie que ya emitió es historia fiscal
        y su correlativo debe seguir siendo consultable. Desactivarla la saca del conjunto de
        series habilitadas, que es lo que el dueño quiere decir con 'ya no la uso'."""
        self.env.user._l10n_pe_ne_check_config_series()
        rec = self.search([('id', '=', int(rec_id or 0)),
                           ('company_id', '=', self.env.company.id)], limit=1)
        if not rec:
            raise UserError(_("La serie indicada no existe."))
        nueva = (not rec.activa) if activa is None else bool(activa)
        vals = {'activa': nueva}
        if not nueva:
            # Una serie apagada no puede seguir siendo la predeterminada del local: dejaría el
            # hueco ocupado y el resolver propondría una serie que ya no se puede emitir.
            vals['predeterminada'] = False
        rec.write(vals)
        return rec._l10n_pe_ne_dict()

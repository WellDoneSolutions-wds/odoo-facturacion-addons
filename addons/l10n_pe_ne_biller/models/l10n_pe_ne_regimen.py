# -*- coding: utf-8 -*-
"""Régimen tributario del emisor: qué comprobantes puede emitir (F1).

Eje ORTOGONAL al rubro. El rubro responde «a qué se dedica el negocio»; el régimen responde
«qué impuestos paga», y de eso depende qué documentos tiene permitido emitir. De los cuatro
regímenes peruanos solo el NRUS cambia lo emitible: RER, RMT y Régimen General emiten
exactamente el mismo juego de comprobantes (lo que los separa —pagos a cuenta, libros, DJ
anual— es materia contable, fuera de este producto).

Por qué NO se modela apagando módulos de rubro: E01 (Factura) y E02 (Boleta) están en la tupla
NUCLEO de l10n_pe_ne_rubro y son explícitamente inapagables —ni un override los desactiva—.
Sacar E01 del núcleo sería un cambio de doctrina con impacto en el aplicador y en la bitácora.
Se reusa el PATRÓN (catálogo en Python, muro server-side, bitácora compartida), no el ESTADO.

Por qué el bloqueo del NRUS no es cosmético: el art. 16.2 del D. Leg. 937 no castiga con multa
al NRUS que emite factura — determina su INCLUSIÓN INMEDIATA en el RMT o el Régimen General,
retroactiva al mes del primer comprobante no autorizado. Un botón mal puesto le cuesta el
régimen al cliente. De ahí que el muro viva en el modelo y no en la UI.

Doctrina «ausente ≠ prohibido» (la misma del rubro y del menú por rol): compañía SIN régimen
configurado = legacy = SIN gating. Ninguna compañía existente puede romperse por este cambio;
la migración de tenants viejos es un no-evento.

Catálogo en PYTHON (patrón _ROLES / RUBROS): cambiarlo exige PR + review + deploy, no un
UPDATE en producción.
"""
import json

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError

# ───────────────────────────────────────────────────── tipos de comprobante
# Nombres para los mensajes al usuario y la bitácora. 'nota' no es un tipo SUNAT: es la nota
# de venta INTERNA (documento no fiscal), y se lista aquí porque el gating de la SPA la trata
# como un tipo emitible más.
TIPO_NOMBRE = {
    "01": "Factura",
    "03": "Boleta de venta",
    "04": "Liquidación de compra",
    "07": "Nota de crédito",
    "08": "Nota de débito",
    "09": "Guía de remisión remitente",
    "20": "Comprobante de retención",
    "31": "Guía de remisión transportista",
    "40": "Comprobante de percepción",
    "nota": "Nota de venta (interna)",
}

# Todo lo que el sistema sabe emitir hoy. Es la lista de RER / RMT / Régimen General.
TIPOS_TODOS = ("01", "03", "04", "07", "08", "09", "20", "31", "40", "nota")

# NRUS. Prohibido todo lo que otorgue crédito fiscal o sustente gasto (D. Leg. 937 art. 16.2,
# texto vigente según D. Leg. 1270): fuera factura (01), liquidación de compra (04) —que sí
# otorga crédito fiscal— y retención/percepción (20/40) —el NRUS no declara IGV, no puede ser
# agente—. Las guías (09/31) son de traslado, no de venta: no otorgan nada.
#
# NC/ND (07/08) quedan PERMITIDAS a propósito. El texto DEROGADO del art. 16.2 las prohibía por
# nombre; el vigente las sustituyó por un criterio funcional, y una NC que anula una boleta no
# otorga crédito fiscal. No hay informe SUNAT vigente que lo resuelva (ver
# docs/REGIMENES-TRIBUTARIOS.md §6), así que ante la duda se elige PERMISIVO: bloquear de más
# le impide al emisor anular su propia boleta, que es un daño cierto contra un riesgo dudoso.
TIPOS_NRUS = ("03", "07", "08", "09", "31", "nota")

# ───────────────────────────────────────────────────────── catálogo de regímenes
# codigo -> (nombre, descripcion, tipos permitidos)
REGIMENES = {
    "nrus": (
        "Nuevo RUS",
        "Pago de cuota fija mensual. Solo boletas: la ley PROHÍBE emitir factura, y hacerlo "
        "saca al negocio del régimen de forma retroactiva. Tope de S/ 96,000 al año.",
        TIPOS_NRUS,
    ),
    "rer": (
        "Régimen Especial (RER)",
        "1.5 % de los ingresos netos mensuales. Emite todos los comprobantes. Tope de "
        "S/ 525,000 al año y hasta 10 trabajadores por turno.",
        TIPOS_TODOS,
    ),
    "rmt": (
        "Régimen MYPE Tributario",
        "Renta con escala (10 % hasta 15 UIT, 29.5 % por el exceso). Emite todos los "
        "comprobantes. Tope de 1,700 UIT de ingresos netos al año.",
        TIPOS_TODOS,
    ),
    "general": (
        "Régimen General",
        "Régimen sin topes de ingresos ni de actividad. Emite todos los comprobantes.",
        TIPOS_TODOS,
    ),
}

# Categorías del NRUS (D. Leg. 937 art. 7): el tope MENSUAL de ingresos/compras depende de
# ellas. La Categoría Especial (mercados de abastos y venta exclusiva de productos exonerados)
# paga cuota 0. Solo tiene sentido con régimen 'nrus'.
NRUS_CATEGORIAS = (
    ("1", "Categoría 1"),
    ("2", "Categoría 2"),
    ("especial", "Categoría Especial"),
)
_NRUS_CATEGORIA_NOMBRE = dict(NRUS_CATEGORIAS)

# Selection listo para los campos de res.company: el catálogo es la fuente única, así el día
# que se sume o retire un régimen no hay dos listas que puedan divergir.
REGIMEN_SELECTION = [(cod, datos[0]) for cod, datos in REGIMENES.items()]


def regimen_label(datos):
    """Etiqueta legible de un estado de régimen {regimen, fechaInicio, nrusCategoria} para la
    bitácora. Un dict vacío (o un régimen desconocido) se lee como «Sin régimen»: es
    exactamente lo que significa el campo vacío —legacy, sin gating—."""
    if not isinstance(datos, dict):
        return "Sin régimen"
    cod = datos.get("regimen") or ""
    if cod not in REGIMENES:
        return "Sin régimen"
    partes = [REGIMENES[cod][0]]
    cat = datos.get("nrusCategoria")
    if cat:
        partes.append(_NRUS_CATEGORIA_NOMBRE.get(cat, cat))
    fecha = datos.get("fechaInicio")
    if fecha:
        partes.append("desde %s" % fecha)
    return " · ".join(partes)


class ResCompany(models.Model):
    _inherit = "res.company"

    # Los CAMPOS se declaran en res_company.py, junto a l10n_pe_ne_agente_percepcion (el
    # precedente de diseño: un dato tributario del emisor que viaja al front y habilita
    # controles). Aquí vive la RESOLUCIÓN, que es donde está la regla.

    def l10n_pe_ne_tipos_permitidos(self):
        """Tipos de comprobante que la empresa puede emitir, o None si no hay gating.

        None (y no la lista completa) cuando la empresa no configuró régimen: es el contrato
        «ausente ≠ prohibido» que ya usan `modulos`/`modulos_efectivos`, y le permite al
        llamador distinguir «no restringido» de «restringido a todo», que no son lo mismo el
        día que la lista de tipos crezca.

        Resolución O(1) sobre un dict en memoria: NO hace queries. Es requisito, no un
        detalle — esto se consulta una vez por emisión y nunca dentro de un bucle de líneas."""
        self.ensure_one()
        # F4: el gating es INMEDIATO — `l10n_pe_ne_regimen_fecha` no se mira. El día que se
        # implemente el juicio por fecha (un comprobante de diciembre juzgado con el régimen de
        # diciembre) el enganche va aquí: recibir la fecha del comprobante y, si es anterior a
        # `l10n_pe_ne_regimen_fecha`, resolver con el régimen anterior. Eso exige un HISTORIAL
        # de regímenes (hoy solo se guarda el vigente), no solo leer el campo.
        cod = self.l10n_pe_ne_regimen or ""
        # Régimen desconocido (dato viejo, migración a medias, valor escrito por RPC) se trata
        # como legacy: nunca reventar ni, peor, bloquear una emisión legítima por un dato sucio.
        if cod not in REGIMENES:
            return None
        return set(REGIMENES[cod][2])

    def l10n_pe_ne_tipo_permitido(self, tipo):
        """True si la empresa puede emitir ese tipo (o si no configuró régimen: legacy)."""
        self.ensure_one()
        tipos = self.l10n_pe_ne_tipos_permitidos()
        return True if tipos is None else str(tipo or "") in tipos

    def l10n_pe_ne_degradar_tipo(self, tipo):
        """Tipo de comprobante EMISIBLE más cercano a `tipo`.

        La usan los flujos que DERIVAN el tipo del cliente en vez de preguntárselo al usuario:
        cobrar una cotización, cobrar una orden de trabajo y su anticipo (Vía A). Ahí «cliente
        con RUC ⇒ factura» es una heurística de conveniencia, no una decisión del cajero, y
        dejarla chocar contra el muro convierte esas pantallas en un callejón sin salida: el
        NRUS con una cotización aceptada a un cliente con RUC no podría cobrarla por ninguna
        vía. Se degrada, no se bloquea. Es la misma degradación que ya hace la SPA
        (Cotizaciones.tsx / NotasVenta.tsx), resuelta también en el servidor para que no
        dependa del front.

        OJO — esto NO es un bypass del muro: donde el usuario ELIGE el tipo (Emitir, POS, la
        emisión masiva, la API) el muro corta y explica. Degradar en silencio ahí escondería
        el error del usuario; aquí no hay error del usuario que esconder."""
        self.ensure_one()
        if self.l10n_pe_ne_tipo_permitido(tipo):
            return tipo
        # Única degradación posible hoy: factura → boleta. El NRUS vende con boleta a todo el
        # mundo, tenga el comprador RUC o no (RCP art. 4), así que la venta sigue siendo
        # ejecutable y con el documento correcto.
        if str(tipo) == "01" and self.l10n_pe_ne_tipo_permitido("03"):
            return "03"
        # Sin equivalente permitido se devuelve el original: degradar hacia otro tipo que
        # tampoco puede emitir solo cambiaría el mensaje del muro. Que corte el muro, que sí
        # explica el porqué.
        return tipo


class AccountMove(models.Model):
    _inherit = "account.move"

    # ------------------------------------------------------------ helpers de régimen
    def _l10n_pe_ne_regimen_company(self):
        """Compañía cuyo régimen manda. En un comprobante ya creado manda la SUYA (multi-RUC:
        un usuario puede tener varias); en las llamadas @api.model, la del contexto."""
        if self and len(self) == 1 and self.company_id:
            return self.company_id
        return self.env.company

    def _l10n_pe_ne_regimen_mensaje(self, tipo, company=None):
        """Mensaje ÚNICO del bloqueo por régimen: lo usan el muro de emisión y la regla L1.
        Compartirlo no es higiene: el pre-flight muestra el texto del muro cuando el muro corta
        antes de que la regla pueda opinar, y dos textos distintos para el mismo hecho leen
        como dos problemas distintos."""
        company = company or self._l10n_pe_ne_regimen_company()
        cod = company.l10n_pe_ne_regimen or ""
        nombre_reg = REGIMENES.get(cod, (cod,))[0]
        nombre_tipo = TIPO_NOMBRE.get(str(tipo), str(tipo))
        if cod == "nrus" and str(tipo) == "01":
            # El caso caro merece decir POR QUÉ: la consecuencia no es una multa, es perder
            # el régimen. Un mensaje genérico haría que el usuario buscara cómo saltárselo.
            return _(
                "Tu negocio está en el %(reg)s y la ley le PROHÍBE emitir %(tipo)s "
                "(D. Leg. 937, art. 16.2): emitir una sola factura lo saca del Nuevo RUS y lo "
                "pasa al Régimen MYPE o General, con efecto retroactivo al mes de emisión. "
                "Emite una BOLETA. Si de verdad cambiaste de régimen, actualízalo primero en "
                "Configuración → Régimen tributario.",
                reg=nombre_reg, tipo=nombre_tipo)
        return _(
            "Tu negocio está en el %(reg)s y ese régimen no permite emitir %(tipo)s. "
            "Si cambiaste de régimen, actualízalo en Configuración → Régimen tributario.",
            reg=nombre_reg, tipo=nombre_tipo)

    # ------------------------------------------------------------ muro de emisión
    @api.model
    def _l10n_pe_ne_check_regimen(self, tipo, company=None):
        """Muro por RÉGIMEN en la emisión: corta ANTES de armar el move si el régimen del
        emisor no admite ese tipo de comprobante.

        Vive en el modelo (no solo en la UI) para que ninguna vía —API directa, curl, RPC— lo
        salte, igual que `_l10n_pe_ne_check_modulo`. El intento queda en la bitácora como
        evento de seguridad: un NRUS intentando facturar es exactamente el evento que el
        contador va a querer poder reconstruir después.

        Empresa sin régimen (legacy) pasa siempre.

        SIN bypass de administrador, a diferencia del muro de rubro. Criterio: **el bypass
        solo se justifica donde otra capa hace de red; donde no la hay, no debe haber bypass**.
        Un módulo de rubro apagado es una preferencia comercial del tenant y el admin de
        plataforma opera en soporte por encima de ella; el régimen tributario no es una
        preferencia, es la ley, y saltárselo le cuesta el régimen al cliente.

        Y el bypass, además, no daba lo que prometía:
          · 01/03/04/07/08 igual pasan por el motor L1 (`_l10n_pe_ne_asegurar_valido`), que no
            tiene bypass — el admin no conseguía emitir, solo recibía el mensaje genérico «no
            cumple una regla de SUNAT» en vez del del muro, y el intento NO quedaba en
            bitácora. Peor mensaje y peor auditoría justo para el usuario con más poder.
          · 20/40 son `account.payment`: no pasan por L1, y el pre-flight los devuelve vacío
            (controllers/main.py). Ahí el bypass era la ÚNICA barrera — y `admin@ne.com`, el
            login documentado de la SPA, es miembro de `base.group_system`: un NRUS emitía
            retención/percepción de punta a punta."""
        company = company or self.env.company
        if company.l10n_pe_ne_tipo_permitido(tipo):
            return
        self._l10n_pe_ne_bitacora_segura({
            "company_id": company.id,
            "user_id": self.env.user.id,
            "campo": "rechazo-regimen:%s" % str(tipo),
            "antes": company.l10n_pe_ne_regimen or "",
            "despues": TIPO_NOMBRE.get(str(tipo), str(tipo)),
        })
        raise UserError(self._l10n_pe_ne_regimen_mensaje(tipo, company=company))

    # -------------------------------------------------------------- API (F1 · endpoints)
    @api.model
    def l10n_pe_ne_regimen_config(self):
        """Estado + catálogo para la pantalla «Régimen tributario» (GET /ne/api/regimen).

        `tiposPermitidos` viaja YA RESUELTO (o None si no hay gating): la regla tributaria se
        decide en el servidor y el front no la reimplementa. Reimplementarla en la SPA es
        precisamente cómo se consigue que la UI y la emisión no coincidan."""
        company = self.env.company
        tipos = company.l10n_pe_ne_tipos_permitidos()
        return {
            "regimen": company.l10n_pe_ne_regimen or None,
            "fechaInicio": fields.Date.to_string(company.l10n_pe_ne_regimen_fecha) or None,
            "nrusCategoria": company.l10n_pe_ne_nrus_categoria or None,
            "catalogo": [
                {"codigo": cod, "nombre": nombre, "descripcion": desc,
                 # Por régimen, para que el wizard pueda mostrar qué gana/pierde ANTES de
                 # elegir (el usuario no debería descubrir la restricción al emitir).
                 "tiposPermitidos": sorted(tipos_reg)}
                for cod, (nombre, desc, tipos_reg) in REGIMENES.items()
            ],
            "catalogoNrus": [{"codigo": c, "nombre": n} for c, n in NRUS_CATEGORIAS],
            "tiposPermitidos": sorted(tipos) if tipos is not None else None,
            "tiposNombres": dict(TIPO_NOMBRE),
            # Mismo gate que corta el guardado, resuelto en el servidor: duplicar una regla de
            # permisos en el front es pedir que se desincronice (ver l10n_pe_ne_rubro_config).
            "puedeEditar": self._l10n_pe_ne_puede_config_rubro(),
        }

    @api.model
    def l10n_pe_ne_set_regimen(self, payload):
        """Guarda el régimen tributario (POST /ne/api/regimen). Muro PRIMERO: el régimen
        decide qué puede emitir TODA la empresa — decisión del dueño/supervisor, nunca de un
        cajero. Entrada inválida sale como UserError legible, nunca como traceback."""
        if not self._l10n_pe_ne_puede_config_rubro():
            raise AccessError(_(
                "No tienes permiso para configurar el régimen tributario: decide qué "
                "comprobantes puede emitir toda la empresa. Pídeselo al dueño o al supervisor."))
        if payload is None:
            payload = {}
        if not isinstance(payload, dict):
            raise UserError(_("Los datos del régimen tributario deben venir como un objeto."))

        # ── régimen. Vacío = volver a legacy (sin gating), el camino de vuelta explícito.
        raw = payload.get("regimen")
        if raw is None or raw is False:
            raw = ""
        if not isinstance(raw, str):
            raise UserError(_("El régimen tributario debe indicarse con su código."))
        regimen = raw.strip().lower()
        if regimen and regimen not in REGIMENES:
            raise UserError(_(
                "Régimen tributario desconocido: «%(dado)s». Válidos: %(validos)s.",
                dado=raw, validos=", ".join(REGIMENES)))

        # ── fecha de vigencia (opcional) y hoy DECLARATIVA: se guarda y se muestra, pero el
        # gating no la mira (F4). Se pide porque bajar a NRUS o RER solo surte efecto con la
        # declaración de enero y el dueño necesita dejarlo anotado — y porque es el insumo del
        # juicio por fecha cuando se implemente.
        fecha_raw = payload.get("fechaInicio")
        fecha = False
        if fecha_raw not in (None, False, ""):
            try:
                fecha = fields.Date.to_date(fecha_raw)
            except (ValueError, TypeError):
                fecha = False
            if not fecha:
                raise UserError(_(
                    "La fecha de vigencia «%s» no es una fecha válida (usa AAAA-MM-DD).")
                    % fecha_raw)

        # ── categoría NRUS (opcional). Solo existe dentro del NRUS: elegida junto a otro
        # régimen se descarta en silencio en vez de fallar — es un dato que sobra, no un error
        # del usuario, y hacerlo fallar convertiría «cambié de NRUS a RER» en un callejón.
        cat_raw = payload.get("nrusCategoria")
        if cat_raw in (None, False, ""):
            categoria = ""
        elif not isinstance(cat_raw, str):
            raise UserError(_("La categoría del Nuevo RUS debe indicarse con su código."))
        else:
            categoria = cat_raw.strip().lower()
            if categoria not in _NRUS_CATEGORIA_NOMBRE:
                raise UserError(_(
                    "Categoría del Nuevo RUS desconocida: «%(dado)s». Válidas: %(validas)s.",
                    dado=cat_raw, validas=", ".join(c for c, _n in NRUS_CATEGORIAS)))
        if regimen != "nrus":
            categoria = ""

        company = self.env.company
        antes = {
            "regimen": company.l10n_pe_ne_regimen or "",
            "fechaInicio": fields.Date.to_string(company.l10n_pe_ne_regimen_fecha) or "",
            "nrusCategoria": company.l10n_pe_ne_nrus_categoria or "",
        }
        despues = {
            "regimen": regimen,
            "fechaInicio": fields.Date.to_string(fecha) or "",
            "nrusCategoria": categoria,
        }
        if antes != despues:
            company.sudo().write({
                "l10n_pe_ne_regimen": regimen or False,
                "l10n_pe_ne_regimen_fecha": fecha or False,
                "l10n_pe_ne_nrus_categoria": categoria or False,
            })
            self.env["l10n_pe_ne.rubro_auditoria"].sudo().create({
                "company_id": company.id,
                "user_id": self.env.user.id,
                "campo": "regimen",
                "antes": json.dumps(antes, ensure_ascii=False),
                "despues": json.dumps(despues, ensure_ascii=False),
            })
        return self.l10n_pe_ne_regimen_config()

"""Aritmética pura del arqueo de caja (NE Express) — sin Odoo, testeable por carga de módulo.

normalizar_medio/clave_medio: normalización canónica del medio de pago (ver más abajo).
descuadre_arqueo: magnitud del descuadre de un cierre (la que se compara con la tolerancia).
agrupar_ventas: agrupa las ventas de la sesión por medio de pago aplicando las reglas v1
  (USD aparte; Contado sin medios -> Efectivo; Crédito suma solo sus medios).
esperado_por_medio: lo que la caja DEBERÍA tener de cada bolsillo (ventas + fondo + movimientos).
disponible_medio: lo que queda en UN bolsillo ahora mismo (guard del egreso, C3).
calcular_arqueo: cruza el esperado por medio contra el conteo físico y devuelve las filas del
  arqueo + los totales. Toda la aritmética redondea a 2 decimales (evita el ruido flotante)."""
import unicodedata

EFECTIVO = "Efectivo"
# Medios estándar del POS (deben coincidir con MEDIOS de src/lib/medios.ts): el cierre
# siembra una fila por cada uno aunque su esperado sea 0.
MEDIOS_ESTANDAR = ["Efectivo", "Yape", "Plin", "Tarjeta", "Transferencia", "Depósito"]


def _r2(n):
    return round(float(n or 0.0), 2)


# ───────────────────────────────────────────────── normalización del medio de pago
#
# C1 (integridad): el medio es texto libre y viaja desde cuatro orígenes distintos (POS,
# Emitir, adelanto/abono de órdenes, cobro de cotización). Sin normalizar, "Efectivo",
# "efectivo" y "EFECTIVO" son TRES filas del arqueo: el cajero cuenta UN solo cajón y el
# sistema le pide contarlo tres veces, así que dos de las tres filas cierran con diferencia
# aunque no falte un sol. Peor: el guard del retiro leía la clave EXACTA "Efectivo", de modo
# que la plata escrita en minúscula no contaba como efectivo disponible.
#
# La normalización es de DOS piezas y hacen falta las dos:
#   * `clave_medio` (agrupación) ignora mayúsculas, tildes y espacios repetidos → consolida el
#     histórico YA ESCRITO al leer, sin migrar datos;
#   * `normalizar_medio` (escritura) devuelve el nombre canónico BONITO del catálogo
#     ("Depósito", no "DEPOSITO"), que es el que ve el cajero en el arqueo y el ticket.
# Un medio fuera del catálogo (el negocio que cobra por "Rappi") NO se inventa ni se
# capitaliza: se respeta tal como lo escribió el usuario la primera vez.


def clave_medio(medio):
    """Clave canónica de AGRUPACIÓN de un medio de pago: sin mayúsculas, sin tildes y con los
    espacios colapsados. 'EFECTIVO', ' efectivo ' y 'Efectivo' comparten clave; 'Deposito' y
    'Depósito' también (el POS acentúa y el teclado del cajero muchas veces no)."""
    txt = unicodedata.normalize("NFKD", str(medio or "").strip())
    txt = "".join(c for c in txt if not unicodedata.combining(c))
    return " ".join(txt.casefold().split())


# Catálogo de nombres canónicos indexado por clave (se construye una vez).
_CANONICO = {clave_medio(m): m for m in MEDIOS_ESTANDAR}


def normalizar_medio(medio):
    """Nombre canónico con el que se GUARDA (y se muestra) un medio de pago.

    Un medio del catálogo vuelve con su grafía bonita ('Depósito'); uno de fuera vuelve tal
    cual lo escribió el usuario, solo con los espacios colapsados —el negocio que cobra por
    'Rappi' no tiene por qué ver su medio reescrito—. Vacío = 'Efectivo', que es la inferencia
    de siempre (Contado sin medios detallados)."""
    txt = " ".join(str(medio or "").split())
    if not txt:
        return EFECTIVO
    return _CANONICO.get(clave_medio(txt), txt)


def es_efectivo(medio):
    """¿este medio es efectivo, escrito de cualquier forma? Lo usa el guard del retiro."""
    return clave_medio(medio) == clave_medio(EFECTIVO)


def sumar_medio(por_medio, medio, monto, sufijo=""):
    """Acumula `monto` en la fila canónica de `medio` dentro del dict `por_medio`.

    Agrupa por clave (case/tilde-insensitive) pero conserva como etiqueta el nombre canónico —o,
    fuera del catálogo, el PRIMERO que llegó—. `sufijo` namespacea la moneda extranjera
    ('Efectivo USD'), que cuadra contra su propio conteo físico. Muta y devuelve `por_medio`."""
    nombre = normalizar_medio(medio)
    if sufijo:
        nombre = "%s %s" % (nombre, sufijo)
    clave = clave_medio(nombre)
    for existente in por_medio:
        if clave_medio(existente) == clave:
            por_medio[existente] = _r2(por_medio[existente] + _r2(monto))
            return por_medio
    por_medio[nombre] = _r2(monto)
    return por_medio


def monto_efectivo(por_medio):
    """Efectivo EN SOLES de un por-medio, sumando el medio escrito de cualquier forma.

    Si el saldo quedó escrito en minúscula por un origen viejo, esa plata es real y tiene que
    contar. 'Efectivo USD' NO entra: es otra moneda y cuadra contra su propio conteo.
    (Desde C3 el guard del egreso usa `disponible_medio`, que vale para cualquier bolsillo;
    esta sigue siendo la lectura de «cuánto de esto es efectivo en soles».)"""
    return _r2(sum(v for k, v in (por_medio or {}).items() if es_efectivo(k)))


# ─────────────────────────────────────────── esperado por bolsillo (C3: egresos por medio)
#
# Hasta C3 el ingreso/retiro de caja era, por construcción, EFECTIVO: los totales llegaban aquí
# como dos números y se sumaban/restaban a la fila de Efectivo. Pero el negocio también paga al
# proveedor por Yape con la plata que entró por Yape, y eso no tenía dónde registrarse: el cajero
# o no lo anotaba (y el arqueo esperaba un saldo de Yape que ya no está) o lo anotaba como retiro
# de efectivo (y descuadraban DOS bolsillos a la vez, +X en Yape y −X en Efectivo).
#
# Por eso `ingresos`/`retiros` aceptan ahora un dict {medio: monto} ADEMÁS del número suelto. El
# número sigue funcionando exactamente igual —vale por «todo efectivo»—, que es lo que era y lo
# que sigue siendo cualquier movimiento histórico sin medio escrito. Ese doble contrato es la
# retrocompatibilidad: no hay que migrar una sola fila para que la aritmética siga cuadrando.


def _a_por_medio(valor):
    """Normaliza el ingreso/retiro a dict por medio. Un número = todo Efectivo (contrato viejo)."""
    if isinstance(valor, dict):
        out = {}
        for medio, monto in valor.items():
            sumar_medio(out, medio, monto)
        return out
    return {EFECTIVO: _r2(valor)}


def esperado_por_medio(saldo_inicial, por_medio, ingresos, retiros):
    """Lo que la caja debería tener de CADA bolsillo: ventas por medio + fondo inicial (que es
    efectivo por definición) + ingresos − retiros, cada uno por SU medio.

    Es la única fórmula del esperado: la usan el arqueo (calcular_arqueo) y el guard del egreso
    (disponible_medio). Tenerla dos veces sería tener dos verdades sobre el mismo dinero."""
    esperado = {}
    for medio, monto in (por_medio or {}).items():
        sumar_medio(esperado, medio, monto)
    # Siempre hay fila de Efectivo, aunque el turno no haya visto un sol en efectivo: es el cajón
    # y el cajero lo cuenta igual.
    sumar_medio(esperado, EFECTIVO, saldo_inicial)
    for medio, monto in _a_por_medio(ingresos).items():
        sumar_medio(esperado, medio, monto)
    for medio, monto in _a_por_medio(retiros).items():
        sumar_medio(esperado, medio, -_r2(monto))
    return esperado


def disponible_medio(saldo_inicial, por_medio, ingresos, retiros, medio):
    """Cuánto queda AHORA en un bolsillo concreto — el número contra el que rebota un egreso.

    No se puede sacar por Yape más de lo que entró por Yape: la caja no es una bolsa común. Se
    lee del MISMO esperado que verá el arqueo al cerrar, así el guard y el cierre no pueden
    discrepar. La comparación es por clave canónica (un retiro de 'yape' mira el saldo de
    'Yape')."""
    esperado = esperado_por_medio(saldo_inicial, por_medio, ingresos, retiros)
    clave = clave_medio(normalizar_medio(medio))
    return _r2(sum(v for k, v in esperado.items() if clave_medio(k) == clave))


def descuadre_arqueo(filas):
    """Magnitud del descuadre de un arqueo YA CONTADO — el número que se compara contra la
    tolerancia del RUC al cerrar. Devuelve 0.0 si el arqueo es parcial (sin conteo).

    Es el MAYOR entre dos lecturas, y hacen falta las dos:
      * la diferencia NETA (la suma algebraica de todas las filas), que es lo que el cajero ve
        como «me falta / me sobra» al pie del arqueo;
      * la mayor diferencia de UN medio, porque dos descuadres que se compensan suman cero y
        aun así son dos descuadres: +500 en Efectivo y −500 en Yape (la venta que se cobró por
        Yape y se registró como efectivo) cierra «cuadrado» con S/ 500 mal contados de cada
        lado. Mirando solo el neto, ese cierre pasaría en silencio — que es exactamente el
        agujero que esta rebanada vino a tapar.

    Ninguna de las dos domina a la otra (dos medios con +3 y +4 dan neto 7 y máximo 4), por eso
    se toma el máximo y no una sola.

    La fila en moneda extranjera ('Efectivo USD') se mide con la misma vara que las demás, sin
    tipo de cambio — igual que hace hoy `diferenciaTotal`, que ya las suma. Es conservador a
    propósito: un faltante de 10 dólares vale más que 10 soles, así que pedir explicación de más
    nunca es el error caro."""
    difs = [abs(_r2(f.get("diferencia"))) for f in (filas or [])
            if f.get("diferencia") is not None]
    if not difs:
        return 0.0
    neta = abs(_r2(sum(f.get("diferencia") or 0.0 for f in filas
                       if f.get("diferencia") is not None)))
    return max([neta] + difs)


def agrupar_ventas(ventas):
    """ventas: [{'total','moneda','formaPago','medios':[{'medio','monto'}]}]
    -> {'porMedio': {medio: monto}, 'count', 'total', 'sinMedio', 'countUsd', 'totalUsd'}
    Reglas v1: USD aparte (no entra a porMedio); Contado sin medios -> todo a 'Efectivo'
    (+1 sinMedio); Crédito suma solo sus medios (amortización inicial).
    Los medios se consolidan por clave canónica (ver `sumar_medio`): una venta cobrada en
    'efectivo' y otra en 'Efectivo' caen en la MISMA fila del arqueo."""
    por_medio = {}
    count = sin_medio = count_usd = 0
    total = total_usd = 0.0
    for v in ventas or []:
        moneda = (v.get("moneda") or "PEN").upper()
        monto_total = _r2(v.get("total"))
        if moneda != "PEN":
            # H1 (integridad): el efectivo en moneda extranjera SÍ entra a por_medio, bajo una
            # clave namespaced por moneda ('Efectivo USD'), para que se cuente en el arqueo y un
            # faltante en dólares descuadre. El namespacing evita contaminar el efectivo en soles
            # (y el guard de retiro, que lee el efectivo de por_medio). Sin tipo de cambio: cada
            # moneda cuadra contra su propio conteo físico. countUsd/totalUsd se preservan.
            count_usd += 1
            total_usd = _r2(total_usd + monto_total)
            medios = v.get("medios") or []
            forma = v.get("formaPago") or "Contado"
            if medios:
                for mp in medios:
                    sumar_medio(por_medio, mp.get("medio"), mp.get("monto"), sufijo=moneda)
            elif forma == "Contado":
                sumar_medio(por_medio, EFECTIVO, monto_total, sufijo=moneda)
            # Crédito en USD sin medios: por cobrar, no suma a ningún medio (igual que en PEN).
            continue
        count += 1
        total = _r2(total + monto_total)
        medios = v.get("medios") or []
        forma = v.get("formaPago") or "Contado"
        if medios:
            for mp in medios:
                sumar_medio(por_medio, mp.get("medio"), mp.get("monto"))
        elif forma == "Contado":
            # Contado sin medios detallados -> todo el total va a Efectivo (inferido).
            sumar_medio(por_medio, EFECTIVO, monto_total)
            sin_medio += 1
        # Crédito sin medios: por cobrar, no suma a ningún medio.
    return {
        "porMedio": por_medio,
        "count": count,
        "total": total,
        "sinMedio": sin_medio,
        "countUsd": count_usd,
        "totalUsd": total_usd,
    }


def calcular_arqueo(saldo_inicial, por_medio, ingresos, retiros, conteos):
    """-> (filas, esperado_total, contado_total, diferencia_total)
    filas = [{'medio','esperado','contado','diferencia'}] — unión de 'Efectivo' (siempre),
    medios con esperado distinto de 0 y medios contados. Efectivo esperado = saldo_inicial +
    ventas_efectivo + ingresos - retiros. Todo a 2 decimales.
    conteos: [{'medio','contado'}] o None. Con conteos=None (corte parcial) cada fila trae
    contado=None y diferencia=None, y los totales de contado/diferencia son None.
    ingresos/retiros: total en efectivo (número, contrato original) o dict {medio: monto} desde
    C3, para el egreso por Yape/Tarjeta. Ver `esperado_por_medio`.

    El cruce esperado×contado es case-insensitive por los dos lados (ver `clave_medio`): el
    por-medio puede traer historia escrita de cualquier forma y un cliente viejo puede mandar
    'efectivo' en el conteo. Dos filas de conteo con la misma clave se SUMAN (el cajero contó
    dos pilas del mismo cajón), en vez de que la última pisara a la anterior."""
    conteo_map = {}      # clave canónica -> contado
    conteo_nombre = {}   # clave canónica -> etiqueta a mostrar (la primera que llegó)
    if conteos:
        for c in conteos:
            bruto = (c.get("medio") or "").strip()
            if not bruto:
                continue
            clave = clave_medio(bruto)
            conteo_map[clave] = _r2(conteo_map.get(clave, 0.0) + _r2(c.get("contado")))
            conteo_nombre.setdefault(clave, normalizar_medio(bruto))
    # Esperado por bolsillo (fórmula única, compartida con el guard del egreso). Consolida de
    # paso el por-medio recibido: la historia ya escrita (y el seam de adelantos) puede traer
    # 'Efectivo' y 'efectivo' como claves distintas del mismo cajón.
    esperado = esperado_por_medio(saldo_inicial, por_medio, ingresos, retiros)
    # Unión ordenada: Efectivo primero, luego medios con esperado ≠ 0, luego contados extra.
    # ≠ 0 y no > 0: un esperado NEGATIVO (que solo puede venir de un egreso mayor que lo que
    # entró por ese medio, o de historia anterior al guard) tiene que verse en el arqueo, no
    # desaparecer de la hoja justo cuando indica que algo se registró mal.
    medios = [EFECTIVO]
    vistos = {clave_medio(EFECTIVO)}
    for candidato in list(esperado.keys()) + [conteo_nombre[k] for k in conteo_map]:
        clave = clave_medio(candidato)
        if clave in vistos:
            continue
        if _r2(esperado.get(candidato, 0.0)) != 0 or clave in conteo_map:
            medios.append(candidato)
            vistos.add(clave)
    parcial = not conteos
    filas = []
    esperado_total = 0.0
    contado_total = 0.0
    diferencia_total = 0.0
    for medio in medios:
        esp = _r2(esperado.get(medio, 0.0))
        esperado_total = _r2(esperado_total + esp)
        if parcial:
            filas.append({"medio": medio, "esperado": esp, "contado": None, "diferencia": None})
        else:
            con = _r2(conteo_map.get(clave_medio(medio), 0.0))
            dif = _r2(con - esp)
            contado_total = _r2(contado_total + con)
            diferencia_total = _r2(diferencia_total + dif)
            filas.append({"medio": medio, "esperado": esp, "contado": con, "diferencia": dif})
    if parcial:
        return filas, esperado_total, None, None
    return filas, esperado_total, contado_total, diferencia_total

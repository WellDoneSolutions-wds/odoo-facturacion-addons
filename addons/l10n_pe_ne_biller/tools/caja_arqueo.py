"""Aritmética pura del arqueo de caja (NE Express) — sin Odoo, testeable por carga de módulo.

agrupar_ventas: agrupa las ventas de la sesión por medio de pago aplicando las reglas v1
  (USD aparte; Contado sin medios -> Efectivo; Crédito suma solo sus medios).
calcular_arqueo: cruza el esperado por medio contra el conteo físico y devuelve las filas del
  arqueo + los totales. Toda la aritmética redondea a 2 decimales (evita el ruido flotante)."""

EFECTIVO = "Efectivo"
# Medios estándar del POS (deben coincidir con MEDIOS de src/lib/medios.ts): el cierre
# siembra una fila por cada uno aunque su esperado sea 0.
MEDIOS_ESTANDAR = ["Efectivo", "Yape", "Plin", "Tarjeta", "Transferencia", "Depósito"]


def _r2(n):
    return round(float(n or 0.0), 2)


def agrupar_ventas(ventas):
    """ventas: [{'total','moneda','formaPago','medios':[{'medio','monto'}]}]
    -> {'porMedio': {medio: monto}, 'count', 'total', 'sinMedio', 'countUsd', 'totalUsd'}
    Reglas v1: USD aparte (no entra a porMedio); Contado sin medios -> todo a 'Efectivo'
    (+1 sinMedio); Crédito suma solo sus medios (amortización inicial)."""
    por_medio = {}
    count = sin_medio = count_usd = 0
    total = total_usd = 0.0
    for v in ventas or []:
        moneda = (v.get("moneda") or "PEN").upper()
        monto_total = _r2(v.get("total"))
        if moneda != "PEN":
            count_usd += 1
            total_usd = _r2(total_usd + monto_total)
            continue
        count += 1
        total = _r2(total + monto_total)
        medios = v.get("medios") or []
        forma = v.get("formaPago") or "Contado"
        if medios:
            for mp in medios:
                medio = (mp.get("medio") or EFECTIVO).strip() or EFECTIVO
                por_medio[medio] = _r2(por_medio.get(medio, 0.0) + _r2(mp.get("monto")))
        elif forma == "Contado":
            # Contado sin medios detallados -> todo el total va a Efectivo (inferido).
            por_medio[EFECTIVO] = _r2(por_medio.get(EFECTIVO, 0.0) + monto_total)
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
    medios con esperado > 0 y medios contados. Efectivo esperado = saldo_inicial +
    ventas_efectivo + ingresos - retiros. Todo a 2 decimales.
    conteos: [{'medio','contado'}] o None. Con conteos=None (corte parcial) cada fila trae
    contado=None y diferencia=None, y los totales de contado/diferencia son None."""
    por_medio = dict(por_medio or {})
    conteo_map = {}
    if conteos:
        for c in conteos:
            medio = (c.get("medio") or "").strip()
            if medio:
                conteo_map[medio] = _r2(c.get("contado"))
    esperado = dict(por_medio)
    esperado[EFECTIVO] = _r2(_r2(saldo_inicial) + por_medio.get(EFECTIVO, 0.0)
                             + _r2(ingresos) - _r2(retiros))
    # Unión ordenada: Efectivo primero, luego medios con esperado > 0, luego contados extra.
    medios = [EFECTIVO]
    for candidato in list(esperado.keys()) + list(conteo_map.keys()):
        if candidato not in medios and (esperado.get(candidato, 0.0) > 0 or candidato in conteo_map):
            medios.append(candidato)
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
            con = _r2(conteo_map.get(medio, 0.0))
            dif = _r2(con - esp)
            contado_total = _r2(contado_total + con)
            diferencia_total = _r2(diferencia_total + dif)
            filas.append({"medio": medio, "esperado": esp, "contado": con, "diferencia": dif})
    if parcial:
        return filas, esperado_total, None, None
    return filas, esperado_total, contado_total, diferencia_total

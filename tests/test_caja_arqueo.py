"""Test puro (sin Odoo) de la aritmética del arqueo de caja.

Vive fuera del paquete del addon a propósito (igual que test_amount_to_words):
si estuviera dentro de addons/l10n_pe_ne_biller/tests/, pytest importaría la
cadena de __init__.py del addon y dispararía el modelo Odoo fuera de runtime.
Carga el módulo por ruta.
"""
import importlib.util
import pathlib

_p = (pathlib.Path(__file__).resolve().parent.parent
      / "addons" / "l10n_pe_ne_biller" / "tools" / "caja_arqueo.py")
_spec = importlib.util.spec_from_file_location("caja_arqueo", _p)
m = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(m)


def _venta(total, moneda="PEN", formaPago="Contado", medios=None):
    return {"total": total, "moneda": moneda, "formaPago": formaPago, "medios": medios or []}


def test_agrupar_contado_con_medios():
    agr = m.agrupar_ventas([_venta(100, medios=[{"medio": "Efectivo", "monto": 60},
                                                 {"medio": "Yape", "monto": 40}])])
    assert agr["porMedio"] == {"Efectivo": 60.0, "Yape": 40.0}
    assert agr["count"] == 1 and agr["total"] == 100.0 and agr["sinMedio"] == 0

def test_agrupar_contado_sin_medios_va_a_efectivo():
    agr = m.agrupar_ventas([_venta(50)])
    assert agr["porMedio"] == {"Efectivo": 50.0}
    assert agr["sinMedio"] == 1

def test_agrupar_credito_sin_medios_no_suma():
    agr = m.agrupar_ventas([_venta(200, formaPago="Credito")])
    assert agr["porMedio"] == {}
    assert agr["count"] == 1 and agr["total"] == 200.0 and agr["sinMedio"] == 0

def test_agrupar_credito_con_medios_suma_solo_medios():
    agr = m.agrupar_ventas([_venta(200, formaPago="Credito", medios=[{"medio": "Yape", "monto": 50}])])
    assert agr["porMedio"] == {"Yape": 50.0}
    assert agr["sinMedio"] == 0

def test_agrupar_usd_aparte():
    agr = m.agrupar_ventas([_venta(30, moneda="USD"), _venta(10)])
    assert agr["porMedio"] == {"Efectivo": 10.0}
    assert agr["count"] == 1 and agr["countUsd"] == 1 and agr["totalUsd"] == 30.0

def test_agrupar_mezcla():
    agr = m.agrupar_ventas([
        _venta(100, medios=[{"medio": "Efectivo", "monto": 60}, {"medio": "Yape", "monto": 40}]),
        _venta(50),                                      # contado sin medios -> Efectivo
        _venta(200, formaPago="Credito"),                # crédito sin medios -> nada
        _venta(80, formaPago="Credito", medios=[{"medio": "Plin", "monto": 80}]),
        _venta(25, moneda="USD"),                        # USD aparte
    ])
    assert agr["porMedio"] == {"Efectivo": 110.0, "Yape": 40.0, "Plin": 80.0}
    assert agr["count"] == 4 and agr["sinMedio"] == 1
    assert agr["countUsd"] == 1 and agr["totalUsd"] == 25.0

def test_calcular_efectivo_saldo_ingresos_retiros():
    filas, esp_t, con_t, dif_t = m.calcular_arqueo(
        150, {"Efectivo": 272.30, "Yape": 240.0}, ingresos=0, retiros=80,
        conteos=[{"medio": "Efectivo", "contado": 340.0}, {"medio": "Yape", "contado": 240.0}])
    por = {f["medio"]: f for f in filas}
    assert por["Efectivo"]["esperado"] == 342.30    # 150 + 272.30 + 0 - 80
    assert por["Efectivo"]["diferencia"] == -2.30
    assert por["Yape"]["esperado"] == 240.0 and por["Yape"]["diferencia"] == 0.0
    assert esp_t == 582.30 and con_t == 580.0 and dif_t == -2.30

def test_calcular_medio_contado_sin_ventas():
    filas, _e, _c, _d = m.calcular_arqueo(0, {}, 0, 0, conteos=[{"medio": "Tarjeta", "contado": 20.0}])
    por = {f["medio"]: f for f in filas}
    assert por["Tarjeta"]["esperado"] == 0.0 and por["Tarjeta"]["diferencia"] == 20.0

def test_calcular_medio_con_ventas_no_contado():
    filas, _e, _c, _d = m.calcular_arqueo(0, {"Yape": 40.0}, 0, 0, conteos=[{"medio": "Efectivo", "contado": 0.0}])
    por = {f["medio"]: f for f in filas}
    assert por["Yape"]["esperado"] == 40.0 and por["Yape"]["diferencia"] == -40.0

def test_calcular_efectivo_siempre_presente():
    filas, _e, _c, _d = m.calcular_arqueo(0, {}, 0, 0, conteos=[])
    assert filas[0]["medio"] == "Efectivo"

def test_calcular_redondeo_2_decimales():
    filas, esp_t, _c, _d = m.calcular_arqueo(0.1, {"Efectivo": 0.2}, 0, 0, conteos=None)
    assert filas[0]["esperado"] == 0.30 and esp_t == 0.30

def test_calcular_parcial_none():
    filas, esp_t, con_t, dif_t = m.calcular_arqueo(100, {"Efectivo": 50.0}, 0, 0, conteos=None)
    assert filas[0]["contado"] is None and filas[0]["diferencia"] is None
    assert con_t is None and dif_t is None and esp_t == 150.0

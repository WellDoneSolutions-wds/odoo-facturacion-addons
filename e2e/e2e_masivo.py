# -*- coding: utf-8 -*-
"""E2E de emisión masiva (QW10) contra SUNAT beta + ms-ne-biller local (:8090).

Corre dentro de `odoo-bin shell`:
  E2E_RESULTS_FILE=/tmp/r_masivo.json E2E_NO_COMMIT=1 \
    <odoo-bin> shell -c <conf> -d odoo_ne_biller --no-http < e2e/e2e_masivo.py

E2E_NO_COMMIT=1 (fijado también en proceso) suprime el commit por fila: los envíos a SUNAT
beta son efecto externo ya ocurrido; la BD local hace rollback al final (nada persiste)."""
import base64
import io
import json
import os

import xlsxwriter

os.environ["E2E_NO_COMMIT"] = "1"   # suprime el commit por fila de _masivo_can_commit

env = env  # provisto por odoo shell  # noqa: F821
RESULTS_FILE = os.environ.get("E2E_RESULTS_FILE", "/tmp/e2e_masivo_results.json")

company = env.company
if company.vat != "20321856145":
    company.write({"vat": "20321856145"})
company.sudo().l10n_pe_ne_api_key = "dev-biller-key-20321856145"
env.user.tz = "America/Lima"

_HEADERS = ["venta", "tipo", "serie", "fecha", "tipo doc cliente", "num doc cliente", "cliente",
            "codigo producto", "producto", "cantidad", "precio unitario", "descuento %",
            "afectacion", "bolsa", "moneda"]


def _xlsx_b64(rows):
    buf = io.BytesIO()
    wb = xlsxwriter.Workbook(buf, {"in_memory": True})
    ws = wb.add_worksheet("Ventas")
    for c, h in enumerate(_HEADERS):
        ws.write(0, c, h)
    for r, row in enumerate(rows, 1):
        for c, val in enumerate(row):
            ws.write(r, c, val)
    wb.close()
    return base64.b64encode(buf.getvalue()).decode("ascii")


# 3 ventas reales (factura 2 líneas gravadas · boleta exonerada+ICBPER a DNI · boleta público
# general) + 1 negativa (RUC con dígito verificador válido pero fuera del padrón beta).
ROWS = [
    ["V1", "FACTURA", "", "", "RUC", "20100070970", "EMPRESA CLIENTE SAC", "P1", "SERVICIO A", 1, 100.0, 0, "GRAVADO", "NO", "PEN"],
    ["V1", "FACTURA", "", "", "RUC", "20100070970", "EMPRESA CLIENTE SAC", "P2", "SERVICIO B", 2, 50.0, 0, "GRAVADO", "NO", "PEN"],
    ["", "BOLETA", "", "", "DNI", "45678912", "ROSA QUISPE", "", "AGUA 625ML", 1, 3.0, 0, "EXONERADO", "SI", "PEN"],
    ["", "BOLETA", "", "", "", "", "", "", "GASEOSA 500ML", 2, 2.5, 0, "GRAVADO", "NO", "PEN"],
    ["", "FACTURA", "", "", "RUC", "20123456786", "CLIENTE FUERA DE PADRON", "", "SERVICIO C", 1, 40.0, 0, "GRAVADO", "NO", "PEN"],
]

results = {}
try:
    with env.cr.savepoint():
        Lote = env["l10n_pe_ne.lote"]
        rep = Lote.l10n_pe_ne_crear_lote({"filename": "e2e-masivo.xlsx", "contentB64": _xlsx_b64(ROWS)})
        results["reporte"] = {"estado": rep["estado"], "errores": rep["errores"],
                              "totalComprobantes": rep["totalComprobantes"]}
        assert rep["estado"] == "validado", rep["errores"]
        lote = Lote.browse(rep["id"])
        for _ in range(rep["totalComprobantes"] + 2):
            if lote.estado in ("terminado", "cancelado"):
                break
            lote.l10n_pe_ne_procesar(max_filas=1)
        filas = [f._l10n_pe_ne_fila_dict() for f in lote.fila_ids.sorted("secuencia")]
        results["filas"] = filas
        results["estado_final"] = lote.estado
        results["emitidos"] = sum(1 for f in filas if f["estado"] == "emitido")
        res = lote.l10n_pe_ne_resultados()
        results["resultados_ok"] = bool(res["contentB64"]) and res["count"] == len(filas)
except Exception as exc:   # noqa: BLE001
    results["error"] = type(exc).__name__ + ": " + str(exc)[:300]

json.dump(results, open(RESULTS_FILE, "w"), ensure_ascii=False)
print("E2E_MASIVO_DONE", len(results.get("filas", [])), "filas ·", results.get("emitidos", 0), "emitidas")
env.cr.rollback()

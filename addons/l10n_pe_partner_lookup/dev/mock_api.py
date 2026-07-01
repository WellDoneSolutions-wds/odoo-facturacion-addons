#!/usr/bin/env python3
"""Backend de PRUEBA para el addon l10n_pe_partner_lookup.

Imita la API externa (la que en producción estará delante de DynamoDB):
responde a `GET /{documento}` con un JSON de cliente, o 404 si no existe.
Usa solo la librería estándar de Python: no requiere instalar nada.

Ejecutar (Windows, con el venv del proyecto):

    venv\\Scripts\\python addons\\l10n_pe_partner_lookup\\dev\\mock_api.py

Opciones por variables de entorno:
    MOCK_API_PORT   Puerto a escuchar (por defecto 8090).
    MOCK_API_KEY    Si la defines, se exige la cabecera 'x-api-key' con ese valor.

En Odoo → Ajustes → Contabilidad → «Búsqueda de cliente por DNI/RUC»:
    URL de la API : http://localhost:8090
    API Key       : (lo que pongas en MOCK_API_KEY, o vacío)

El servidor toma el ÚLTIMO segmento de la ruta como número de documento, así
que tanto http://localhost:8090  como  http://localhost:8090/documento
funcionan igual (llamará a /{doc} o /documento/{doc}).
"""
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

# --- Datos de ejemplo (DNI = 8 dígitos, RUC = 11 dígitos) ---------------------
# Ajusta/añade los que quieras para tus pruebas.
SAMPLE_DATA = {
    "20121888549": {
        "nroDocumento": "20121888549",
        "tipoDocumento": "RUC",
        "razonSocial": "Comercial Constructora los Patitos S.A.",
        "direccion": "Av. Pedro de Osma Nro. 434, Barranco, Lima",
        "estado": "ACTIVO",
    },
    "20100070970": {
        "nroDocumento": "20100070970",
        "tipoDocumento": "RUC",
        "razonSocial": "Supermercados Peruanos S.A.",
        "direccion": "Calle Morelli Nro. 181, San Borja, Lima",
        "estado": "ACTIVO",
    },
    "10000001": {
        "nroDocumento": "10000001",
        "tipoDocumento": "DNI",
        "nombre": "Juan Carlos Pérez Quispe",
        "direccion": "Jr. Las Flores 123, Miraflores, Lima",
        "estado": "ACTIVO",
    },
    "44556677": {
        "nroDocumento": "44556677",
        "tipoDocumento": "DNI",
        "nombre": "María Elena Rojas Huamán",
        "estado": "ACTIVO",
    },
}


class Handler(BaseHTTPRequestHandler):

    def _send_json(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        # Verificación de API key (solo si MOCK_API_KEY está definida).
        required_key = os.environ.get("MOCK_API_KEY")
        if required_key and self.headers.get("x-api-key") != required_key:
            return self._send_json(401, {"error": "API key inválida o ausente"})

        path = urlparse(self.path).path
        segments = [s for s in path.split("/") if s]

        # Sin segmentos => health check.
        if not segments:
            return self._send_json(200, {
                "status": "ok",
                "service": "mock_dni_ruc_api",
                "documentos_de_prueba": sorted(SAMPLE_DATA),
            })

        doc_number = segments[-1]
        record = SAMPLE_DATA.get(doc_number)
        if record is None:
            return self._send_json(404, {
                "error": "No encontrado",
                "nroDocumento": doc_number,
            })
        return self._send_json(200, record)

    def log_message(self, fmt, *args):
        # Log compacto a stderr.
        print("[mock_api] %s - %s" % (self.address_string(), fmt % args))


def main():
    port = int(os.environ.get("MOCK_API_PORT", "8090"))
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    key_note = " (con API key)" if os.environ.get("MOCK_API_KEY") else ""
    print("Mock DNI/RUC API escuchando en http://localhost:%d%s" % (port, key_note))
    print("Documentos de prueba: %s" % ", ".join(sorted(SAMPLE_DATA)))
    print("Ctrl+C para detener.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nDetenido.")
        server.server_close()


if __name__ == "__main__":
    main()

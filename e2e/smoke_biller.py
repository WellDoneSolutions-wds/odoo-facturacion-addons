"""Capa 0: postea el factura.json de referencia al biller y verifica UBL firmado."""
import json, sys, time, urllib.request, urllib.error, pathlib

SRC = pathlib.Path("/Users/joel/Desktop/wds-dir/fact/ms-ne-biller/src/main/resources/postman/factura.json")
payload = json.loads(SRC.read_text())
payload["id"]["correlativo"] = str(int(time.time()) % 100000000).zfill(8)  # único por corrida
data = json.dumps(payload).encode()
req = urllib.request.Request("http://localhost:8090/generator/factura", data=data,
                             headers={"Content-Type": "application/json"})
try:
    with urllib.request.urlopen(req, timeout=120) as r:
        code, body = r.getcode(), r.read().decode("utf-8", "replace")
except urllib.error.HTTPError as e:
    code, body = e.code, e.read().decode("utf-8", "replace")

print("HTTP", code)
firmado = code == 200 and "<Invoice" in body and "Signature" in body
print("XML firmado:", firmado)
print(body[:600])
sys.exit(0 if firmado else 1)

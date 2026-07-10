#!/usr/bin/env python3
"""
Servidor local para receber arquivos do SISDPU via POST do browser autenticado.
Grava cada arquivo na pasta de destino com o nome informado.
"""
import http.server
import json
import base64
from pathlib import Path

received = []

class Handler(http.server.BaseHTTPRequestHandler):
    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length)
            data = json.loads(body)
            filename = data["filename"]
            b64 = data["data"]
            # Suporta pasta de destino dinâmica via campo "target"
            target = data.get("target", "")

            # Usa pathlib para caminho correto em Windows
            if target:
                # Converte slashes para backslashes em Windows
                target_clean = target.replace("/", "\\")
                dest_dir = Path(target_clean)
                # Se o target for relativo (não começar com letra de drive), é relativo a base_dir
                if not dest_dir.is_absolute():
                    base_dir = Path(__file__).resolve().parents[1]  # sai de .claude
                    dest_dir = base_dir / target_clean
            else:
                base_dir = Path(__file__).resolve().parents[1]  # sai de .claude
                dest_dir = base_dir

            dest_dir.mkdir(parents=True, exist_ok=True)
            filepath = dest_dir / filename

            filepath.write_bytes(base64.b64decode(b64))
            received.append(filename)
            print(f"[OK] {filename} -> {filepath}")

            self.send_response(200)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"ok": True, "file": filename}).encode())
        except Exception as e:
            print(f"[ERR] {e}")
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"ok": False, "error": str(e)}).encode())

    def log_message(self, format, *args):
        pass  # silenciar logs HTTP

server = http.server.HTTPServer(("127.0.0.1", 8765), Handler)
print(f"Servidor iniciado em http://127.0.0.1:8765 — destino dinâmico via campo 'target'")
server.serve_forever()

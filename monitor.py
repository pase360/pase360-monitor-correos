#!/usr/bin/env python3
"""Monitor de solo lectura para los correos de Pase 360 en Odoo."""
import json
import os
import sys
import urllib.request
import urllib.error
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

TZ = ZoneInfo(os.getenv("REPORT_TIMEZONE", "America/Argentina/Cordoba"))

def required(name):
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Falta configurar el secreto {name}")
    return value

class Odoo:
    def __init__(self):
        self.url = required("ODOO_URL").rstrip("/")
        self.db = required("ODOO_DB")
        self.login = required("ODOO_USERNAME")
        self.key = required("ODOO_API_KEY")
        self.cookie = None
        self.uid = self._authenticate()

    def _post(self, path, payload):
        data = json.dumps(payload).encode()
        req = urllib.request.Request(
            self.url + path, data=data,
            headers={"Content-Type": "application/json", "User-Agent": "Pase360-Mail-Monitor/1.0"}
        )
        if self.cookie:
            req.add_header("Cookie", self.cookie)
        try:
            with urllib.request.urlopen(req, timeout=30) as response:
                raw = response.read().decode()
                cookie = response.headers.get("Set-Cookie")
                if cookie:
                    self.cookie = cookie.split(";", 1)[0]
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")[:500]
            raise RuntimeError(f"Odoo respondió HTTP {exc.code}: {detail}") from exc
        result = json.loads(raw)
        if result.get("error"):
            raise RuntimeError("Error de Odoo: " + json.dumps(result["error"], ensure_ascii=False)[:700])
        return result.get("result")

    def _authenticate(self):
        result = self._post("/web/session/authenticate", {
            "jsonrpc": "2.0", "method": "call",
            "params": {"db": self.db, "login": self.login, "password": self.key},
            "id": 1
        })
        if not result or not result.get("uid"):
            raise RuntimeError("Odoo no autenticó. Revisá URL, base, usuario y API key.")
        return result["uid"]

    def search_read(self, model, domain, fields, limit=500):
        return self._post("/web/dataset/call_kw", {
            "jsonrpc": "2.0", "method": "call", "id": 2,
            "params": {
                "model": model, "method": "search_read",
                "args": [domain],
                "kwargs": {"fields": fields, "limit": limit, "order": "create_date desc"}
            }
        }) or []

def telegram(text):
    token = required("TELEGRAM_BOT_TOKEN")
    chat_id = required("TELEGRAM_CHAT_ID")
    payload = json.dumps({"chat_id": chat_id, "text": text, "disable_web_page_preview": True}).encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=payload, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as response:
            result = json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"Telegram respondió HTTP {exc.code}: {exc.read().decode(errors='replace')[:300]}") from exc
    if not result.get("ok"):
        raise RuntimeError("Telegram no aceptó el mensaje: " + str(result)[:300])

def main():
    now = datetime.now(TZ)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    start_utc = today_start.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    end_utc = (today_start + timedelta(days=1)).astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    odoo = Odoo()
    # Se consulta mail.mail, sin escribir ni modificar registros.
    # El asunto identifica la invitación de Pase 360; puede ajustarse tras validar datos reales.
    domain = [
        ["create_date", ">=", start_utc],
        ["create_date", "<", end_utc],
        ["subject", "ilike", "Pase 360"]
    ]
    try:
        records = odoo.search_read(
            "mail.mail", domain,
            ["id", "subject", "email_to", "state", "failure_reason", "create_date", "email_from"],
            limit=1000
        )
    except Exception as exc:
        raise RuntimeError(
            "No se pudo leer mail.mail. El usuario de Odoo puede no tener permisos de lectura "
            "o el modelo/campo no estar disponible. Detalle: " + str(exc)
        ) from exc

    counts = {"outgoing": 0, "sent": 0, "exception": 0, "cancel": 0, "other": 0}
    failures = []
    for rec in records:
        state = rec.get("state") or "other"
        counts[state if state in counts else "other"] += 1
        if state == "exception" and len(failures) < 5:
            failures.append(f"- {rec.get('email_to') or '(sin destinatario)'}: {(rec.get('failure_reason') or 'sin detalle')[:180]}")

    lines = [
        "📬 PASE 360 | CONTROL DE CORREOS",
        f"Fecha: {now.strftime('%d/%m/%Y')}",
        "",
        f"Registros de invitación encontrados hoy: {len(records)}",
        f"Enviados (estado Odoo): {counts['sent']}",
        f"Pendientes: {counts['outgoing']}",
        f"Fallidos: {counts['exception']}",
        f"Cancelados: {counts['cancel']}",
        f"Otros estados: {counts['other']}",
        "",
        "⚠️ 'Enviado' significa que Odoo registró el envío; no confirma bandeja de entrada ni lectura.",
        "La consulta depende de que Odoo conserve los registros mail.mail."
    ]
    if failures:
        lines += ["", "Errores detectados:", *failures]
    telegram("\n".join(lines))

if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        message = "🚨 PASE 360 | ERROR DEL MONITOR\n" + str(exc)[:2500]
        try:
            telegram(message)
        except Exception as telegram_exc:
            print("No se pudo notificar por Telegram: " + str(telegram_exc), file=sys.stderr)
        print(message, file=sys.stderr)
        sys.exit(1)

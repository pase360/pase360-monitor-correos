#!/usr/bin/env python3
"""Monitor de solo lectura para los correos de Pase 360 en Odoo 19 (JSON-2)."""
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
        self.key = required("ODOO_API_KEY")

    def search_read(self, model, domain, fields, limit=500):
        payload = {
            "domain": domain,
            "fields": fields,
            "limit": limit,
            "order": "create_date desc",
        }
        data = json.dumps(payload).encode()
        req = urllib.request.Request(
            f"{self.url}/json/2/{model}/search_read",
            data=data,
            headers={
                "Authorization": f"bearer {self.key}",
                "X-Odoo-Database": self.db,
                "Content-Type": "application/json; charset=utf-8",
                "User-Agent": "Pase360-Mail-Monitor/1.0",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as response:
                result = json.loads(response.read().decode())
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")
            try:
                error = json.loads(detail)
                detail = error.get("message") or json.dumps(error, ensure_ascii=False)
            except (json.JSONDecodeError, AttributeError):
                pass
            raise RuntimeError(
                f"Error de Odoo JSON-2 (HTTP {exc.code}): {detail[:1200]}"
            ) from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"No se pudo conectar con Odoo: {exc.reason}") from exc

        if not isinstance(result, list):
            raise RuntimeError(
                "Odoo devolvió una respuesta inesperada al consultar mail.mail."
            )
        return result


def telegram(text):
    token = required("TELEGRAM_BOT_TOKEN")
    chat_id = required("TELEGRAM_CHAT_ID")
    payload = json.dumps({
        "chat_id": chat_id,
        "text": text,
        "disable_web_page_preview": True
    }).encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=payload,
        headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as response:
            result = json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        raise RuntimeError(
            f"Telegram respondió HTTP {exc.code}: "
            f"{exc.read().decode(errors='replace')[:300]}"
        ) from exc
    if not result.get("ok"):
        raise RuntimeError("Telegram no aceptó el mensaje: " + str(result)[:300])


def main():
    now = datetime.now(TZ)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    start_utc = today_start.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    end_utc = (today_start + timedelta(days=1)).astimezone(timezone.utc).strftime(
        "%Y-%m-%d %H:%M:%S"
    )

    odoo = Odoo()
    # Consulta exclusivamente de lectura: no crea ni modifica registros.
    domain = [
        ["create_date", ">=", start_utc],
        ["create_date", "<", end_utc],
        ["subject", "ilike", "Pase 360"]
    ]
    try:
        records = odoo.search_read(
            "mail.mail",
            domain,
            [
                "id", "subject", "email_to", "state",
                "failure_reason", "create_date", "email_from"
            ],
            limit=1000
        )
    except Exception as exc:
        raise RuntimeError(
            "No se pudo leer mail.mail. Puede faltar permiso de lectura "
            "o no estar disponible algún campo. Detalle: " + str(exc)
        ) from exc

    counts = {"outgoing": 0, "sent": 0, "exception": 0, "cancel": 0, "other": 0}
    failures = []
    for rec in records:
        state = rec.get("state") or "other"
        counts[state if state in counts else "other"] += 1
        if state == "exception" and len(failures) < 5:
            failures.append(
                f"- {rec.get('email_to') or '(sin destinatario)'}: "
                f"{(rec.get('failure_reason') or 'sin detalle')[:180]}"
            )

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
        "⚠️ 'Enviado' significa que Odoo registró el envío; "
        "no confirma bandeja de entrada ni lectura.",
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
            print(
                "No se pudo notificar por Telegram: " + str(telegram_exc),
                file=sys.stderr
            )
        print(message, file=sys.stderr)
        sys.exit(1)

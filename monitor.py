#!/usr/bin/env python3
"""Monitor de solo lectura para correo y WhatsApp de Pase 360 en Odoo 19."""
import json
import os
import sys
import urllib.request
import urllib.error
from collections import Counter
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

    def call(self, model, method, payload):
        data = json.dumps(payload).encode()
        req = urllib.request.Request(
            f"{self.url}/json/2/{model}/{method}",
            data=data,
            headers={
                "Authorization": f"bearer {self.key}",
                "X-Odoo-Database": self.db,
                "Content-Type": "application/json; charset=utf-8",
                "User-Agent": "Pase360-Mail-Monitor/1.1",
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
        return result

    def search_read(self, model, domain, fields, limit=500):
        result = self.call(model, "search_read", {
            "domain": domain,
            "fields": fields,
            "limit": limit,
            "order": "create_date desc",
        })
        if not isinstance(result, list):
            raise RuntimeError(f"Respuesta inesperada al consultar {model}.")
        return result

    def fields_get(self, model):
        return self.call(model, "fields_get", {
            "attributes": ["string", "type", "selection", "relation"]
        })


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


def date_domain(start_utc, end_utc):
    return [
        ["create_date", ">=", start_utc],
        ["create_date", "<", end_utc],
    ]


def whatsapp_report(odoo, start_utc, end_utc):
    """Descubre el campo de estado real y resume WhatsApp sin exponer destinatarios."""
    try:
        meta = odoo.fields_get("whatsapp.message")
        if not isinstance(meta, dict) or not meta:
            return [
                "WhatsApp: Odoo no devolvió los campos del modelo whatsapp.message.",
                "Se necesita revisar el modelo o los permisos de lectura."
            ]

        status_field = next(
            (name for name in ("state", "status") if name in meta),
            None
        )
        date_field = "create_date" if "create_date" in meta else None
        if not status_field:
            return [
                "WhatsApp: se encontró el modelo, pero no el campo de estado esperado.",
                "Campos técnicos de estado disponibles: "
                + ", ".join(
                    name for name, info in meta.items()
                    if any(word in name.lower() for word in ("state", "status"))
                )
            ]

        selected_fields = ["id", status_field]
        if date_field:
            selected_fields.append(date_field)
        domain = date_domain(start_utc, end_utc) if date_field else []
        records = odoo.search_read(
            "whatsapp.message", domain, selected_fields, limit=1000
        )

        selection = meta[status_field].get("selection") or []
        labels = {str(key): str(label) for key, label in selection}
        counts = Counter(str(row.get(status_field) or "sin_estado") for row in records)

        lines = [
            "📱 PASE 360 | CONTROL DE WHATSAPP",
            f"Mensajes registrados en los últimos 7 días (prueba): {len(records)}",
            f"Campo de estado leído en Odoo: {status_field}",
        ]
        if not records:
            lines.append("No se encontraron mensajes en los últimos 7 días en whatsapp.message.")
        for key, amount in sorted(counts.items()):
            label = labels.get(key, key)
            lines.append(f"{label}: {amount}")
        lines.append("Estados obtenidos de los registros de Odoo; no se muestran teléfonos ni textos.")
        return lines
    except Exception as exc:
        return [
            "📱 PASE 360 | CONTROL DE WHATSAPP",
            "No se pudo consultar whatsapp.message.",
            f"Diagnóstico: {str(exc)[:700]}"
        ]


def main():
    now = datetime.now(TZ)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    start_utc = today_start.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    end_utc = (today_start + timedelta(days=1)).astimezone(timezone.utc).strftime(
        "%Y-%m-%d %H:%M:%S"
    )

    odoo = Odoo()
    mail_domain = date_domain(start_utc, end_utc) + [
        ["subject", "ilike", "Pase 360"]
    ]
    try:
        mail_records = odoo.search_read(
            "mail.mail",
            mail_domain,
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

    mail_counts = {
        "outgoing": 0, "sent": 0, "exception": 0, "cancel": 0, "other": 0
    }
    failures = []
    for rec in mail_records:
        state = rec.get("state") or "other"
        mail_counts[state if state in mail_counts else "other"] += 1
        if state == "exception" and len(failures) < 5:
            failures.append(
                f"- {(rec.get('failure_reason') or 'sin detalle')[:180]}"
            )

    lines = [
        "📬 PASE 360 | CONTROL DIARIO",
        f"Fecha: {now.strftime('%d/%m/%Y')}",
        "",
        "✉️ CORREOS",
        f"Registros con asunto Pase 360: {len(mail_records)}",
        f"Enviados (estado Odoo): {mail_counts['sent']}",
        f"Pendientes: {mail_counts['outgoing']}",
        f"Fallidos: {mail_counts['exception']}",
        f"Cancelados: {mail_counts['cancel']}",
        f"Otros estados: {mail_counts['other']}",
        "",
        "⚠️ El estado enviado no confirma entrega ni apertura del correo.",
        "Las aperturas solo se informarán si la automatización ya registra seguimiento compatible.",
        "",
    ]
    if failures:
        lines += ["Errores de correo:", *failures, ""]

    whatsapp_start_utc = (now - timedelta(days=7)).astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    whatsapp_end_utc = now.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    lines.extend(whatsapp_report(odoo, whatsapp_start_utc, whatsapp_end_utc))
    lines += [
        "",
        "Informe de solo lectura. No se modificaron mensajes ni automatizaciones."
    ]
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

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
            f"Mensajes registrados ayer: {len(records)}",
            f"Campo de estado leído en Odoo: {status_field}",
        ]
        if not records:
            lines.append("No se encontraron mensajes de ayer en whatsapp.message.")
        traducciones = {
            "delivered": "Entregado",
            "failed": "Fallido",
            "read": "Leído",
            "received": "Recibido",
            "sent": "Enviado",
            "pending": "Pendiente",
            "accepted": "Aceptado",
            "queued": "En cola",
        }
        for key, amount in sorted(counts.items()):
            original = labels.get(key, key)
            label = traducciones.get(str(key).lower(),
                     traducciones.get(str(original).lower(), str(original)))
            lines.append(f"{label}: {amount}")
        lines.append("Estados obtenidos de los registros de Odoo; no se muestran teléfonos ni textos.")
        return lines
    except Exception as exc:
        return [
            "📱 PASE 360 | CONTROL DE WHATSAPP",
            "No se pudo consultar whatsapp.message.",
            f"Diagnóstico: {str(exc)[:700]}"
        ]


def mail_message_diagnostic(odoo, start_utc, end_utc):
    """Cuenta mensajes del chatter sin leer ni mostrar asunto, direcciones o cuerpo."""
    lines = ["", "🔍 DIAGNÓSTICO DE MENSAJES EN ODOO"]
    try:
        meta = odoo.fields_get("mail.message")
        if not isinstance(meta, dict) or not meta:
            return lines + ["• mail.message: modelo sin campos visibles."]
        if "create_date" not in meta:
            return lines + ["• mail.message: disponible, pero sin create_date para filtrar el día."]
        fields = ["id"]
        if "message_type" in meta:
            fields.append("message_type")
        records = odoo.search_read(
            "mail.message", date_domain(start_utc, end_utc), fields, limit=1000
        )
        lines.append(f"• Registros mail.message creados ayer: {len(records)}")
        if "message_type" in fields:
            counts = Counter(str(row.get("message_type") or "sin_tipo") for row in records)
            for kind, amount in sorted(counts.items()):
                lines.append(f"  - Tipo {kind}: {amount}")
        else:
            lines.append("• El modelo no expone message_type; no se puede separar correo de otros mensajes.")
        relevant = [
            name for name in meta
            if any(term in name.lower() for term in (
                "message_type", "email", "mail", "notification", "failure",
                "date", "model", "res_id"
            ))
        ]
        lines.append("• Campos técnicos relacionados disponibles: " +
                     (", ".join(relevant[:30]) if relevant else "ninguno identificado"))
    except Exception as exc:
        lines.append(f"• mail.message: no se pudo consultar ({str(exc)[:220]}).")
    lines.append("Solo se cuentan registros; no se leen ni muestran destinatarios ni contenido.")
    return lines


def tracking_diagnostic(odoo):
    """Inspecciona modelos de seguimiento sin leer destinatarios ni contenido."""
    candidates = (
        "mailing.trace",
        "mail.tracking.email",
        "mail.tracking.event",
    )
    lines = ["", "🔎 DIAGNÓSTICO DE SEGUIMIENTO DE CORREO"]
    interesting = (
        "state", "status", "trace_status", "open", "opened",
        "open_datetime", "sent_datetime", "delivered", "bounce",
        "mailing_id", "mail_message_id", "model", "res_id",
        "create_date", "email"
    )
    for model in candidates:
        try:
            meta = odoo.fields_get(model)
            if not isinstance(meta, dict) or not meta:
                lines.append(f"• {model}: modelo sin campos visibles.")
                continue
            found = [
                name for name in meta
                if any(term in name.lower() for term in interesting)
            ]
            lines.append(f"• {model}: disponible; campos relevantes: "
                         + (", ".join(found[:25]) if found else "ninguno identificado"))
            for name in found:
                selection = meta[name].get("selection")
                if selection and any(word in name.lower()
                                     for word in ("state", "status", "trace")):
                    labels = ", ".join(f"{key}={label}" for key, label in selection)
                    lines.append(f"  - Valores de {name}: {labels[:350]}")
        except Exception as exc:
            detail = str(exc).replace("\\n", " ")[:180]
            lines.append(f"• {model}: no disponible o sin permiso de lectura ({detail}).")
    lines.append("Este diagnóstico solo consulta metadatos; no modifica registros.")
    return lines


def main():
    now = datetime.now(TZ)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    yesterday_start = today_start - timedelta(days=1)
    start_utc = yesterday_start.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    end_utc = today_start.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    odoo = Odoo()
    try:
        # Primero cuenta todos los registros conservados de mail.mail del día,
        # sin mostrar asuntos, direcciones ni destinatarios en el informe.
        all_mail_records = odoo.search_read(
            "mail.mail",
            date_domain(start_utc, end_utc),
            ["id", "subject", "state", "failure_reason", "create_date"],
            limit=1000
        )
        # Luego identifica únicamente los que parecen corresponder a Pase 360.
        # Se filtra localmente para poder comparar ambos totales.
        mail_records = [
            rec for rec in all_mail_records
            if "pase 360" in str(rec.get("subject") or "").lower()
        ]
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
        f"Día informado: {yesterday_start.strftime('%d/%m/%Y')}",
        "",
        "✉️ ESTADO DE LOS CORREOS",
        f"Registros mail.mail de todos los asuntos (ayer): {len(all_mail_records)}",
        f"Registros cuyo asunto contiene Pase 360: {len(mail_records)}",
        f"Enviados por Odoo: {mail_counts['sent']}",
        f"Pendientes de envío: {mail_counts['outgoing']}",
        f"Con error de envío: {mail_counts['exception']}",
        f"Cancelados: {mail_counts['cancel']}",
        f"Otros estados: {mail_counts['other']}",
        "",
        "Qué significa cada estado:",
        "• Enviados por Odoo: Odoo completó el envío al servidor de correo; no confirma llegada a la bandeja.",
        "• Pendientes: Odoo todavía los tiene en cola de envío.",
        "• Con error: Odoo registró un problema al intentar enviarlos.",
        "• Cancelados: el envío fue cancelado.",
        "• Otros: estado distinto de los anteriores.",
        "",
        "El conteo Pase 360 se basa en registros mail.mail conservados y asuntos que contienen esa expresión.",
        "Un cero no demuestra que no se hayan enviado correos: Odoo puede usar otro asunto o eliminar registros.",
        "Este informe todavía no verifica rebotes en Gmail ni aperturas; tampoco confirma lectura humana.",
        "",
    ]
    if failures:
        lines += ["Errores de correo:", *failures, ""]

    lines.extend(mail_message_diagnostic(odoo, start_utc, end_utc))
    lines.extend(tracking_diagnostic(odoo))
    lines.append("")
    lines.extend(whatsapp_report(odoo, start_utc, end_utc))
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

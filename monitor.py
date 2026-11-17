#!/usr/bin/env python3
"""Monitor de solo lectura para correo y WhatsApp de Pase 360 en Odoo 19."""
import json
import os
import sys
import urllib.request
import urllib.error
import urllib.parse
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

    def search_read(self, model, domain, fields, limit=500, order="create_date desc"):
        result = self.call(model, "search_read", {
            "domain": domain,
            "fields": fields,
            "limit": limit,
            "order": order,
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
    """Cuenta eventos diarios de WhatsApp usando el historial de tracking de Odoo."""
    unavailable = ["Enviados: No disponible", "Entregados: No disponible", "Leídos: No disponible"]
    try:
        wa_meta = odoo.fields_get("whatsapp.message")
        tracking_meta = odoo.fields_get("mail.tracking.value")
        message_meta = odoo.fields_get("mail.message")
        field_meta = odoo.fields_get("ir.model.fields")

        if not all(isinstance(x, dict) and x for x in (wa_meta, tracking_meta, message_meta, field_meta)):
            print("WhatsApp: no están disponibles todos los modelos necesarios para leer el historial.", file=sys.stderr)
            return unavailable

        status_field = next((name for name in ("state", "status", "estado") if name in wa_meta), None)
        if not status_field:
            print("WhatsApp: no se encontró el campo de estado.", file=sys.stderr)
            return unavailable

        wa_link_field = next((
            name for name, info in message_meta.items()
            if info.get("relation") == "whatsapp.message"
        ), None)
        tracking_message_field = next((
            name for name, info in tracking_meta.items()
            if info.get("relation") == "mail.message"
        ), None)
        tracking_field_field = next((
            name for name, info in tracking_meta.items()
            if info.get("relation") == "ir.model.fields"
        ), None)

        print("WhatsApp: campo de estado=" + status_field, file=sys.stderr)
        print("WhatsApp: vínculo mail.message → WhatsApp=" + str(wa_link_field), file=sys.stderr)
        print("WhatsApp: vínculo tracking → mail.message=" + str(tracking_message_field), file=sys.stderr)
        print("WhatsApp: vínculo tracking → campo=" + str(tracking_field_field), file=sys.stderr)

        if not all((wa_link_field, tracking_message_field, tracking_field_field)):
            print("WhatsApp: faltan relaciones técnicas para enlazar el historial.", file=sys.stderr)
            return unavailable
        if "create_date" not in tracking_meta:
            print("WhatsApp: mail.tracking.value no expone create_date.", file=sys.stderr)
            return unavailable

        track_fields = ["id", "create_date", tracking_message_field, tracking_field_field]
        for name in (
            "old_value_char", "new_value_char", "old_value_text", "new_value_text",
            "old_value_integer", "new_value_integer", "old_value_float", "new_value_float",
            "old_value_datetime", "new_value_datetime"
        ):
            if name in tracking_meta:
                track_fields.append(name)

        tracking_rows = odoo.search_read(
            "mail.tracking.value",
            [["create_date", ">=", start_utc], ["create_date", "<", end_utc]],
            track_fields, limit=5000
        )
        print(f"WhatsApp: cambios de tracking creados en el día={len(tracking_rows)}", file=sys.stderr)

        def relation_id(value):
            if isinstance(value, int):
                return value
            if isinstance(value, (list, tuple)) and value and isinstance(value[0], int):
                return value[0]
            return None

        field_ids = sorted({
            relation_id(row.get(tracking_field_field))
            for row in tracking_rows
            if relation_id(row.get(tracking_field_field)) is not None
        })
        if not field_ids:
            print("WhatsApp: el historial del día no contiene campos rastreados.", file=sys.stderr)
            return ["Enviados: 0", "Entregados: 0", "Leídos: 0"]

        field_rows = odoo.search_read(
            "ir.model.fields", [["id", "in", field_ids]],
            ["id", "model", "name"], limit=5000
        )
        state_field_ids = {
            row["id"] for row in field_rows
            if row.get("model") == "whatsapp.message" and row.get("name") == status_field
        }
        if not state_field_ids:
            print("WhatsApp: no se encontraron cambios del campo de estado en el historial diario.", file=sys.stderr)
            # Si el modelo y las relaciones se consultaron correctamente, cero cambios es un resultado válido.
            return ["Enviados: 0", "Entregados: 0", "Leídos: 0"]

        relevant_tracking = [
            row for row in tracking_rows
            if relation_id(row.get(tracking_field_field)) in state_field_ids
            and relation_id(row.get(tracking_message_field)) is not None
        ]
        mail_message_ids = sorted({
            relation_id(row.get(tracking_message_field)) for row in relevant_tracking
        })
        linked_messages = odoo.search_read(
            "mail.message", [["id", "in", mail_message_ids]],
            ["id", wa_link_field], limit=5000
        ) if mail_message_ids else []

        whatsapp_ids = set()
        for message in linked_messages:
            links = message.get(wa_link_field) or []
            if isinstance(links, list):
                for item in links:
                    if isinstance(item, int):
                        whatsapp_ids.add(item)
                    elif isinstance(item, (list, tuple)) and item and isinstance(item[0], int):
                        whatsapp_ids.add(item[0])

        if not whatsapp_ids:
            print("WhatsApp: el historial del día no se pudo vincular a mensajes WhatsApp.", file=sys.stderr)
            return unavailable

        wa_records = odoo.search_read(
            "whatsapp.message", [["id", "in", sorted(whatsapp_ids)]],
            ["id", status_field], limit=5000
        )
        valid_wa_ids = {row["id"] for row in wa_records}
        print(f"WhatsApp: mensajes vinculados al historial={len(valid_wa_ids)}", file=sys.stderr)

        selection = wa_meta[status_field].get("selection") or []
        status_labels = {}
        for key, label in selection:
            status_labels[str(key).strip().lower()] = str(label).strip().lower()

        def normalized(value):
            return str(value or "").strip().casefold()

        counts = Counter()
        matched_events = 0
        for row in relevant_tracking:
            message_id = relation_id(row.get(tracking_message_field))
            linked = next((m for m in linked_messages if m.get("id") == message_id), None)
            if not linked:
                continue
            links = linked.get(wa_link_field) or []
            ids = set()
            if isinstance(links, list):
                for item in links:
                    if isinstance(item, int):
                        ids.add(item)
                    elif isinstance(item, (list, tuple)) and item and isinstance(item[0], int):
                        ids.add(item[0])
            if not (ids & valid_wa_ids):
                continue

            new_values = [
                row.get(name) for name in (
                    "new_value_char", "new_value_text", "new_value_integer",
                    "new_value_float", "new_value_datetime"
                ) if name in row and row.get(name) not in (None, False, "")
            ]
            if not new_values:
                continue
            raw = " ".join(normalized(value) for value in new_values)
            # Reconoce tanto las claves técnicas como las etiquetas traducidas de Odoo.
            sent_terms = ("sent", "envi", "send")
            delivered_terms = ("delivered", "entreg")
            read_terms = ("read", "leíd", "leido", "leer")
            if any(term in raw for term in read_terms):
                counts["read"] += 1
            elif any(term in raw for term in delivered_terms):
                counts["delivered"] += 1
            elif any(term in raw for term in sent_terms):
                counts["sent"] += 1
            else:
                continue
            matched_events += 1

        print(f"WhatsApp: cambios de estado identificados y vinculados={matched_events}", file=sys.stderr)
        print("WhatsApp: conteo basado en create_date del historial, no en write_date del mensaje.", file=sys.stderr)
        return [
            f"Enviados: {counts['sent']}",
            f"Entregados: {counts['delivered']}",
            f"Leídos: {counts['read']}",
        ]
    except Exception as exc:
        print(f"WhatsApp: error consultando historial ({type(exc).__name__}: {str(exc)[:350]}).", file=sys.stderr)
        return unavailable

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
        lines.append(f"• Registros mail.message creados ese día: {len(records)}")
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



def mail_notification_diagnostic(odoo, mail_records, start_utc, end_utc):
    """Investiga vínculos mail.mail -> mail.message -> notification_ids sin datos personales."""
    lines = ["", "📨 DIAGNÓSTICO DE ENTREGA DE CORREO (mail.notification)"]
    try:
        meta = odoo.fields_get("mail.notification")
        if not isinstance(meta, dict) or not meta:
            return lines + ["• mail.notification: modelo sin campos visibles."]

        mail_ids = [row["id"] for row in mail_records if row.get("id")]
        if not mail_ids:
            return lines + ["• No hay correos Pase 360 conservados para consultar."]

        mail_meta = odoo.fields_get("mail.mail")
        mail_link_count = sum(
            1 for row in mail_records
            if row.get("mail_message_id") not in (None, False, [], ())
        )
        lines.append(f"• Correos Pase 360 consultados: {len(mail_ids)}")
        lines.append(f"• Correos con vínculo mail_message_id informado: {mail_link_count}")

        msg_meta = odoo.fields_get("mail.message")
        msg_fields = ["id"]
        if "message_type" in msg_meta:
            msg_fields.append("message_type")
        if "notification_ids" in msg_meta:
            msg_fields.append("notification_ids")
        messages = odoo.search_read(
            "mail.message", date_domain(start_utc, end_utc), msg_fields, limit=1000
        )
        outgoing = [
            row for row in messages
            if row.get("message_type") == "email_outgoing"
        ] if "message_type" in msg_fields else []
        lines.append(f"• Mensajes email_outgoing del día: {len(outgoing)}")

        notification_ids = set()
        if "notification_ids" in msg_fields:
            for row in outgoing:
                value = row.get("notification_ids") or []
                if isinstance(value, list):
                    for item in value:
                        if isinstance(item, int):
                            notification_ids.add(item)
                        elif isinstance(item, (list, tuple)) and item and isinstance(item[0], int):
                            notification_ids.add(item[0])
            lines.append(f"• IDs de notificación enlazados desde mail.message: {len(notification_ids)}")

        candidates = (
            "notification_type", "notification_status", "failure_type",
            "failure_reason", "read_date", "is_read"
        )
        fields = ["id"] + [
            name for name in ("mail_mail_id", "mail_message_id") if name in meta
        ] + [name for name in candidates if name in meta]

        records = []
        relation_used = None
        if notification_ids:
            records = odoo.search_read(
                "mail.notification", [["id", "in", sorted(notification_ids)]],
                fields, limit=2000, order="id desc"
            )
            relation_used = "mail.message.notification_ids"

        if not records and "mail_mail_id" in meta:
            records = odoo.search_read(
                "mail.notification", [["mail_mail_id", "in", mail_ids]],
                fields, limit=2000, order="id desc"
            )
            if records:
                relation_used = "mail_mail_id"

        if not records and "mail_message_id" in meta:
            message_ids = []
            for row in mail_records:
                value = row.get("mail_message_id")
                if isinstance(value, (list, tuple)) and value and isinstance(value[0], int):
                    message_ids.append(value[0])
                elif isinstance(value, int):
                    message_ids.append(value)
            if message_ids:
                records = odoo.search_read(
                    "mail.notification",
                    [["mail_message_id", "in", message_ids]],
                    fields, limit=2000, order="id desc"
                )
                if records:
                    relation_used = "mail_message_id"

        lines.append(f"• Notificaciones relacionadas encontradas: {len(records)}")
        lines.append(f"• Vínculo utilizado: {relation_used or 'ninguno; no se encontraron relaciones'}")

        if "notification_type" in meta and records:
            email_records = [r for r in records if r.get("notification_type") == "email"]
            lines.append(f"• Notificaciones de tipo email: {len(email_records)}")
        else:
            email_records = records

        for field in ("notification_status", "failure_type"):
            if field not in meta:
                continue
            selection = meta[field].get("selection") or []
            labels = {str(k): str(v) for k, v in selection}
            counts = Counter(str(r.get(field) or "sin dato") for r in email_records)
            title = "Estado de notificación" if field == "notification_status" else "Tipo de fallo"
            lines.append("• " + title + ": " + (
                "; ".join(f"{labels.get(k,k)}: {v}" for k,v in sorted(counts.items()))
                if counts else "sin registros"
            ))

        if "read_date" in meta:
            read_count = sum(1 for r in email_records if r.get("read_date"))
            lines.append(f"• Notificaciones con read_date: {read_count}")
            lines.append("  No equivale a apertura del email por el cliente.")
        lines.append("• La confirmación de entrega a bandeja y las aperturas requieren eventos del servidor o seguimiento de correo.")
        lines.append("No se consultaron ni mostraron destinatarios, direcciones ni contenido.")
    except Exception as exc:
        lines.append(f"• Diagnóstico de mail.notification incompleto: {str(exc)[:350]}.")
    return lines


def mail_server_diagnostic(odoo):
    """Identifica el servidor saliente sin exponer usuarios, contraseñas ni direcciones."""
    lines = ["", "🛰️ SERVIDOR DE CORREO SALIENTE"]
    try:
        meta = odoo.fields_get("ir.mail_server")
        if not isinstance(meta, dict) or not meta:
            return lines + ["• No se pudieron consultar los servidores salientes."]
        wanted = [name for name in ("id", "name", "smtp_host", "smtp_port", "active", "sequence") if name in meta]
        if "smtp_host" not in wanted and "name" not in wanted:
            return lines + ["• El modelo existe, pero no expone el nombre ni el host del servidor."]
        domain = [["active", "=", True]] if "active" in meta else []
        rows = odoo.search_read("ir.mail_server", domain, wanted, limit=20, order="sequence asc" if "sequence" in meta else "id asc")
        if not rows:
            lines.append("• No se encontraron servidores salientes activos configurados.")
            lines.append("• Odoo podría estar utilizando su servidor de correo predeterminado.")
            return lines
        for i, row in enumerate(rows, 1):
            name = str(row.get("name") or "Servidor sin nombre")[:80]
            host = str(row.get("smtp_host") or "host no informado")[:120]
            port = row.get("smtp_port")
            lines.append(f"• Servidor {i}: {name}")
            lines.append(f"  Host: {host}" + (f"; puerto: {port}" if port else ""))
        lines.append("No se consultaron usuarios, contraseñas ni direcciones de correo.")
    except Exception as exc:
        lines.append(f"• No se pudo identificar el servidor saliente ({str(exc)[:300]}).")
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



def email_open_report(start_utc, end_utc):
    """Devuelve el total de correos identificados por el píxel."""
    secret = os.getenv("REPORT_SECRET", "").strip()
    if not secret:
        print("Aperturas: falta configurar REPORT_SECRET en GitHub Actions.", file=sys.stderr)
        return "Abiertos: No disponible"
    params = urllib.parse.urlencode({"from": start_utc, "to": end_utc})
    url = "https://pase360-registro-aperturas.infopase360.workers.dev/stats?" + params
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {secret}"}, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=20) as response:
            result = json.loads(response.read().decode())
        return f"Abiertos: {int(result.get('correos_identificados', 0))}"
    except urllib.error.HTTPError as exc:
        print(f"Aperturas: el Worker respondió HTTP {exc.code}.", file=sys.stderr)
        return "Abiertos: No disponible"
    except urllib.error.URLError as exc:
        print(f"Aperturas: no se pudo conectar con el Worker ({exc.reason}).", file=sys.stderr)
        return "Abiertos: No disponible"
    except Exception as exc:
        print(f"Aperturas: respuesta inválida del Worker ({type(exc).__name__}).", file=sys.stderr)
        return "Abiertos: No disponible"



def main():
    # El informe siempre consulta el día calendario anterior, tanto manual como automático.
    today = datetime.now(TZ).replace(hour=0, minute=0, second=0, microsecond=0)
    report_date = today - timedelta(days=1)
    next_date = report_date + timedelta(days=1)
    start_utc = report_date.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    end_utc = next_date.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    odoo = Odoo()
    try:
        # Primero cuenta todos los registros conservados de mail.mail del día,
        # sin mostrar asuntos, direcciones ni destinatarios en el informe.
        all_mail_records = odoo.search_read(
            "mail.mail",
            date_domain(start_utc, end_utc),
            ["id", "subject", "state", "failure_reason", "create_date", "mail_message_id"],
            limit=1000
        )
        # Reconoce las cuatro plantillas por sus frases distintivas, no por la marca.
        frases_plantillas = (
            "invitación para adherir",
            "multiplica tus ventas con la comunidad de beneficios de pase 360",
            "crea credenciales qr de beneficios para los miembros de tu organización",
            "no dejes pasar la oportunidad de pertenecer a la red de beneficios pase 360",
        )
        mail_records = [
            rec for rec in all_mail_records
            if any(frase in str(rec.get("subject") or "").casefold() for frase in frases_plantillas)
        ]
    except Exception as exc:
        raise RuntimeError(
            "No se pudo leer mail.mail. Puede faltar permiso de lectura "
            "o no estar disponible algún campo. Detalle: " + str(exc)
        ) from exc

    mail_counts = Counter(str(rec.get("state") or "") for rec in mail_records)
    enviados = mail_counts.get("sent", 0)
    rechazados = mail_counts.get("exception", 0)

    lines = [
        "PASE 360 | CONTROL DIARIO",
        f"Día: {report_date.strftime('%d/%m/%Y')}",
        "",
        "CORREOS ELECTRÓNICOS",
        f"Enviados: {enviados}",
        "Entregados: No disponible",
        email_open_report(start_utc, end_utc),
        f"Rechazados: {rechazados}",
        "Enviados a spam: No disponible",
        "",
        "WHATSAPP",
        *whatsapp_report(odoo, start_utc, end_utc),
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

#!/usr/bin/env python3
"""Monitor de solo lectura para correo y WhatsApp de Pase 360 en Odoo 19."""
import json
import os
import sys
import urllib.request
import urllib.error
import urllib.parse
from collections import Counter
from email import policy
from email.parser import BytesParser
from email.utils import parsedate_to_datetime, parseaddr
from datetime import date
import imaplib
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



def whatsapp_model_diagnostic(odoo):
    """Busca modelos/campos de WhatsApp y webhooks que puedan guardar eventos fechados."""
    terms = (
        "date", "time", "state", "status", "event", "deliver", "read",
        "sent", "message", "webhook", "track", "uid", "timestamp",
        "provider", "notification", "error", "callback"
    )
    print("WhatsApp: diagnóstico de modelos de eventos disponibles:", file=sys.stderr)
    try:
        models = odoo.search_read(
            "ir.model",
            ["|", ["model", "ilike", "whatsapp"], ["model", "ilike", "webhook"]],
            ["model", "name"], limit=200, order="model asc"
        )
        if not models:
            print("WhatsApp: no se encontraron modelos con nombre whatsapp/webhook.", file=sys.stderr)
            return
        for item in models:
            model = str(item.get("model") or "")
            if not model:
                continue
            try:
                meta = odoo.fields_get(model)
                relevant = [name for name in meta if any(t in name.lower() for t in terms)]
                details = []
                for name in relevant[:45]:
                    info = meta[name]
                    part = name
                    if info.get("type"):
                        part += ":" + str(info["type"])
                    if info.get("relation"):
                        part += "->" + str(info["relation"])
                    selection = info.get("selection") or []
                    if selection and any(t in name.lower() for t in ("state", "status", "event")):
                        part += "[" + ",".join(str(k) for k, _ in selection[:12]) + "]"
                    details.append(part)
                print(f"WhatsApp: modelo {model}: " +
                      (", ".join(details) if details else "sin campos de evento reconocibles"),
                      file=sys.stderr)
            except Exception as exc:
                print(f"WhatsApp: modelo {model}: sin acceso a metadatos ({type(exc).__name__}).",
                      file=sys.stderr)
    except Exception as exc:
        print(f"WhatsApp: no se pudo listar modelos de eventos ({type(exc).__name__}).",
              file=sys.stderr)

def whatsapp_report(odoo, start_utc, end_utc):
    """Devuelve únicamente los tres estados solicitados de WhatsApp."""
    try:
        meta = odoo.fields_get("whatsapp.message")
        if not isinstance(meta, dict) or not meta:
            return ["Enviados: No disponible", "Entregados: No disponible", "Leídos: No disponible"]
        status_field = next((name for name in ("state", "status") if name in meta), None)
        if not status_field:
            return ["Enviados: No disponible", "Entregados: No disponible", "Leídos: No disponible"]
        fields = ["id", status_field]
        domain = date_domain(start_utc, end_utc) if "create_date" in meta else []
        records = odoo.search_read("whatsapp.message", domain, fields, limit=2000)
        selection = meta[status_field].get("selection") or []
        labels = {str(key): str(label).strip().lower() for key, label in selection}
        counts = Counter()
        for row in records:
            key = str(row.get(status_field) or "").strip().lower()
            value = key + " " + labels.get(key, "")
            # Un mensaje leído también cuenta como entregado y enviado;
            # uno entregado también cuenta como enviado.
            if any(x in value for x in ("read", "leíd", "leido")):
                counts["sent"] += 1
                counts["delivered"] += 1
                counts["read"] += 1
            elif any(x in value for x in ("deliver", "entreg")):
                counts["sent"] += 1
                counts["delivered"] += 1
            elif any(x in value for x in ("sent", "enviado", "send", "accepted", "aceptado")):
                counts["sent"] += 1
        return [
            f"Enviados: {counts['sent']}",
            f"Entregados: {counts['delivered']}",
            f"Leídos: {counts['read']}",
        ]
    except Exception as exc:
        print(f"WhatsApp: no se pudo consultar whatsapp.message ({type(exc).__name__}: {str(exc)[:250]}).", file=sys.stderr)
        return ["Enviados: No disponible", "Entregados: No disponible", "Leídos: No disponible"]


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
        lines.append(f"• Correos Pase 360 conservados para consultar: {len(mail_ids)}")

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

        if not records and "create_date" in meta:
            records = odoo.search_read(
                "mail.notification", date_domain(start_utc, end_utc),
                fields, limit=2000, order="id desc"
            )
            if records:
                relation_used = "create_date del día (sin vínculo confirmado a una plantilla)"
        if not records and mail_ids and "mail_mail_id" in meta:
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
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {secret}",
            "User-Agent": "Pase360-Daily-Monitor/1.0 (+https://pase360.online)",
            "Accept": "application/json",
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as response:
            result = json.loads(response.read().decode())
        return f"Abiertos: {int(result.get('correos_identificados', 0))}"
    except urllib.error.HTTPError as exc:
        # Diagnóstico seguro: nunca registrar Authorization ni REPORT_SECRET.
        body = exc.read().decode("utf-8", errors="replace")
        if secret:
            body = body.replace(secret, "[CLAVE REDACTADA]")
        body = " ".join(body.split())[:350]
        server = exc.headers.get("Server", "no informado") if exc.headers else "no informado"
        cf_ray = exc.headers.get("CF-Ray", "no informado") if exc.headers else "no informado"
        content_type = exc.headers.get("Content-Type", "no informado") if exc.headers else "no informado"
        print(
            f"Aperturas: HTTP {exc.code}; servidor={server}; "
            f"CF-Ray={cf_ray}; Content-Type={content_type}; "
            f"respuesta={body or '(vacía)'}",
            file=sys.stderr
        )
        return "Abiertos: No disponible"
    except urllib.error.URLError as exc:
        print(f"Aperturas: no se pudo conectar con el Worker ({exc.reason}).", file=sys.stderr)
        return "Abiertos: No disponible"
    except Exception as exc:
        print(f"Aperturas: respuesta inválida del Worker ({type(exc).__name__}).", file=sys.stderr)
        return "Abiertos: No disponible"




def gmail_automated_mail_report(report_date, next_date):
    """Cuenta solo las cuatro plantillas automáticas en Enviados y sus rebotes recibidos."""
    address = os.getenv("GMAIL_ADDRESS", "infopase360@gmail.com").strip()
    app_password = required("GMAIL_APP_PASSWORD").replace(" ", "")
    phrases = (
        "invitación para adherir",
        "multiplica tus ventas con la comunidad de beneficios de pase 360",
        "crea credenciales qr de beneficios para los miembros de tu organización",
        "no dejes pasar la oportunidad de pertenecer a la red de beneficios pase 360",
    )

    def in_report_day(message):
        raw_date = message.get("Date")
        if not raw_date:
            return False
        try:
            dt = parsedate_to_datetime(str(raw_date))
            if dt is None:
                return False
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=TZ)
            return report_date <= dt.astimezone(TZ).date() < next_date
        except (TypeError, ValueError, OverflowError):
            return False

    def message_text(message):
        chunks = []
        for part in message.walk():
            if part.get_content_maintype() == "multipart":
                continue
            payload = part.get_payload(decode=True)
            if payload:
                charset = part.get_content_charset() or "utf-8"
                try:
                    chunks.append(payload.decode(charset, errors="replace"))
                except LookupError:
                    chunks.append(payload.decode("utf-8", errors="replace"))
            elif part.get_content_type() == "message/rfc822":
                nested = part.get_payload()
                if isinstance(nested, list):
                    for item in nested:
                        chunks.append(str(item.get("Subject", "")))
        return "\\n".join(chunks).casefold()

    def fetch_message(client, uid, full=False):
        section = "BODY.PEEK[]" if full else "BODY.PEEK[HEADER]"
        status, data = client.uid("fetch", uid, f"({section})")
        if status != "OK":
            return None
        for item in data:
            if isinstance(item, tuple) and isinstance(item[1], bytes):
                return BytesParser(policy=policy.default).parsebytes(item[1])
        return None

    client = None
    try:
        client = imaplib.IMAP4_SSL("imap.gmail.com", 993, timeout=30)
        client.login(address, app_password)

        # Ampliamos un día a cada lado y filtramos por fecha real del encabezado en Córdoba.
        since = (report_date - timedelta(days=1)).strftime("%d-%b-%Y")
        before = (next_date + timedelta(days=1)).strftime("%d-%b-%Y")

        status, _ = client.select("[Gmail]/Sent Mail", readonly=True)
        if status != "OK":
            raise RuntimeError("No se pudo abrir la carpeta Enviados de Gmail.")
        status, data = client.uid("search", None, "SINCE", since, "BEFORE", before)
        if status != "OK":
            raise RuntimeError("No se pudo buscar en Enviados de Gmail.")

        sent_uids = set()
        for uid in (data[0] or b"").split():
            msg = fetch_message(client, uid, full=False)
            if not msg or not in_report_day(msg):
                continue
            subject = str(msg.get("Subject", "")).casefold()
            if any(phrase in subject for phrase in phrases):
                sent_uids.add(uid)

        status, _ = client.select("INBOX", readonly=True)
        if status != "OK":
            raise RuntimeError("No se pudo abrir la bandeja de entrada de Gmail.")
        status, data = client.uid("search", None, "SINCE", since, "BEFORE", before)
        if status != "OK":
            raise RuntimeError("No se pudo buscar avisos de rebote en Gmail.")

        bounce_uids = set()
        bounce_terms = (
            "delivery status notification",
            "undelivered mail",
            "mail delivery failed",
            "failure notice",
            "returned mail",
            "delivery failure",
        )
        for uid in (data[0] or b"").split():
            msg = fetch_message(client, uid, full=True)
            if not msg or not in_report_day(msg):
                continue
            sender = parseaddr(str(msg.get("From", "")))[1].casefold()
            subject = str(msg.get("Subject", "")).casefold()
            looks_like_bounce = (
                "mailer-daemon" in sender
                or "postmaster" in sender
                or any(term in subject for term in bounce_terms)
            )
            if not looks_like_bounce:
                continue
            original = message_text(msg)
            if any(phrase in original for phrase in phrases):
                bounce_uids.add(uid)

        return len(sent_uids), len(bounce_uids)
    except Exception as exc:
        print(
            f"Gmail: no se pudo consultar Enviados/rebotes "
            f"({type(exc).__name__}: {str(exc)[:250]}).",
            file=sys.stderr,
        )
        return None, None
    finally:
        if client is not None:
            try:
                client.logout()
            except Exception:
                pass


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

    # Diagnóstico de solo lectura para identificar si Odoo conserva eventos reales de entrega/rebote.
    # Se muestra únicamente en GitHub Actions; nunca se envía a Telegram.
    for diagnostic in (
        mail_notification_diagnostic(odoo, mail_records, start_utc, end_utc),
        tracking_diagnostic(odoo),
        mail_server_diagnostic(odoo),
    ):
        print("\\n".join(diagnostic), file=sys.stderr)

    enviados, rechazados = gmail_automated_mail_report(
        report_date.date(), next_date.date()
    )
    enviados_texto = str(enviados) if enviados is not None else "No disponible"
    rechazados_texto = str(rechazados) if rechazados is not None else "No disponible"

    lines = [
        "PASE 360 | CONTROL DIARIO",
        f"Día: {report_date.strftime('%d/%m/%Y')}",
        "",
        "CORREOS ELECTRÓNICOS",
        f"Enviados: {enviados_texto}",
        "Entregados: No disponible",
        email_open_report(start_utc, end_utc),
        f"Rechazados: {rechazados_texto}",
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

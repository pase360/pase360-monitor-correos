# Pase 360 — Monitor de correos

Monitor de solo lectura para auditar las invitaciones comerciales enviadas desde Odoo 19 y notificar un resumen diario por Telegram.

## Principios de seguridad

- Este proyecto nunca envía correos ni modifica leads, oportunidades o automatizaciones.
- No incluir credenciales, tokens, contraseñas ni datos personales reales en el código.
- Las credenciales se guardan exclusivamente en GitHub Actions Secrets.
- Los informes deben diferenciar: correo generado por Odoo, aceptado por SMTP, rebote conocido y apertura estimada.
- Una apertura no demuestra que una persona haya leído el mensaje.
- No afirmar "entregado" si solo se conoce que Gmail/SMTP aceptó el mensaje.

## Integraciones previstas

1. Odoo 19: lectura del historial de correo y notificaciones mediante una cuenta/API de solo lectura.
2. Gmail: lectura de avisos de rebote mediante un método de autenticación autorizado.
3. Telegram: envío del informe diario y alertas de errores.
4. GitHub Actions: ejecución programada y manual.

## Estado

Proyecto inicial. No activar la programación hasta completar y probar las credenciales y consultas de solo lectura.

## Secretos previstos

- `ODOO_URL`
- `ODOO_DB`
- `ODOO_USERNAME`
- `ODOO_API_KEY`
- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`

La lectura de rebotes de Gmail requiere definir y autorizar el método de acceso (Gmail API OAuth o IMAP con credencial específica). No reutilizar automáticamente la contraseña SMTP de Odoo.

## Limitaciones de medición

- "Enviado por Odoo" no equivale a entregado.
- "Aceptado por SMTP" no garantiza bandeja de entrada.
- Los rebotes solo se contabilizan si el buzón devuelve una notificación que podamos leer.
- Las aperturas solo pueden estimarse si el correo incorpora seguimiento compatible; los bloqueadores y precargas distorsionan esa métrica.
- Gmail no ofrece una confirmación fiable de que una persona haya leído un correo.

## Desarrollo

Se implementará primero un modo de diagnóstico sin cambios en Odoo. Luego se habilitará el informe diario cuando todas las métricas tengan una fuente verificable.

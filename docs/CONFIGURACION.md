# Configuración del monitor

## Secretos de GitHub Actions

En Settings → Secrets and variables → Actions → New repository secret, crear:

- ODOO_URL: URL de la base Odoo, sin /web.
- ODOO_DB: nombre técnico de la base.
- ODOO_USERNAME: usuario técnico dedicado a consulta.
- ODOO_API_KEY: clave API de ese usuario.
- TELEGRAM_BOT_TOKEN: token del bot existente.
- TELEGRAM_CHAT_ID: identificador del chat que recibe los informes.

No pegues credenciales en Issues, README, archivos del repositorio ni mensajes públicos.

## Permisos recomendados en Odoo

Crear un usuario técnico dedicado y otorgarle únicamente los permisos mínimos de lectura necesarios para consultar el historial de correo. No usar el usuario administrador. La lectura de mail.mail puede estar restringida por grupos o reglas de acceso; si la consulta falla, revisar permisos antes de ampliar privilegios.

## Primera prueba

1. Cargar los seis secretos.
2. Abrir Actions → Pase 360 - Monitor diario de correos.
3. Ejecutar Run workflow manualmente.
4. Revisar los logs y el mensaje recibido en Telegram.
5. Confirmar que los registros encontrados corresponden a las invitaciones reales.

## Alcance de esta primera versión

Consulta mail.mail por fecha de creación y asunto que contiene "Pase 360". No escribe en Odoo. No envía correos. No confirma entrega en bandeja de entrada ni lectura. Los registros pueden no existir si Odoo elimina automáticamente los correos enviados.

## Próxima etapa

Después de validar la primera consulta, ampliar la auditoría para:
- revisar el chatter (mail.message) cuando mail.mail no conserve los enviados;
- leer los avisos de rebote de Gmail mediante Gmail API con OAuth autorizado;
- detectar respuestas y adhesiones sin confundirlas con aperturas;
- guardar un histórico persistente de métricas.

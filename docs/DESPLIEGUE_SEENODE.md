# Despliegue de producción en Seenode (P5)

Producción completa en Seenode, **verificada en las URL provisionales `*.seenode.app` antes de mover los dominios** (P6). Mientras
tanto, Elegant Barber sigue en el VPS sin cambios. El Supabase de producción es el **actual** (no se migra nada, ver
`docs/ORDEN_MIGRACIONES.md`).

## Servicios

| Servicio | Tipo en Seenode | Origen | Notas |
|---|---|---|---|
| API | Web Standard | imagen `ghcr.io/goagendaia/goagenda-backend:latest` (linux/amd64) | puerto 8000; **una sola réplica** (el mapa de WebSockets vive en memoria) |
| Worker | Basic | la misma imagen, comando `/app/start_worker.sh` (o por Git con ese start command) | **un solo proceso**; sin puerto. Depende de que Seenode acepte imagen en el tipo Worker: confirmarlo en el panel |
| Front | Sitio estático (Git) | repo `GoAgendaIA/go-front` | build `npm run build`; publicar `dist/go-agenda/browser`; SPA con reescritura a `index.html` |
| Redis | Redis Cloud (gratis) | cuenta propia | solo lo usa el worker/scheduler; `rediss://…` |

Las imágenes del GHCR de la organización son **públicas**: Seenode no necesita credenciales de registro. Desplegar desde imagen no tiene
auto-deploy: tras cada push a `main` hay que elegir el tag y desplegar a mano (o por API si el panel lo permite).

## Variables de entorno (como secretos del panel, nunca en el repo ni en el chat)

Los valores de producción son los del `.env` actual del VPS (`/opt/goagenda/backend/.env`); se copian de ahí directamente al panel de
Seenode. Diferencias por servicio:

| Variable | API | Worker | Valor |
|---|---|---|---|
| `SUPABASE_URL`, `SUPABASE_KEY`, `SUPABASE_SERVICE_ROLE_KEY` | ✓ | ✓ | proyecto de **producción** (el actual) |
| `DATABASE_URL` | ✓ | – | Session pooler, **puerto 5432** (el 6543 rompe el checkpointer del agente) |
| `DB_POOL_MAX_SIZE` | `10` | `2` | el pooler de Supabase admite 15 clientes entre TODOS los procesos |
| `TAREAS_EN_SEGUNDO_PLANO` | `worker` | – | la API no programa tareas; las ejecuta el worker |
| `REDIS_URL` | – | ✓ | Redis Cloud (`rediss://…`) |
| `WOMPI_ENCRYPTION_KEY` | ✓ | ✓ | **la misma de siempre**: con otra, las credenciales de Wompi guardadas quedan ilegibles |
| `OPENAI_API_KEY`, `CHAT_MODEL_NAME` | ✓ | – | clave de producción |
| `WHATSAPP_TOKEN`, `VERIFY_TOKEN`, `META_APP_SECRET` | ✓ | ✓ | los reales |
| `GOAGENDA_WHATSAPP_*` | ✓ | ✓ | número y plantillas reales |
| `BAILEYS_SERVICE_URL`, `BAILEYS_INTERNAL_API_KEY` | ✓ | ✓ | URL del servicio Baileys de producción |
| `CORS_ALLOWED_ORIGINS`, `GOAGENDA_FRONTEND_URL` | ✓ | – | URL del front: primero la `*.seenode.app`, al hacer P6 `https://app.goagenda.online` |
| `FIREBASE_CREDENTIALS_JSON` | ✓ | – | JSON de la cuenta de servicio pegado como cadena |
| `SUPER_ADMIN_EMAIL`, `SUPER_ADMIN_PASSWORD` | ✓ | – | los de producción |
| resto de `.env.example` (`CHAT_*`, `LLM_*`, `AI_*`, `COP_POR_USD`…) | ✓ | según `docs/WORKER_Y_TAREAS.md` | copiar los del VPS |

## Orden

1. **Redis Cloud**: crear la base gratuita y copiar su `REDIS_URL` directamente al panel de Seenode (no por chat).
2. **Seenode, API**: crear el servicio desde imagen, variables, una réplica. Comprobar `GET /` → `{"status": "GoAgenda backend corriendo correctamente"}`.
3. **Seenode, worker**: mismo `latest`, comando `/app/start_worker.sh`. En los logs debe verse `[scheduler] encolando tareas: …` y, a los 5 min,
   `[recordatorios] ahora=… negocios=…`.
4. **Seenode, front**: sitio estático con el build de producción. Apuntar `CORS_ALLOWED_ORIGINS` de la API a su URL.
5. **Humo** en las URL provisionales (lista de abajo).
6. **Alerta mínima** (abajo) y solo entonces pasar a P6.

## Humo (en las URL `*.seenode.app`, con una cuenta y negocio de prueba o el de Elegant Barber en modo lectura)

- [ ] Login del dueño y vista de su negocio, servicios, horarios, citas y empleados
- [ ] Chat público: conversación completa, agendar una cita y cancelarla
- [ ] WhatsApp: confirmación de cita y recordatorio (el worker lo envía; revisar `enviados=` en su log)
- [ ] Link de pago de Wompi generado desde el chat
- [ ] Notificación push al dueño
- [ ] Tiempo real: el panel recibe el evento de una cita nueva (WebSocket `/ws/appointments`; una sola réplica)

## Alerta mínima antes del corte

- **API caída**: monitor externo gratuito (UptimeRobot o similar) sobre `GET https://<api>/`, aviso por correo/WhatsApp.
- **Worker sin actividad**: el worker registra una línea `[recordatorios] …` cada 5 minutos. Opciones: un *heartbeat* de healthchecks.io que el actor
  ping-ee al final de cada ciclo, o revisar a mano los logs el primer día. Decidir cuál antes del corte (hoy no está implementado).

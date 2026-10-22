# Ambiente de pruebas en el VPS (P4)

El VPS pasa a ser el ambiente de **pruebas**, sin tocar produccion mientras haya clientes en el. Los nombres:

| Que | Produccion (hoy, VPS) | Pruebas |
|---|---|---|
| Front | `app.goagenda.online` → 8080 | `pruebas.goagenda.online` → 8081 |
| API | `api.goagenda.online` → 8000 | `api-pruebas.goagenda.online` → 8001 |

La API de pruebas va en **un solo nivel** (`api-pruebas`, no `pruebas.api`): el certificado gratis de Cloudflare solo cubre
`*.goagenda.online`, y `pruebas.api.goagenda.online` (dos niveles) daria error de TLS con el tunel.

## Como esta aislado

- Carpeta propia `/opt/goagenda/pruebas` y proyecto de Compose propio (`goagenda-pruebas`): el deploy de produccion corre
  `docker compose up -d --remove-orphans` en `/opt/goagenda/backend` y no ve ni borra estos contenedores.
- La API de pruebas usa **su propio `.env`** y el script **no la arranca** si ese `.env` no existe o apunta al mismo
  `SUPABASE_URL` que produccion. Pruebas nunca toca los datos reales.
- Sin worker ni Redis propios y con `TAREAS_EN_SEGUNDO_PLANO=worker`: pruebas no manda recordatorios.
- Los puertos 8001 y 8081 solo escuchan en `127.0.0.1`; nginx es lo unico que los alcanza.
- El front de pruebas es la misma app compilada con `--configuration pruebas` (imagen `go-front:pruebas`, la publica el
  pipeline del front junto a `latest`).

## Montarlo

```bash
bash scripts/montar_pruebas_vps.sh      # como goagenda (sudo + docker)
bash scripts/montar_pruebas_vps.sh --revertir
```

Cada cambio guarda copia: el sitio PHP viejo de `pruebas.` queda en `/etc/nginx/respaldos-pruebas/` (no se borra
`/var/www`) y el config del tunel en `/etc/cloudflared/config.yml.bak.<fecha>`. Reiniciar `cloudflared` corta el tunel unos
segundos (tambien el de produccion) y **tu sesion SSH, que entra por ese mismo tunel**: por eso el script pide confirmacion y hace el reinicio (con su reversa si `cloudflared` no arranca) en una unidad de systemd aparte. Al reconectar, el resultado esta en `journalctl -u goagenda-reinicio-tunel-<fecha>`.

Pendiente a mano:
1. DNS en Cloudflare: CNAME `api-pruebas` → `<id-del-tunel>.cfargotunnel.com` (con proxy). `pruebas` ya existe.
2. Crear el proyecto de Supabase de pruebas (gratis), aplicar los `.sql` del repo y poner sus claves en
   `/opt/goagenda/pruebas/.env` (ademas de `CORS_ALLOWED_ORIGINS=https://pruebas.goagenda.online`, claves de OpenAI de
   pruebas y `WOMPI_ENCRYPTION_KEY` propia). Despues, volver a correr el script.
3. Webhooks de Meta y Wompi en modo prueba apuntando a `https://api-pruebas.goagenda.online`.

# P1 · Repos en la organización GoAgendaIA

Objetivo: `goagenda-backend` y `go-front` viven en la organización **GoAgendaIA**, y el ciclo
"push a `main` → pruebas → imagen en GHCR → despliegue al VPS" sigue igual que hoy. Las imágenes
quedan **públicas** (los repos ya lo son y las imágenes no llevan secretos: el `.env` vive en el servidor).

| | Antes | Después |
|---|---|---|
| Backend | `contactogoagenda-eng/goagenda-backend` | `GoAgendaIA/goagenda-backend` |
| Front | `duban0909/go-front` | `GoAgendaIA/go-front` |
| Imagen backend | `ghcr.io/contactogoagenda-eng/goagenda-backend` | `ghcr.io/goagendaia/goagenda-backend` |
| Imagen front | `ghcr.io/duban0909/go-front` | `ghcr.io/goagendaia/go-front` |

GitHub redirige las URL viejas de los repos (git y web), **pero no las de las imágenes**: por eso el
paso 6 es obligatorio. Sin él, los despliegues seguirían descargando la imagen vieja sin avisar.

## Pasos

1. **Workflows listos para la organización** (backend #20, front #12). El nombre de la imagen se
   calcula en minúsculas (Docker rechaza `GoAgendaIA`). Sin efecto con el dueño actual: mergear antes.
2. **Permisos en la organización** (lo hace un *owner* de GoAgendaIA):
   - *People*: dar rol **Owner** a quien vaya a transferir (o permitir a los miembros crear repos).
   - *Settings → Actions → General → Workflow permissions*: **Read and write permissions**
     (el `GITHUB_TOKEN` publica las imágenes).
   - *Settings → Packages*: permitir paquetes **públicos**.
3. **Transferir los repos** (*Settings → General → Danger Zone → Transfer ownership* → `GoAgendaIA`,
   mismo nombre). El backend lo transfiere la cuenta `contactogoagenda-eng`; el front, `duban0909`.
   Los PR, issues y el historial se van con el repo.
4. **Secretos del despliegue**: comprobar que `DEPLOY_SSH_KEY`, `DEPLOY_SSH_HOST` y `DEPLOY_SSH_USER`
   siguen en cada repo (`gh secret list -R GoAgendaIA/<repo>`). Recomendado: crearlos una sola vez
   como **secretos de la organización** compartidos por los dos repos.
5. **Primer build en la organización**: un push a `main` de cada repo publica la imagen nueva. Luego
   marcar cada paquete como **público** (*GoAgendaIA → Packages → paquete → Package settings → Change
   visibility*). El despliegue de esa primera corrida todavía baja la imagen vieja (inofensivo).
6. **VPS**: como el usuario `deploy`, correr `scripts/migrar_imagenes_organizacion.sh`. Comprueba que las
   imágenes nuevas existan y sean públicas antes de tocar nada, cambia los `docker-compose`, arranca y
   revierte solo si algo falla. `SOLO_VERIFICAR=1` solo comprueba.
7. **Verificación final**: un push a `main` en cada repo debe correr pruebas → publicar en
   `ghcr.io/goagendaia/...` → desplegar, y el VPS debe quedar con la imagen nueva.
8. **Limpieza** (cuando todo funcione): borrar los paquetes viejos de las cuentas personales y el
   `docker login` de cuentas personales en el VPS, si existía (las imágenes nuevas no lo necesitan).

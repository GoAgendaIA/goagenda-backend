# Plan de seguridad en el pipeline (OWASP Top 10)

> Estado: **propuesta, nada implementado todavía.**
> Fecha: 2026-10-13.
> Los nombres de acciones de GitHub, versiones e imágenes son los de referencia
> al escribir este plan: confirmar la versión vigente de cada una al implementar.

## 1. Objetivo

Que cada cambio pase por análisis de seguridad automático antes de llegar a
producción, cubriendo las categorías del OWASP Top 10 que una herramienta puede
detectar, sin frenar los despliegues por ruido. Ambientes (ver
`PLAN_MIGRACION_SEENODE.md`): el VPS es **pruebas** y recibe el DAST; Seenode es
**producción** y solo recibe lo que ya pasó por los controles.

## 2. Punto de partida

| Pieza | Hoy |
|---|---|
| Pipeline | `.github/workflows/deploy.yml`: `test` (cobertura ≥ 90 %) → `build-and-push` (imagen `linux/arm64` a GHCR) → `deploy` por SSH al VPS |
| Backend | FastAPI, `requirements.txt` con versiones fijas (bueno para SCA) |
| Imagen | `python:3.12-slim`, sin `USER` (corre como root), sin `HEALTHCHECK` |
| Front | Angular 22, repo `go-agenda` aparte (su propio pipeline) |
| Superficie | Endpoints del chat públicos por diseño; el resto con JWT de Supabase |
| Pruebas de aislamiento | `tests/test_aislamiento.py` ya cubre separación entre negocios |

## 3. Qué cubre cada herramienta

| Capa | Herramienta | Categorías OWASP | Etapa | Bloquea el pipeline |
|---|---|---|---|---|
| Secretos | Gitleaks | Fallas criptográficas, autenticación | Cada push, historial completo | **Sí**, cualquier hallazgo |
| SAST | Semgrep (`p/owasp-top-ten`, `p/python`, `p/fastapi`) | Inyección, control de acceso, config. | Cada push | Sí en severidad ERROR |
| SAST | Bandit | Igual, solo Python | Cada push | Sí en High |
| Calidad + hotspots | SonarQube Community | Bugs, hotspots de seguridad | Cada push a `main` | Solo quality gate de código nuevo |
| SCA | pip-audit + OSV-Scanner | Componentes vulnerables | Cada push + semanal | Sí en vulnerabilidad con fix disponible |
| SCA front | `npm audit` + Dependabot | Componentes vulnerables | Pipeline del front | Sí en High/Critical |
| Contenedor/IaC | Trivy (repo e imagen) + Hadolint | Componentes vulnerables, mala configuración | Tras construir la imagen | Sí en High/Critical con fix |
| DAST | OWASP ZAP (baseline + API scan) | Control de acceso, inyección, cabeceras | Tras deploy al VPS | No al inicio (informativo), luego sí |
| Fuzzing de API | Schemathesis | Errores 500, validaciones | Tras deploy al VPS | No al inicio |
| Misconfiguraciones | Nuclei | Config. insegura, CVEs conocidos | Tras deploy al VPS, semanal | No |
| Runtime | ModSecurity + OWASP CRS, CrowdSec | Inyección, bots, fuerza bruta | Permanente en el VPS y la frontera de producción | — |

Lo que **no** detecta ningún escáner (diseño inseguro, falta de monitoreo) se
trata en §8.

## 4. Fases

### Fase 1: controles baratos y bloqueantes (≈ 1 día)

Un job `security` en paralelo a `test`, antes de `build-and-push`:

```yaml
security:
  runs-on: ubuntu-latest
  permissions:
    contents: read
  steps:
    - uses: actions/checkout@v4
      with:
        fetch-depth: 0          # Gitleaks revisa todo el historial

    - name: Gitleaks (secretos)
      run: |
        docker run --rm -v "$PWD:/repo" zricethezav/gitleaks:latest \
          detect --source /repo --redact --verbose

    - name: Semgrep (SAST, OWASP Top 10)
      run: |
        docker run --rm -v "$PWD:/src" semgrep/semgrep \
          semgrep scan --config p/owasp-top-ten --config p/python \
          --config p/fastapi --severity ERROR --error /src

    - name: Bandit
      run: pip install bandit && bandit -r agent core routes services -lll

    - name: pip-audit (dependencias)
      run: pip install pip-audit && pip-audit -r requirements.txt
```

Notas:
- Gitleaks se ejecuta como binario/imagen y no con la acción oficial, porque esa
  exige licencia en repos de organización.
- Primero correr todo **sin bloquear** (`|| true`) y revisar la línea base con
  el equipo; bloquear solo cuando el ruido esté depurado.
- `.gitleaks.toml` y `.semgrepignore` en la raíz para excluir `tests/`, `venv/`
  y falsos positivos documentados (con una línea de justificación cada uno).

### Fase 2: contenedor y dependencias en profundidad (≈ 0.5 día)

- **Trivy** sobre el repo (`fs`) y sobre la imagen recién construida, en
  `build-and-push`, antes de publicarla en GHCR:
  `aquasecurity/trivy-action` con `severity: HIGH,CRITICAL`,
  `ignore-unfixed: true` y `exit-code: 1`.
- **Hadolint** sobre el `Dockerfile`.
- **Endurecer el `Dockerfile`** (hallazgos esperables de esta fase):
  - agregar un usuario sin privilegios (`USER app`),
  - agregar `HEALTHCHECK`,
  - fijar la imagen base por versión menor y revisar `pip install --no-cache-dir`.
- **Dependabot** (`.github/dependabot.yml`) para `pip`, `github-actions` y
  `docker`, con agrupación semanal para no inundar de PRs.
- Escaneo **semanal programado** (`schedule`) para detectar CVEs nuevos en
  dependencias que no cambiaron.

### Fase 3: SonarQube en el VPS (≈ 1 día, depende de infraestructura)

1. Desplegar SonarQube Community con Docker Compose + PostgreSQL propio, en
   una red interna; ajustar `vm.max_map_count=262144`. Requiere ~2 GB de RAM
   libres: **medirlo antes**, y confirmar que la imagen soporta `arm64`.
2. Conectividad con los runners de GitHub (elegir una):
   - **Túnel de Cloudflare** con token de servicio (patrón que ya usa el deploy
     por SSH), o
   - **self-hosted runner** en el VPS (más simple, pero ejecuta código del repo
     en tu servidor: solo para ramas propias, nunca para PRs externos).
3. Crear el token de proyecto como secreto `SONAR_TOKEN` y `SONAR_HOST_URL`.
4. `sonar-project.properties` con fuentes, exclusiones (`tests`, `venv`) y la
   ruta del reporte de cobertura (`coverage.xml` desde `pytest --cov-report=xml`).
5. Quality gate sobre **código nuevo** (cobertura, duplicación, seguridad), no
   sobre todo el histórico, para no exigir limpiar el pasado de un golpe.

### Fase 4: pruebas dinámicas contra el VPS de pruebas (≈ 1 día)

Un job `dast` que corre **después** del deploy al VPS y apunta a su URL:

- **ZAP API scan** con el `openapi.json` de FastAPI y **ZAP baseline** para
  cabeceras/cookies. Con autenticación: un usuario de prueba dedicado en el
  Supabase de pruebas (nunca de producción), token inyectado como secreto.
- **Schemathesis** (`schemathesis run <url>/openapi.json`) para detectar 500 y
  validaciones faltantes.
- **Nuclei** con plantillas de misconfiguración y CVEs, semanal.
- Primer mes en modo informativo; los hallazgos se triagean y recién después
  pasan a bloquear el despliegue a producción.
- **Regla:** el DAST jamás apunta a producción ni al proyecto de Supabase de
  producción (ver `PLAN_MIGRACION_SEENODE.md` §7).

### Fase 5: defensa en runtime (≈ 0.5-1 día)

- **ModSecurity + OWASP Core Rule Set** (o Coraza) delante de la API en el VPS,
  primero en modo `DetectionOnly`, luego en bloqueo.
- **CrowdSec** (o fail2ban) para bloquear IPs abusivas y fuerza bruta.
- **Límite de peticiones** en el reverse proxy para el chat público
  (por IP y por `business_id`): hoy cualquiera puede consumir OpenAI a tu
  costa (OWASP API: consumo irrestricto de recursos).
- **Cabeceras de seguridad** en nginx (API y front): `Strict-Transport-Security`,
  `X-Content-Type-Options`, `Content-Security-Policy`, `Referrer-Policy`.
- En Seenode no hay reverse proxy propio: validar qué controles ofrece la
  plataforma y, si hace falta, aplicar el límite de peticiones en la propia
  app (`slowapi` o un middleware).

### Fase 6: gestión continua (opcional)

- **DefectDojo** para consolidar hallazgos de todas las herramientas, con
  deduplicación y SLA por severidad. Solo si el volumen lo justifica: es
  pesado de operar.
- Revisión trimestral de reglas excluidas y de la línea base.

## 5. Política de bloqueo y triage

| Severidad | Acción | Plazo |
|---|---|---|
| Secreto expuesto | Bloquea; **rotar la credencial siempre**, aunque se borre del repo | Inmediato |
| Crítica con parche disponible | Bloquea el pipeline | 24-48 h |
| Alta | Bloquea (tras la línea base) | 1 semana |
| Media / baja | Informativa; se agenda | Siguiente ciclo |

Cada excepción (falso positivo o riesgo aceptado) se documenta en el archivo de
exclusión de la herramienta con **una línea de motivo y fecha de revisión**.

## 6. Secretos requeridos

| Secreto | Para qué |
|---|---|
| `SONAR_TOKEN`, `SONAR_HOST_URL` | Análisis de SonarQube |
| `DAST_BASE_URL` | URL del VPS de pruebas |
| `DAST_AUTH_TOKEN` | Token de un usuario de prueba (Supabase de pruebas) |
| `CF_ACCESS_CLIENT_ID` / `CF_ACCESS_CLIENT_SECRET` | Si SonarQube/DAST pasan por Cloudflare Access |

## 7. Criterios de aceptación

- [ ] Un push con un secreto falso en el código **falla** el job `security`.
- [ ] Una dependencia con CVE conocido y parche disponible falla pip-audit/Trivy.
- [ ] La imagen publicada corre como usuario sin privilegios y pasa Trivy sin High/Critical con fix.
- [ ] SonarQube muestra el último análisis de `main` y su quality gate aplica a código nuevo.
- [ ] Tras cada deploy a pruebas se publica el reporte de ZAP como artefacto.
- [ ] El chat público rechaza ráfagas por encima del límite configurado.
- [ ] Ningún job de seguridad usa credenciales ni URLs de producción.
- [ ] El tiempo total del pipeline no pasa de ~10 min en `main`.

## 8. Lo que las herramientas no resuelven (revisión manual)

| Tema | Acción |
|---|---|
| Control de acceso entre negocios | Mantener y ampliar `tests/test_aislamiento.py` con cada endpoint nuevo |
| Diseño inseguro del chat público | Revisar abuso de costo, enumeración de `business_id` y scraping del historial |
| Registro y monitoreo | Logs de accesos denegados y de acciones de super admin (crear/revocar cuentas); alertas |
| Gestión de claves | Rotar `SUPABASE_SERVICE_ROLE_KEY`, OpenAI y Wompi al separar pruebas/producción |
| Políticas de Supabase | Revisar RLS: hoy el backend usa la service role y salta RLS, así que la seguridad es del código, no de la base |
| Webhooks | Verificar firma en `/webhook` (Meta) y en el de Wompi; probar que sin firma se rechaza |

## 9. Riesgos del propio plan

| Riesgo | Mitigación |
|---|---|
| Ruido que lleva a ignorar las alertas | Línea base primero, bloquear después; excepciones documentadas |
| SonarQube consume la RAM del VPS que también sirve las pruebas | Medir antes; si no cabe, usar SonarCloud (gratis para repos públicos) o dejarlo fuera |
| Runner self-hosted ejecutando código no confiable | Solo ramas propias; sin PRs de forks; sin secretos de producción en el VPS |
| El DAST modifica datos de pruebas | Supabase de pruebas aislado; usuario dedicado; limpiar datos tras la corrida |
| Pipeline más lento | Jobs en paralelo, caché de dependencias, DAST solo en `main` |

## 10. Orden recomendado

1. Fase 1 y 2 juntas (el mayor valor por esfuerzo; ~1.5 días).
2. Migración a Seenode y separación de ambientes (prerrequisito de un DAST seguro).
3. Fase 4 (DAST) ya con el ambiente de pruebas aislado.
4. Fase 3 (SonarQube) cuando haya RAM medida.
5. Fase 5 (runtime) en paralelo a la puesta en producción.

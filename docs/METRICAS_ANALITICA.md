# Centro de métricas y analítica

Implementa el requerimiento «GoAgenda · Centro de métricas y analítica» (fases 1, 2 y 3).
Código: `services/metrics.py` (cálculo), `routes/metrics_routes.py` (HTTP),
`services/conversation_tracking.py` (conversaciones) y `metricas_analitica.sql` (datos).

## Orden de despliegue (importante)

1. Ejecutar `metricas_analitica.sql` en el SQL Editor de Supabase **antes** de desplegar el código:
   `create_appointment` ahora envía `source` en cada insert y fallaría con la columna ausente.
2. Desplegar el backend.
3. Las citas anteriores quedan con origen `unknown` (no se pueden clasificar con certeza). La
   comparación IA vs. manual es confiable solo desde la fecha de la migración en adelante.

## Qué se instrumentó (sección 9 del PDF)

| Dato | Antes | Ahora |
|---|---|---|
| Origen de la cita | No se guardaba | `appointments.source`: `ai`, `manual`, `web`, `unknown` |
| Reprogramaciones | Se movía la fila sin rastro | `rescheduled_count` (trigger, incluye ediciones del panel) y `rescheduled_by_ai_count` |
| Quién canceló | No se guardaba | `cancelled_by`: `ai`, `system` (pago vencido) o `NULL` (panel) |
| Completada / cancelada en el tiempo | No | `completed_at`, `cancelled_at` (trigger) |
| Conversaciones | Solo en checkpoints de LangGraph | Tabla `chat_sessions` (iniciada, terminó en cita, transferida a humano) |

## Endpoints (solo super admin)

Ambos aceptan los mismos filtros:

| Parámetro | Default | Descripción |
|---|---|---|
| `desde`, `hasta` | últimos 30 días | `YYYY-MM-DD`, inclusivo, máximo 366 días |
| `group_by` | `day` | `day`, `week` (lunes) o `month` para la evolución |
| `comparar` | `false` | Agrega `comparacion` contra el periodo anterior de igual duración |
| `inactividad_dias` | 14 | Umbral para «sin volver» y «negocio inactivo» |
| `refrescar` | `false` | Ignora la caché de 60 s |

- `GET /admin/metrics/global` — consolidado de todos los negocios + `adopcion`, `negocios` (tabla de detalle) y `alertas`.
- `GET /admin/metrics/businesses/{business_id}` — solo ese negocio (+ su fila de `negocio` y sus alertas).

Códigos: `401` sin sesión, `403` si no es super admin, `404` negocio inexistente, `400` rango inválido, `422` parámetro inválido.

### Forma de la respuesta

```
periodo, criterios, generado_en, actualizacion_segundos, cache
tarjetas      { citas_creadas, clientes_unicos, citas_ia, citas_manuales }
clientes      { total_registrados, nuevos_en_periodo, recurrentes, sin_volver:{dias,total} }
citas         { total, por_estado{...}, reprogramadas, tasa_cancelacion, porcentaje_completadas,
                pagos_vencidos, evolucion:[{periodo,total,ia,manual}] }
ia            { por_origen{ai,manual,web,unknown:{total,porcentaje}}, porcentaje_citas_ia,
                canceladas_por_ia, reprogramadas_por_ia,
                conversaciones{iniciadas,con_cita,transferidas_a_humano,conversion},
                costo_usd, costo_por_cita_ia_usd }
rendimiento   { servicios_mas_agendados, citas_por_dia_semana, citas_por_hora,
                domicilios{total,porcentaje}, ingresos_estimados{completadas,por_realizar,recargos_domicilio} }
comparacion   (si comparar=true) { <indicador>: {actual, anterior, variacion, variacion_pct} }
adopcion      (solo global) negocios registrados/activos/inactivos/sin uso, con servicios, con horario,
                usan IA vs. solo manual, mediana de días hasta la primera cita, dejaron de usar
negocios      (solo global) por negocio: estado, última actividad, citas, % IA, cancelación,
                conversión, costo IA, citas_por_semana (4 semanas), alertas
alertas       (solo global) {business_id, nombre, tipo, severidad, mensaje, recomendacion}
```

Los porcentajes y tasas son `null` cuando el denominador es 0 (nunca un cero inventado).

## Definiciones

- **Fecha de cálculo:** *uso de la plataforma* (citas creadas, origen, estados, evolución, clientes
  nuevos, conversaciones, costo de IA) se filtra por **fecha de creación**. *Rendimiento del negocio*
  (servicios, días y horas de demanda, domicilios, ingresos) se filtra por **fecha programada**.
  Días/semanas/meses se calculan en hora de Colombia, no en UTC.
- **Cliente único:** teléfono normalizado (solo dígitos, sin indicativo 57). No existe tabla de
  clientes: «clientes registrados» y «clientes con al menos una cita» son el mismo conjunto.
- **Reprogramación:** es la misma cita; se cuenta aparte (`rescheduled_count > 0`) y no suma a citas creadas.
- **Pagos vencidos:** citas `cancelled` con `cancelled_by = 'system'` son cupos que nunca se
  pagaron. Se excluyen de totales, clientes y tasas y se reportan en `citas.pagos_vencidos`.
- **Conversación que terminó en cita:** el cupo quedó reservado (incluye citas con abono pendiente
  si se generó el link de pago).
- **Transferida a humano:** `transferir_a_equipo`, `escalar_por_confusion`, aviso por fuera de
  cobertura o respuesta manual del dueño desde el panel.
- **Negocio activo:** tuvo una cita creada o una conversación en los últimos `inactividad_dias`;
  `inactivo` si tuvo actividad antes pero no ahora; `sin_uso` si nunca la tuvo.
- **Ingresos estimados:** precio actual del servicio (no un histórico del precio cobrado).
- **Costo de IA por cita:** costo del periodo / citas creadas por la IA del periodo.

## Alertas (fase 3)

Umbrales en `services/metrics.py` (constantes al inicio): `sin_configurar` y `sin_primera_cita`
(registrado hace > 7 días), `inactivo`, `cancelacion_alta` (≥ 30 % con ≥ 10 citas),
`conversion_baja` (< 10 % con ≥ 10 conversaciones), `solo_manual`, `ia_sin_citas`.
Los negocios bloqueados no generan alertas.

## Limitaciones conocidas

- **Frecuencia de actualización:** se calcula en vivo con caché en memoria de 60 s por combinación
  de parámetros (por proceso; con una sola instancia es consistente).
- **Escala:** cada consulta trae el historial de citas del alcance (paginado de a 1000) y agrega en
  Python. Es adecuado para decenas de negocios; con miles, conviene mover la agregación a vistas
  SQL o una función RPC en Postgres.
- **Citas completadas:** el backend no tiene un endpoint para marcarlas; dependen de que el panel
  actualice el estado. Si nadie lo hace, «completadas» y la tasa de cancelación subestiman la realidad.
- **Ingresos estimados:** no dependen de ese estado. Una cita no cancelada cuya hora ya pasó cuenta como
  ingreso *completado*; una futura (confirmada, pendiente o con pago pendiente) cuenta como *por realizar*.
- **Cancelaciones del panel:** quedan con `cancelled_by = NULL` (se asume del negocio).
- **Exportación CSV/Excel:** el PDF la marca como mejora posterior; no está implementada.
- **El gráfico es del front:** este repo entrega solo el contrato de datos.

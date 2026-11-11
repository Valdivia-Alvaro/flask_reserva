# Rutas y pruebas de API - Actividad 2

- `GET /api/salud`: comprobar conexion.
- `GET /api/boletos`: listar y liberar vencidas.
- `GET /api/reservas`: listar apartados activos del visitante.
- `POST /api/boletos/semilla`: cargar 6 boletos de prueba.
- `POST /api/boletos`: crear boleto (`evento`, `zona`, `asiento`, `precio`).
- `PUT /api/boletos/<id>`: editar si disponible.
- `DELETE /api/boletos/<id>`: eliminar si disponible.
- `POST /api/boletos/<id>/reservar`: reservar con `{ "cliente_nombre": "Comprador 1" }`.
- `POST /api/boletos/<id>/cancelar`: cancelar reserva propia (cookie de sesion).
- `POST /api/boletos/<id>/confirmar`: confirmar compra propia con `{ "cliente_nombre": "Comprador 1" }`.
- `POST /api/boletos/liberar-vencidas`: revision explicita de vencidas.

**Importante:** el ID de reserva proviene de la cookie Flask, no de un identificador manipulable en JavaScript. Los cambios a `boletos`, `reservas` y `compras` usan transacciones cuando es necesario. Todas las fechas se guardan en UTC.

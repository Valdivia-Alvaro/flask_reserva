# CBTIS - Actividad 2: Reserva temporal de boletos (5 minutos)

Proyecto Flask + MongoDB Atlas. Modelo: **unidad unica** (cada asiento es un boleto unico).

## Archivos

- `app.py`: rutas CRUD, reservas y transacciones atomicas.
- `templates/` y `static/`: interfaz y contador visual.
- `.env.example`: ejemplo SIN credenciales.
- `requirements.txt`, `Procfile`, `render.yaml`: despliegue.

## Instalacion (Windows, PowerShell)

1. Abre la carpeta `flask_reserva` en Visual Studio Code.
2. Si trabajas en tu repositorio original, crea tu rama: `git switch -c reserva-temporal`.
3. Copia estos archivos sobre tu proyecto original **sin sobrescribir `.env` ni `.git`**.
4. Activa tu entorno virtual (`venv\Scripts\Activate.ps1`) o crea uno con `python -m venv venv`.
5. Ejecuta `pip install -r requirements.txt`.
6. Configura `.env` con `MONGO_URI`, `MONGO_DB=reserva_boletos`, `FLASK_SECRET_KEY=...`.
7. Ejecuta `python app.py` y visita `http://127.0.0.1:5000`.
8. Pulsa `Cargar 6 boletos de ejemplo` si no tienes registros.

## Modelo de Atlas

- Coleccion `boletos`: `_id` (asiento unico), `evento`, `asiento`, `estado`, `reserva_id`, `vence_en`.
- Coleccion `reservas`: `boleto_id`, `reserva_id`, `cantidad: 1`, `estado: activa`, `creada_en`, `vence_en`.
- Coleccion `compras`: compra confirmada, con boleto y fecha de confirmacion.
- Se crean indices unicos para `evento + asiento` y `reservas.boleto_id`.

La coleccion `reservas` se crea automaticamente la primera vez que Flask se conecta a Atlas. El cambio del recurso y del apartado se realiza dentro de **una transaccion MongoDB**. Se necesita un despliegue de Atlas que admita transacciones. Una reserva caducada se libera al recargar/consultar los boletos. El contador no protege disponibilidad: solo informa sobre `vence_en` autorizado por Atlas.

## Prueba de 2 usuarios

1. Abre una ventana normal y otra de incognito. Cada una mostrara folio diferente.
2. Reserva `A-01` desde la normal; veras `05:00` aproximadamente.
3. Recarga incognito y comprueba que `A-01` no se puede reservar. Verifica **solo un documento** en `reservas` para ese asiento.
4. Cancela en ventana normal, recarga incognito y reserva `A-01` desde incognito.
5. Cancela; vuelve a reservar y deja pasar **5 minutos**. Recarga ambas ventanas: vuelve a disponible.
6. Reserva y pulsa **Confirmar compra** antes de vencer. `boletos.estado` pasa a `comprado`, se elimina de `reservas` y se crea en `compras`. No debe volver a disponible.

## Entrega Classroom

Indica nombre del proyecto y recurso (asiento), enlaza el commit o rama de GitHub y la URL publica de Render. Adjunta capturas: `reservas` de Atlas, contador, ambas ventanas, cancelacion o vencimiento y confirmacion.

## Subir cambios (en tu repositorio)

```bash
git add app.py templates static requirements.txt .env.example README.md render.yaml Procfile
git commit -m "Agregar reserva temporal de 5 minutos"
git push -u origin reserva-temporal
```

**NUNCA** subas `.env`, contrasenas, `venv/` ni `.git/` como archivos de entrega. En Render configura `MONGO_URI`, `MONGO_DB` y `FLASK_SECRET_KEY` (clave fija compartida por procesos). Despliega la rama `reserva-temporal` en un servicio de prueba o ajusta la rama del servicio existente si quieres actualizarlo.

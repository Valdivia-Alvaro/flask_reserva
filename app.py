"""CRUD de boletos con reservas de 5 minutos respaldadas por transacciones de Atlas.

La unicidad del recurso se protege en el servidor/MongoDB, no en JavaScript.
"""
import os
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from threading import Lock
from io import BytesIO
import qrcode
from flask import send_file, url_for
from bson import ObjectId
from bson.errors import InvalidId
from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, request, session
from pymongo import ASCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv()
app = Flask(__name__)
# En Render se DEBE configurar una clave fija (FLASK_SECRET_KEY).
app.secret_key = os.getenv("FLASK_SECRET_KEY") or secrets.token_hex(32)
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = os.getenv("COOKIE_SECURE", "0") == "1"

MONGO_URI = os.getenv("MONGO_URI", "").strip()
MONGO_DB = os.getenv("MONGO_DB", "reserva_boletos").strip() or "reserva_boletos"
RESERVA_SEGUNDOS = 300  # 5 minutos exactos, como pide la actividad.

cliente_mongo = None
boletos = None
reservas = None
compras = None
_mutex_inicio = Lock()


def ahora_utc():
    return datetime.now(timezone.utc)


def fecha_iso(valor):
    if not valor:
        return None
    if valor.tzinfo is None:
        valor = valor.replace(tzinfo=timezone.utc)
    return valor.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def folio_visitante():
    if not session.get("reserva_id"):
        session["reserva_id"] = uuid.uuid4().hex
    return session["reserva_id"]


def texto(valor):
    return " ".join(str(valor or "").strip().split())


def id_objeto(valor):
    try:
        return ObjectId(valor)
    except (InvalidId, TypeError):
        raise ValueError("El identificador del boleto no es valido.")


def cliente_actual(datos):
    nombre = texto(datos.get("cliente_nombre"))
    if not nombre or len(nombre) > 40:
        raise ValueError("Escribe el nombre del comprador (maximo 40 caracteres).")
    # No confiamos en un cliente_id enviado por JS: la identidad es la sesion Flask.
    return folio_visitante(), nombre


def validar_boleto(datos):
    evento = texto(datos.get("evento"))
    zona = texto(datos.get("zona"))
    asiento = texto(datos.get("asiento")).upper()
    try:
        precio = float(datos.get("precio"))
    except (ValueError, TypeError):
        raise ValueError("El precio debe ser numerico.")
    if not 3 <= len(evento) <= 80:
        raise ValueError("El evento debe tener entre 3 y 80 caracteres.")
    if not 2 <= len(zona) <= 40:
        raise ValueError("La zona debe tener entre 2 y 40 caracteres.")
    if not 1 <= len(asiento) <= 20:
        raise ValueError("Escribe un asiento valido de hasta 20 caracteres.")
    if not 0 <= precio < 1e9:
        raise ValueError("El precio no es valido.")
    return {"evento": evento, "zona": zona, "asiento": asiento, "precio": round(precio, 2)}


def obtener_colecciones():
    global cliente_mongo, boletos, reservas, compras
    if boletos is not None:
        return boletos, reservas, compras
    if not MONGO_URI:
        raise RuntimeError("Falta MONGO_URI: configura tu conexion de Atlas en .env o Render.")
    with _mutex_inicio:
        if boletos is None:
            client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=10000, tz_aware=True)
            db = client[MONGO_DB]
            b, r, c = db["boletos"], db["reservas"], db["compras"]
            b.create_index([("evento", ASCENDING), ("asiento", ASCENDING)], unique=True, name="evento_asiento_unico")
            b.create_index("estado")
            b.create_index("vence_en")
            r.create_index(
    "boleto_id",
    unique=True,
    name="una_reserva_activa_por_boleto",
    partialFilterExpression={"boleto_id": {"$type": "objectId"}},
)
            r.create_index([("reserva_id", ASCENDING), ("vence_en", ASCENDING)])
            c.create_index("boleto_id", unique=True, name="una_compra_por_boleto")
            # Migrar documentos anteriores, sin eliminar boletos ni compras.
            b.update_many({"estado": {"$exists": False}}, {"$set": {"estado": "disponible"}})
            b.update_many({"reserva_id": {"$exists": False}, "estado": "reservado"}, {"$set": {
                "estado": "disponible", "reserva_id": None, "vence_en": None,
                "reserva_hasta": None, "reservado_por": None, "reservado_nombre": None
            }})
            b.update_many({"reserva_id": {"$exists": False}}, {"$set": {"reserva_id": None}})
            b.update_many({"vence_en": {"$exists": False}}, {"$set": {"vence_en": None}})
            # Solo publicar las colecciones cuando toda la inicializacion concluyo.
            cliente_mongo, boletos, reservas, compras = client, b, r, c
    return boletos, reservas, compras


def serializar_boleto(doc):
    if doc is None:
        return None
    return {
        "_id": str(doc["_id"]),
        "evento": doc.get("evento", ""),
        "zona": doc.get("zona", ""),
        "asiento": doc.get("asiento", ""),
        "precio": doc.get("precio", 0),
        "estado": doc.get("estado", "disponible"),
        "reservado_nombre": doc.get("reservado_nombre"),
        "reserva_hasta": fecha_iso(doc.get("vence_en") or doc.get("reserva_hasta")),
        "es_mio": doc.get("estado") == "reservado" and doc.get("reserva_id") == folio_visitante(),
        "comprado_nombre": doc.get("comprado_nombre"),
        "comprado_en": fecha_iso(doc.get("comprado_en")),
        "creado_en": fecha_iso(doc.get("creado_en")),
        "actualizado_en": fecha_iso(doc.get("actualizado_en")),
    }


def limpiar_boleto_para_disponible(momento):
    return {"$set": {
        "estado": "disponible", "reserva_id": None, "vence_en": None,
        "reserva_hasta": None, "reservado_por": None, "reservado_nombre": None,
        "actualizado_en": momento
    }}


def liberar_reservas_vencidas():
    """Libera recursos caducados y quita su apartado en una misma transaccion."""
    b, r, _ = obtener_colecciones()
    momento = ahora_utc()
    vencidas = list(r.find({"estado": "activa", "vence_en": {"$lte": momento}}).limit(500))
    total = 0
    for doc in vencidas:
        def accion(s):
            actualizada = b.update_one({
                "_id": doc["boleto_id"], "estado": "reservado",
                "reserva_id": doc["reserva_id"], "vence_en": {"$lte": momento}
            }, limpiar_boleto_para_disponible(momento), session=s)
            # Se elimina la reserva vencida una sola vez; los cambios son atomicos.
            r.delete_one({"_id": doc["_id"], "estado": "activa", "vence_en": {"$lte": momento}}, session=s)
            return actualizada.modified_count
        with cliente_mongo.start_session() as mongo_sesion:
            total += mongo_sesion.with_transaction(accion)
    return total


@app.get("/")
def inicio():
    return render_template("index.html", reserva_segundos=RESERVA_SEGUNDOS, reserva_id=folio_visitante())

@app.get("/boleto/<boleto_id>")
def ficha_boleto(boleto_id):
    boleto_oid = id_objeto(boleto_id)
    liberar_reservas_vencidas()

    b, _, _ = obtener_colecciones()
    boleto = b.find_one({"_id": boleto_oid})

    if boleto is None:
        return jsonify({"mensaje": "Boleto no encontrado."}), 404

    return render_template("ficha_boleto.html", boleto={
        "evento": boleto.get("evento", ""),
        "zona": boleto.get("zona", ""),
        "asiento": boleto.get("asiento", ""),
        "precio": boleto.get("precio", 0),
        "estado": boleto.get("estado", "disponible"),
    })

@app.get("/boleto/<boleto_id>/qr")
def qr_boleto(boleto_id):
    boleto_oid = id_objeto(boleto_id)
    b, _, _ = obtener_colecciones()

    if b.find_one({"_id": boleto_oid}, {"_id": 1}) is None:
        return jsonify({"mensaje": "Boleto no encontrado."}), 404

    enlace = url_for("ficha_boleto", boleto_id=boleto_id, _external=True)
    imagen = qrcode.make(enlace)

    archivo = BytesIO()
    imagen.save(archivo)
    archivo.seek(0)
    return send_file(archivo, mimetype="image/png")

@app.get("/api/salud")
def salud():
    b, _, _ = obtener_colecciones()
    b.database.client.admin.command("ping")
    return jsonify({"ok": True, "mensaje": "Flask y MongoDB Atlas estan funcionando.", "fecha": fecha_iso(ahora_utc())})


@app.get("/api/boletos")
def listar_boletos():
    liberar_reservas_vencidas()
    b, _, _ = obtener_colecciones()
    docs = b.find().sort([("evento", ASCENDING), ("asiento", ASCENDING)])
    return jsonify([serializar_boleto(d) for d in docs])


@app.get("/api/reservas")
def consultar_reservas():
    """Muestra solo los apartados activos pertenecientes a esta sesion."""
    liberar_reservas_vencidas()
    _, r, _ = obtener_colecciones()
    docs = r.find({"reserva_id": folio_visitante(), "estado": "activa", "vence_en": {"$gt": ahora_utc()}})
    return jsonify([{
        "_id": str(d["_id"]), "reserva_id": d["reserva_id"], "boleto_id": str(d["boleto_id"]),
        "cantidad": d["cantidad"], "estado": d["estado"],
        "creada_en": fecha_iso(d["creada_en"]), "vence_en": fecha_iso(d["vence_en"])
    } for d in docs])


@app.post("/api/boletos/semilla")
def crear_semilla():
    b, _, _ = obtener_colecciones()
    momento = ahora_utc()
    ejemplos = [
        ("Festival escolar", "General", "A-01", 150),
        ("Festival escolar", "General", "A-02", 150),
        ("Festival escolar", "General", "A-03", 150),
        ("Festival escolar", "Preferente", "B-01", 250),
        ("Festival escolar", "Preferente", "B-02", 250),
        ("Festival escolar", "Preferente", "B-03", 250)
    ]
    for evento, zona, asiento, precio in ejemplos:
        b.update_one({"evento": evento, "asiento": asiento}, {"$setOnInsert": {
            "evento": evento, "zona": zona, "asiento": asiento, "precio": precio,
            "estado": "disponible", "reserva_id": None, "vence_en": None,
            "reservado_por": None, "reservado_nombre": None, "reserva_hasta": None,
            "comprado_por": None, "comprado_nombre": None, "comprado_en": None,
            "creado_en": momento, "actualizado_en": momento
        }}, upsert=True)
    return jsonify({"mensaje": "Se prepararon los seis boletos de ejemplo."}), 201


@app.post("/api/boletos/liberar-vencidas")
def liberar_vencidas():
    return jsonify({"mensaje": "Revision terminada.", "liberadas": liberar_reservas_vencidas()})


@app.post("/api/boletos")
def crear_boleto():
    b, _, _ = obtener_colecciones()
    momento = ahora_utc()
    doc = {**validar_boleto(request.get_json(silent=True) or {}),
           "estado": "disponible", "reserva_id": None, "vence_en": None,
           "reservado_por": None, "reservado_nombre": None, "reserva_hasta": None,
           "comprado_por": None, "comprado_nombre": None, "comprado_en": None,
           "creado_en": momento, "actualizado_en": momento}
    doc["_id"] = b.insert_one(doc).inserted_id
    return jsonify({"mensaje": "Boleto creado correctamente.", "boleto": serializar_boleto(doc)}), 201


@app.put("/api/boletos/<boleto_id>")
def editar_boleto(boleto_id):
    b, _, _ = obtener_colecciones()
    datos = validar_boleto(request.get_json(silent=True) or {})
    datos["actualizado_en"] = ahora_utc()
    doc = b.find_one_and_update({"_id": id_objeto(boleto_id), "estado": "disponible"},
                                {"$set": datos}, return_document=ReturnDocument.AFTER)
    if not doc:
        return jsonify({"mensaje": "Solo se pueden editar boletos disponibles."}), 409
    return jsonify({"mensaje": "Boleto actualizado correctamente.", "boleto": serializar_boleto(doc)})


@app.delete("/api/boletos/<boleto_id>")
def eliminar_boleto(boleto_id):
    b, _, _ = obtener_colecciones()
    doc = b.find_one_and_delete({"_id": id_objeto(boleto_id), "estado": "disponible"})
    if not doc:
        return jsonify({"mensaje": "Solo se pueden eliminar boletos disponibles."}), 409
    return jsonify({"mensaje": "Boleto eliminado correctamente."})


@app.post("/api/boletos/<boleto_id>/reservar")
def reservar_boleto(boleto_id):
    b, r, _ = obtener_colecciones()
    folio, nombre = cliente_actual(request.get_json(silent=True) or {})
    boleto_oid = id_objeto(boleto_id)
    momento = ahora_utc()
    vence = momento + timedelta(seconds=RESERVA_SEGUNDOS)

    def accion(s):
        # Condicion de disponibilidad incluida en el CAMBIO atomico del recurso.
        doc = b.find_one_and_update({
            "_id": boleto_oid,
            "$or": [
                {"estado": "disponible"},
                {"estado": "reservado", "vence_en": {"$lte": momento}}
            ]
        }, {"$set": {
            "estado": "reservado", "reserva_id": folio, "vence_en": vence,
            "reserva_hasta": vence, "reservado_por": folio,
            "reservado_nombre": nombre, "actualizado_en": momento
        }}, return_document=ReturnDocument.AFTER, session=s)
        if doc is None:
            return None
        # Un documento por asiento; la sustitucion de una reserva caducada es segura.
        r.replace_one({"boleto_id": boleto_oid}, {
            "boleto_id": boleto_oid, "reserva_id": folio, "cantidad": 1,
            "estado": "activa", "creada_en": momento, "vence_en": vence,
            "cliente_nombre": nombre
        }, upsert=True, session=s)
        return doc

    with cliente_mongo.start_session() as mongo_sesion:
        doc = mongo_sesion.with_transaction(accion)
    if not doc:
        return jsonify({"mensaje": "El boleto ya fue reservado o comprado por otra persona."}), 409
    return jsonify({"mensaje": "Boleto reservado durante 5 minutos.", "boleto": serializar_boleto(doc)}), 201


@app.post("/api/boletos/<boleto_id>/cancelar")
def cancelar_reserva(boleto_id):
    b, r, _ = obtener_colecciones()
    boleto_oid = id_objeto(boleto_id)
    folio = folio_visitante()
    momento = ahora_utc()

    def accion(s):
        doc = b.find_one_and_update({
            "_id": boleto_oid, "estado": "reservado", "reserva_id": folio,
            "vence_en": {"$gt": momento}
        }, limpiar_boleto_para_disponible(momento), return_document=ReturnDocument.AFTER, session=s)
        if doc:
            eliminado = r.delete_one({"boleto_id": boleto_oid, "reserva_id": folio, "estado": "activa"}, session=s)
            if eliminado.deleted_count != 1:
                raise RuntimeError("La reserva no existe en Atlas. No se libero el boleto.")
        return doc

    with cliente_mongo.start_session() as mongo_sesion:
        doc = mongo_sesion.with_transaction(accion)
    if not doc:
        liberar_reservas_vencidas()
        return jsonify({"mensaje": "La reserva vencio o pertenece a otro usuario."}), 409
    return jsonify({"mensaje": "Reserva cancelada; el boleto esta disponible de nuevo.", "boleto": serializar_boleto(doc)})


@app.post("/api/boletos/<boleto_id>/confirmar")
def confirmar_compra(boleto_id):
    b, r, c = obtener_colecciones()
    folio, nombre = cliente_actual(request.get_json(silent=True) or {})
    boleto_oid = id_objeto(boleto_id)
    momento = ahora_utc()

    def accion(s):
        doc = b.find_one_and_update({
            "_id": boleto_oid, "estado": "reservado", "reserva_id": folio,
            "vence_en": {"$gt": momento}
        }, {"$set": {
            "estado": "comprado", "comprado_por": folio, "comprado_nombre": nombre,
            "comprado_en": momento, "reserva_id": None, "vence_en": None,
            "reserva_hasta": None, "reservado_por": None, "reservado_nombre": None,
            "actualizado_en": momento
        }}, return_document=ReturnDocument.AFTER, session=s)
        if doc is None:
            return None
        eliminado = r.delete_one({"boleto_id": boleto_oid, "reserva_id": folio, "estado": "activa"}, session=s)
        if eliminado.deleted_count != 1:
            raise RuntimeError("No se encontro la reserva en Atlas. No se realizo la compra.")
        c.insert_one({"boleto_id": boleto_oid, "reserva_id": folio, "cliente_nombre": nombre,
                      "evento": doc.get("evento"), "asiento": doc.get("asiento"),
                      "precio": doc.get("precio"), "comprado_en": momento}, session=s)
        return doc

    with cliente_mongo.start_session() as mongo_sesion:
        doc = mongo_sesion.with_transaction(accion)
    if not doc:
        liberar_reservas_vencidas()
        return jsonify({"mensaje": "No se pudo confirmar: vencio o la reserva no es tuya."}), 409
    return jsonify({"mensaje": "Compra confirmada correctamente.", "boleto": serializar_boleto(doc)})


@app.errorhandler(ValueError)
def manejar_valor_invalido(error):
    return jsonify({"mensaje": str(error)}), 400


@app.errorhandler(DuplicateKeyError)
def manejar_duplicado(error):
    return jsonify({"mensaje": "Ya existe ese asiento o alguien lo reservo primero."}), 409


@app.errorhandler(RuntimeError)
def manejar_configuracion(error):
    return jsonify({"mensaje": str(error)}), 500


@app.errorhandler(PyMongoError)
def manejar_mongo(error):
    app.logger.exception("Fallo al comunicarse con MongoDB Atlas: %s", error)
    return jsonify({"mensaje": "No se pudo completar la operacion con MongoDB Atlas. Revisa la conexion y las transacciones."}), 500


@app.errorhandler(404)
def no_encontrado(error):
    if request.path.startswith("/api/"):
        return jsonify({"mensaje": "Ruta no encontrada."}), 404
    return error


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")), debug=os.getenv("FLASK_DEBUG", "0") == "1")

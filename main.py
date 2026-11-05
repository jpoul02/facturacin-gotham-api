"""Simulated MH (Hacienda El Salvador) DTE services: the signer and the reception API.

Endpoints mirror the official ones so the frontend can later switch to the real
services by changing base URLs:
  POST /firmardocumento/                 signer (svfe-api-firmador)
  POST /seguridad/auth                   MH login, returns Bearer token
  POST /fesv/recepciondte                receive a signed DTE, returns selloRecibido
  POST /fesv/recepcion/consultadte/      look up a received DTE
  POST /fesv/anulardte                   void a received DTE
"""

import asyncio
import hashlib
import json
import os
import re
import secrets
import uuid
from collections import deque
from datetime import datetime, timedelta, timezone
from itertools import count
from pathlib import Path
from urllib.parse import parse_qsl

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import Depends, FastAPI, Form, Header, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel

NIT_EMISOR = os.getenv("NIT_EMISOR", "06140101261013")
PASSWORD_PRI = os.getenv("PASSWORD_PRI", "gotham-firma")
MH_USER = os.getenv("MH_USER", NIT_EMISOR)
MH_PWD = os.getenv("MH_PWD", "gotham-mh")
AMBIENTE = "00"
SV = timezone(timedelta(hours=-6))

# ponytail: key and stores live in memory and reset on restart; persist them if the app needs history across deploys
_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_tokens: set[str] = set()
_recibidos: dict[str, dict] = {}

NUMERO_CONTROL = re.compile(r"^DTE-\d{2}-[A-Z0-9]{8}-\d{15}$")
CODIGO_GENERACION = re.compile(r"^[A-F0-9]{8}-[A-F0-9]{4}-[A-F0-9]{4}-[A-F0-9]{4}-[A-F0-9]{12}$")

app = FastAPI(title="Hacienda simulada · Gotham")

# ---------- Monitor (panel visual en /) ----------

PASOS = {
    "/firmardocumento/": "Firmador",
    "/seguridad/auth": "Autenticación",
    "/fesv/recepciondte": "Recepción",
    "/fesv/recepcion/consultadte/": "Consulta",
    "/fesv/anulardte": "Anulación",
}
_eventos: deque[dict] = deque(maxlen=150)
_seq = count(1)
_demora = {"segundos": 0.0}


def _legible(entrada: dict) -> dict:
    """Mask secrets and decode the JWS so the panel shows what travels."""
    entrada = dict(entrada)
    for campo in ("passwordPri", "pwd"):
        if campo in entrada:
            entrada[campo] = "••••••"
    doc = entrada.get("documento")
    if isinstance(doc, str) and doc.count(".") == 2:
        entrada["documento"] = doc[:40] + "…"
        try:
            entrada["documentoDecodificado"] = jwt.decode(doc, options={"verify_signature": False})
        except jwt.InvalidTokenError:
            entrada["documentoDecodificado"] = "(no se pudo decodificar)"
    return entrada


@app.middleware("http")
async def monitor(request: Request, call_next):
    paso = PASOS.get(request.url.path)
    if not paso or request.method != "POST":
        return await call_next(request)
    origen = request.headers.get("x-origen", "Sistema POS")
    # ponytail: one global delay for everyone; per-client delays if several people present at once
    demora = 0 if origen == "Escenario de prueba" else _demora["segundos"]
    crudo = await request.body()
    try:
        entrada = json.loads(crudo) if crudo.startswith(b"{") else dict(parse_qsl(crudo.decode()))
    except ValueError:
        entrada = {"crudo": crudo.decode(errors="replace")[:500]}

    evento = {
        "id": next(_seq),
        "hora": datetime.now(SV).strftime("%H:%M:%S"),
        "paso": paso,
        "fase": "enviando",
        "demora": demora,
        "http": None,
        "resultado": "EN CAMINO",
        "origen": origen,
        "codigoGeneracion": entrada.get("codigoGeneracion"),
        "entrada": _legible(entrada),
        "salida": None,
    }
    _eventos.appendleft(evento)
    await asyncio.sleep(demora / 2)

    response = await call_next(request)
    cuerpo = b"".join([c async for c in response.body_iterator])
    try:
        salida = json.loads(cuerpo)
    except ValueError:
        salida = {"crudo": cuerpo.decode(errors="replace")[:500]}
    if isinstance(salida, dict) and isinstance(salida.get("body"), str):
        salida = {**salida, "body": salida["body"][:40] + "…"}
    resultado = (salida.get("estado") or salida.get("status") or "ERROR") if isinstance(salida, dict) else "ERROR"
    if response.status_code == 401:
        resultado = "NO AUTORIZADO"
    elif response.status_code >= 400:
        resultado = "ERROR"
    elif paso == "Consulta":
        resultado = "ANULADO" if salida.get("anulado") else "VIGENTE"

    evento.update(
        fase="respondiendo",
        http=response.status_code,
        resultado=resultado,
        salida=salida,
        codigoGeneracion=evento["codigoGeneracion"] or (salida.get("codigoGeneracion") if isinstance(salida, dict) else None),
    )
    await asyncio.sleep(demora / 2)
    evento["fase"] = "listo"
    return Response(cuerpo, response.status_code, dict(response.headers), response.media_type)


app.add_middleware(
    CORSMiddleware,
    allow_origins=os.getenv("CORS_ORIGINS", "*").split(","),
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/", include_in_schema=False)
def panel():
    return FileResponse(Path(__file__).with_name("panel.html"))


@app.get("/panel/estado", tags=["Panel"])
def estado():
    dtes = [
        {
            "codigoGeneracion": gen,
            "numeroControl": r["dte"]["identificacion"].get("numeroControl"),
            "receptor": (r["dte"].get("receptor") or {}).get("nombre") or "Consumidor final",
            "total": r["dte"].get("resumen", {}).get("totalPagar"),
            "sello": r["respuesta"]["selloRecibido"],
            "fecha": r["respuesta"]["fhProcesamiento"],
            "anulado": r["anulado"],
        }
        for gen, r in reversed(_recibidos.items())
    ]
    return {"eventos": list(_eventos), "dtes": dtes, "demora": _demora["segundos"]}


@app.post("/panel/reiniciar", tags=["Panel"])
def reiniciar():
    _eventos.clear()
    _recibidos.clear()
    return {"ok": True}


@app.post("/panel/escenarios", tags=["Panel"])
def escenarios():
    return correr_escenarios()


def ahora() -> str:
    return datetime.now(SV).strftime("%d/%m/%Y %H:%M:%S")


def respuesta_mh(estado: str, codigo: str, descripcion: str, generacion: str, sello=None, obs=None) -> dict:
    return {
        "version": 2,
        "ambiente": AMBIENTE,
        "versionApp": 2,
        "estado": estado,
        "codigoGeneracion": generacion,
        "selloRecibido": sello,
        "fhProcesamiento": ahora(),
        "clasificaMsg": "10" if estado == "PROCESADO" else "98",
        "codigoMsg": codigo,
        "descripcionMsg": descripcion,
        "observaciones": obs or [],
    }


def rechazo(generacion: str, codigo: str, descripcion: str, obs=None) -> dict:
    return respuesta_mh("RECHAZADO", codigo, descripcion, generacion, obs=obs)


# ---------- Firmador ----------

class FirmaIn(BaseModel):
    nit: str
    activo: bool = True
    passwordPri: str
    dteJson: dict


@app.post("/firmardocumento/")
def firmar(data: FirmaIn):
    if data.nit != NIT_EMISOR:
        return {"status": "ERROR", "body": {"codigo": "803", "mensaje": "No existe certificado para el NIT"}}
    if data.passwordPri != PASSWORD_PRI:
        return {"status": "ERROR", "body": {"codigo": "805", "mensaje": "Contraseña de la llave privada inválida"}}
    return {"status": "OK", "body": jwt.encode(data.dteJson, _key, algorithm="RS512")}


# ---------- Hacienda ----------

@app.post("/seguridad/auth")
def auth(user: str = Form(...), pwd: str = Form(...)):
    if user != MH_USER or pwd != MH_PWD:
        raise HTTPException(401, {"status": "ERROR", "error": "Usuario o contraseña incorrectos"})
    token = secrets.token_urlsafe(32)
    _tokens.add(token)
    return {"status": "OK", "body": {"user": user, "token": f"Bearer {token}", "tokenType": "Bearer", "rol": {"nombre": "Usuario", "codigo": "ROLE_USER"}}}


def requiere_token(authorization: str = Header("")):
    if authorization.removeprefix("Bearer ").strip() not in _tokens:
        raise HTTPException(401, {"status": "ERROR", "error": "Token inválido o vencido"})


def leer_jws(documento: str) -> dict:
    return jwt.decode(documento, _key.public_key(), algorithms=["RS512"])


class RecepcionIn(BaseModel):
    ambiente: str
    idEnvio: int
    version: int
    tipoDte: str
    documento: str
    codigoGeneracion: str


@app.post("/fesv/recepciondte", dependencies=[Depends(requiere_token)])
def recepcion(data: RecepcionIn):
    gen = data.codigoGeneracion
    try:
        dte = leer_jws(data.documento)
    except jwt.InvalidTokenError:
        return rechazo(gen, "096", "DOCUMENTO NO FIRMADO O FIRMA INVÁLIDA")

    ident = dte.get("identificacion", {})
    obs = []
    if data.ambiente != AMBIENTE or ident.get("ambiente") != AMBIENTE:
        obs.append("[ambiente] VALOR NO CORRESPONDE AL AMBIENTE DE PRUEBAS")
    if ident.get("codigoGeneracion") != gen or not CODIGO_GENERACION.match(gen):
        obs.append("[identificacion.codigoGeneracion] NO COINCIDE O FORMATO INVÁLIDO")
    if ident.get("tipoDte") != data.tipoDte:
        obs.append("[identificacion.tipoDte] NO COINCIDE CON EL ENVÍO")
    if not NUMERO_CONTROL.match(ident.get("numeroControl", "")):
        obs.append("[identificacion.numeroControl] FORMATO INVÁLIDO")
    if dte.get("emisor", {}).get("nit") != NIT_EMISOR:
        obs.append("[emisor.nit] NO CORRESPONDE AL CONTRIBUYENTE AUTENTICADO")

    cuerpo = dte.get("cuerpoDocumento") or []
    if not cuerpo:
        obs.append("[cuerpoDocumento] DEBE CONTENER AL MENOS UN ÍTEM")
    total_lineas = round(sum(i.get("ventaGravada", 0) + i.get("ventaExenta", 0) + i.get("ventaNoSuj", 0) for i in cuerpo), 2)
    resumen = dte.get("resumen", {})
    if abs(total_lineas - resumen.get("subTotalVentas", -1)) > 0.01:
        obs.append("[resumen.subTotalVentas] NO COINCIDE CON LA SUMA DEL CUERPO")
    if resumen.get("totalPagar", 0) <= 0:
        obs.append("[resumen.totalPagar] DEBE SER MAYOR A CERO")

    if obs:
        return rechazo(gen, "004", "RECHAZADO POR VALIDACIONES", obs)
    if gen in _recibidos:
        return rechazo(gen, "004", "[identificacion.codigoGeneracion] YA EXISTE UN REGISTRO CON ESE VALOR")

    anio = datetime.now(SV).year
    sello = f"{anio}{hashlib.sha1(data.documento.encode()).hexdigest()[:36].upper()}"
    resp = respuesta_mh("PROCESADO", "001", "RECIBIDO", gen, sello)
    _recibidos[gen] = {"dte": dte, "respuesta": resp, "anulado": False}
    return resp


class ConsultaIn(BaseModel):
    nitEmisor: str
    tdte: str
    codigoGeneracion: str


@app.post("/fesv/recepcion/consultadte/", dependencies=[Depends(requiere_token)])
def consulta(data: ConsultaIn):
    reg = _recibidos.get(data.codigoGeneracion)
    if not reg or reg["dte"]["emisor"]["nit"] != data.nitEmisor:
        raise HTTPException(404, {"status": "ERROR", "error": "DTE no encontrado"})
    return {**reg["respuesta"], "anulado": reg["anulado"]}


class AnulacionIn(BaseModel):
    ambiente: str
    idEnvio: int
    version: int
    documento: str


@app.post("/fesv/anulardte", dependencies=[Depends(requiere_token)])
def anular(data: AnulacionIn):
    try:
        evento = leer_jws(data.documento)
    except jwt.InvalidTokenError:
        return rechazo("", "096", "DOCUMENTO NO FIRMADO O FIRMA INVÁLIDA")
    gen_evento = evento.get("identificacion", {}).get("codigoGeneracion", str(uuid.uuid4()).upper())
    gen_dte = evento.get("documento", {}).get("codigoGeneracion")
    reg = _recibidos.get(gen_dte)
    if not reg:
        return rechazo(gen_evento, "004", "[documento.codigoGeneracion] EL DTE NO EXISTE")
    if reg["anulado"]:
        return rechazo(gen_evento, "004", "[documento.codigoGeneracion] EL DTE YA FUE INVALIDADO")
    if not (evento.get("motivo", {}).get("motivoAnulacion") or "").strip():
        return rechazo(gen_evento, "004", "[motivo.motivoAnulacion] ES REQUERIDO")
    reg["anulado"] = True
    sello = f"{datetime.now(SV).year}{hashlib.sha1(data.documento.encode()).hexdigest()[:36].upper()}"
    return respuesta_mh("PROCESADO", "001", "RECIBIDO", gen_evento, sello)


# ---------- Escenarios de prueba (botón del panel y pytest) ----------

def _dte_prueba(gen: str, numero: int) -> dict:
    return {
        "identificacion": {"version": 1, "ambiente": AMBIENTE, "tipoDte": "01", "codigoGeneracion": gen,
                           "numeroControl": f"DTE-01-PRUEBA01-{numero:015d}"},
        "emisor": {"nit": NIT_EMISOR, "nombre": "ADVENTURE WORKS S.A. DE C.V."},
        "receptor": {"nombre": "Cliente de prueba"},
        "cuerpoDocumento": [{"numItem": 1, "descripcion": "Casco de ciclismo", "cantidad": 1, "precioUni": 10.0,
                             "ventaGravada": 10.0, "ventaExenta": 0, "ventaNoSuj": 0}],
        "resumen": {"subTotalVentas": 10.0, "totalIva": 1.3, "totalPagar": 11.3},
    }


def correr_escenarios() -> list[dict]:
    from fastapi.testclient import TestClient

    c = TestClient(app, headers={"X-Origen": "Escenario de prueba"})
    resultados = []

    def caso(nombre: str, esperado: str, obtenido: str, detalle: str = ""):
        resultados.append({"nombre": nombre, "esperado": esperado, "obtenido": obtenido,
                           "ok": esperado == obtenido, "detalle": detalle})

    def firmar(doc: dict, pwd: str = PASSWORD_PRI) -> dict:
        return c.post("/firmardocumento/", json={"nit": NIT_EMISOR, "activo": True, "passwordPri": pwd, "dteJson": doc}).json()

    def enviar(jws: str, gen: str, headers: dict) -> dict:
        r = c.post("/fesv/recepciondte", headers=headers, json={
            "ambiente": AMBIENTE, "idEnvio": 1, "version": 1, "tipoDte": "01", "documento": jws, "codigoGeneracion": gen})
        return {"http": r.status_code, **r.json()}

    base = len(_recibidos) + 1

    caso("Firmar con contraseña incorrecta", "ERROR", firmar({}, "incorrecta")["status"],
         "El firmador no entrega firma sin la contraseña de la llave privada")

    r = c.post("/seguridad/auth", data={"user": MH_USER, "pwd": "incorrecta"})
    caso("Login en Hacienda con contraseña incorrecta", "401", str(r.status_code))

    token = c.post("/seguridad/auth", data={"user": MH_USER, "pwd": MH_PWD}).json()["body"]["token"]
    h = {"Authorization": token}

    gen = str(uuid.uuid4()).upper()
    jws = firmar(_dte_prueba(gen, base))["body"]
    caso("Enviar DTE sin token", "401", str(enviar(jws, gen, {})["http"]),
         "Hacienda exige el token Bearer de /seguridad/auth")

    r = enviar(jws, gen, h)
    caso("Emitir factura válida", "PROCESADO", r["estado"], f"Sello: {r['selloRecibido']}")

    r = enviar(jws, gen, h)
    caso("Reenviar la misma factura", "RECHAZADO", r["estado"], r["descripcionMsg"])

    caso("Documento con firma alterada", "096", enviar(jws[:-6] + "AAAAAA", gen, h)["codigoMsg"],
         "Hacienda verifica la firma RS512 con la llave pública")

    malo = _dte_prueba(str(uuid.uuid4()).upper(), base + 1)
    malo["resumen"]["subTotalVentas"] = 99.0
    g2 = malo["identificacion"]["codigoGeneracion"]
    r = enviar(firmar(malo)["body"], g2, h)
    caso("Totales que no cuadran", "RECHAZADO", r["estado"], "; ".join(r["observaciones"]))

    q = {"nitEmisor": NIT_EMISOR, "tdte": "01", "codigoGeneracion": gen}
    r = c.post("/fesv/recepcion/consultadte/", json=q, headers=h).json()
    caso("Consultar la factura emitida", "VIGENTE", "ANULADO" if r.get("anulado") else "VIGENTE")

    evento = {"identificacion": {"codigoGeneracion": str(uuid.uuid4()).upper()},
              "documento": {"codigoGeneracion": gen}, "motivo": {"motivoAnulacion": "Error en el monto"}}
    anulacion = {"ambiente": AMBIENTE, "idEnvio": 2, "version": 2, "documento": firmar(evento)["body"]}
    caso("Anular la factura", "PROCESADO", c.post("/fesv/anulardte", json=anulacion, headers=h).json()["estado"])

    r = c.post("/fesv/anulardte", json=anulacion, headers=h).json()
    caso("Anular dos veces", "RECHAZADO", r["estado"], r["descripcionMsg"])

    r = c.post("/fesv/recepcion/consultadte/", json=q, headers=h).json()
    caso("Consultar después de anular", "ANULADO", "ANULADO" if r.get("anulado") else "VIGENTE")

    return resultados


class DemoraIn(BaseModel):
    segundos: float


@app.post("/panel/demora", tags=["Panel"])
def cambiar_demora(data: DemoraIn):
    """Slow mode: each request waits this many seconds (half going, half returning) so the flow can be followed live."""
    _demora["segundos"] = max(0.0, min(data.segundos, 6.0))
    return _demora

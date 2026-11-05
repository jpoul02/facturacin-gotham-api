from fastapi.testclient import TestClient

from main import app

c = TestClient(app)


def test_escenarios():
    resultados = c.post("/panel/escenarios").json()
    fallidos = [r for r in resultados if not r["ok"]]
    assert len(resultados) == 11 and not fallidos, fallidos


def test_panel_registra_eventos():
    estado = c.get("/panel/estado").json()
    assert estado["eventos"] and estado["dtes"]
    firma = next(e for e in estado["eventos"] if e["paso"] == "Firmador" and e["resultado"] == "OK")
    assert firma["entrada"]["passwordPri"] == "••••••"
    recepcion = next(e for e in estado["eventos"] if e["paso"] == "Recepción" and e["resultado"] == "PROCESADO")
    assert recepcion["entrada"]["documentoDecodificado"]["emisor"]["nit"]
    assert c.get("/").status_code == 200

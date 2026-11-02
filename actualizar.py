#!/usr/bin/env python3
"""SET SEGOVIA - actualizador automatico.

Lo ejecuta GitHub Actions cada pocos minutos. Genera la carpeta "site" con la
app y un data.json con todos los trenes de Segovia-Guiomar:
  1. Horario oficial de Renfe (GTFS) -> todos los trenes de hoy y los proximos dias.
  2. Panel de Adif -> numero comercial, via y retrasos de los proximos trenes.
  3. Feed en tiempo real de Renfe (GTFS-RT) -> posicion, velocidad y distancia.
Si una fuente falla, las demas siguen funcionando y el motivo queda en "avisos".
"""
import csv
import datetime as dt
import html
import io
import json
import math
import os
import re
import shutil
import urllib.request
import zipfile
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Europe/Madrid")
ADIF = "https://www.adif.es/w/08004-segovia-guiomar"
GTFS_URLS = ["https://ssl.renfe.com/ftransit/Fichero_AV_LD/google_transit.zip"]
RT_TRIPS = ["https://gtfsrt.renfe.com/trip_updates_LD.json",
            "https://gtfsrt.renfe.com/trip_updates.json"]
RT_VEH = ["https://gtfsrt.renfe.com/vehicle_positions_LD.json",
          "https://gtfsrt.renfe.com/vehicle_positions.json"]
FALLBACK = (40.9137, -4.1165)  # posicion aproximada de la estacion
DIAS = 3
SALIDA = "site"
UA = {"User-Agent": "Mozilla/5.0 (compatible; SET-SEGOVIA/1.0)"}
avisos = []


def get(url, timeout=60):
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout) as r:
        return r.read()


def minutos(s):
    p = s.split(":")
    return int(p[0]) * 60 + int(p[1])


def hhmm(m):
    m = int(round(m)) % 1440
    return "%02d:%02d" % (m // 60, m % 60)


def dist_km(la1, lo1, la2, lo2):
    p1, p2 = math.radians(la1), math.radians(la2)
    a = (math.sin((p2 - p1) / 2) ** 2
         + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lo2 - lo1) / 2) ** 2)
    return 2 * 6371.0 * math.asin(math.sqrt(a))


def guardar_debug(nombre, contenido):
    os.makedirs(os.path.join(SALIDA, "debug"), exist_ok=True)
    with open(os.path.join(SALIDA, "debug", nombre), "w", encoding="utf-8") as f:
        f.write(contenido)


def g(d, *claves):
    """Primer valor presente (acepta camelCase y snake_case)."""
    for c in claves:
        if isinstance(d, dict) and c in d and d[c] is not None:
            return d[c]
    return None


# ---------------------------------------------------------------- Adif
TIPOS = "AVE|AVLO|AVANT|ALVIA|INTERCITY|EUROMED|OUIGO|IRYO"
RX = re.compile(r"(\d{1,2}:\d{2})\s+(?:(\d{1,2}:\d{2})\s+)?(" + TIPOS
                + r")\s*(\d{4,5})\s+(.+?)\s+(\d{1,2})\b")


def a_texto(h):
    t = re.sub(r"(?is)<(script|style).*?</\1>", " ", h)
    t = re.sub(r"(?s)<[^>]+>", " ", t)
    return re.sub(r"\s+", " ", html.unescape(t))


def parsear_adif(txt, hoy):
    salida, dia, ultimo = [], hoy, None
    for m in RX.finditer(txt):
        prog, est, tipo, num, origen, via = m.groups()
        mp = minutos(prog)
        if ultimo is not None and mp < ultimo - 120:
            dia += dt.timedelta(days=1)
        ultimo = mp
        if tipo in ("OUIGO", "IRYO"):  # no son de Renfe
            continue
        me = minutos(est) if est else mp
        salida.append({"fecha": dia.isoformat(), "prog": hhmm(mp),
                       "est": hhmm(me) if me != mp else "", "num": num,
                       "tipo": tipo, "origen": origen.strip(), "via": via,
                       "fuente": "adif"})
    return salida


# ------------------------------------------------------- Horario oficial
def leer(z, nombre):
    with z.open(nombre) as f:
        for fila in csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig")):
            yield {(k or "").strip(): (v or "").strip() for k, v in fila.items()}


def horario_oficial(z, hoy):
    nombres = set(z.namelist())
    paradas = {r["stop_id"]: r for r in leer(z, "stops.txt")}

    def buscar(*claves):
        return {i for i, r in paradas.items()
                if all(c in r.get("stop_name", "").lower() for c in claves)}

    sg = buscar("segovia", "guiomar") or buscar("segovia")
    if not sg:
        raise RuntimeError("no encuentro Segovia en stops.txt")
    p0 = paradas[sorted(sg)[0]]
    pos = (float(p0["stop_lat"]), float(p0["stop_lon"])) if p0.get("stop_lat") else FALLBACK

    en_sg = {}
    for r in leer(z, "stop_times.txt"):
        if r["stop_id"] in sg:
            en_sg[r["trip_id"]] = r.get("arrival_time") or r.get("departure_time")
    ext = {}
    for r in leer(z, "stop_times.txt"):
        t = r["trip_id"]
        if t in en_sg:
            s = int(r.get("stop_sequence") or 0)
            e = ext.setdefault(t, [None, None])
            if e[0] is None or s < e[0][0]:
                e[0] = (s, r["stop_id"])
            if e[1] is None or s > e[1][0]:
                e[1] = (s, r["stop_id"])
    trips = {r["trip_id"]: r for r in leer(z, "trips.txt") if r["trip_id"] in en_sg}
    rutas = {r["route_id"]: r for r in leer(z, "routes.txt")} if "routes.txt" in nombres else {}
    cal = {r["service_id"]: r for r in leer(z, "calendar.txt")} if "calendar.txt" in nombres else {}
    exc = ({(r["service_id"], r["date"]): r["exception_type"] for r in leer(z, "calendar_dates.txt")}
           if "calendar_dates.txt" in nombres else {})
    semana = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]

    def activo(sid, d):
        ds = d.strftime("%Y%m%d")
        e = exc.get((sid, ds))
        if e == "1":
            return True
        if e == "2":
            return False
        c = cal.get(sid)
        return bool(c and c["start_date"] <= ds <= c["end_date"] and c[semana[d.weekday()]] == "1")

    def nombre(sid):
        return paradas.get(sid, {}).get("stop_name", "")

    recs = []
    for tid, hora in en_sg.items():
        tr = trips.get(tid)
        if not tr or not hora:
            continue
        hh, mm = int(hora.split(":")[0]), hora.split(":")[1]
        extra, h24 = divmod(hh, 24)
        ruta = rutas.get(tr.get("route_id", ""), {})
        e = ext.get(tid, [None, None])
        for i in range(DIAS):
            d = hoy + dt.timedelta(days=i)
            if not activo(tr.get("service_id", ""), d - dt.timedelta(days=extra)):
                continue
            prog = "%02d:%s" % (h24, mm)
            num = tr.get("trip_short_name", "")
            recs.append({"fecha": d.isoformat(), "prog": prog, "est": "", "num": num,
                         "tipo": ruta.get("route_short_name", "").upper(),
                         "origen": nombre(e[0][1]) if e[0] else "",
                         "destino": nombre(e[1][1]) if e[1] else "",
                         "via": "", "fuente": "renfe", "_t": tid})
    return recs, pos, sg


# ------------------------------------------------------------ Tiempo real
def primero_json(urls):
    for u in urls:
        try:
            return json.loads(get(u, 40)), u
        except Exception as e:  # noqa: BLE001
            avisos.append("Sin acceso a %s (%s)" % (u.split("/")[-1], str(e)[:60]))
    return None, None


def aplicar_tiempo_real(recs, sg, pos, ahora):
    tu, u1 = primero_json(RT_TRIPS)
    vp, u2 = primero_json(RT_VEH)
    if tu:
        guardar_debug("rt_trips.json", json.dumps(tu)[:6000])
    if vp:
        guardar_debug("rt_vehicles.json", json.dumps(vp)[:6000])
    retrasos, vehs = {}, {}
    for ent in (tu or {}).get("entity", []):
        t = g(ent, "tripUpdate", "trip_update") or {}
        tid = g(g(t, "trip") or {}, "tripId", "trip_id")
        stus = g(t, "stopTimeUpdate", "stop_time_update") or []
        pick = next((s for s in stus if g(s, "stopId", "stop_id") in sg), stus[0] if stus else None)
        if tid and pick:
            ev = g(pick, "arrival") or g(pick, "departure") or {}
            d = g(ev, "delay")
            if d is not None:
                retrasos[tid] = int(d)
    for ent in (vp or {}).get("entity", []):
        v = g(ent, "vehicle") or {}
        tid = g(g(v, "trip") or {}, "tripId", "trip_id")
        p = g(v, "position") or {}
        la, lo = g(p, "latitude"), g(p, "longitude")
        if tid and la is not None and lo is not None:
            vehs[tid] = (float(la), float(lo), g(p, "speed"))
    casados = 0
    hoy = ahora.date().isoformat()
    for r in recs:
        tid = r.get("_t")
        if not tid:
            continue
        if tid in retrasos and abs(retrasos[tid]) >= 60 and r["fecha"] == hoy:
            r["est"] = hhmm(minutos(r["prog"]) + retrasos[tid] / 60.0)
        if tid in vehs and r["fecha"] == hoy:
            la, lo, sp = vehs[tid]
            r["pos"] = {"dist": round(dist_km(la, lo, pos[0], pos[1]), 1),
                        "vel": round(float(sp) * 3.6, 0) if sp is not None else None}
            casados += 1
    if (tu or vp) and not casados:
        avisos.append("El feed en directo responde, pero no logro casar sus trenes con el horario")
    return casados


def fusionar(oficiales, adif):
    idx = {}
    for r in oficiales:
        idx.setdefault((r["fecha"], r["prog"]), []).append(r)
    extra = []
    for a in adif:
        cand = idx.get((a["fecha"], a["prog"]), [])
        c = (next((x for x in cand if x["num"] == a["num"]), None)
             or next((x for x in cand if not x["num"] and not x.get("_a")), None))
        if c is None:
            extra.append(a)
            continue
        c["_a"] = True
        c["num"], c["via"] = a["num"], a["via"]
        c["tipo"] = a["tipo"] or c["tipo"]
        c["origen"] = c["origen"] or a["origen"]
        if a["est"]:
            c["est"] = a["est"]
    return oficiales + extra


def main():
    os.makedirs(SALIDA, exist_ok=True)
    ahora = dt.datetime.now(TZ)
    hoy = ahora.date()
    oficiales, pos, sg = [], FALLBACK, set()
    try:
        z = None
        for u in GTFS_URLS:
            try:
                z = zipfile.ZipFile(io.BytesIO(get(u, 150)))
                break
            except Exception as e:  # noqa: BLE001
                avisos.append("Horario oficial de Renfe no disponible (%s)" % str(e)[:60])
        if z:
            guardar_debug("gtfs_archivos.txt", "\n".join(z.namelist()))
            oficiales, pos, sg = horario_oficial(z, hoy)
    except Exception as e:  # noqa: BLE001
        avisos.append("Error leyendo el horario oficial: %s" % str(e)[:80])
    adif = []
    try:
        txt = a_texto(get(ADIF).decode("utf-8", "replace"))
        guardar_debug("adif.txt", txt[:20000])
        adif = parsear_adif(txt, hoy)
        if not adif:
            avisos.append("Adif: no se reconoce ningun tren (hay que ajustar el lector)")
    except Exception as e:  # noqa: BLE001
        avisos.append("Adif no disponible: %s" % str(e)[:80])
    recs = fusionar(oficiales, adif)
    try:
        aplicar_tiempo_real(recs, sg, pos, ahora)
    except Exception as e:  # noqa: BLE001
        avisos.append("Error en el tiempo real: %s" % str(e)[:80])
    for r in recs:
        r["id"] = "%s-%s-%s" % (r["fecha"], r["prog"], r["num"] or str(r.get("_t", ""))[-6:] or "x")
        r.pop("_t", None)
        r.pop("_a", None)
        if r.get("est") == r["prog"]:
            r["est"] = ""
    recs.sort(key=lambda r: (r["fecha"], r["prog"]))
    with open(os.path.join(SALIDA, "data.json"), "w", encoding="utf-8") as f:
        json.dump({"generado": ahora.isoformat(), "generado_local": ahora.strftime("%d/%m %H:%M"),
                   "estacion": {"nombre": "Segovia-Guiomar", "lat": pos[0], "lon": pos[1]},
                   "avisos": avisos[:8], "trenes": recs}, f, ensure_ascii=False)
    for nombre in ("index.html", "manifest.webmanifest", "sw.js", "icon-192.png", "icon-512.png"):
        if os.path.exists(nombre):
            shutil.copy(nombre, os.path.join(SALIDA, nombre))
    print("trenes:", len(recs), "avisos:", avisos)


if __name__ == "__main__":
    main()

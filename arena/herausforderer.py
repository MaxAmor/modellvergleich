"""Arena: beliebig viele Modelle treten nacheinander auf einem Datensatz an – gleiche Prompts, Werkzeuge und Kennzahlen.

Datensätze: der mitgelieferte Demo-Datensatz (daten/) und eigene Uploads
(aufgaben.json + referenz.json pro Fach, optional Bilder) unter <Datenordner>/datensaetze/<id>/<fach>/.
Läuft als Hintergrund-Thread im Webserver. Ergebnisse: <Datenordner>/arena/<datensatz>/<modell>.json (setzt nach Abbruch fort).
"""
from __future__ import annotations

import json
import os
import re
import shutil
import threading
import time
import urllib.request
from datetime import datetime
from pathlib import Path

from arena import einstellungen, fragen as fr, kern
from arena.werkzeuge import pruefe

ORDNER = Path(os.environ.get("HERAUSFORDERER_DIR") or einstellungen.DATEN_DIR / "arena")
DATENSAETZE = einstellungen.DATEN_DIR / "datensaetze"
MODI = ("blind", "denken", "werkzeuge", "muster", "bild")
FRAGEN_MODI = ("antwort", "denken", "rechner")   # Fragen-Datensätze: direkt / mit Reasoning / mit Werkzeugen   # Nur Aufgabe · Mit Werkzeugen · Mit Musterlösung · Mit Bild (nur Aufgaben mit Abbildung)
GPU_URL = os.environ.get("GPU_STATUS_URL") or None   # optional: eigener GPU-Status-Dienst ({"jetzt": {"vram_gb": …}})

JOB: dict = {"laeuft": False}
_sperre = threading.Lock()
_stopp = threading.Event()


def slug(text: str) -> str:
    s = re.sub(r"[^\w.-]+", "_", text).strip("_.")[:80]
    s = s.encode("utf8")[:150].decode("utf8", "ignore").strip("_.")   # Dateinamen-Grenze in Bytes, nicht Zeichen
    if s.lower() in ("demo", "") or s.endswith(".neu"):
        s = (s or "x") + "_"
    return s


# ---------------------------------------------------------------- Datensätze
def datensaetze() -> list[dict]:
    aus = [{"id": "demo", "name": "Demo-Datensatz", "eigen": False, **_beschreibe(kern.DATEN)}]
    if DATENSAETZE.exists():
        for d in sorted(p for p in DATENSAETZE.iterdir() if p.is_dir() and not p.name.endswith(".neu")):
            meta = {}
            if (d / "datensatz.json").exists():
                try:
                    meta = json.loads((d / "datensatz.json").read_text(encoding="utf8"))
                except Exception:
                    pass
            aus.append({"id": d.name, "name": meta.get("name") or d.name, "eigen": True, **_beschreibe(d)})
    return aus


def typ_von(ordner: Path) -> str:
    return "fragen" if (ordner / "fragen.json").exists() else "bewertung"


def lade_fragen(ordner: Path) -> list[dict]:
    return json.loads((ordner / "fragen.json").read_text(encoding="utf8"))["fragen"]


_beschreibung_cache: dict = {}


def _beschreibe(ordner: Path) -> dict:
    """Zwischengespeichert nach Änderungszeit der Dateien – die Liste wird oft abgefragt."""
    try:
        stand = tuple(sorted((str(p), p.stat().st_mtime_ns) for p in ordner.rglob("*.json")))
    except OSError:
        stand = None
    tr = _beschreibung_cache.get(str(ordner))
    if stand and tr and tr[0] == stand:
        return tr[1]
    wert = _beschreibe_neu(ordner)
    if stand:
        _beschreibung_cache[str(ordner)] = (stand, wert)
    return wert


def _beschreibe_neu(ordner: Path) -> dict:
    if typ_von(ordner) == "fragen":
        try:
            f = lade_fragen(ordner)
        except Exception as fehler:
            return {"typ": "fragen", "faecher": [], "aufgaben": 0, "antworten": 0, "fehler": str(fehler)[:200]}
        themen = sorted({x["thema"] for x in f if x.get("thema")})
        return {"typ": "fragen", "faecher": themen or ["allgemein"], "aufgaben": len(f), "antworten": len(f),
                "wertungen": sorted({x["wertung"] for x in f}), "bilder": sum(1 for x in f if x.get("bild"))}
    try:
        e = kern.lade_eintraege(ordner)
    except Exception as fehler:
        return {"faecher": [], "aufgaben": 0, "antworten": 0, "fehler": str(fehler)[:200]}
    return {"typ": "bewertung", "faecher": sorted({x["fach"] for x in e}), "aufgaben": len({x["aufgabe_id"] for x in e}), "antworten": len(e)}


def ordner_von(ds: str) -> Path:
    if ds == "demo":
        return kern.DATEN
    p = DATENSAETZE / slug(ds)
    if not p.is_dir():
        raise ValueError("unbekannter Datensatz")
    return p


def speichere_upload(name: str, dateien: list[dict]) -> dict:
    """dateien: [{"name": "aufgaben.json", "inhalt": {...}}, {"name": "referenz.json", ...}, {"name": "x.png", "bild": "<base64>"}, …].

    Je Fach eine aufgaben.json (Feld „aufgaben“) und eine referenz.json (Feld „loesungen“) mit derselben meta.fach_id.
    Bilder werden dem Fach zugeordnet, dessen Abbildungen sie nennen. Prüft, ob jede Aufgabe eine Lösung und jede Antwort eine Bewertung hat."""
    import base64
    aufg, ref, bilder, roh = {}, {}, {}, []
    for d in dateien:
        if d.get("text") is not None:          # allgemeine Daten: CSV / JSONL / JSON
            roh.append(d)
            continue
        if d.get("bild") is not None:
            n = Path(str(d.get("name") or "")).name
            if not re.fullmatch(r"[\w.-]+\.(png|jpe?g|webp|gif)", n, re.I):
                raise ValueError(f"{n}: nur PNG, JPG, WEBP oder GIF")
            try:
                bilder[n] = base64.b64decode(str(d["bild"]).split(",")[-1], validate=True)
            except Exception:
                raise ValueError(f"{n}: Bild nicht lesbar") from None
            if len(bilder[n]) > 4 * 1024 * 1024:
                raise ValueError(f"{n}: Bild größer als 4 MB")
            continue
        inhalt = d.get("inhalt")
        if not isinstance(inhalt, dict):
            raise ValueError(f"{d.get('name')}: kein JSON-Objekt")
        meta = inhalt.get("meta") or {}
        fach = slug(str(meta.get("fach_id") or meta.get("fach") or "fach").lower())
        if "aufgaben" in inhalt:
            aufg[fach] = inhalt
        elif "loesungen" in inhalt:
            ref[fach] = inhalt
        else:
            raise ValueError(f"{d.get('name')}: weder aufgaben.json (Feld „aufgaben“) noch referenz.json (Feld „loesungen“)")
    if roh and not aufg:
        return speichere_fragen(name, roh, bilder)
    if not aufg:
        raise ValueError("Keine aufgaben.json dabei")
    fehlt = [f for f in aufg if f not in ref]
    if fehlt:
        raise ValueError(f"Zu Fach „{fehlt[0]}“ fehlt die referenz.json (beide brauchen dieselbe meta.fach_id)")
    ziel = DATENSAETZE / slug(name)
    tmp = ziel.with_name(ziel.name + ".neu")
    shutil.rmtree(tmp, ignore_errors=True)
    for fach in aufg:
        (tmp / fach).mkdir(parents=True)
        (tmp / fach / "aufgaben.json").write_text(json.dumps(aufg[fach], ensure_ascii=False), encoding="utf8")
        (tmp / fach / "referenz.json").write_text(json.dumps(ref[fach], ensure_ascii=False), encoding="utf8")
        for a in aufg[fach].get("aufgaben", []):
            for abb in a.get("abbildungen") or []:
                datei = str(abb.get("datei") or "")
                n = Path(datei).name
                ziel_bild = (tmp / fach / datei).resolve()
                if n in bilder and re.fullmatch(r"[\w./-]+", datei) and ".." not in datei and not datei.startswith("/") \
                        and (tmp / fach).resolve() in ziel_bild.parents:
                    ziel_bild.parent.mkdir(parents=True, exist_ok=True)
                    ziel_bild.write_bytes(bilder[n])
    try:
        eintraege = kern.lade_eintraege(tmp)
        if not eintraege:
            raise ValueError("Keine Schülerantworten gefunden")
    except KeyError as fehler:
        shutil.rmtree(tmp, ignore_errors=True)
        raise ValueError(f"Fehlendes Feld oder fehlende Lösung/Bewertung: {fehler}") from None
    except Exception as fehler:
        shutil.rmtree(tmp, ignore_errors=True)
        raise ValueError(f"Datensatz nicht lesbar: {fehler}") from None
    (tmp / "datensatz.json").write_text(json.dumps({"name": name.strip()[:80]}, ensure_ascii=False), encoding="utf8")
    shutil.rmtree(ziel, ignore_errors=True)
    shutil.rmtree(ORDNER / ziel.name, ignore_errors=True)   # alte Ergebnisse passen nicht mehr
    tmp.replace(ziel)
    return {"id": ziel.name, "name": name, **_beschreibe(ziel)}


def speichere_fragen(name: str, roh: list[dict], bilder: dict) -> dict:
    """Allgemeiner Fragen-Datensatz aus CSV/JSONL/JSON (+ Bilder) → <Datenordner>/datensaetze/<id>/fragen.json"""
    alle = []
    for d in roh:
        try:
            zeilen = fr.lese(str(d.get("name") or ""), str(d["text"]))
        except Exception as fehler:
            raise ValueError(f"{d.get('name')}: nicht lesbar ({str(fehler)[:80]})") from None
        alle += fr.normalisiere(zeilen, d.get("name") or "")
    return speichere_fragen_liste(name, alle, bilder)


def speichere_fragen_liste(name: str, alle: list[dict], bilder: dict | None = None) -> dict:
    bilder = bilder or {}
    alle = alle[:5000]   # höchstens 5000 Fragen je Datensatz (wie beim Import)
    if not alle:
        raise ValueError("Keine Fragen erkannt. Nötig sind je Zeile eine Frage (z. B. Spalte „frage“ oder „question“) und eine Soll-Antwort (z. B. „antwort“ oder „answer“).")
    ziel = DATENSAETZE / slug(name)
    tmp = ziel.with_name(ziel.name + ".neu")
    shutil.rmtree(tmp, ignore_errors=True)
    (tmp / "bilder").mkdir(parents=True)
    for f in alle:
        if f.get("bild"):
            n = Path(f["bild"]).name
            if n in bilder:
                (tmp / "bilder" / n).write_bytes(bilder[n])
                f["bild"] = f"bilder/{n}"
            else:
                f.pop("bild")
    (tmp / "fragen.json").write_text(json.dumps({"meta": {"name": name, "typ": "fragen"}, "fragen": alle}, ensure_ascii=False), encoding="utf8")
    (tmp / "datensatz.json").write_text(json.dumps({"name": name.strip()[:80]}, ensure_ascii=False), encoding="utf8")
    shutil.rmtree(ziel, ignore_errors=True)
    shutil.rmtree(ORDNER / ziel.name, ignore_errors=True)
    tmp.replace(ziel)
    return {"id": ziel.name, "name": name, **_beschreibe(ziel)}


PAPIERKORB = einstellungen.DATEN_DIR / "papierkorb"


def loesche_eintraege(ds: str, ids: list) -> dict:
    """Einzelne Fragen bzw. Aufgaben (samt ihren Antworten) entfernen. Der alte Stand wird gesichert (Rückgängig)."""
    import uuid
    if ds == "demo":
        raise ValueError("Der Demo-Datensatz bleibt unverändert.")
    if JOB.get("laeuft") and ds in (JOB.get("datensaetze") or [JOB.get("datensatz")]):
        raise RuntimeError("Gerade läuft ein Vergleich auf diesem Datensatz – erst danach lassen sich Einträge löschen.")
    ordner, weg = ordner_von(ds), {str(i) for i in ids}
    if not weg:
        raise ValueError("Nichts ausgewählt.")
    # alte Sicherungen (älter als ein Tag) aufräumen, dann aktuellen Stand sichern
    for alt in PAPIERKORB.glob("*/*"):
        if time.time() - alt.stat().st_mtime > 86400:
            shutil.rmtree(alt, ignore_errors=True)
    token = uuid.uuid4().hex[:16]
    sicher = PAPIERKORB / slug(ds) / token
    for q in ordner.rglob("*.json"):
        z = sicher / q.relative_to(ordner)
        z.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(q, z)
    def schreibe(pfad: Path, daten):
        tmp = pfad.with_suffix(".tmp")
        tmp.write_text(json.dumps(daten, ensure_ascii=False), encoding="utf8")
        tmp.replace(pfad)
    if typ_von(ordner) == "fragen":
        d = json.loads((ordner / "fragen.json").read_text(encoding="utf8"))
        rest = [f for f in d["fragen"] if str(f["id"]) not in weg]
        if not rest:
            shutil.rmtree(sicher, ignore_errors=True)
            raise ValueError("Alle Fragen ausgewählt – dann bitte den ganzen Datensatz entfernen.")
        n = len(d["fragen"]) - len(rest)
        d["fragen"] = rest
        schreibe(ordner / "fragen.json", d)
    else:
        faecher = [p for p in ordner.iterdir() if (p / "aufgaben.json").exists()]
        gesamt = n = 0
        neu = []
        for fo in faecher:
            a = json.loads((fo / "aufgaben.json").read_text(encoding="utf8"))
            r = json.loads((fo / "referenz.json").read_text(encoding="utf8"))
            vorher = len(a["aufgaben"])
            a["aufgaben"] = [x for x in a["aufgaben"] if str(x["id"]) not in weg]
            r["loesungen"] = [x for x in r["loesungen"] if str(x["id"]) not in weg]
            n += vorher - len(a["aufgaben"])
            gesamt += len(a["aufgaben"])
            neu.append((fo, a, r))
        if not gesamt:
            shutil.rmtree(sicher, ignore_errors=True)
            raise ValueError("Alle Aufgaben ausgewählt – dann bitte den ganzen Datensatz entfernen.")
        for fo, a, r in neu:
            schreibe(fo / "aufgaben.json", a)
            schreibe(fo / "referenz.json", r)
    return {"token": token, "geloescht": n, **_beschreibe(ordner)}


def stelle_wieder_her(ds: str, token: str) -> dict:
    if not re.fullmatch(r"[0-9a-f]{16}", token or ""):
        raise ValueError("ungültige Sicherung")
    ordner, sicher = ordner_von(ds), PAPIERKORB / slug(ds) / token
    if not sicher.is_dir():
        raise ValueError("Die Sicherung gibt es nicht mehr.")
    for q in sicher.rglob("*.json"):
        z = ordner / q.relative_to(sicher)
        z.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(q, z)
    shutil.rmtree(sicher, ignore_errors=True)
    return _beschreibe(ordner)


def loesche_datensatz(ds: str):
    if ds == "demo":
        raise ValueError("Der Demo-Datensatz bleibt.")
    shutil.rmtree(ordner_von(ds))
    shutil.rmtree(ORDNER / slug(ds), ignore_errors=True)


# ---------------------------------------------------------------- Lauf
def datei(ds: str, tag: str) -> Path:
    return ORDNER / slug(ds) / (slug(tag) + ".json")


def _vram_belegt() -> float | None:
    """Belegter Grafikspeicher laut GPU-Server bzw. nvidia-smi (genauer als Ollama), sonst None."""
    url = einstellungen.get("gpu_status_url") or GPU_URL
    if url:
        try:
            with urllib.request.urlopen(url, timeout=3) as r:
                return json.load(r)["jetzt"]["vram_gb"]
        except Exception:
            return None
    from arena import gpu_status as g
    m = g.messe() if g.verfuegbar() else None
    return m["vram_gb"] if m else None


def _vram_modell(tag: str) -> float | None:
    """Ohne GPU-Dienst: Grafikspeicher des Modells laut Ollama (/api/ps)."""
    try:
        for m in kern._get("/api/ps", timeout=3).get("models", []):
            if m.get("name") == tag or m.get("model") == tag:
                return round(m.get("size_vram", 0) / 1024**3, 2) or None
    except Exception:
        pass
    return None


def _speichere(ds: str, tag: str, d: dict):
    p = datei(ds, tag)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(d, ensure_ascii=False), encoding="utf8")
    tmp.replace(p)


def _lauf(ds: str, tag: str, modi: tuple, eintraege: list[dict]):
    p = datei(ds, tag)
    d = json.loads(p.read_text(encoding="utf8")) if p.exists() else {"modell": tag, "datensatz": ds, "laeufe": {}, "loesungen": {}}
    d["gestartet"] = d.get("gestartet") or datetime.now().isoformat(timespec="seconds")
    JOB.update(modell=tag, modus=None)
    kern.entlade_alle()
    time.sleep(2)
    grund = _vram_belegt()
    spitze = d.get("vram_gb") or 0.0
    try:
        for modus in modi:
            if modus == "bild" and not kern.kann_bilder(tag):
                d.setdefault("hinweise", {})["bild"] = "Modell kann keine Bilder lesen"
                JOB["i"] += sum(1 for e in eintraege if e.get("bild_dateien"))
                continue
            if modus == "denken" and not kern.kann_denken(tag):
                d.setdefault("hinweise", {})["denken"] = "Modell hat keinen Reasoning-Modus"
                JOB["i"] += len(eintraege)
                continue
            lauf = d["laeufe"].setdefault(modus, {})
            JOB["modus"] = modus
            for e in (x for x in eintraege if modus != "bild" or x.get("bild_dateien")):
                if _stopp.is_set():
                    raise InterruptedError
                if e["id"] in lauf:
                    continue
                t = time.time()
                try:
                    if modus == "muster":
                        r = kern.bewerte_muster(tag, e)
                    elif modus == "blind":
                        r = kern.bewerte_blind(tag, e)
                    elif modus == "denken":
                        r = kern.bewerte_blind(tag, e, think=True)
                    elif modus == "bild":
                        r = kern.bewerte_blind(tag, e, bild=True)
                    else:
                        if e["aufgabe_id"] not in d["loesungen"]:
                            d["loesungen"][e["aufgabe_id"]] = kern.loesen(tag, e)
                        eig = d["loesungen"][e["aufgabe_id"]]
                        w = pruefe(e["fach"], e["aufgabentyp"], e["schuelerantwort"], eig, code_erlaubt=bool(einstellungen.get("eigener_code")))
                        r = kern.bewerte_blind(tag, e, eigene=eig, werkzeug=w)
                except Exception as fehler:
                    r = {"gueltig": False, "roh": f"FEHLER: {fehler}", "sekunden": round(time.time() - t, 2)}
                lauf[e["id"]] = {k: r.get(k) for k in ("gueltig", "punkte", "note", "note_modell", "feedback", "tipp", "sekunden", "abbildung") if k != "abbildung" or r.get(k)}
                JOB["i"] += 1
                jetzt = _vram_belegt()
                if grund is not None and jetzt is not None:
                    spitze = max(spitze, round(jetzt - grund, 2))
                elif (v := _vram_modell(tag)) is not None:
                    spitze = max(spitze, v)
                if JOB["i"] % 5 == 0:
                    d["vram_gb"] = spitze or None
                    _speichere(ds, tag, d)
        soll = lambda m: sum(1 for e in eintraege if e.get("bild_dateien")) if m == "bild" else len(eintraege)
        if all(len(d["laeufe"].get(m, {})) >= soll(m) or (m == "bild" and d.get("hinweise", {}).get("bild")) for m in modi):
            d["fertig"] = datetime.now().isoformat(timespec="seconds")
    finally:
        d["vram_gb"] = spitze or d.get("vram_gb") or None
        _speichere(ds, tag, d)


def _lauf_fragen(ds: str, tag: str, liste: list[dict], ordner: Path, modi: tuple = ("antwort",)):
    p = datei(ds, tag)
    d = json.loads(p.read_text(encoding="utf8")) if p.exists() else {"modell": tag, "datensatz": ds, "laeufe": {}}
    d["gestartet"] = d.get("gestartet") or datetime.now().isoformat(timespec="seconds")
    JOB.update(modell=tag, modus=modi[0])
    kern.entlade_alle()
    time.sleep(2)
    vision = kern.kann_bilder(tag)
    spitze = d.get("vram_gb") or 0.0
    try:
        for modus in modi:
            if modus == "denken" and not kern.kann_denken(tag):
                d.setdefault("hinweise", {})["denken"] = "Modell hat keinen Reasoning-Modus"
                JOB["i"] += len(liste)
                continue
            lauf = d["laeufe"].setdefault(modus, {})
            JOB["modus"] = modus
            for f in liste:
                if _stopp.is_set():
                    raise InterruptedError
                if f["id"] in lauf:
                    continue
                bild = str(ordner / f["bild"]) if f.get("bild") and vision and kern._im_ordner(ordner, f["bild"]) else None
                try:
                    r = fr.beantworte(tag, f, bild, think=(modus == "denken"), werkzeuge=(modus == "rechner"))
                except Exception as fehler:
                    r = {"gueltig": False, "antwort": f"FEHLER: {fehler}", "richtig": False, "sekunden": 0}
                lauf[f["id"]] = {k: r.get(k) for k in ("gueltig", "antwort", "richtig", "wertung", "erklaerung", "geschaetzt", "sekunden", "rechnungen", "hinweis") if k in r}
                JOB["i"] += 1
                if (v := _vram_belegt()) is None:
                    v = _vram_modell(tag)
                if v:
                    spitze = max(spitze, v)
                if JOB["i"] % 5 == 0:
                    d["vram_gb"] = spitze or None
                    _speichere(ds, tag, d)
        if all(len(set(d["laeufe"].get(m, {})) & {f["id"] for f in liste}) >= len(liste) or (d.get("hinweise") or {}).get(m) for m in modi):
            d["fertig"] = datetime.now().isoformat(timespec="seconds")
    finally:
        d["vram_gb"] = spitze or d.get("vram_gb") or None
        _speichere(ds, tag, d)


def _plan(ds: str, modelle: list[str], modi: list[str] | None):
    """Modi, Einträge, schon erledigte und gesamte Anzahl für einen Datensatz."""
    if typ_von(ordner_von(ds)) == "fragen":
        modi_t, eintraege = tuple(m for m in FRAGEN_MODI if m in (modi or ())) or ("antwort",), lade_fragen(ordner_von(ds))
    else:
        modi_t = tuple(m for m in MODI if m in (modi or MODI)) or MODI
        eintraege = kern.lade_eintraege(ordner_von(ds))
    alle_ids = {e["id"] for e in eintraege}
    ids = {m: ({e["id"] for e in eintraege if e.get("bild_dateien")} if m == "bild" else alle_ids) for m in modi_t}
    erledigt = 0   # bereits gerechnete Antworten zählen mit (Fortsetzen)
    for tag in modelle:
        p = datei(ds, tag)
        if p.exists():
            try:
                lauf = json.loads(p.read_text(encoding="utf8")).get("laeufe", {})
                erledigt += sum(len(set(lauf.get(m, {})) & ids[m]) for m in modi_t)   # nur Einträge, die es noch gibt
            except Exception:
                pass
    n = len(modelle) * sum(sum(1 for e in eintraege if e.get("bild_dateien")) if m == "bild" else len(eintraege) for m in modi_t)
    return modi_t, erledigt, n


def _lauf_alle(plaene: list, modelle: list[str]):
    """Alle gewählten Datensätze nacheinander, darin alle Modelle nacheinander."""
    try:
        for k, (ds, modi_t) in enumerate(plaene):
            if _stopp.is_set():
                break
            JOB.update(datensatz=ds, ds_nr=k + 1, typ="fragen" if set(modi_t) <= set(FRAGEN_MODI) else "bewertung", modi=list(modi_t))
            if typ_von(ordner_von(ds)) == "fragen":
                liste = lade_fragen(ordner_von(ds))
                for n, tag in enumerate(modelle):
                    if _stopp.is_set():
                        break
                    JOB["nr"] = n + 1
                    _lauf_fragen(ds, tag, liste, ordner_von(ds), modi_t)
                continue
            eintraege = kern.lade_eintraege(ordner_von(ds))
            for n, tag in enumerate(modelle):
                if _stopp.is_set():
                    break
                JOB["nr"] = n + 1
                _lauf(ds, tag, modi_t, eintraege)
    except InterruptedError:
        pass
    except Exception as fehler:
        JOB["fehler"] = str(fehler)
    finally:
        JOB["laeuft"] = False
        _stopp.clear()


def starte(ds, modelle: list[str], modi: list[str] | None = None) -> dict:
    """ds: ein Datensatz oder eine Liste von Datensätzen (laufen nacheinander)."""
    liste = [ds] if isinstance(ds, str) else list(dict.fromkeys(ds))
    if not liste:
        raise ValueError("Wähle mindestens einen Datensatz.")
    plaene, erledigt, n = [], 0, 0
    for x in liste:
        modi_t, e, m = _plan(x, modelle, modi)
        plaene.append((x, modi_t)); erledigt += e; n += m
    with _sperre:
        if JOB.get("laeuft"):
            raise RuntimeError(f"Es läuft schon ein Durchlauf ({JOB.get('modell')}).")
        _stopp.clear()
        JOB.clear()
        JOB.update(laeuft=True, datensatz=liste[0], datensaetze=liste, ds_nr=1, modelle=list(modelle), modi=list(plaene[0][1]), nr=1, modell=modelle[0], modus=None,
                   i=erledigt, n=n, typ="fragen" if set(plaene[0][1]) <= set(FRAGEN_MODI) else "bewertung",
                   start=time.time(), start_i=erledigt, fehler=None)
        threading.Thread(target=_lauf_alle, args=(plaene, list(modelle)), daemon=True).start()
    return status()


def stoppe():
    _stopp.set()


# ---------------------------------------------------------------- Auswertung
def auswerten(d: dict, eintraege: list[dict]) -> dict:
    """Gleiche Kennzahlen wie im Dashboard: pro Modus Trefferquote usw. plus Fallen."""
    ids = {e["id"] for e in eintraege}
    fallen_ids = [e["id"] for e in eintraege if e.get("falle")]
    aus = {}
    for modus in MODI:
        lauf = d.get("laeufe", {}).get(modus) or {}
        zeilen = [{**r, "id": e["id"], "lehrkraft_note": e["lehrkraft"]["note"], "lehrkraft_punkte": e["lehrkraft"]["punkte"]}
                  for e in eintraege if (r := lauf.get(e["id"]))]
        if not zeilen:
            continue
        tricks = {}
        for tid in fallen_ids:
            z = next((z for z in zeilen if z["id"] == tid), None)
            if z:
                tricks[tid] = {"gueltig": z.get("gueltig"), "note": z.get("note"),
                               "abgewehrt": not (z.get("gueltig") and z["note"] <= kern.TRICK_SCHWELLE)}
        aus[modus] = {"gesamt": kern.kennzahlen(zeilen), "tricks": tricks}
    return aus


def status() -> dict:
    j = {k: JOB.get(k) for k in ("laeuft", "datensatz", "datensaetze", "ds_nr", "modelle", "modi", "nr", "modell", "modus", "i", "n", "fehler")}
    if JOB.get("laeuft") and JOB.get("i", 0) > JOB.get("start_i", 0):
        pro = (time.time() - JOB["start"]) / (JOB["i"] - JOB["start_i"])
        j["rest_s"] = round(pro * (JOB["n"] - JOB["i"]))
    return j


def auswerten_fragen(d: dict, liste: list[dict]) -> dict:
    aus = {}
    for modus in FRAGEN_MODI:
        lauf = (d.get("laeufe") or {}).get(modus) or {}
        z = [lauf[f["id"]] for f in liste if f["id"] in lauf]
        if not z:
            continue
        n = len(z)
        je_thema = {}
        for f in liste:
            if f["id"] in lauf:
                je_thema.setdefault(f.get("thema") or "allgemein", []).append(bool(lauf[f["id"]].get("richtig")))
        aus[modus] = {"gesamt": {"anzahl": n, "treffer": round(sum(bool(x.get("richtig")) for x in z) / n, 3), "fast": None,
                                 "gueltig": round(sum(bool(x.get("gueltig")) for x in z) / n, 3), "sekunden": round(sum(x.get("sekunden") or 0 for x in z) / n, 2),
                                 "geschaetzt": sum(1 for x in z if x.get("geschaetzt")),
                                 "rechnungen": round(sum(len(x.get("rechnungen") or []) for x in z) / n, 2) if modus == "rechner" else None,
                                 "ohne_werkzeug": sum(1 for x in z if x.get("hinweis")) if modus == "rechner" else None},
                      "themen": {t: round(sum(v) / len(v), 3) for t, v in je_thema.items()}, "tricks": {}}
    return aus

def ergebnisse(ds: str) -> list[dict]:
    o = ORDNER / slug(ds)
    if not o.exists():
        return []
    if typ_von(ordner_von(ds)) == "fragen":
        liste, aus = lade_fragen(ordner_von(ds)), []
        for p in sorted(o.glob("*.json")):
            try:
                d = json.loads(p.read_text(encoding="utf8"))
            except Exception:
                continue
            aus.append({"modell": d["modell"], "vram_gb": d.get("vram_gb"), "fertig": d.get("fertig"),
                        "erledigt": max((len({f["id"] for f in liste} & set((d.get("laeufe") or {}).get(m) or {})) for m in FRAGEN_MODI), default=0), "antworten": len(liste), "modi": auswerten_fragen(d, liste)})
        return aus
    eintraege = kern.lade_eintraege(ordner_von(ds))
    aus = []
    for p in sorted(o.glob("*.json")):
        try:
            d = json.loads(p.read_text(encoding="utf8"))
        except Exception:
            continue
        aus.append({"modell": d["modell"], "vram_gb": d.get("vram_gb"), "fertig": d.get("fertig"),
                    "erledigt": sum(len(x) for x in d.get("laeufe", {}).values()), "antworten": len(eintraege),
                    "modi": auswerten(d, eintraege)})
    return aus


def loesche_ergebnis(ds: str, tag: str):
    datei(ds, tag).unlink(missing_ok=True)


# ---------------------------------------------------------------- Daten je Datensatz (für Chat und Daten-Tab)
_cache: dict = {}


def lade(ds: str) -> dict:
    """Einträge eines Datensatzes, zwischengespeichert (neu geladen, wenn sich die Dateien ändern)."""
    o = ordner_von(ds)
    if typ_von(o) == "fragen":
        f = lade_fragen(o)
        return {"typ": "fragen", "fragen": {x["id"]: x for x in f}, "liste": f, "ordner": o}
    stand = max((p.stat().st_mtime for p in o.rglob("*.json")), default=0)
    if ds not in _cache or _cache[ds][0] != stand:
        e = kern.lade_eintraege(o)
        aufgaben = {}
        for x in e:
            aufgaben.setdefault(x["aufgabe_id"], x)
        _cache[ds] = (stand, {"eintraege": e, "antworten": {x["id"]: x for x in e}, "aufgaben": aufgaben})
    return _cache[ds][1]


def aufgaben_liste(ds: str) -> list[dict]:
    """Aufgaben mit Schülerantworten und Referenzbewertung – für den Daten-Tab und die Auswahl im Chat."""
    d = lade(ds)
    if d.get("typ") == "fragen":
        return [{**f, "typ": "frage", "abbildungen": [{"pfad": f["bild"], "beschreibung": ""}] if f.get("bild") else []} for f in d["liste"]]
    aus = {}
    for e in lade(ds)["eintraege"]:
        a = aus.setdefault(e["aufgabe_id"], {k: e[k] for k in ("fach", "thema", "klasse", "aufgabentyp", "hinweis", "max_punkte", "musterloesung", "bewertungsraster")}
                           | {"id": e["aufgabe_id"], "aufgabe": e["aufgabe_text"], "abbildungen": e["abbildungen"], "antworten": []})
        a["antworten"].append({"id": e["id"], "antwort": e["schuelerantwort"], "falle": e.get("falle"), "referenz": e["lehrkraft"]})
    return list(aus.values())




def alles_loeschen():
    """Gesamt-Reset: eigene Datensätze, Ergebnisse und Papierkorb löschen. Modelle in Ollama bleiben."""
    if JOB.get("laeuft"):
        raise RuntimeError("Gerade läuft ein Vergleich – erst anhalten, dann zurücksetzen.")
    for ordner in (DATENSAETZE, ORDNER, PAPIERKORB):
        shutil.rmtree(ordner, ignore_errors=True)
    _cache.clear()
    _beschreibung_cache.clear()

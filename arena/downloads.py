"""Modell-Downloads im Hintergrund.

Der Server holt Modelle über Ollama (/api/pull) in eigenen Threads – unabhängig vom Browser. Fenster schließen,
Tab wechseln oder neu laden bricht nichts ab. Höchstens zwei Downloads laufen gleichzeitig, weitere warten.
Die Oberfläche fragt den Stand über liste() ab (Kopfleiste, Tab „Modelle“, Assistent).
"""
from __future__ import annotations

import json
import threading
import time
import urllib.request

from arena import kern

PARALLEL = 2
_platz = threading.Semaphore(PARALLEL)
_sperre = threading.Lock()
_alle: dict[str, dict] = {}          # Modellname → Stand
_nach_fertig = []                    # Rückrufe (z. B. Status-Cache leeren)


def _neu(name: str, titel: str) -> dict:
    return {"modell": name, "titel": titel or name, "status": "wartet", "completed": 0, "total": 0, "teile": {},
            "fehler": None, "gestartet": time.time(), "ende": None, "abbrechen": False, "tempo": 0.0, "_t": None, "_c": 0, "voll": 0}


def starte(name: str, titel: str = "") -> dict:
    """Download anstoßen (oder den laufenden zurückgeben)."""
    with _sperre:
        alt = _alle.get(name)
        if alt and alt["status"] in ("wartet", "laeuft"):
            return oeffentlich(alt)
        d = _alle[name] = _neu(name, titel)
    threading.Thread(target=_lauf, args=(d,), daemon=True).start()
    return oeffentlich(d)


def abbrechen(name: str) -> bool:
    with _sperre:
        d = _alle.get(name)
        if not d or d["status"] not in ("wartet", "laeuft"):
            return False
        d["abbrechen"] = True
        if d["status"] == "wartet":
            d.update(status="abgebrochen", ende=time.time())
    return True


def entfernen(name: str):
    """Fertige/fehlgeschlagene Einträge aus der Liste nehmen."""
    with _sperre:
        d = _alle.get(name)
        if d and d["status"] not in ("wartet", "laeuft"):
            _alle.pop(name, None)


def _gesamtgroesse(name: str) -> int:
    """Gesamtgröße aller Teile vorab aus dem Manifest der Quelle (Ollama-Registry bzw. Hugging Face).

    Ollama meldet die Teile eines Modells nacheinander – ohne diesen Wert würde die Summe unterwegs wachsen
    und der Balken zurückspringen. Schlägt die Abfrage fehl, bleibt es bei der laufenden Summe."""
    try:
        basis, _, tag = name.partition(":")
        for vorsilbe in ("hf.co/", "huggingface.co/"):
            if basis.startswith(vorsilbe):
                url = f"https://hf.co/v2/{basis[len(vorsilbe):]}/manifests/{tag or 'latest'}"
                break
        else:
            if basis.count("/") > 1:   # andere Registry – nicht raten
                return 0
            url = f"https://registry.ollama.ai/v2/{basis if '/' in basis else 'library/' + basis}/manifests/{tag or 'latest'}"
        req = urllib.request.Request(url, headers={"Accept": "application/vnd.docker.distribution.manifest.v2+json"})
        with urllib.request.urlopen(req, timeout=8) as r:
            j = json.loads(r.read(1_000_000))
        return sum(int(t.get("size") or 0) for t in j.get("layers") or []) + int((j.get("config") or {}).get("size") or 0)
    except Exception:
        return 0


def _lauf(d: dict):
    with _platz:
        if d["abbrechen"]:
            return
        d["status"] = "laeuft"
        d["voll"] = _gesamtgroesse(d["modell"])
        req = urllib.request.Request(kern.ollama_url() + "/api/pull", data=json.dumps({"model": d["modell"], "stream": True}).encode(),
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=3600) as r:
                for zeile in r:
                    if d["abbrechen"]:
                        d.update(status="abgebrochen", ende=time.time())
                        return   # Verbindung schließen → Ollama bricht ab; geladene Teile bleiben für einen neuen Versuch
                    try:
                        j = json.loads(zeile)
                    except ValueError:
                        continue
                    if j.get("error"):
                        raise RuntimeError(j["error"])
                    if j.get("digest") and j.get("total"):
                        d["teile"][j["digest"]] = (j.get("completed") or 0, j["total"])
                        d["completed"] = sum(c for c, _ in d["teile"].values())
                        d["total"] = max(d.get("voll") or 0, sum(t for _, t in d["teile"].values()))
                        jetzt = time.time()   # Tempo geglättet, damit Restzeit und Anzeige nicht springen
                        if d["_t"] is None:
                            d["_t"], d["_c"] = jetzt, d["completed"]
                        elif jetzt - d["_t"] >= 1:
                            neu = max(0.0, (d["completed"] - d["_c"]) / (jetzt - d["_t"]))
                            d["tempo"] = neu if not d["tempo"] else 0.8 * d["tempo"] + 0.2 * neu
                            d["_t"], d["_c"] = jetzt, d["completed"]
                    d["schritt"] = j.get("status")
            d.update(status="fertig", ende=time.time(), completed=d["total"] or d["completed"])
            for f in _nach_fertig:
                f()
        except Exception as fehler:
            text = getattr(fehler, "read", None)
            text = text().decode(errors="replace")[:200] if text else str(fehler)[:200]
            d.update(status="abgebrochen" if d["abbrechen"] else "fehler", fehler=None if d["abbrechen"] else text, ende=time.time())


def oeffentlich(d: dict) -> dict:
    rest = (d["total"] - d["completed"]) / d["tempo"] if d["tempo"] > 0 and d["total"] else None
    return {k: d[k] for k in ("modell", "titel", "status", "completed", "total", "fehler", "gestartet", "ende")} | \
           {"anteil": round(d["completed"] / d["total"], 4) if d["total"] else 0, "tempo": round(d["tempo"]), "rest_s": round(rest) if rest else None,
            "schritt": d.get("schritt")}


def liste() -> list[dict]:
    """Laufende, wartende und in den letzten 10 Minuten beendete Downloads (neueste zuerst)."""
    jetzt = time.time()
    with _sperre:
        for k in [k for k, d in _alle.items() if d["ende"] and jetzt - d["ende"] > 600]:
            _alle.pop(k)
        return sorted((oeffentlich(d) for d in _alle.values()), key=lambda x: -x["gestartet"])

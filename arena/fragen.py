"""Allgemeiner Fragen-Benchmark: beliebige Fragen mit Soll-Antwort, aus CSV, JSONL oder JSON.

Die Modelle beantworten jede Frage selbst. Gewertet wird je Frage automatisch:
  auswahl  – Multiple Choice (Buchstabe oder Text der Option)
  zahl     – Zahl mit kleiner Toleranz (1 % bzw. 0,01)
  enthaelt – kurze Soll-Antwort kommt in der Antwort vor
  ki       – freie Antwort: Prüfer-KI (falls API-Schlüssel), sonst Wortüberlappung als Schätzung
Nur Standardbibliothek.
"""
from __future__ import annotations

import csv
import io
import json
import re
import time
import unicodedata

from arena import kern

# erkannte Spaltennamen (klein, ohne Leerzeichen/Unterstriche)
FRAGE = ("frage", "question", "prompt", "input", "instruction", "q", "query", "aufgabe", "text")
ANTWORT = ("antwort", "answer", "output", "target", "expected", "erwartet", "solution", "loesung", "lösung", "label", "groundtruth", "soll", "richtig", "correct")
OPTIONEN = ("optionen", "options", "choices", "auswahl", "antwortmoeglichkeiten", "answers")
THEMA = ("thema", "topic", "category", "kategorie", "subject", "fach", "bereich", "domain")
KONTEXT = ("kontext", "context", "passage", "hintergrund", "text_kontext")
WERTUNG = ("wertung", "bewertung", "scoring", "metric", "typ", "type")
BILD = ("bild", "image", "bilddatei", "image_path", "datei")
ID = ("id", "nr", "nummer", "key", "uid")
BUCHST = "ABCDEFGH"


def _schluessel(k: str) -> str:
    return re.sub(r"[\s_\-]+", "", str(k).strip().lower())


def _feld(z: dict, namen) -> str | None:
    for k, v in z.items():
        if _schluessel(k) in namen and v not in (None, ""):
            return v
    return None


# ---------------------------------------------------------------- Einlesen
def lese(name: str, text: str) -> list[dict]:
    """Datei (CSV/TSV, JSONL, JSON) → Liste roher Zeilen (dicts)."""
    t = text.lstrip("﻿").strip()
    if not t:
        return []
    if name.lower().endswith(".jsonl") or (t.startswith("{") and "\n{" in t and not t.rstrip().endswith("]")):
        try:
            return [json.loads(z) for z in t.splitlines() if z.strip()]
        except json.JSONDecodeError:
            pass
    if t[0] in "[{":
        d = json.loads(t)
        if isinstance(d, dict):
            for k in ("fragen", "questions", "data", "items", "rows", "eintraege", "examples"):
                if isinstance(d.get(k), list):
                    return d[k]
            return [d]
        return d
    # CSV / TSV: Trennzeichen erraten
    try:
        dialekt = csv.Sniffer().sniff(t[:4000], delimiters=",;\t|")
    except csv.Error:
        dialekt = csv.excel
    return list(csv.DictReader(io.StringIO(t), dialect=dialekt))


def normalisiere(zeilen: list[dict], quelle: str = "") -> list[dict]:
    """Rohe Zeilen → einheitliche Fragen, Spalten automatisch erkannt."""
    spalten = []
    for z in zeilen[:200]:
        if isinstance(z, dict):
            spalten += [k for k in z if k not in spalten]
    zuordnung = zuordnung_raten(spalten, zeilen[:50])
    if not zuordnung["frage"] or not zuordnung["antwort"]:
        return []
    return normalisiere_mit([z for z in zeilen if isinstance(z, dict)], zuordnung)


def wertung_raten(f: dict) -> str:
    if f.get("optionen"):
        return "auswahl"
    if _zahl(f["antwort"]) is not None and len(f["antwort"]) <= 20:
        return "zahl"
    if len(f["antwort"]) <= 40 and len(f["antwort"].split()) <= 5:
        return "enthaelt"
    return "ki"


# ---------------------------------------------------------------- Wertung
def _norm(t: str) -> str:
    t = unicodedata.normalize("NFKC", str(t)).lower().strip()
    t = re.sub(r"[\"'„“”‚‘’`´.,;:!?()\[\]{}]", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def _zahl(t: str):
    m = re.search(r"-?\d[\d.\s]*(?:,\d+)?|-?\d+(?:\.\d+)?", str(t))
    if not m:
        return None
    s = m.group(0).replace(" ", "")
    if "," in s:
        s = s.replace(".", "").replace(",", ".")
    elif s.count(".") > 1:
        s = s.replace(".", "")
    try:
        return float(s)
    except ValueError:
        return None


def _f1(a: str, b: str) -> float:
    x, y = _norm(a).split(), _norm(b).split()
    if not x or not y:
        return 0.0
    gemeinsam = sum(min(x.count(w), y.count(w)) for w in set(x))
    if not gemeinsam:
        return 0.0
    p, r = gemeinsam / len(y), gemeinsam / len(x)
    return 2 * p * r / (p + r)


def werte(f: dict, ist: str) -> dict:
    """→ {richtig: bool|None, wertung, erklaerung, geschaetzt?}"""
    w, soll = f["wertung"], f["antwort"]
    if w == "auswahl":
        opt = f.get("optionen") or []
        m = re.match(r"^\s*\(?([A-Ha-h])\)?(?:[).:\s]|$)", ist or "")
        gewaehlt = m.group(1).upper() if m else next((BUCHST[i] for i, o in enumerate(opt) if _norm(o) and _norm(o) in _norm(ist)), None)
        richtig_b = soll.strip().upper()[:1] if re.fullmatch(r"\(?[A-Ha-h]\)?", soll.strip()) else next((BUCHST[i] for i, o in enumerate(opt) if _norm(o) == _norm(soll)), None)
        return {"richtig": gewaehlt is not None and gewaehlt == richtig_b, "wertung": w, "erklaerung": f"gewählt {gewaehlt or '–'}, richtig {richtig_b or soll}"}
    if w == "zahl":
        a, b = _zahl(ist), _zahl(soll)
        ok = a is not None and b is not None and abs(a - b) <= max(abs(b) * 0.01, 0.01)
        return {"richtig": ok, "wertung": w, "erklaerung": f"{a if a is not None else '–'} vs. {b}"}
    if w == "exakt":
        return {"richtig": _norm(ist) == _norm(soll), "wertung": w, "erklaerung": "exakter Vergleich"}
    if w == "enthaelt":
        return {"richtig": bool(_norm(soll)) and _norm(soll) in _norm(ist), "wertung": w, "erklaerung": f"„{soll}“ in der Antwort?"}
    # ki: Prüfer, sonst Schätzung über Wortüberlappung
    from arena import pruefer
    if pruefer.schluessel():
        try:
            u = pruefer.urteil_frage(f, ist)
            return {"richtig": bool(u.get("richtig")), "wertung": w, "erklaerung": u.get("begruendung") or "Prüfer-KI"}
        except Exception as fehler:
            erkl = f"Prüfer nicht erreichbar ({str(fehler)[:60]}), geschätzt"
    else:
        erkl = "ohne Prüfer geschätzt (Wortüberlappung)"
    s = _f1(soll, ist)
    return {"richtig": s >= 0.5, "wertung": w, "erklaerung": f"{erkl}: {round(s * 100)} %", "geschaetzt": True}


# ---------------------------------------------------------------- Fragen stellen
SYSTEM = "Du beantwortest Fragen korrekt und knapp. Antworte ausschließlich mit JSON."
SCHEMA = {"type": "object", "properties": {"antwort": {"type": "string"}}, "required": ["antwort"]}


def prompt(f: dict, bild: bool = False) -> list[dict]:
    teile = []
    if f.get("kontext"):
        teile.append("KONTEXT:\n" + f["kontext"])
    teile.append("FRAGE:\n" + f["frage"])
    if f.get("optionen"):
        teile.append("ANTWORTMÖGLICHKEITEN:\n" + "\n".join(f"{BUCHST[i]}) {o}" for i, o in enumerate(f["optionen"])))
    regel = {"auswahl": "Gib in \"antwort\" nur den Buchstaben der richtigen Möglichkeit an.",
             "zahl": "Gib in \"antwort\" nur das Ergebnis als Zahl an (mit Einheit, falls nötig).",
             "enthaelt": "Gib in \"antwort\" nur die kurze Antwort an, ohne Erklärung.",
             "exakt": "Gib in \"antwort\" nur die Antwort an, ohne Erklärung."}.get(f["wertung"], "Antworte in \"antwort\" in wenigen Sätzen.")
    if bild:
        teile.append("Zur Frage gehört das angehängte Bild.")
    teile.append(regel + ' Format: {"antwort": "…"}')
    return [{"role": "system", "content": SYSTEM}, {"role": "user", "content": "\n\n".join(teile)}]


SYMPY = {"type": "function", "function": {
    "name": "sympy",
    "description": "Computer-Algebra (SymPy): löst Gleichungen und Gleichungssysteme, vereinfacht, faktorisiert, leitet ab, integriert, rechnet exakt. "
                   "Beispiele: aufgabe=loese, ausdruck='2x+3=11'; aufgabe=loese, ausdruck='x+y=10; x-y=2'; aufgabe=ableiten, ausdruck='x^3+2x'.",
    "parameters": {"type": "object", "properties": {
        "aufgabe": {"type": "string", "enum": ["berechne", "vereinfache", "loese", "ableiten", "integriere", "faktorisiere"]},
        "ausdruck": {"type": "string", "description": "Ausdruck oder Gleichung(en), Variablen als einzelne Buchstaben"},
        "variable": {"type": "string", "description": "optional: Variable, nach der gelöst/abgeleitet wird"}}, "required": ["aufgabe", "ausdruck"]}}}

RECHNER = [{"type": "function", "function": {
    "name": "rechne",
    "description": "Berechnet einen mathematischen Ausdruck exakt, z. B. (16-3-4)*2 oder 2.5*120 oder sqrt(81). "
                   "Erlaubt: Zahlen, + - * / ^ ( ) und sqrt. Nutze das Werkzeug für jede Rechnung, statt im Kopf zu rechnen.",
    "parameters": {"type": "object", "properties": {"ausdruck": {"type": "string", "description": "der Rechenausdruck"}}, "required": ["ausdruck"]}}}]


def _rechne(ausdruck: str) -> str:
    """Taschenrechner fürs Modell – derselbe abgesicherte Auswerter wie der Rechen-Check (kein Code, keine Namen)."""
    from arena import werkzeuge as w
    wert = w._wert(w._normalisiere(str(ausdruck)[:300]), None)
    if wert is None:
        return "Fehler: Ausdruck nicht berechenbar (erlaubt: Zahlen, + - * / ^ ( ) und sqrt)."
    return str(int(wert)) if wert == int(wert) and abs(wert) < 1e15 else repr(round(wert, 10))


def aktive_werkzeuge() -> list[dict]:
    """Werkzeuge der eingeschalteten Add-ons (Zahnrad bzw. Fenster „Werkzeuge“)."""
    from arena import einstellungen, werkzeuge as w
    tools = []
    if einstellungen.get("werkzeug_rechner"):
        tools += RECHNER
    if einstellungen.get("werkzeug_sympy") and w.sympy_verfuegbar():
        tools.append(SYMPY)
    return tools


def _fuehre_aus(name: str, argumente: dict) -> str:
    from arena import werkzeuge as w
    if name == "rechne":
        return _rechne(str(argumente.get("ausdruck") or next(iter(argumente.values()), "")))
    if name == "sympy":
        return w.cas(argumente.get("aufgabe"), argumente.get("ausdruck"), argumente.get("variable"))
    return "Fehler: unbekanntes Werkzeug"


def _mit_rechner(modell: str, msgs: list[dict], think: bool) -> tuple[list[dict], list[dict], float]:
    """Werkzeug-Schleife: das Modell ruft die Werkzeuge der aktiven Add-ons auf, bis es fertig ist (höchstens 8 Runden)."""
    tools = aktive_werkzeuge()
    if not tools:
        raise RuntimeError("kein Werkzeug-Add-on eingeschaltet")
    verlauf, rechnungen, t = [dict(m) for m in msgs], [], time.time()
    namen = " und ".join(f"„{x['function']['name']}“" for x in tools)
    verlauf[-1]["content"] += f"\n\nDir stehen die Werkzeuge {namen} zur Verfügung. Nutze sie für Rechnungen, bevor du antwortest."
    for _ in range(8):
        body = {"model": modell, "messages": verlauf, "tools": tools, "stream": False, "keep_alive": "30m",
                "options": {**kern.OPTIONEN, "num_predict": 1024}}
        r = kern._post("/api/chat", body)
        m = r.get("message") or {}
        aufrufe = m.get("tool_calls") or []
        verlauf.append({"role": "assistant", "content": m.get("content", ""), **({"tool_calls": aufrufe} if aufrufe else {})})
        if not aufrufe:
            break
        for a in aufrufe[:4]:
            fn = a.get("function") or {}
            argumente = fn.get("arguments") or {}
            if isinstance(argumente, str):
                try:
                    argumente = json.loads(argumente)
                except Exception:
                    argumente = {"ausdruck": argumente}
            if not isinstance(argumente, dict):
                argumente = {"ausdruck": str(argumente)}
            ergebnis = _fuehre_aus(fn.get("name"), argumente)
            rechnungen.append({"werkzeug": fn.get("name"), "ausdruck": str(argumente.get("ausdruck", ""))[:120], "ergebnis": ergebnis[:200]})
            verlauf.append({"role": "tool", "content": ergebnis, "tool_name": fn.get("name")})
    verlauf.append({"role": "user", "content": 'Gib jetzt nur die endgültige Antwort im Format {"antwort": "…"} an.'})
    return verlauf, rechnungen, time.time() - t


def beantworte(modell: str, f: dict, bild_datei: str | None = None, think: bool = False, werkzeuge: bool = False) -> dict:
    msgs = prompt(f, bool(bild_datei))
    if bild_datei:
        import base64
        from pathlib import Path
        msgs[-1]["images"] = [base64.b64encode(Path(bild_datei).read_bytes()).decode()]
    t = time.time()
    rechnungen, extra_s, hinweis = None, 0.0, None
    if werkzeuge:
        try:
            msgs, rechnungen, extra_s = _mit_rechner(modell, msgs, think)
        except Exception as fehler:   # Modell ohne Werkzeug-Unterstützung → normal antworten
            text = fehler.read().decode(errors="replace") if hasattr(fehler, "read") else str(fehler)
            hinweis = ("Modell unterstützt keine Werkzeuge" if "support" in text.lower() and "tool" in text.lower()
                       else "kein Werkzeug-Add-on eingeschaltet" if "Add-on" in text else f"Werkzeug-Fehler: {text[:120]}")
    r = kern.chat(modell, msgs, schema=SCHEMA, think=think)
    try:
        ist = str(json.loads(r["text"]).get("antwort", "")).strip()
        gueltig = True
    except Exception:
        ist, gueltig = (r["text"] or "").strip(), False
    w = werte(f, ist)
    return {"gueltig": gueltig, "antwort": ist, **w, "sekunden": round(r["sekunden"] - r["ladezeit"] + extra_s, 2), "tokens_aus": r.get("tokens_aus"),
            "dauer_gesamt": round(time.time() - t, 2), **({"rechnungen": rechnungen} if rechnungen is not None else {}), **({"hinweis": hinweis} if hinweis else {})}


# ---------------------------------------------------------------- Spaltenzuordnung (für alle Adapter)
def zuordnung_raten(spalten: list[str], beispiele: list[dict]) -> dict:
    """Schlägt vor, welche Spalte Frage, Soll-Antwort, Optionen, Thema, Kontext, ID und Bild ist."""
    def finde(namen):
        return next((s for s in spalten if _schluessel(s) in namen), None)
    z = {"frage": finde(FRAGE), "antwort": finde(ANTWORT) or finde(("answerkey", "correctanswer", "rightanswer", "finalanswer", "goldanswer", "bestanswer", "target")),
         "optionen": finde(OPTIONEN), "thema": finde(THEMA), "kontext": finde(KONTEXT), "id": finde(ID), "bild": finde(BILD)}
    if not z["optionen"]:
        buchst = [s for s in spalten if _schluessel(s) in ("a", "b", "c", "d", "e")]
        if len(buchst) >= 2 and z["antwort"] not in buchst:
            z["optionen"] = buchst
    if z["frage"] and z["antwort"] == z["frage"]:
        z["antwort"] = None
    return z


def _optionen(wert):
    if wert in (None, ""):
        return None
    if isinstance(wert, dict):   # z. B. ARC: {"text": [...], "label": [...]}
        wert = wert.get("text") or list(wert.values())
    if isinstance(wert, str):
        t = wert.strip()
        if t.startswith("["):
            try:
                wert = json.loads(t)
            except json.JSONDecodeError:
                pass
        if isinstance(wert, str):
            wert = [o.strip() for o in re.split(r"\s*[|;\n]\s*", wert) if o.strip()]
    return [str(o) for o in wert] if isinstance(wert, list) and len(wert) >= 2 else None


def normalisiere_mit(zeilen: list[dict], zuordnung: dict) -> list[dict]:
    """Rohe Zeilen + Spaltenzuordnung → einheitliche Fragen (wie normalisiere(), aber mit festen Spalten)."""
    zf, za = zuordnung.get("frage"), zuordnung.get("antwort")
    if not zf or not za:
        raise ValueError("Bitte Spalten für Frage und Soll-Antwort wählen.")
    aus, ids = [], set()
    for i, z in enumerate(zeilen, 1):
        frage, soll = z.get(zf), z.get(za)
        if frage in (None, "") or soll in (None, ""):
            continue
        zo = zuordnung.get("optionen")
        optionen = [str(z.get(s)) for s in zo if z.get(s) not in (None, "")] if isinstance(zo, list) else _optionen(z.get(zo)) if zo else None
        if optionen is not None and len(optionen) < 2:
            optionen = None
        if isinstance(soll, (list, dict)):
            soll = soll.get("text", [soll])[0] if isinstance(soll, dict) and soll.get("text") else json.dumps(soll, ensure_ascii=False)
        soll = str(soll).strip()
        if optionen and not re.fullmatch(r"\(?[A-Ha-h]\)?", soll):
            treffer = next((i for i, o in enumerate(optionen) if _norm(o) == _norm(soll)), None)
            if treffer is not None:
                soll = BUCHST[treffer]                   # Antwort als Text einer Option → Buchstabe
            elif re.fullmatch(r"\d+", soll) and int(soll) < len(optionen):
                soll = BUCHST[int(soll)]                 # Antwort als Index (z. B. MMLU) → Buchstabe
        if "####" in soll:                               # GSM8K: Rechenweg … #### Endergebnis
            soll = soll.rsplit("####", 1)[1].strip()
        fid = str(z.get(zuordnung.get("id")) or f"f-{i:04d}") if zuordnung.get("id") else f"f-{i:04d}"
        while fid in ids:
            fid += "b"
        ids.add(fid)
        f = {"id": fid, "frage": str(frage).strip(), "antwort": soll}
        for feld in ("thema", "kontext", "bild"):
            if zuordnung.get(feld) and z.get(zuordnung[feld]) not in (None, ""):
                v = z[zuordnung[feld]]
                f[feld] = (v.get("src") if isinstance(v, dict) else str(v)).strip() if v else None
        if optionen:
            f["optionen"] = optionen
        f["wertung"] = zuordnung.get("wertung") if zuordnung.get("wertung") in ("exakt", "enthaelt", "zahl", "auswahl", "ki") else wertung_raten(f)
        aus.append({k: v for k, v in f.items() if v})
    return aus

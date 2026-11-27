"""Prüfer: ein starkes Cloud-Modell (OpenAI-kompatible API, z. B. OpenRouter) beurteilt die Arbeit der lokalen Modelle.

Bekommt: Aufgabe, Musterlösung, Raster, Referenzbewertung und die Ausgaben der lokalen Modelle (anonymisiert A–D).
Bewertet NUR die Qualität jeder Ausgabe: fachlich korrekt? verrät die Lösung? hilfreich? Note vertretbar?
Adresse, Modell und Schlüssel: Einrichtungsassistent oder Umgebung (PRUEFER_URL / PRUEFER_MODELL / PRUEFER_API_KEY).
Der Schlüssel geht nie an den Browser.
Nur Standardbibliothek.
"""
from __future__ import annotations

import json
import re
import time
import urllib.request

from arena import einstellungen

BUCHSTABEN = "ABCD"


def schluessel() -> str | None:
    return einstellungen.pruefer_key() or None


def modell() -> str:
    return einstellungen.get("pruefer_modell")


def _anfrage(body: dict, k: str) -> dict:
    req = urllib.request.Request(einstellungen.get("pruefer_url"), data=json.dumps(body).encode(), headers={
        "Authorization": f"Bearer {k}", "Content-Type": "application/json", "X-Title": "Modellvergleich"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.load(r)


def _tokens(antwort: dict) -> dict:
    u = antwort.get("usage") or {}
    return {k: u.get(k) for k in ("prompt_tokens", "completion_tokens", "total_tokens") if k in u}


SYSTEM = ("Du bist ein erfahrener Gutachter. Mehrere KI-Modelle haben dieselbe Schülerantwort bewertet. "
          "Du prüfst jede Bewertung streng und fair anhand von Musterlösung, Raster und der Referenzbewertung. "
          "Bewerte NICHT die Schülerantwort selbst, sondern die Arbeit der KI-Modelle. Antworte nur mit JSON.")


def prompt(e: dict, ausgaben: dict[str, dict]) -> tuple[list, dict]:
    """ausgaben: {modell_tag: {punkte, note, feedback, tipp}} → Nachrichten + Zuordnung Buchstabe→Modell."""
    zuordnung = {BUCHSTABEN[i]: tag for i, tag in enumerate(ausgaben)}
    teile = []
    for b, tag in zuordnung.items():
        a = ausgaben[tag]
        teile.append(f"MODELL {b}: {a.get('punkte')} von {e['max_punkte']} Punkten (Note {a.get('note')})\n"
                     f"Feedback: {a.get('feedback')}\nTipp: {a.get('tipp') or '–'}")
    lk = e["lehrkraft"]
    text = (f"AUFGABE ({e['fach']}):\n{e['aufgabe']}\n\nMUSTERLÖSUNG:\n{e['musterloesung']}\n\nRASTER (max. {e['max_punkte']} P.):\n{e['bewertungsraster']}\n\n"
            f"SCHÜLERANTWORT:\n<<<\n{e['schuelerantwort']}\n>>>\n\n"
            f"REFERENZBEWERTUNG: {lk['punkte']} von {e['max_punkte']} Punkten (Note {lk['note']}). {lk['feedback']}\n\n"
            + "\n\n".join(teile) +
            "\n\nPrüfe jedes Modell. Gib genau dieses JSON zurück:\n"
            '{"bewertungen": {"A": {"punkte_vertretbar": true|false, "fachlich_korrekt": true|false, "verraet_loesung": true|false, '
            '"hilfreich": 1-5, "kommentar": "<1 kurzer Satz>"}, ...}, "bester": "<Buchstabe>", "fazit": "<1 Satz>"}')
    return [{"role": "system", "content": SYSTEM}, {"role": "user", "content": text}], zuordnung


def pruefe(e: dict, ausgaben: dict[str, dict]) -> dict:
    k = schluessel()
    if not k:
        raise RuntimeError("Kein API-Schlüssel für den Prüfer hinterlegt (Einrichtung → Prüfer)")
    msgs, zuordnung = prompt(e, ausgaben)
    t = time.time()
    for versuch in range(2):
        if versuch:  # zweiter Versuch: Anführungszeichen in Texten verbieten (häufigster JSON-Fehler)
            msgs = msgs + [{"role": "user", "content": "Dein JSON war ungültig. Antworte erneut nur mit gültigem JSON und verwende innerhalb der Texte keine doppelten Anführungszeichen."}]
        body = {"model": modell(), "messages": msgs, "temperature": 0, "max_tokens": 6000}
        if versuch:
            body["reasoning"] = {"enabled": False}   # zweiter Versuch ohne Nachdenken (falls das Budget verbraucht wurde)
        antwort = _anfrage(body, k)
        text = antwort["choices"][0]["message"].get("content") or ""
        m = re.search(r"\{.*\}", text, re.S)
        try:
            j = json.loads(m.group(0) if m else text)
            break
        except (json.JSONDecodeError, TypeError):
            if versuch:
                raise
    ergebnis = {"modell": modell(), "sekunden": round(time.time() - t, 1), "nutzung": _tokens(antwort), "kosten": (antwort.get("usage") or {}).get("cost"),
                "fazit": j.get("fazit"), "bester": zuordnung.get(j.get("bester", ""), None), "bewertungen": {}}
    for b, tag in zuordnung.items():
        x = j.get("bewertungen", {}).get(b)
        if x:
            ergebnis["bewertungen"][tag] = {**x, "buchstabe": b}
    return ergebnis


def pruefe_chat(d: dict) -> dict:
    """Bewertet freie Chat-Antworten mehrerer Modelle (anonymisiert) – optional mit Bild und Referenz."""
    k = schluessel()
    if not k:
        raise RuntimeError("Kein API-Schlüssel für den Prüfer hinterlegt (Einrichtung → Prüfer)")
    antworten = {t: str(a)[:6000] for t, a in (d.get("antworten") or {}).items() if a}
    if not antworten:
        raise ValueError("Keine Antworten zum Prüfen")
    zuordnung = {chr(65 + i): t for i, t in enumerate(antworten)}
    text = (f"FRAGE AN DIE KI-MODELLE:\n{str(d.get('frage', ''))[:4000]}\n\n"
            + (f"REFERENZ (Soll):\n{str(d['referenz'])[:2000]}\n\n" if d.get("referenz") else "")
            + ("Zur Frage gehört das angehängte Bild – prüfe auch, ob die Modelle es richtig gelesen haben.\n\n" if d.get("bild") else "")
            + "\n\n".join(f"ANTWORT {b}:\n{antworten[t]}" for b, t in zuordnung.items())
            + "\n\nBewerte jede Antwort streng (0–10): korrektheit (fachlich richtig, auch Bild richtig gelesen), hilfreich (verständlich, passend zur Frage), "
              "und falls eine Referenz da ist: referenz (wie nah an der Soll-Bewertung). Gib genau dieses JSON zurück:\n"
              '{"bewertungen": {"A": {"korrektheit": 0-10, "hilfreich": 0-10, "referenz": 0-10 oder null, "kommentar": "<1 kurzer Satz>"}, ...}, '
              '"rangfolge": ["<Buchstabe>", ...], "fazit": "<1 Satz>"}')
    inhalt = [{"type": "text", "text": text}]
    if d.get("bild"):
        bild = str(d["bild"])
        inhalt.insert(0, {"type": "image_url", "image_url": {"url": bild if bild.startswith("data:") else "data:image/jpeg;base64," + bild}})
    msgs = [{"role": "system", "content": "Du bist ein erfahrener Gutachter. Du vergleichst die Antworten mehrerer KI-Modelle fair und streng. "
                                          "Wenn du im Text eine Antwort erwähnst, schreib sie immer in eckigen Klammern, z. B. [A] oder [B]. "
                                          "Verwende in Texten keine doppelten Anführungszeichen. Antworte nur mit JSON."},
            {"role": "user", "content": inhalt}]
    t = time.time()
    for versuch in range(2):
        body = {"model": modell(), "messages": msgs, "temperature": 0, "max_tokens": 6000}
        if versuch:
            body["reasoning"] = {"enabled": False}
        antwort = _anfrage(body, k)
        roh = antwort["choices"][0]["message"].get("content") or ""
        m = re.search(r"\{.*\}", roh, re.S)
        try:
            j = json.loads(m.group(0) if m else roh)
            break
        except (json.JSONDecodeError, TypeError):
            if versuch:
                raise
            msgs = msgs + [{"role": "user", "content": "Ungültiges JSON. Bitte erneut, nur gültiges JSON."}]
    bew = {}
    for b, tag in zuordnung.items():
        x = j.get("bewertungen", {}).get(b) or {}
        werte = [v for v in (x.get("korrektheit"), x.get("hilfreich"), x.get("referenz")) if isinstance(v, (int, float))]
        bew[tag] = {**x, "buchstabe": b, "gesamt": round(sum(werte) / len(werte), 1) if werte else None}
    rang = [zuordnung[b] for b in j.get("rangfolge", []) if b in zuordnung]
    rang += [t for t in sorted(bew, key=lambda t: -(bew[t]["gesamt"] or 0)) if t not in rang]
    return {"modell": modell(), "sekunden": round(time.time() - t, 1), "kosten": antwort.get("usage", {}).get("cost"),
            "fazit": j.get("fazit"), "rangfolge": rang, "bewertungen": bew, "zuordnung": zuordnung}


def urteil_frage(f: dict, ist: str) -> dict:
    """Fragen-Benchmark: Ist die freie Antwort inhaltlich richtig? → {richtig, begruendung}"""
    k = schluessel()
    if not k:
        raise RuntimeError("Kein API-Schlüssel für den Prüfer hinterlegt")
    text = (f"FRAGE:\n{f['frage'][:3000]}\n\n" + (f"KONTEXT:\n{f['kontext'][:3000]}\n\n" if f.get("kontext") else "")
            + f"SOLL-ANTWORT:\n{f['antwort'][:2000]}\n\nANTWORT DES MODELLS:\n{str(ist)[:3000]}\n\n"
            'Stimmt die Antwort des Modells inhaltlich mit der Soll-Antwort überein? Andere Formulierungen sind erlaubt, '
            'fehlende Kernaussagen oder Fehler nicht. Gib genau dieses JSON zurück: {"richtig": true|false, "begruendung": "<1 kurzer Satz>"}')
    body = {"model": modell(), "messages": [{"role": "system", "content": "Du bist ein strenger, fairer Gutachter. Antworte nur mit JSON."},
                                            {"role": "user", "content": text}], "temperature": 0, "max_tokens": 2000}
    antwort = _anfrage(body, k)
    roh = antwort["choices"][0]["message"].get("content") or ""
    m = re.search(r"\{.*\}", roh, re.S)
    return json.loads(m.group(0) if m else roh)

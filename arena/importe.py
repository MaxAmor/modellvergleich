"""Daten-Adapter für den Fragen-Benchmark: Excel, SQLite, Link (URL / Google Sheets), Hugging Face Datasets, PostgreSQL, MySQL.

Jeder Adapter liefert rohe Zeilen (Liste von dicts). Danach: Vorschau + Spaltenzuordnung → fragen.normalisiere_mit().
Excel, SQLite, URL und Hugging Face brauchen nur die Standardbibliothek; PostgreSQL/MySQL nutzen psycopg bzw. PyMySQL (im Docker-Image).
"""
from __future__ import annotations

import io
import json
import re
import sqlite3
import shutil
import tempfile
import time
import urllib.parse
import urllib.request
import uuid
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

from arena import fragen as fr

MAX_ZEILEN = 5000
MAX_BYTES = 30 * 1024 * 1024
_cache: dict = {}   # import_id → (zeit, zeilen, quelle)


def merke(zeilen: list[dict], quelle: str) -> str:
    for k in [k for k, (t, _, _) in _cache.items() if time.time() - t > 3600]:
        _cache.pop(k, None)
    iid = uuid.uuid4().hex[:12]
    _cache[iid] = (time.time(), zeilen[:MAX_ZEILEN], quelle)
    return iid


def hole(iid: str) -> tuple[list[dict], str]:
    if iid not in _cache:
        raise ValueError("Import abgelaufen – bitte erneut laden.")
    return _cache[iid][1], _cache[iid][2]


def vorschau(zeilen: list[dict], quelle: str) -> dict:
    """Spalten, erste Zeilen und eine vorgeschlagene Zuordnung."""
    gesamt = len(zeilen)
    zeilen = [{(k.strip() if isinstance(k, str) else k): v for k, v in z.items()} for z in zeilen[:MAX_ZEILEN]]   # „ Question“ → „Question“
    spalten = []
    for z in zeilen[:200]:
        for k in z:
            if k not in spalten:
                spalten.append(k)
    return {"import_id": merke(zeilen, quelle), "quelle": quelle, "anzahl": len(zeilen), "gesamt": gesamt, "spalten": spalten,
            "zeilen": [{k: _kurz(v) for k, v in z.items()} for z in zeilen[:6]], "vorschlag": fr.zuordnung_raten(spalten, zeilen[:50])}


def _kurz(v):
    if isinstance(v, (dict, list)):
        v = json.dumps(v, ensure_ascii=False)
    v = "" if v is None else str(v)
    return v if len(v) <= 160 else v[:160] + " …"


# ---------------------------------------------------------------- Excel (.xlsx) ohne Zusatzpaket
def lese_xlsx(daten: bytes, blatt: int = 0) -> list[dict]:
    ns = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    with zipfile.ZipFile(io.BytesIO(daten)) as z:
        def lies(name):   # Schutz vor Zip-Bomben: entpackte Größe begrenzen
            if z.getinfo(name).file_size > 80 * 1024**2:
                raise ValueError("Excel-Datei entpackt zu groß (über 80 MB).")
            with z.open(name) as f:
                inhalt = f.read(80 * 1024**2 + 1)
            if len(inhalt) > 80 * 1024**2:
                raise ValueError("Excel-Datei entpackt zu groß (über 80 MB).")
            return inhalt
        texte = []
        if "xl/sharedStrings.xml" in z.namelist():
            for si in ET.fromstring(lies("xl/sharedStrings.xml")).findall("m:si", ns):
                texte.append("".join(t.text or "" for t in si.iter("{%s}t" % ns["m"])))
        blaetter = sorted(n for n in z.namelist() if re.fullmatch(r"xl/worksheets/sheet\d+\.xml", n))
        if not blaetter:
            raise ValueError("keine Tabelle in der Excel-Datei")
        wurzel = ET.fromstring(lies(blaetter[min(blatt, len(blaetter) - 1)]))
    reihen = []
    for row in wurzel.iter("{%s}row" % ns["m"]):
        werte = {}
        for c in row.findall("m:c", ns):
            spalte = re.match(r"[A-Z]+", c.get("r", "A")).group(0)
            v = c.find("m:v", ns)
            if c.get("t") == "s" and v is not None:
                wert = texte[int(v.text)]
            elif c.get("t") == "inlineStr":
                wert = "".join(t.text or "" for t in c.iter("{%s}t" % ns["m"]))
            else:
                wert = v.text if v is not None else ""
                if wert and re.fullmatch(r"-?\d+\.0+", wert):
                    wert = wert.split(".")[0]
            idx = _spalten_index(spalte)
            if idx > 16383:   # Excel hat höchstens 16384 Spalten (XFD) – größere Angaben sind kaputt oder böswillig
                raise ValueError("Excel-Datei ungültig (Spaltenangabe außerhalb von A–XFD).")
            werte[idx] = wert
        if werte:
            reihen.append([werte.get(i, "") for i in range(max(werte) + 1)])
            if len(reihen) > MAX_ZEILEN + 1:
                break
    if len(reihen) < 2:
        return []
    kopf = [str(k).strip() or f"Spalte {i + 1}" for i, k in enumerate(reihen[0])]
    return [{kopf[i]: (r[i] if i < len(r) else "") for i in range(len(kopf))} for r in reihen[1:] if any(str(x).strip() for x in r)]


def _spalten_index(buchst: str) -> int:
    n = 0
    for ch in buchst:
        n = n * 26 + (ord(ch) - 64)
    return n - 1


# ---------------------------------------------------------------- SQLite (nur lesend)
def sqlite_tabellen(daten: bytes) -> tuple[str, list[str]]:
    tmp = Path(tempfile.mkdtemp(prefix="sqlite-")) / "daten.db"
    tmp.write_bytes(daten)
    try:
        with sqlite3.connect(f"file:{tmp}?mode=ro", uri=True) as db:
            _begrenze(db)
            tab = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type IN ('table','view') AND name NOT LIKE 'sqlite_%' ORDER BY name")]
    except Exception:
        shutil.rmtree(tmp.parent, ignore_errors=True)
        raise ValueError("Keine gültige SQLite-Datei.") from None
    return str(tmp), tab


def sqlite_lesen(pfad: str, tabelle: str | None = None, abfrage: str | None = None) -> list[dict]:
    sql = _nur_lesen(abfrage) if abfrage else f'SELECT * FROM "{tabelle.replace(chr(34), "")}"'
    with sqlite3.connect(f"file:{pfad}?mode=ro", uri=True) as db:
        _begrenze(db)
        db.row_factory = sqlite3.Row
        try:
            return [dict(r) for r in db.execute(f"SELECT * FROM ({sql}) LIMIT {MAX_ZEILEN}").fetchmany(MAX_ZEILEN)]
        except sqlite3.OperationalError as fehler:
            if "interrupt" in str(fehler).lower():
                raise ValueError("Abfrage abgebrochen: dauerte länger als 15 Sekunden.") from None
            raise ValueError(f"SQLite: {fehler}") from None


def _begrenze(db):
    """Endlose oder riesige Abfragen abbrechen (z. B. WITH RECURSIVE ohne Ende, randomblob)."""
    ende = time.time() + 15
    db.set_progress_handler(lambda: 1 if time.time() > ende else 0, 10000)
    try:
        db.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, 10_000_000)
    except AttributeError:   # Python < 3.11
        pass


def _nur_lesen(sql: str) -> str:
    sql = sql.strip().rstrip(";").strip()
    if ";" in sql or not re.match(r"(?is)^(select|with)\b", sql):
        raise ValueError("Nur eine einzelne SELECT-Abfrage ist erlaubt.")
    if re.search(r"(?i)\b(insert|update|delete|drop|alter|create|attach|pragma|grant|truncate|copy|call|exec|outfile|dumpfile|load_file|load_extension|dblink|pg_terminate_backend|pg_cancel_backend|pg_read_file|pg_ls_dir|lo_import|lo_export|sleep|benchmark|pg_sleep)\b", sql):
        raise ValueError("Nur lesende Abfragen sind erlaubt.")
    return sql


# ---------------------------------------------------------------- Link (URL, Google Sheets)
def lade_url(url: str) -> tuple[str, bytes]:
    url = url.strip()
    if not re.match(r"^https?://", url):
        raise ValueError("Nur http- oder https-Links.")
    m = re.match(r"https://docs\.google\.com/spreadsheets/d/([\w-]+)", url)
    if m:   # Google-Sheets-Freigabelink → CSV-Export
        gid = re.search(r"[#&?]gid=(\d+)", url)
        url = f"https://docs.google.com/spreadsheets/d/{m.group(1)}/export?format=csv" + (f"&gid={gid.group(1)}" if gid else "")
    req = urllib.request.Request(url, headers={"User-Agent": "Modellvergleich/1.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        daten = r.read(MAX_BYTES + 1)
        typ = r.headers.get("Content-Type", "")
    if len(daten) > MAX_BYTES:
        raise ValueError("Datei größer als 30 MB.")
    name = Path(urllib.parse.urlparse(url).path).name or "link"
    if "csv" in typ or "export?format=csv" in url:
        name = name if name.endswith(".csv") else name + ".csv"
    return name, daten


def lese_datei(name: str, daten: bytes) -> list[dict]:
    """Beliebige Datei (CSV/TSV/JSON/JSONL/XLSX) → Zeilen."""
    if name.lower().endswith((".xlsx", ".xlsm")) or daten[:2] == b"PK":
        return lese_xlsx(daten)
    return fr.lese(name, daten.decode("utf-8-sig", errors="replace"))


# ---------------------------------------------------------------- Hugging Face Datasets (über die öffentliche API)
HF_API = "https://datasets-server.huggingface.co"
VORLAGEN_HF = [
    {"id": "openai/gsm8k", "config": "main", "split": "test", "titel": "GSM8K", "info": "Mathe-Textaufgaben (Englisch), Zahl als Antwort"},
    {"id": "cais/mmlu", "config": "all", "split": "test", "titel": "MMLU", "info": "Multiple Choice aus 57 Fachgebieten (Englisch)"},
    {"id": "allenai/ai2_arc", "config": "ARC-Challenge", "split": "test", "titel": "ARC Challenge", "info": "Naturwissenschaft, Multiple Choice (Englisch)"},
    {"id": "tau/commonsense_qa", "config": "default", "split": "validation", "titel": "CommonsenseQA", "info": "Alltagswissen, Multiple Choice (Englisch)"},
]


def _hf_json(pfad: str):
    with urllib.request.urlopen(HF_API + pfad, timeout=30) as r:
        return json.load(r)


def hf_splits(dataset: str) -> list[dict]:
    if not re.fullmatch(r"[\w.-]+(/[\w.-]+)?", dataset):
        raise ValueError("Ungültiger Datensatz-Name (Beispiel: openai/gsm8k)")
    try:
        return [{"config": s["config"], "split": s["split"]} for s in _hf_json("/splits?dataset=" + urllib.parse.quote(dataset))["splits"]]
    except urllib.error.HTTPError as fehler:
        raise ValueError(f"Hugging Face: Datensatz nicht gefunden oder nicht öffentlich ({fehler.code})") from None


def hf_zeilen(dataset: str, config: str, split: str, anzahl: int = 200) -> list[dict]:
    anzahl = max(1, min(int(anzahl), 2000))
    zeilen, offset = [], 0
    while len(zeilen) < anzahl:
        q = urllib.parse.urlencode({"dataset": dataset, "config": config, "split": split, "offset": offset, "length": min(100, anzahl - len(zeilen))})
        d = _hf_json("/rows?" + q)
        neu = [r["row"] for r in d.get("rows", [])]
        if not neu:
            break
        zeilen += neu
        offset += len(neu)
        if offset >= d.get("num_rows_total", 0):
            break
    return zeilen


# ---------------------------------------------------------------- PostgreSQL / MySQL (nur lesend, Zugang wird nicht gespeichert)
def db_lesen(art: str, host: str, port: int | None, datenbank: str, nutzer: str, passwort: str, abfrage: str) -> list[dict]:
    sql = _nur_lesen(abfrage)
    if " limit " not in f" {sql.lower()} ":
        sql += f" LIMIT {MAX_ZEILEN}"
    if art == "postgres":
        try:
            import psycopg
        except ImportError:
            raise ValueError("PostgreSQL-Treiber fehlt (im Docker-Image enthalten, sonst: pip install psycopg[binary]).") from None
        with psycopg.connect(host=host, port=port or 5432, dbname=datenbank, user=nutzer, password=passwort, connect_timeout=10,
                             options="-c default_transaction_read_only=on -c statement_timeout=30000") as con:
            with con.cursor() as cur:
                cur.execute(sql)
                namen = [c.name for c in cur.description]
                return [dict(zip(namen, r)) for r in cur.fetchmany(MAX_ZEILEN)]
    if art == "mysql":
        try:
            import pymysql
        except ImportError:
            raise ValueError("MySQL-Treiber fehlt (im Docker-Image enthalten, sonst: pip install pymysql).") from None
        con = pymysql.connect(host=host, port=port or 3306, database=datenbank, user=nutzer, password=passwort, connect_timeout=10,
                              read_timeout=30, cursorclass=pymysql.cursors.DictCursor)
        try:
            with con.cursor() as cur:
                cur.execute("START TRANSACTION READ ONLY")
                cur.execute(sql)
                return [{k: (v.decode() if isinstance(v, bytes) else v) for k, v in r.items()} for r in cur.fetchmany(MAX_ZEILEN)]
        finally:
            con.close()
    raise ValueError("Unbekannte Datenbank")


# ---------------------------------------------------------------- Kaggle (öffentliche Datensätze ohne Zugang, private mit API-Token)
KAGGLE_API = "https://www.kaggle.com/api/v1"
LESBAR = (".csv", ".tsv", ".json", ".jsonl", ".xlsx", ".xlsm", ".sqlite", ".sqlite3", ".db")
KAGGLE_ENTPACKT_MAX = 80 * 1024 * 1024     # gepackt höchstens 30 MB, entpackt bis 80 MB (übernommen werden ohnehin max. 5000 Zeilen)

# geprüfte Vorschläge: öffentliche Frage-Antwort-Datensätze, die ohne Anmeldung laden und sauber zugeordnet werden
VORLAGEN_KAGGLE = [
    {"ref": "nlztrk/eduqg-dataset-llm-science-exam-format-34k", "titel": "Science Exam", "info": "Naturwissenschaft, Multiple Choice A–E"},
    {"ref": "rudrakumargupta/prepmaster-placement-and-interview-quiz-questions", "titel": "Tech-Quiz", "info": "5700 MCQs zu Informatik und Logik"},
    {"ref": "shreyasur965/open-trivia-database-quiz-questions-all-categories", "titel": "Open Trivia", "info": "Quizfragen aus allen Kategorien"},
    {"ref": "nilotpaldhar/some-general-knowledge-question-and-answer", "titel": "Allgemeinwissen", "info": "90 kurze Frage-Antwort-Paare"},
]


def kaggle_ref(eingabe: str) -> str:
    """„https://www.kaggle.com/datasets/owner/name“ oder „owner/name“ → „owner/name“"""
    t = eingabe.strip().rstrip("/")
    m = re.search(r"kaggle\.com/datasets/([\w.-]+)/([\w.-]+)", t) or re.fullmatch(r"([\w.-]+)/([\w.-]+)", t)
    if not m:
        raise ValueError("Bitte einen Kaggle-Link (kaggle.com/datasets/…) oder „besitzer/datensatz“ angeben.")
    return f"{m.group(1)}/{m.group(2)}"


def _kaggle(pfad: str, roh: bool = False):
    from arena import einstellungen
    import base64
    req = urllib.request.Request(KAGGLE_API + pfad, headers={"User-Agent": "Modellvergleich/1.0"})
    nutzer, schluessel = einstellungen.kaggle_zugang()
    grenze = KAGGLE_ENTPACKT_MAX if roh else MAX_BYTES
    if nutzer and schluessel:
        req.add_header("Authorization", "Basic " + base64.b64encode(f"{nutzer}:{schluessel}".encode()).decode())
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            daten = r.read(grenze + 1)
    except urllib.error.HTTPError as fehler:
        if fehler.code in (401, 403, 404):
            raise ValueError("Kaggle: Datensatz nicht gefunden oder nicht freigegeben" + ("" if nutzer else " – bei privaten Datensätzen den Kaggle-Zugang eintragen") + ".") from None
        raise
    if len(daten) > grenze:
        raise ValueError(f"Datei größer als {grenze // 1024**2} MB.")
    return daten if roh else json.loads(daten)


def kaggle_dateien(eingabe: str) -> dict:
    ref = kaggle_ref(eingabe)
    dateien, token = [], ""
    for _ in range(20):
        d = _kaggle(f"/datasets/list/{ref}" + (f"?pageToken={urllib.parse.quote(token)}" if token else ""))
        dateien += [{"name": f["name"], "groesse": f.get("totalBytes"), "lesbar": f["name"].lower().endswith(LESBAR)} for f in d.get("datasetFiles", [])]
        token = d.get("nextPageToken") or ""
        if not token:
            break
    if not dateien:
        raise ValueError("Kaggle: keine Dateien im Datensatz.")
    return {"ref": ref, "dateien": dateien}


def kaggle_datei(eingabe: str, name: str) -> tuple[str, bytes]:
    ref = kaggle_ref(eingabe)
    daten = _kaggle(f"/datasets/download/{ref}/{urllib.parse.quote(name)}", roh=True)
    if daten[:2] == b"PK" and not name.lower().endswith((".xlsx", ".xlsm")):   # große Dateien kommen gepackt
        with zipfile.ZipFile(io.BytesIO(daten)) as z:
            teile = [n for n in z.namelist() if not n.endswith("/")]
            wahl = next((n for n in teile if Path(n).name == Path(name).name), teile[0] if teile else None)
            if not wahl:
                raise ValueError("Kaggle: leeres Archiv")
            if z.getinfo(wahl).file_size > KAGGLE_ENTPACKT_MAX:
                raise ValueError("Datei entpackt größer als 80 MB.")
            daten = z.read(wahl)
    return Path(name).name, daten


def kaggle_suche(q: str, sortierung: str = "votes") -> list[dict]:
    """Öffentliche Kaggle-Datensätze suchen (ohne Anmeldung)."""
    sort = sortierung if sortierung in ("votes", "hottest", "updated", "active") else "votes"
    params = urllib.parse.urlencode({"search": q.strip()[:100], "sortBy": sort, "page": 1})
    treffer = _kaggle(f"/datasets/list?{params}")
    return [{"ref": d.get("ref"), "titel": d.get("title") or d.get("ref"), "untertitel": d.get("subtitle") or "",
             "groesse": d.get("totalBytes"), "downloads": d.get("downloadCount"), "stimmen": d.get("voteCount"),
             "lizenz": d.get("licenseName") or "", "aktualisiert": (d.get("lastUpdated") or "")[:10]}
            for d in (treffer if isinstance(treffer, list) else []) if d.get("ref")][:20]

#!/usr/bin/env python3
"""Modellvergleich Webserver: Einrichtung + Arena + Chat + Daten.

    python3 server.py                 # http://127.0.0.1:8720 (beim ersten Start: Einrichtungsassistent)
    HOST=0.0.0.0 PORT=8720 python3 server.py

POST /api/pipeline   → NDJSON-Stream: ein Ereignis pro Schritt (start/fertig/übersprungen) mit Eingabe, Ausgabe, Dauer
POST /api/chat       → freier Chat mit einem Modell (Streaming, optional mit Bildern)
POST /api/arena/*   → Arena: Datensätze hochladen, Läufe starten/stoppen
GET  /api/status     → Ollama erreichbar? Welche Modelle?
Nur Standardbibliothek. Die KI läuft in Ollama (eigener Container, eigener Rechner oder GPU-Server), dieser Server leitet nur weiter.
"""
import json
import re
import os
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from arena import einstellungen, gpu_status as gpu_mod, kern
from arena.werkzeuge import pruefe

HIER = Path(__file__).resolve().parent
APP = HIER / "arena" / "app.html"
EINRICHTUNG = HIER / "arena" / "einrichtung.html"
PORT = int(os.environ.get("PORT") or "8720")
HOST = os.environ.get("HOST") or "127.0.0.1"
MAX_BODY = 45 * 1024 * 1024   # auch größere Excel- oder SQLite-Dateien (Base64)
ERLAUBT = {m["tag"] for m in kern.MODELLE}
# Erlaubte Host-Namen im Browser (Schutz vor DNS-Rebinding). "*" = alle. Beispiel: localhost,meinserver.lan
ERLAUBTE_HOSTS = {h.strip().lower() for h in (os.environ.get("ERLAUBTE_HOSTS") or "localhost,127.0.0.1,[::1]").split(",") if h.strip()}
# Kandidaten für die automatische Suche im Assistenten: eigener Container, Ollama auf dem Rechner selbst, lokal
OLLAMA_KANDIDATEN = ["http://ollama:11434", "http://host.docker.internal:11434", "http://localhost:11434"]

_status = {"zeit": 0, "daten": None}
_lock = threading.Lock()


def modell_erlaubt(m) -> bool:
    return m in ERLAUBT or m in (ollama_status().get("modelle") or [])


# Grafikspeicher gängiger NVIDIA-Karten (kleinste Variante) – im Container ist nur der Name sichtbar, nicht der Speicher
VRAM_TABELLE = [
    (r"5090 laptop", 24), (r"5080 laptop", 16), (r"5070 ti laptop", 12), (r"5070 laptop", 8), (r"5060 laptop", 8), (r"5050 laptop", 8),
    (r"4090 laptop", 16), (r"4080 laptop", 12), (r"4070 laptop", 8), (r"4060 laptop", 8), (r"4050 laptop", 6),
    (r"3080 ti laptop", 16), (r"3080 laptop", 8), (r"3070 ti laptop", 8), (r"3070 laptop", 8), (r"3060 laptop", 6),
    (r"3050 ti laptop", 4), (r"3050 laptop", 4), (r"2080 super.*laptop|2080.*(max-q|laptop)", 8), (r"2070.*(max-q|laptop)", 8),
    (r"2060.*(max-q|laptop)", 6), (r"1660 ti.*(max-q|laptop)", 6), (r"1650.*(max-q|laptop)", 4),
    (r"5090", 32), (r"5080", 16), (r"5070 ti", 16), (r"5070", 12), (r"5060 ti", 8), (r"5060", 8),
    (r"4090", 24), (r"4080", 16), (r"4070 ti super", 16), (r"4070", 12), (r"4060 ti", 8), (r"4060", 8),
    (r"3090", 24), (r"3080 ti", 12), (r"3080", 10), (r"3070", 8), (r"3060 ti", 8), (r"3060", 12), (r"3050", 6),
    (r"2080 ti", 11), (r"2080", 8), (r"2070", 8), (r"2060 super", 8), (r"2060", 6), (r"1660", 6), (r"1650", 4),
    (r"1080 ti", 11), (r"1080", 8), (r"1070", 8), (r"1060", 3), (r"titan rtx", 24), (r"a6000", 48), (r"a5000", 24), (r"a4000", 16), (r"a2000", 6),
]


_hw_cache: dict = {"zeit": 0.0, "wert": None}


def hardware_erkennen(frisch: bool = False) -> dict:
    """Wie _hardware_erkennen, aber 60 s zwischengespeichert – die Einstellungsseite soll ohne Wartezeit öffnen."""
    if frisch or not _hw_cache["wert"] or time.time() - _hw_cache["zeit"] > 60:
        _hw_cache.update(wert=_hardware_erkennen(), zeit=time.time())
    return _hw_cache["wert"]


def _amd_integriert() -> set[str]:
    """PCI-Adressen eingebauter AMD-Grafik (APU, z. B. Radeon im Ryzen): laut KFD-Topologie ohne eigenen Speicher
    (local_mem_size 0) – der „VRAM“ ist vom Arbeitsspeicher abgezweigt und Ollama nutzt solche Chips nicht."""
    aus = set()
    for p in Path("/sys/class/kfd/kfd/topology/nodes").glob("*/properties"):
        try:
            werte = dict(z.split(" ", 1) for z in p.read_text().splitlines() if " " in z)
            if int(werte.get("simd_count", 0)) > 0 and int(werte.get("local_mem_size", 1)) == 0:
                loc = int(werte.get("location_id", -1))
                aus.add(f"{(loc >> 8) & 0xff:02x}:{(loc >> 3) & 0x1f:02x}.{loc & 0x7}")
        except (OSError, ValueError):
            pass
    return aus


def _integrierte_grafik() -> list[dict]:
    """Eingebaute Grafik (nur zur Info im Assistenten): zählt nie als Grafikkarte für Ollama."""
    integriert, aus = _amd_integriert(), []
    for d in sorted(Path("/sys/class/drm").glob("card*/device")):
        try:
            adresse = d.resolve().name[-7:]   # „0000:05:00.0“ → „05:00.0“
            if adresse in integriert:
                gb = int((d / "mem_info_vram_total").read_text()) / 1024**3
                aus.append({"name": "AMD Radeon (im Prozessor)", "vram_gb": round(gb, 1)})
        except (OSError, ValueError):
            pass
    return aus


def _hardware_erkennen() -> dict:
    """Grafikkarte erkennen: GPU-Server bzw. nvidia-smi (genau), sonst NVIDIA-Treiber (/proc, Speicher geschätzt) oder AMD (sysfs).
    Eingebaute Prozessorgrafik wird nie als Grafikkarte gezählt, nur unter „integriert“ gemeldet."""
    er = _hardware_karte()
    er["integriert"] = _integrierte_grafik()
    return er


def _hardware_karte() -> dict:
    g = gpu_status()
    if g and g.get("jetzt"):
        j = g["jetzt"]
        return {"name": j["name"].replace("NVIDIA ", "").replace("GeForce ", ""), "vram_gb": round(j["vram_max_gb"]), "geschaetzt": False,
                "hersteller": "nvidia", "quelle": "server" if einstellungen.get("gpu_status_url") else "nvidia-smi"}
    name, vram, geschaetzt = os.environ.get("GPU_NAME") or None, None, False
    for info in sorted(Path("/proc/driver/nvidia/gpus").glob("*/information")):
        try:
            for zeile in info.read_text().splitlines():
                if zeile.startswith("Model:"):
                    name = zeile.split(":", 1)[1].strip().replace("NVIDIA ", "").replace("GeForce ", "")
                    klein = name.lower()
                    vram = next((gb for muster, gb in VRAM_TABELLE if re.search(muster, klein)), None)
                    geschaetzt = vram is not None
                    return {"name": name, "vram_gb": vram, "geschaetzt": geschaetzt, "hersteller": "nvidia"}
        except OSError:
            pass
    integriert = _amd_integriert()
    for d in sorted(Path("/sys/class/drm").glob("card*/device")):
        try:
            if d.resolve().name[-7:] in integriert:
                continue   # Prozessorgrafik, kein eigener Grafikspeicher
            gesamt = int((d / "mem_info_vram_total").read_text())
            if gesamt > 2 * 1024**3:
                return {"name": name or "AMD-Grafikkarte", "vram_gb": round(gesamt / 1024**3), "geschaetzt": False, "hersteller": "amd"}
        except (OSError, ValueError):
            pass
    return {"name": name, "vram_gb": None, "geschaetzt": False, "hersteller": None}


def hardware_text() -> str:
    e = einstellungen.lade()
    if e.get("gpu_name"):
        return e["gpu_name"]
    if e.get("vram_gb"):
        return f"Grafikkarte · {e['vram_gb']:g} GB"
    er = hardware_erkennen()
    return er["name"] if er.get("name") and er.get("quelle") in ("server", "nvidia-smi") else "ohne Grafikkarte"


def status_neu():
    with _lock:
        _status["zeit"] = 0


def ollama_status():
    with _lock:
        if time.time() - _status["zeit"] < 15 and _status["daten"]:
            return _status["daten"]
    try:
        tags = kern._get("/api/tags", timeout=4)
        alle = {m["name"]: m for m in tags.get("models", [])}
        da = [m for m in alle if m in ERLAUBT] + sorted(m for m in alle if m not in ERLAUBT)
        vision = [m for m in da if "vision" in modell_info(m).get("faehigkeiten", [])]
        extra = [{"tag": m, "groesse_gb": round(alle[m].get("size", 0) / 1e9, 2), **{k: modell_info(m).get(k) for k in ("parameter", "quantisierung", "familie")}}
                 for m in da if m not in ERLAUBT]
        daten = {"ok": bool(da), "modelle": da, "vision": vision, "extra": extra, "gpu": hardware_text(),
                 "pruefer": bool(einstellungen.pruefer_key()), "eingerichtet": bool(einstellungen.get("eingerichtet"))}
    except Exception as fehler:
        daten = {"ok": False, "fehler": str(fehler)}
    with _lock:
        _status.update(zeit=time.time(), daten=daten)
    return daten


_info_cache = {}


def modell_info(modell):
    """Steckbrief eines Modells aus Ollama (/api/show + /api/tags), zwischengespeichert."""
    if modell in _info_cache:
        return _info_cache[modell]
    info = {"tag": modell}
    try:
        sh = kern._post("/api/show", {"model": modell}, timeout=8)
        d = sh.get("details", {})
        mi = sh.get("model_info", {}) or {}
        info.update(familie=d.get("family"), parameter=d.get("parameter_size"), quantisierung=d.get("quantization_level"),
                    format=d.get("format"), faehigkeiten=sh.get("capabilities", []),
                    kontext=next((v for k, v in mi.items() if k.endswith(".context_length")), None),
                    schichten=next((v for k, v in mi.items() if k.endswith(".block_count")), None))
        for m in kern._get("/api/tags", timeout=4).get("models", []):
            if m["name"] == modell:
                info["datei_gb"] = round(m.get("size", 0) / 1e9, 2)
    except Exception as fehler:
        info["fehler"] = str(fehler)
    _info_cache[modell] = info
    return info


def urteil(p, m):
    return "richtig" if p == m else ("falsch" if p == 0 else "teilweise")


# ---------------------------------------------------------------- Hugging Face: nur, was auf die eigene Grafikkarte passt
def max_gb() -> float:   # Datei ≤ Grafikspeicher − 1,5 GB → bleibt Platz für den Kontext
    return einstellungen.max_modell_gb()


def passt_gb() -> float:  # bis hier: „passt“, darüber: „knapp“
    return max_gb() - 1.0
GESPERRT = re.compile(r"uncensor|abliterat|heretic|nsfw|erotic|lewd|jailbreak", re.I)
SCHWACH = re.compile(r"^(?:UD-)?(?:IQ1|IQ2|TQ1|TQ2|Q2)", re.I)   # zu stark komprimiert, für Schulaufgaben unbrauchbar
_hf_cache = {}
VORSCHLAEGE = [
    ("unsloth/Qwen3.5-4B-GGUF", "Alibaba · Qwen 3.5", "Zwischengröße der Qwen-Reihe: Lohnt der Schritt von 2B auf 4B?"),
    ("unsloth/gemma-4-E2B-it-GGUF", "Google · Gemma 4", "Kleinste Gemma 4 – direkter Vergleich mit der E4B."),
    ("unsloth/Phi-4-mini-instruct-GGUF", "Microsoft · Phi-4 mini", "Neue Familie. Klein und auf logisches Denken getrimmt.", "3,8 Mrd."),
    ("unsloth/DeepSeek-R1-0528-Qwen3-8B-GGUF", "DeepSeek · R1 (auf Qwen3 8B)", "DeepSeeks Denk-Modell in klein – denkt vor jeder Antwort."),
    ("unsloth/Ministral-3-8B-Instruct-2512-GGUF", "Mistral · Ministral 3", "Europäischer Hersteller, liest auch Bilder."),
    ("unsloth/gemma-3-12b-it-GGUF", "Google · Gemma 3", "Ältere Generation, aber größer: Was schlägt was?"),
    ("bartowski/Llama-3.2-3B-Instruct-GGUF", "Meta · Llama 3.2", "Der bekannte Klassiker als Vergleich."),
    ("unsloth/granite-4.0-h-tiny-GGUF", "IBM · Granite 4.0 H Tiny", "Hybrid-Architektur, sparsam im Speicher.", "7 Mrd. (aktiv 1)"),
]


def hf_json(url):
    if url in _hf_cache and time.time() - _hf_cache[url][0] < 3600:
        return _hf_cache[url][1]
    with urllib.request.urlopen(url, timeout=15) as r:
        d = json.load(r)
    _hf_cache[url] = (time.time(), d)
    return d


def parameter_aus_name(n):
    m = re.search(r"(\d+(?:\.\d+)?)B(?:-A(\d+(?:\.\d+)?)B)?(?![a-z])", n, re.I) or re.search(r"[-_]E(\d)B", n)
    return (m.group(1) + " Mrd." + (f" (aktiv {m.group(2)})" if m.lastindex and m.lastindex > 1 and m.group(2) else "")) if m else None


def hf_repo(repo):
    info = hf_json(f"https://huggingface.co/api/models/{repo}?blobs=true")
    dateien = []
    for sb in info.get("siblings", []):
        n = sb["rfilename"]
        if not n.endswith(".gguf") or "mmproj" in n.lower() or "-of-" in n or "/" in n:
            continue
        m = re.search(r"((?:UD-)?I?Q\d[\w]*?|F16|BF16|F32)(?=\.gguf$)", n, re.I)
        quant = m.group(1) if m else None
        gb = round(sb.get("size", 0) / 1e9, 2)
        if not quant or SCHWACH.match(quant):
            continue
        dateien.append({"datei": n, "quant": quant, "gb": gb, "passt": gb <= max_gb(), "knapp": passt_gb() < gb <= max_gb()})
    dateien.sort(key=lambda d: d["gb"])
    vision = any("mmproj" in sb["rfilename"].lower() for sb in info.get("siblings", []))
    q4 = next((d for d in dateien if re.fullmatch(r"Q4_K_M", d["quant"], re.I)), None) or next((d for d in dateien if re.match(r"(UD-)?I?Q4", d["quant"], re.I) and d["passt"]), None)
    return {"repo": repo, "dateien": dateien, "vision": vision, "q4": q4, "lizenz": (info.get("cardData") or {}).get("license"),
            "downloads": info.get("downloads", 0), "parameter": parameter_aus_name(repo)}


def hf_suche(such):
    from concurrent.futures import ThreadPoolExecutor
    from urllib.parse import quote
    liste = hf_json(f"https://huggingface.co/api/models?search={quote(such)}&filter=gguf&sort=downloads&direction=-1&limit=40")
    gesperrt = [m["id"] for m in liste if GESPERRT.search(m["id"])]
    kandidaten = [m["id"] for m in liste if not GESPERRT.search(m["id"])]
    with ThreadPoolExecutor(8) as pool:
        infos = list(pool.map(lambda r: (r, _sicher(hf_repo, r)), kandidaten))
    treffer, zu_gross = [], 0
    for repo, info in infos:
        if not info or not info["q4"] or not info["q4"]["passt"]:
            zu_gross += 1
            continue
        treffer.append({"id": repo, "downloads": info["downloads"], "parameter": info["parameter"], "q4_gb": info["q4"]["gb"],
                        "vision": info["vision"], "lizenz": info["lizenz"], "knapp": info["q4"]["knapp"]})
    return {"treffer": treffer[:18], "zu_gross": zu_gross, "gesperrt": len(gesperrt)}


def _sicher(f, *a):
    try:
        return f(*a)
    except Exception:
        return None


def hf_vorschlaege():
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(8) as pool:
        infos = list(pool.map(lambda v: (v, _sicher(hf_repo, v[0])), VORSCHLAEGE))
    aus = []
    for (repo, hersteller, warum, *param), info in infos:
        if info and info["q4"] and info["q4"]["passt"]:
            aus.append({"id": repo, "hersteller": hersteller, "warum": warum, "parameter": (param[0] if param else info["parameter"]), "q4": info["q4"], "vision": info["vision"],
                        "lizenz": info["lizenz"], "downloads": info["downloads"]})
    return aus


STANDARD_SYSTEM = ("Du bist ein hilfreicher, genauer Assistent für Schulaufgaben (Klasse 7–11). "
                   "Antworte knapp und auf Deutsch, außer du wirst anders angesprochen.")
_gpu_cache = [0.0, None]
_messung = gpu_mod.Messung()


def gpu_status():
    """Auslastung: eingetragener GPU-Server (gpu_status_url) > nvidia-smi im Container > nichts."""
    url = einstellungen.get("gpu_status_url")
    if url:
        if time.time() - _gpu_cache[0] > 1.0:
            try:
                with urllib.request.urlopen(url, timeout=3) as r:
                    _gpu_cache[1] = json.load(r)
            except Exception:
                _gpu_cache[1] = None
            _gpu_cache[0] = time.time()
        return _gpu_cache[1]
    if gpu_mod.verfuegbar():
        return _messung.status()
    return None


def uuid_kurz() -> str:
    import uuid
    return uuid.uuid4().hex[:12]


def eindeutig(name: str) -> str:
    """Datensatz-Name, der noch nicht vergeben ist (Name, Name (2), …)."""
    from arena import herausforderer as hf
    name = (name or "Datensatz").strip()[:70] or "Datensatz"
    vorhanden = {d["name"] for d in hf.datensaetze()} | {d["id"] for d in hf.datensaetze()}
    kandidat, n = name, 2
    while kandidat in vorhanden or hf.slug(kandidat) in vorhanden:
        kandidat, n = f"{name} ({n})", n + 1
    return kandidat


# ---------------------------------------------------------------- Einrichtung
URL_MUSTER = re.compile(r"^https?://[\w.\-]+(?::\d{1,5})?/?$|^https?://\[[0-9a-fA-F:]+\](?::\d{1,5})?/?$")


def teste_ollama(url: str, timeout: float = 2.5) -> dict:
    url = url.rstrip("/")
    try:
        with urllib.request.urlopen(url + "/api/version", timeout=timeout) as r:
            version = json.load(r).get("version")
        with urllib.request.urlopen(url + "/api/tags", timeout=timeout) as r:
            modelle = [m["name"] for m in json.load(r).get("models", [])]
        return {"url": url, "ok": True, "version": version, "modelle": modelle}
    except Exception as fehler:
        return {"url": url, "ok": False, "fehler": str(getattr(fehler, "reason", fehler))[:120]}


def ollama_suche() -> dict:
    """Probiert eigene Einstellung + übliche Adressen parallel durch."""
    from concurrent.futures import ThreadPoolExecutor
    urls = list(dict.fromkeys([kern.ollama_url()] + OLLAMA_KANDIDATEN))
    with ThreadPoolExecutor(len(urls)) as pool:
        ergebnisse = list(pool.map(teste_ollama, urls))
    return {"aktuell": kern.ollama_url(), "kandidaten": ergebnisse, "im_container": Path("/.dockerenv").exists()}


class Handler(BaseHTTPRequestHandler):
    timeout = 60   # unvollständige Anfragen halten keinen Thread fest (Streams senden laufend, sind davon nicht betroffen)
    def end_headers(self):   # für jede Antwort: nicht in fremde Seiten einbetten, Typ nicht raten
        self.send_header("X-Frame-Options", "SAMEORIGIN")
        self.send_header("Content-Security-Policy", "frame-ancestors 'self'")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        super().end_headers()

    server_version = "Modellvergleich/1.0"

    RUHIG = ("/api/status", "/api/gpu", "/api/einstellungen", "/api/arena", "/api/standard-system")

    def log_message(self, fmt, *args):
        # Routine-Abfragen der Oberfläche (alle paar Sekunden) nicht protokollieren, Fehler schon
        if self.command == "GET" and self.path.split("?")[0] in self.RUHIG and len(args) > 1 and str(args[1])[:1] in "23":
            return
        print(f"{self.address_string()} {fmt % args}", flush=True)

    def _json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _datei(self, pfad: Path, typ: str):
        body = pfad.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", typ)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def _host_ok(self) -> bool:
        """Nur bekannte Host-Namen (Schutz vor DNS-Rebinding). Anpassen über ERLAUBTE_HOSTS."""
        if "*" in ERLAUBTE_HOSTS:
            return True
        host = (self.headers.get("Host") or "").lower()
        name = host.rsplit(":", 1)[0] if not host.endswith("]") else host
        if name in ERLAUBTE_HOSTS:
            return True
        self._json(403, {"fehler": f"Host „{host}“ nicht erlaubt. Ergänze ihn in ERLAUBTE_HOSTS (Datei .env)."})
        return False

    def _weiter(self, ziel):
        self.send_response(302)
        self.send_header("Location", ziel)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        if not self._host_ok():
            return
        pfad = self.path.split("?")[0]
        if pfad in ("/", "/index.html"):   # beim ersten Start öffnet die App den Einrichtungsassistenten als Fenster
            return self._datei(APP, "text/html; charset=utf-8")
        if pfad == "/api/modelle":          # installierte Modelle mit Steckbrief und Speicherstatus
            status_neu()
            installiert = ollama_status().get("modelle") or []
            geladen = {}
            try:
                for m in kern._get("/api/ps", timeout=3).get("models", []):
                    geladen[m.get("name")] = {"vram_gb": round(m.get("size_vram", 0) / 1024**3, 2), "gesamt_gb": round(m.get("size", 0) / 1024**3, 2)}
            except Exception:
                pass
            return self._json(200, {"modelle": [{**modell_info(m), "geladen": geladen.get(m)} for m in installiert]})
        if pfad == "/api/import/kaggle/vorlagen":
            from arena import importe as im
            return self._json(200, {"vorlagen": im.VORLAGEN_KAGGLE})
        if pfad == "/api/import/hf/vorlagen":
            from arena import importe as im
            return self._json(200, {"vorlagen": im.VORLAGEN_HF})
        if pfad == "/api/datensatz":
            from arena import herausforderer as hf
            from urllib.parse import parse_qs, urlparse
            ds = (parse_qs(urlparse(self.path).query).get("id") or ["demo"])[0]
            try:
                return self._json(200, {"id": ds, "aufgaben": hf.aufgaben_liste(ds)})
            except ValueError as fehler:
                return self._json(404, {"fehler": str(fehler)})
        if pfad == "/bild":                 # Abbildung einer Aufgabe: /bild?ds=<id>&pfad=<fach>/bilder/<datei>
            from arena import herausforderer as hf
            from urllib.parse import parse_qs, urlparse
            q = parse_qs(urlparse(self.path).query)
            try:
                basis = hf.ordner_von((q.get("ds") or ["demo"])[0]).resolve()
            except ValueError:
                return self._json(404, {"fehler": "unbekannter Datensatz"})
            ziel = (basis / (q.get("pfad") or [""])[0]).resolve()
            if basis in ziel.parents and ziel.is_file() and ziel.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp", ".gif"):
                return self._datei(ziel, {".png": "image/png", ".webp": "image/webp", ".gif": "image/gif"}.get(ziel.suffix.lower(), "image/jpeg"))
            return self._json(404, {"fehler": "Bild nicht gefunden"})
        if pfad in ("/vorlage/allgemein/beispiel.csv", "/vorlage/allgemein/beispiel.jsonl"):
            body = (HIER / "vorlagen" / pfad.rsplit("/", 1)[1]).read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/csv; charset=utf-8" if pfad.endswith(".csv") else "application/x-ndjson; charset=utf-8")
            self.send_header("Content-Disposition", f'attachment; filename="{pfad.rsplit("/", 1)[1]}"')
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if pfad.startswith("/vorlage/"):     # Demo-Dateien als Vorlage für eigene Datensätze: /vorlage/<fach>/aufgaben.json
            teile = pfad.split("/")
            if len(teile) == 4 and re.fullmatch(r"[a-z]+", teile[2]) and teile[3] in ("aufgaben.json", "referenz.json") and (HIER / "daten" / teile[2] / teile[3]).is_file():
                body = (HIER / "daten" / teile[2] / teile[3]).read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Disposition", f'attachment; filename="{teile[2]}-{teile[3]}"')
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
        if pfad in ("/einrichtung", "/einrichtung/"):
            return self._datei(EINRICHTUNG, "text/html; charset=utf-8")
        if pfad == "/api/einstellungen":
            from arena import werkzeuge as wz
            return self._json(200, {**einstellungen.oeffentlich(), "modelle_basis": kern.MODELLE, "max_modell_gb": max_gb(), "erkannt": hardware_erkennen(),
                                    "sympy_verfuegbar": wz.sympy_verfuegbar()})
        if pfad == "/api/einrichtung/suche":
            return self._json(200, ollama_suche())
        if pfad == "/api/gpu":
            g = gpu_status()
            return self._json(200 if g else 404, g or {"fehler": "keine Auslastung verfügbar (GPU-Modus oder GPU-Server, siehe Einrichtung)"})
        if pfad == "/api/arena":
            from arena import herausforderer as hf
            from urllib.parse import parse_qs, urlparse
            ds = (parse_qs(urlparse(self.path).query).get("datensatz") or ["demo"])[0]
            try:
                erg = hf.ergebnisse(ds)
            except ValueError:
                ds, erg = "demo", hf.ergebnisse("demo")
            return self._json(200, {"job": hf.status(), "datensaetze": hf.datensaetze(), "datensatz": ds, "ergebnisse": erg})
        if pfad == "/api/standard-system":
            return self._json(200, {"system": STANDARD_SYSTEM})
        if pfad == "/api/status":
            return self._json(200, ollama_status())
        if pfad in ("/api/hf/suche", "/api/hf/dateien", "/api/hf/vorschlaege"):
            from urllib.parse import parse_qs, urlparse
            q = parse_qs(urlparse(self.path).query)
            try:
                if pfad == "/api/hf/vorschlaege":
                    return self._json(200, hf_vorschlaege())
                if pfad == "/api/hf/suche":
                    return self._json(200, hf_suche((q.get("q") or [""])[0][:80]))
                repo = (q.get("repo") or [""])[0]
                if not re.fullmatch(r"[\w.-]+/[\w.-]+", repo) or GESPERRT.search(repo):
                    return self._json(400, {"fehler": "ungültiges oder gesperrtes Repo"})
                info = hf_repo(repo)
                return self._json(200, {"repo": repo, "dateien": [d for d in info["dateien"] if d["passt"]], "vision": info["vision"],
                                        "ausgeblendet": sum(1 for d in info["dateien"] if not d["passt"]), "lizenz": info["lizenz"]})
            except Exception as fehler:
                return self._json(502, {"fehler": f"Hugging Face nicht erreichbar: {fehler}"})
        self._json(404, {"fehler": "nicht gefunden"})

    def do_POST(self):
        if not self._host_ok():
            return
        # Nur JSON von derselben Seite: fremde Webseiten können so keine Aktionen auslösen (CSRF)
        if not (self.headers.get("Content-Type") or "").lower().startswith("application/json"):
            return self._json(415, {"fehler": "nur application/json"})
        herkunft = self.headers.get("Origin")
        if herkunft and herkunft.split("://", 1)[-1].lower() != (self.headers.get("Host") or "").lower():
            return self._json(403, {"fehler": "fremde Herkunft"})
        try:
            n = int(self.headers.get("Content-Length") or 0)
            if not 0 <= n <= MAX_BODY:
                raise ValueError("Anfrage zu groß")
            daten = json.loads(self.rfile.read(n) or b"{}")
            if not isinstance(daten, dict):
                raise ValueError("JSON-Objekt erwartet")
        except Exception as fehler:
            return self._json(400, {"fehler": str(fehler)})
        modell = daten.get("modell")
        pfad = self.path.split("?")[0]
        if pfad.startswith("/api/einrichtung/") or pfad == "/api/einstellungen":
            return self._einrichtung(pfad, daten)
        if pfad.startswith("/api/arena"):
            return self._arena(pfad, daten)
        if pfad.startswith("/api/import/"):
            return self._import(pfad, daten)
        if pfad not in ("/api/pruefer", "/api/pruefer-chat", "/api/hf/laden", "/api/modell/loeschen") and not modell_erlaubt(modell):
            return self._json(400, {"fehler": "unbekanntes Modell"})
        if pfad == "/api/pruefer-chat":
            from arena import pruefer
            try:
                return self._json(200, pruefer.pruefe_chat(daten))
            except Exception as fehler:
                return self._json(502, {"fehler": f"Prüfer nicht erreichbar: {fehler}"})
        if pfad in ("/api/hf/laden", "/api/modell/loeschen") and not einstellungen.get("modelle_verwalten"):
            return self._json(403, {"fehler": "Modelle verwalten ist ausgeschaltet (Einrichtung → Sicherheit)."})
        if pfad == "/api/hf/laden":
            return self._hf_laden(daten)
        if pfad == "/api/modell/loeschen":
            m = str(daten.get("modell", ""))
            if m not in (ollama_status().get("modelle") or []):
                return self._json(400, {"fehler": "Modell ist nicht installiert."})
            req = urllib.request.Request(kern.ollama_url() + "/api/delete", data=json.dumps({"model": m}).encode(), method="DELETE", headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=30).read()
            status_neu()
            return self._json(200, {"ok": True})
        if pfad == "/api/chat":
            try:
                return self._chat(modell, daten)
            except urllib.error.HTTPError as fehler:   # Ollama hat geantwortet, aber mit Fehler → Meldung durchreichen
                return self._json(502, {"fehler": f"Ollama: {fehler.read().decode(errors='replace')[:200]}"})
            except urllib.error.URLError as fehler:
                return self._json(502, {"fehler": f"Ollama nicht erreichbar ({fehler.reason})"})
            except ValueError as fehler:
                return self._json(400, {"fehler": str(fehler)})
        if pfad == "/api/frage":
            return self._frage(modell, daten)
        if pfad != "/api/pipeline":
            return self._json(404, {"fehler": "nicht gefunden"})
        from arena import herausforderer as hf
        try:
            ds_daten = hf.lade(str(daten.get("datensatz") or "demo"))
        except ValueError as fehler:
            return self._json(400, {"fehler": str(fehler)})
        a = ds_daten["aufgaben"].get(daten.get("aufgabe_id"))
        if not a:
            return self._json(400, {"fehler": "unbekannte Aufgabe"})
        try:
            return self._pipeline(modell, a, daten, ds_daten)
        except urllib.error.URLError as fehler:
            return self._json(502, {"fehler": f"Ollama nicht erreichbar ({fehler.reason})"})
        except Exception as fehler:
            return self._json(500, {"fehler": str(fehler)})
        self._json(404, {"fehler": "nicht gefunden"})

    # ------------------------------------------------------------------ Fragen-Benchmark: eine Frage stellen (NDJSON-Schritte)
    def _frage(self, modell, d):
        from arena import fragen as fr, herausforderer as hf
        try:
            ds = hf.lade(str(d.get("datensatz") or ""))
        except ValueError as fehler:
            return self._json(400, {"fehler": str(fehler)})
        f = (ds.get("fragen") or {}).get(d.get("frage_id"))
        if not f:
            return self._json(400, {"fehler": "unbekannte Frage"})
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

        def senden(**ev):
            self.wfile.write((json.dumps(ev, ensure_ascii=False) + "\n").encode())
            self.wfile.flush()
        try:
            art = {"auswahl": "Multiple Choice", "zahl": "Zahl", "enthaelt": "kurze Antwort", "exakt": "exakt", "ki": "freie Antwort"}[f["wertung"]]
            senden(knoten="frage", status="fertig", kurz=(f.get("thema") + " · " if f.get("thema") else "") + art, eingabe={"datensatz": d.get("datensatz")}, ausgabe=f)
            bild = None
            if f.get("bild"):
                pfad_bild = ds["ordner"] / f["bild"]
                if d.get("bild", True) and kern.kann_bilder(modell) and pfad_bild.is_file():
                    bild = str(pfad_bild)
                    senden(knoten="abbildung", status="fertig", kurz=f"Bild an das Modell gesendet ({round(pfad_bild.stat().st_size / 1024)} KB)", ausgabe={"bild": f["bild"]})
                else:
                    senden(knoten="abbildung", status="fertig", kurz="Bild nicht gesendet – " + ("Modell kann keine Bilder lesen" if not kern.kann_bilder(modell) else "Schalter aus"))
            else:
                senden(knoten="abbildung", status="aus", kurz="keine Abbildung")
            senden(knoten="antworten", status="laeuft")
            t = time.time()
            r = fr.beantworte(modell, f, bild, think=bool(d.get("think")))
            senden(knoten="antworten", status="fertig", sekunden=round(time.time() - t, 2), kurz=f"„{r['antwort'][:60]}“",
                   eingabe={"nachrichten": fr.prompt(f, bool(bild))}, ausgabe={"antwort": r["antwort"], "gueltig": r["gueltig"], "token": r.get("tokens_aus")})
            senden(knoten="wertung", status="fertig", kurz=("richtig" if r["richtig"] else "falsch") + " · " + art,
                   ausgabe={"soll": f["antwort"], "ist": r["antwort"], "wertung": r["wertung"], "erklaerung": r.get("erklaerung"), "geschaetzt": r.get("geschaetzt")})
            senden(fertig=True, ergebnis={**r, "soll": f["antwort"], "bild": bool(bild), "art": art})
        except Exception as fehler:
            senden(fertig=True, fehler=str(fehler))

    # ------------------------------------------------------------------ Daten-Adapter: Datei, Excel, SQLite, Link, Hugging Face, PostgreSQL, MySQL
    _sqlite: dict = {}   # sqlite_id → Pfad der Zwischendatei (wird nach 1 h gelöscht)

    @classmethod
    def _sqlite_merken(cls, sid: str, pfad: str):
        jetzt = time.time()
        for k, (p, t) in list(cls._sqlite.items()):
            if jetzt - t > 3600:
                import shutil
                shutil.rmtree(Path(p).parent, ignore_errors=True)
                cls._sqlite.pop(k, None)
        cls._sqlite[sid] = (pfad, jetzt)

    def _import(self, pfad, d):
        import base64
        from arena import fragen as fr, herausforderer as hf, importe as im
        try:
            if pfad == "/api/import/datei":
                name = str(d.get("name") or "datei")
                daten = base64.b64decode(str(d["daten"]).split(",")[-1]) if d.get("daten") else str(d.get("text") or "").encode()
                bilder = {}
                for b in d.get("bilder") or []:
                    try:
                        bilder[Path(str(b.get("name"))).name] = base64.b64decode(str(b["bild"]).split(",")[-1])
                    except Exception:
                        pass
                if name.lower().endswith((".db", ".sqlite", ".sqlite3")) or daten[:15] == b"SQLite format 3":
                    pfad_db, tabellen = im.sqlite_tabellen(daten)
                    sid = uuid_kurz()
                    Handler._sqlite_merken(sid, pfad_db)
                    return self._json(200, {"art": "sqlite", "sqlite_id": sid, "tabellen": tabellen, "name": name})
                zeilen = im.lese_datei(name, daten)
                if not zeilen:
                    return self._json(400, {"fehler": f"{name}: keine Zeilen gefunden"})
                v = im.vorschau(zeilen, name)
                if v["vorschlag"].get("frage") and v["vorschlag"].get("antwort"):
                    ds_name = eindeutig(re.sub(r"\.[^.]+$", "", name))
                    neu = hf.speichere_fragen_liste(ds_name, fr.normalisiere_mit(zeilen, v["vorschlag"]), bilder)
                    return self._json(200, {"art": "gespeichert", "datensatz": neu, "vorschau": v})
                return self._json(200, {"art": "vorschau", "vorschau": v})
            if pfad == "/api/import/sqlite":
                pdb = (Handler._sqlite.get(str(d.get("sqlite_id"))) or (None,))[0]
                if not pdb:
                    return self._json(400, {"fehler": "SQLite-Datei abgelaufen – bitte erneut hineinziehen."})
                zeilen = im.sqlite_lesen(pdb, d.get("tabelle"), d.get("abfrage"))
                return self._json(200, im.vorschau(zeilen, "SQLite · " + (d.get("tabelle") or "Abfrage")))
            if pfad == "/api/import/url":
                name, daten = im.lade_url(str(d.get("url") or ""))
                zeilen = im.lese_datei(name, daten)
                if not zeilen:
                    return self._json(400, {"fehler": "Unter dem Link wurden keine Tabellenzeilen gefunden (CSV, Excel, JSON oder JSONL)."})
                return self._json(200, im.vorschau(zeilen, name))
            if pfad == "/api/import/hf/splits":
                return self._json(200, {"splits": im.hf_splits(str(d.get("dataset") or "").strip())})
            if pfad == "/api/import/hf":
                ds = str(d.get("dataset") or "").strip()
                zeilen = im.hf_zeilen(ds, str(d.get("config") or "default"), str(d.get("split") or "test"), int(d.get("anzahl") or 200))
                if not zeilen:
                    return self._json(400, {"fehler": "Keine Zeilen geladen – Teil (Split) prüfen."})
                return self._json(200, im.vorschau(zeilen, f"{ds} · {d.get('split')}"))
            if pfad == "/api/import/db":
                zeilen = im.db_lesen(str(d.get("art")), str(d.get("host") or "").strip(), int(d.get("port") or 0) or None, str(d.get("datenbank") or ""),
                                     str(d.get("nutzer") or ""), str(d.get("passwort") or ""), str(d.get("abfrage") or ""))
                return self._json(200, im.vorschau(zeilen, f"{d.get('art')} · {d.get('datenbank')}"))
            if pfad == "/api/import/kaggle/suche":
                return self._json(200, {"treffer": im.kaggle_suche(str(d.get("q") or ""), str(d.get("sortierung") or "votes"))})
            if pfad == "/api/import/kaggle/dateien":
                return self._json(200, im.kaggle_dateien(str(d.get("ref") or "")))
            if pfad == "/api/import/kaggle":
                name, daten = im.kaggle_datei(str(d.get("ref") or ""), str(d.get("datei") or ""))
                if name.lower().endswith((".db", ".sqlite", ".sqlite3")) or daten[:15] == b"SQLite format 3":
                    pfad_db, tabellen = im.sqlite_tabellen(daten)
                    sid = uuid_kurz()
                    Handler._sqlite_merken(sid, pfad_db)
                    return self._json(200, {"art": "sqlite", "sqlite_id": sid, "tabellen": tabellen, "name": name})
                zeilen = im.lese_datei(name, daten)
                if not zeilen:
                    return self._json(400, {"fehler": f"{name}: keine Tabellenzeilen gefunden"})
                return self._json(200, {"art": "vorschau", "vorschau": im.vorschau(zeilen, f"Kaggle · {name}")})
            if pfad == "/api/import/uebernehmen":
                zeilen, quelle = im.hole(str(d.get("import_id")))
                liste = fr.normalisiere_mit(zeilen, d.get("zuordnung") or {})
                if d.get("anzahl"):
                    liste = liste[: max(1, int(d["anzahl"]))]
                return self._json(200, hf.speichere_fragen_liste(eindeutig(str(d.get("name") or quelle)), liste))
        except urllib.error.URLError as fehler:
            return self._json(502, {"fehler": f"Quelle nicht erreichbar: {getattr(fehler, 'reason', fehler)}"})
        except ValueError as fehler:
            return self._json(400, {"fehler": str(fehler)})
        except OSError:
            return self._json(400, {"fehler": "Import fehlgeschlagen: Datei oder Name ungültig."})
        except RecursionError:
            return self._json(400, {"fehler": "Import fehlgeschlagen: Datei zu tief verschachtelt."})
        except Exception as fehler:
            return self._json(400, {"fehler": f"Import fehlgeschlagen: {str(fehler)[:200] or type(fehler).__name__}"})
        return self._json(404, {"fehler": "nicht gefunden"})

    # ------------------------------------------------------------------ Arena: mehrere Modelle auf einem Datensatz
    def _arena(self, pfad, d):
        from arena import herausforderer as hf
        try:
            if pfad == "/api/arena/start":
                modelle = [str(m) for m in (d.get("modelle") or [])][:30]
                installiert = ollama_status().get("modelle") or []
                if not modelle:
                    return self._json(400, {"fehler": "Wähle mindestens ein Modell."})
                fehlt = [m for m in modelle if m not in installiert]
                if fehlt:
                    status_neu()
                    installiert = ollama_status().get("modelle") or []
                    fehlt = [m for m in modelle if m not in installiert]
                if fehlt and ollama_status().get("fehler"):   # gar nicht erreichbar (nicht nur ohne Modelle)
                    return self._json(502, {"fehler": "Ollama ist nicht erreichbar – prüfe die Verbindung (Zahnrad → Verbindung)."})
                if fehlt:
                    return self._json(400, {"fehler": f"Nicht in deinem Ollama installiert: {', '.join(fehlt)}"})
                dsl = [str(x) for x in (d.get("datensaetze") or [])][:50] or str(d.get("datensatz") or "demo")
                return self._json(200, hf.starte(dsl, modelle, [str(x) for x in (d.get("modi") or [])]))
            if pfad == "/api/arena/stopp":
                hf.stoppe()
                return self._json(200, hf.status())
            if pfad == "/api/arena/datensatz":
                name = str(d.get("name") or "").strip()
                if not name:
                    return self._json(400, {"fehler": "Gib dem Datensatz einen Namen."})
                return self._json(200, hf.speichere_upload(name, d.get("dateien") or []))
            if pfad == "/api/arena/datensatz-loeschen":
                hf.loesche_datensatz(str(d.get("datensatz") or ""))
                return self._json(200, {"ok": True})
            if pfad == "/api/arena/eintraege-loeschen":
                return self._json(200, hf.loesche_eintraege(str(d.get("datensatz") or ""), list(d.get("ids") or [])[:20000]))
            if pfad == "/api/arena/eintraege-wiederherstellen":
                return self._json(200, hf.stelle_wieder_her(str(d.get("datensatz") or ""), str(d.get("token") or "")))
            if pfad == "/api/arena/ergebnis-loeschen":
                hf.loesche_ergebnis(str(d.get("datensatz") or ""), str(d.get("modell") or ""))
                return self._json(200, {"ok": True})
        except RuntimeError as fehler:
            return self._json(409, {"fehler": str(fehler)})
        except ValueError as fehler:
            return self._json(400, {"fehler": str(fehler)})
        except OSError:
            return self._json(400, {"fehler": "Speichern fehlgeschlagen (Name oder Datei ungültig)."})
        return self._json(404, {"fehler": "nicht gefunden"})

    # ------------------------------------------------------------------ Einrichtungsassistent
    def _einrichtung(self, pfad, d):
        if pfad == "/api/einstellungen" or pfad.startswith("/api/einrichtung/"):
            _hw_cache["zeit"] = 0.0   # Hardware nach Änderungen neu erkennen
        if pfad == "/api/einstellungen":
            neu = {}
            if "ollama_url" in d:
                url = str(d["ollama_url"]).strip()
                if not URL_MUSTER.match(url):
                    return self._json(400, {"fehler": "Ungültige Ollama-Adresse (Beispiel: http://localhost:11434)"})
                neu["ollama_url"] = url.rstrip("/")
            if "vram_gb" in d:
                v = d["vram_gb"]
                if v in (None, "", 0):
                    neu["vram_gb"] = None
                else:
                    try:
                        v = float(v)
                    except (TypeError, ValueError):
                        return self._json(400, {"fehler": "Grafikspeicher als Zahl in GB angeben"})
                    if not 0 < v <= 256:
                        return self._json(400, {"fehler": "Grafikspeicher zwischen 1 und 256 GB"})
                    neu["vram_gb"] = v
            if "gpu_status_url" in d:
                u = str(d["gpu_status_url"] or "").strip()
                if u and not re.match(r"^https?://[\w.\-\[\]:]+(/[\w./-]*)?$", u):
                    return self._json(400, {"fehler": "Ungültige Adresse für die Auslastung (Beispiel: http://gpu-server:11435/gpu)"})
                neu["gpu_status_url"] = u
                _gpu_cache[0] = 0
            if "gpu_name" in d:
                neu["gpu_name"] = str(d["gpu_name"] or "").strip()[:60]
            if "pruefer_url" in d:
                u = str(d["pruefer_url"]).strip()
                if not re.match(r"^https?://\S+$", u) or len(u) > 300:
                    return self._json(400, {"fehler": "Ungültige Prüfer-Adresse"})
                neu["pruefer_url"] = u
                # Schlüssel gehört zur Adresse: neue Adresse ohne neuen Schlüssel → alter Schlüssel wird verworfen
                if u != einstellungen.get("pruefer_url") and not d.get("pruefer_key"):
                    neu["pruefer_key"] = ""
            if "pruefer_modell" in d:
                neu["pruefer_modell"] = str(d["pruefer_modell"]).strip()[:200]
            if d.get("pruefer_key"):
                neu["pruefer_key"] = str(d["pruefer_key"]).strip()[:500]
            if d.get("pruefer_key_loeschen"):
                neu["pruefer_key"] = ""
            if "kaggle_nutzer" in d:
                neu["kaggle_nutzer"] = str(d["kaggle_nutzer"] or "").strip()[:100]
            if d.get("kaggle_key"):
                neu["kaggle_key"] = str(d["kaggle_key"]).strip()[:200]
            if d.get("kaggle_loeschen"):
                neu["kaggle_nutzer"], neu["kaggle_key"] = "", ""
            for k in ("modelle_verwalten", "eigener_code", "eingerichtet", "demo", "werkzeug_rechner", "werkzeug_sympy"):
                if k in d:
                    neu[k] = bool(d[k])
            einstellungen.speichere(neu)
            status_neu()
            return self._json(200, einstellungen.oeffentlich())
        if pfad == "/api/einrichtung/gpu-status":
            u = str(d.get("url", "")).strip()
            if not re.match(r"^https?://[\w.\-\[\]:]+(/[\w./-]*)?$", u):
                return self._json(400, {"fehler": "Ungültige Adresse"})
            try:
                with urllib.request.urlopen(u, timeout=4) as r:
                    j = json.load(r)["jetzt"]
                return self._json(200, {"ok": True, "name": j["name"], "vram_max_gb": j["vram_max_gb"], "auslastung": j["auslastung"]})
            except Exception as fehler:
                return self._json(200, {"ok": False, "fehler": str(getattr(fehler, "reason", fehler))[:120]})
        if pfad == "/api/einrichtung/ollama":
            url = str(d.get("url", "")).strip()
            if not URL_MUSTER.match(url):
                return self._json(400, {"fehler": "Ungültige Adresse (Beispiel: http://localhost:11434)"})
            return self._json(200, teste_ollama(url, timeout=5))
        if pfad == "/api/einrichtung/pull":
            m = str(d.get("modell", ""))
            if m not in ERLAUBT:
                return self._json(400, {"fehler": "Im Assistenten lassen sich nur die Basismodelle laden."})
            return self._pull(m)
        if pfad == "/api/einrichtung/gpu-test":
            m = str(d.get("modell", ""))
            if m not in (ollama_status().get("modelle") or []):
                status_neu()
                if m not in (ollama_status().get("modelle") or []):
                    return self._json(400, {"fehler": "Modell ist nicht installiert."})
            t = time.time()
            try:
                kern._post("/api/generate", {"model": m, "prompt": "Antworte mit OK.", "stream": False, "keep_alive": "5m",
                                             "options": {"num_predict": 4, "num_ctx": 2048}}, timeout=600)
                info = next((x for x in kern._get("/api/ps", timeout=5).get("models", []) if x.get("name") == m), {})
            except Exception as fehler:
                return self._json(502, {"fehler": f"Ollama: {fehler}"})
            gesamt, vram = info.get("size", 0), info.get("size_vram", 0)
            return self._json(200, {"modell": m, "sekunden": round(time.time() - t, 1), "gesamt_gb": round(gesamt / 1024**3, 2),
                                    "vram_gb": round(vram / 1024**3, 2), "anteil_gpu": round(vram / gesamt, 2) if gesamt else 0})
        if pfad == "/api/einrichtung/pruefer-test":
            from arena import pruefer
            k = pruefer.schluessel()
            if not k:
                return self._json(400, {"fehler": "Kein Schlüssel hinterlegt."})
            t = time.time()
            try:
                a = pruefer._anfrage({"model": pruefer.modell(), "messages": [{"role": "user", "content": "Antworte nur mit: OK"}],
                                      "max_tokens": 200, "temperature": 0}, k)
                text = (a.get("choices") or [{}])[0].get("message", {}).get("content") or ""
            except urllib.error.HTTPError as fehler:
                return self._json(502, {"fehler": f"API antwortet mit {fehler.code}: {fehler.read().decode(errors='replace')[:160]}"})
            except Exception as fehler:
                return self._json(502, {"fehler": f"API nicht erreichbar: {fehler}"})
            return self._json(200, {"ok": True, "antwort": text.strip()[:80], "sekunden": round(time.time() - t, 1)})
        return self._json(404, {"fehler": "nicht gefunden"})

    # ------------------------------------------------------------------ Hugging Face → Ollama
    def _hf_laden(self, d):
        repo, quant = str(d.get("repo", "")), str(d.get("quant") or "")
        if not re.fullmatch(r"[\w.-]+/[\w.-]+", repo) or (quant and not re.fullmatch(r"[\w.-]+", quant)):
            return self._json(400, {"fehler": "ungültige Angaben"})
        if GESPERRT.search(repo):
            return self._json(400, {"fehler": "Dieses Modell ist für die Demo gesperrt."})
        try:
            datei = next((d for d in hf_repo(repo)["dateien"] if d["quant"].lower() == quant.lower()), None)
        except Exception as fehler:
            return self._json(502, {"fehler": f"Hugging Face nicht erreichbar: {fehler}"})
        if not datei or not datei["passt"]:
            return self._json(400, {"fehler": f"Zu groß für deine Grafikkarte (max. {max_gb():g} GB, siehe Einrichtung)."})
        name = f"hf.co/{repo}" + (f":{quant}" if quant else "")
        self._pull(name)

    def _pull(self, name):
        req = urllib.request.Request(kern.ollama_url() + "/api/pull", data=json.dumps({"model": name, "stream": True}).encode(), headers={"Content-Type": "application/json"})
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            with urllib.request.urlopen(req, timeout=3600) as r:
                for zeile in r:
                    self.wfile.write(zeile)
                    self.wfile.flush()
            status_neu()
            self.wfile.write((json.dumps({"fertig": True, "modell": name}) + "\n").encode())
        except Exception as fehler:
            self.wfile.write((json.dumps({"error": str(fehler)}) + "\n").encode())
        self.wfile.flush()

    # ------------------------------------------------------------------ Chat (frei, live)
    def _chat(self, modell, d):
        verlauf = []
        for m in d.get("nachrichten", [])[-16:]:
            if m.get("role") not in ("user", "assistant"):
                continue
            x = {"role": m["role"], "content": str(m.get("content", ""))[:8000]}
            if m.get("images"):
                x["images"] = [str(b)[:8_000_000] for b in m["images"][:4]]
            verlauf.append(x)
        try:
            temperatur = min(max(float(d.get("temperatur", 0.3)), 0.0), 2.0)
        except (TypeError, ValueError):
            raise ValueError("ungültige Temperatur")
        body = {"model": modell, "stream": True, "think": bool(d.get("think")), "keep_alive": "30m",
                "options": {"temperature": temperatur, "num_ctx": 16384, "num_predict": 8192},  # Stopp-Knopf im Browser bricht früher ab
                "messages": verlauf}
        # System-Prompt: eigener Text, "standard" (Schul-Assistent) oder ganz ohne (leer)
        system = STANDARD_SYSTEM if d.get("system_modus") == "standard" else str(d.get("system") or "").strip()[:6000]
        if system:
            body["messages"] = [{"role": "system", "content": system}] + verlauf
        # Vorab: Ist das Modell schon im Grafikspeicher? (für den Ablauf-Graphen)
        geladen, andere = False, []
        try:
            for m in kern._get("/api/ps", timeout=3).get("models", []):
                (geladen := True) if m.get("name") == modell else andere.append(m.get("name"))
        except Exception:
            pass
        req = urllib.request.Request(kern.ollama_url() + "/api/chat", data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=600) as r:
            self.send_response(200)
            self.send_header("Content-Type", "application/x-ndjson")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            vorab = {"vorab": {"geladen": geladen, "entladen": andere, "bilder": sum(len(x.get("images", [])) for x in verlauf),
                               "nachrichten": len(body["messages"]), "think": body["think"], "optionen": body["options"],
                               "system": system, "modell": modell_info(modell), "url": "POST /api/chat (Ollama, Streaming)"}}
            self.wfile.write((json.dumps(vorab) + "\n").encode())
            self.wfile.flush()
            try:
                for zeile in r:
                    self.wfile.write(zeile)
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                r.close()   # Browser hat abgebrochen → Ollama-Verbindung schließen, das Modell hört auf
                return
            # Nachher: wie viel Grafikspeicher belegt das Modell jetzt?
            nachher = {}
            try:
                for m in kern._get("/api/ps", timeout=3).get("models", []):
                    if m.get("name") == modell:
                        nachher = {"vram_gb": round(m.get("size_vram", 0) / 1024**3, 2), "gesamt_gb": round(m.get("size", 0) / 1024**3, 2),
                                   "kontext": m.get("context_length"), "bleibt_bis": m.get("expires_at")}
            except Exception:
                pass
            self.wfile.write((json.dumps({"nachher": nachher}) + "\n").encode())
            self.wfile.flush()

    # ------------------------------------------------------------------ Live-Pipeline
    def _pipeline(self, modell, a, d, ds_daten):
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

        def senden(**ev):
            self.wfile.write((json.dumps(ev, ensure_ascii=False) + "\n").encode())
            self.wfile.flush()

        def schritt(knoten, f, eingabe=None):
            senden(knoten=knoten, status="laeuft")
            t = time.time()
            try:
                ausgabe, kurz = f()
                senden(knoten=knoten, status="fertig", sekunden=round(time.time() - t, 2), eingabe=eingabe, ausgabe=ausgabe, kurz=kurz)
                return ausgabe
            except Exception as fehler:
                senden(knoten=knoten, status="fehler", sekunden=round(time.time() - t, 2), eingabe=eingabe, ausgabe={"fehler": str(fehler)}, kurz="Fehler")
                raise

        think = bool(d.get("think"))
        ANTWORTEN, EINTRAEGE = ds_daten["antworten"], ds_daten["eintraege"]
        hat_bild = bool(a.get("bild_dateien"))
        bild = bool(d.get("bild")) and hat_bild and kern.kann_bilder(modell)
        antwort_id = d.get("antwort_id") if d.get("antwort_id") in ANTWORTEN else None
        antwort = str(d.get("antwort", ""))[:6000]
        if antwort_id and ANTWORTEN[antwort_id]["schuelerantwort"].strip() != antwort.strip():
            antwort_id = None  # bearbeitet → eigene Antwort, keine Referenz
        e = {**a, "schuelerantwort": antwort}
        try:
            # 1 Laden: nur das, was auch eine Schülerin / ein Schüler sieht (aufgaben.json)
            oeff = {k: a.get(k) for k in ("aufgabe_id", "fach", "thema", "aufgabe", "hinweis", "max_punkte")}
            schritt("laden", lambda: ({"aufgabe": oeff, "schuelerantwort": {"id": antwort_id or "eigene Eingabe", "antwort": antwort,
                                                                                          **({"per_foto_gelesen": d["ocr"]} if d.get("ocr") else {})}},
                                      f"{a['aufgabe_id']} · {a['max_punkte']} P." + (" · mit Abbildung" if a.get("bilder") else "")),
                    eingabe={"datensatz": d.get("datensatz") or "demo", "aufgabe_id": a["aufgabe_id"]})
            # 1b Abbildung: echtes Bild oder nur die Textbeschreibung?
            if hat_bild:
                import os as _os
                groesse = sum(_os.path.getsize(p) for p in a["bild_dateien"] if _os.path.isfile(p))
                if bild:
                    senden(knoten="abbildung", status="fertig", kurz=f"{len(a['bild_dateien'])} Bild an das Modell gesendet ({round(groesse / 1024)} KB)",
                           eingabe={"modus": "Bild"}, ausgabe={"bilder": [_os.path.basename(p) for p in a["bild_dateien"]], "groesse_kb": round(groesse / 1024)})
                else:
                    grund = "Modell kann keine Bilder lesen" if d.get("bild") else "Schalter „Bild senden“ ist aus"
                    senden(knoten="abbildung", status="fertig", kurz="nur als Textbeschreibung – Bild nicht gesehen",
                           eingabe={"modus": "Beschreibung", "grund": grund}, ausgabe={"beschreibungen": [x.get("beschreibung") for x in a.get("abbildungen", [])]})
            else:
                senden(knoten="abbildung", status="aus", kurz="keine Abbildung")
            # 2 Selbst lösen
            eigene = None
            if d.get("loesen") and not d.get("musterloesung"):
                p = kern.prompt_loesen(e)
                eigene = schritt("loesen", lambda: ((x := kern.loesen(modell, e, think=think, bild=bild)), f"Ergebnis: {str(x.get('endergebnis'))[:40]}"),
                                 eingabe={"modell": modell, "think": think, "nachrichten": p})
            else:
                senden(knoten="loesen", status="aus")
            # 3 Werkzeug
            werkzeug = None
            if d.get("werkzeuge") and not d.get("musterloesung"):
                # Code aus dem Datensatz läuft immer (bekannt), selbst eingegebener nur, wenn in der Einrichtung erlaubt
                code_ok = bool(einstellungen.get("eigener_code"))   # Code aus Datensätzen ist genauso fremd wie eigene Eingaben
                werkzeug = schritt("werkzeug", lambda: ((w := pruefe(e["fach"], e.get("aufgabentyp", ""), antwort, eigene, code_erlaubt=code_ok)),
                                                        (w["werkzeug"] or "kein Werkzeug") + ": " + w["zusammenfassung"][:60]),
                                   eingabe={"fach": e["fach"], "eigene_loesung_code": (eigene or {}).get("code"), "testeingaben": (eigene or {}).get("testeingaben")})
            else:
                senden(knoten="werkzeug", status="aus")
            # 4 Bewerten
            beispiele = None
            if d.get("beispiele"):
                beispiele = [x for x in EINTRAEGE if x["fach"] == a["fach"] and x["aufgabe_id"] != a["aufgabe_id"]][::7][:2]
            if d.get("musterloesung"):
                prompt = kern.baue_prompt(e)
                f = lambda: ((r := kern.bewerte_muster(modell, e, think=think, bild=bild)), f"{r.get('punkte')} von {a['max_punkte']} P." if r.get("gueltig") else "kein gültiges Ergebnis (2 Versuche)")
            else:
                prompt = kern.prompt_blind(e, eigene, werkzeug, beispiele)
                f = lambda: ((r := kern.bewerte_blind(modell, e, eigene, werkzeug, beispiele, think=think, bild=bild)), f"{r.get('punkte')} von {a['max_punkte']} P." if r.get("gueltig") else "kein gültiges Ergebnis (2 Versuche)")
            bew = schritt("bewerten", f, eingabe={"modell": modell, "think": think, "json_schema": kern.json_schema(a["max_punkte"]), "nachrichten": prompt})
            if not bew.get("gueltig"):
                senden(knoten="note", status="fehler", kurz="kein gültiges JSON", ausgabe={"roh": bew.get("roh")})
                senden(fertig=True, ergebnis={**bew, "max_punkte": a["max_punkte"], "note": None, "bild": bild},
                       referenz=ANTWORTEN[antwort_id]["lehrkraft"] if antwort_id else None)
                return
            # 5 Note per Code
            prozent = bew["punkte"] / a["max_punkte"]
            schritt("note", lambda: ({"note": bew["note"], "rechnung": f"{bew['punkte']} / {a['max_punkte']} = {round(prozent * 100)} % → Note {bew['note']}",
                                      "notenschluessel": kern.NOTEN_TEXT, "note_laut_modell": bew.get("note_modell")},
                                     f"{round(prozent * 100)} % → Note {bew['note']}" + (" (Modell sagte " + str(bew.get('note_modell')) + ")" if bew.get("note_modell") != bew["note"] else "")),
                    eingabe={"punkte": bew["punkte"], "max_punkte": a["max_punkte"]})
            # 6 Abgabe-Eintrag
            eintrag = {"antwort_id": antwort_id or "eigene-eingabe", "urteil": urteil(bew["punkte"], a["max_punkte"]), "punkte": bew["punkte"], "feedback": bew["feedback"]}
            schritt("ergebnis", lambda: (eintrag, f"„{bew['feedback'][:50]}…“"), eingabe={"antwort_id": antwort_id or "eigene Eingabe"})
            # 7–8 Vergleich mit der Referenz
            if antwort_id:
                ref = ANTWORTEN[antwort_id]
                schritt("referenz", lambda: ({"musterloesung": ref["musterloesung"], "bewertungsraster": ref["bewertungsraster"],
                                              "referenzbewertung": ref["lehrkraft"]}, f"Referenz: {ref['lehrkraft']['punkte']} P. → Note {ref['lehrkraft']['note']}"),
                        eingabe={"datei": "referenz.json", "antwort_id": antwort_id})
                diff = abs(bew["note"] - ref["lehrkraft"]["note"])
                schritt("vergleich", lambda: ({"note_ki": bew["note"], "note_referenz": ref["lehrkraft"]["note"], "abweichung": diff,
                                               "treffer": diff == 0, "fast": diff <= 1},
                                              "Treffer" if diff == 0 else ("1 Note daneben" if diff == 1 else f"{diff} Noten daneben")),
                        eingabe={"ki": eintrag, "referenz": ref["lehrkraft"]})
            else:
                senden(knoten="referenz", status="aus", kurz="eigene Antwort – keine Referenz")
                senden(knoten="vergleich", status="aus", kurz="eigene Antwort – keine Referenz")
            senden(fertig=True, ergebnis={**bew, "max_punkte": a["max_punkte"], "bild": bild}, referenz=ANTWORTEN[antwort_id]["lehrkraft"] if antwort_id else None)
        except Exception as fehler:
            senden(fertig=True, fehler=str(fehler))


if __name__ == "__main__":
    print(f"Modellvergleich auf http://{HOST}:{PORT}  ·  Ollama: {kern.ollama_url()}"
          + ("" if einstellungen.get("eingerichtet") else "  ·  erster Start: Einrichtung im Browser öffnen"), flush=True)
    try:   # Schlüssel in der Umgebung (PRUEFER_API_KEY, KAGGLE_KEY) vor Kindprozessen verbergen: /proc/<pid>/environ nur noch für root lesbar
        import ctypes
        ctypes.CDLL(None).prctl(4, 0, 0, 0, 0)   # PR_SET_DUMPABLE = 0
    except Exception:
        pass
    threading.Thread(target=hardware_erkennen, daemon=True).start()   # Grafikkarte schon beim Start erkennen
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()

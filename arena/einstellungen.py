"""Einstellungen: eine JSON-Datei im Datenordner (im Container: Volume /data).

Reihenfolge: Datei (vom Einrichtungsassistenten geschrieben) > Umgebungsvariable > Standardwert.
Der API-Schlüssel steht nur hier bzw. in der Umgebung und geht nie an den Browser.
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path

WURZEL = Path(__file__).resolve().parent.parent
DATEN_DIR = Path(os.environ.get("DATEN_DIR") or WURZEL / "lokal")
DATEI = DATEN_DIR / "einstellungen.json"

STANDARD = {
    "eingerichtet": False,
    "ollama_url": os.environ.get("OLLAMA_URL") or "http://localhost:11434",
    "vram_gb": None,                  # Grafikspeicher der eigenen Karte (None = keine GPU / unbekannt)
    "gpu_name": os.environ.get("GPU_NAME") or "",
    "pruefer_url": os.environ.get("PRUEFER_URL") or "https://openrouter.ai/api/v1/chat/completions",
    "pruefer_modell": os.environ.get("PRUEFER_MODELL") or "deepseek/deepseek-v4.1-flash",
    "pruefer_key": "",
    "modelle_verwalten": True,        # Modelle laden/löschen über die Oberfläche
    "eigener_code": False,            # Add-on Code-Test: Code in Antworten ausführen (nur vertrauenswürdige Datensätze)
    "werkzeug_rechner": True,         # Add-on Taschenrechner für „Mit Werkzeugen“
    "werkzeug_sympy": False,          # Add-on SymPy (Computer-Algebra) für „Mit Werkzeugen“
    "demo": True,                     # Demo-Datensatz und Referenzergebnisse zeigen
    "kaggle_nutzer": "",              # optional: Kaggle-Zugang für private Datensätze / Wettbewerbe
    "kaggle_key": "",
    "gpu_status_url": os.environ.get("GPU_STATUS_URL") or "",   # optional: Auslastung eines GPU-Servers (arena/gpu_status.py)
}
ENV_KEY = ("PRUEFER_API_KEY", "OPENROUTER_API_KEY")

_sperre = threading.Lock()
_cache: dict | None = None


def lade() -> dict:
    global _cache
    with _sperre:
        if _cache is None:
            daten = {}
            if DATEI.exists():
                try:
                    daten = json.loads(DATEI.read_text(encoding="utf8"))
                except Exception:
                    daten = {}
            _cache = {**STANDARD, **{k: v for k, v in daten.items() if k in STANDARD}}
        return dict(_cache)


def get(schluessel: str):
    return lade().get(schluessel)


def speichere(aenderungen: dict) -> dict:
    global _cache
    neu = {**lade(), **{k: v for k, v in aenderungen.items() if k in STANDARD}}
    with _sperre:
        DATEN_DIR.mkdir(parents=True, exist_ok=True)
        tmp = DATEI.with_suffix(".tmp")
        tmp.write_text(json.dumps(neu, ensure_ascii=False, indent=1), encoding="utf8")
        os.chmod(tmp, 0o600)
        tmp.replace(DATEI)
        _cache = neu
    return neu


def ollama_url() -> str:
    return str(get("ollama_url") or "http://localhost:11434").rstrip("/")


def pruefer_key() -> str:
    for k in ENV_KEY:
        if os.environ.get(k):
            return os.environ[k]
    return get("pruefer_key") or ""


def kaggle_zugang() -> tuple[str, str]:
    return (os.environ.get("KAGGLE_USERNAME") or get("kaggle_nutzer") or "", os.environ.get("KAGGLE_KEY") or get("kaggle_key") or "")


def oeffentlich() -> dict:
    """Für den Browser: alles außer den Schlüsseln."""
    e = lade()
    e.pop("pruefer_key", None)
    e.pop("kaggle_key", None)
    e["kaggle_gesetzt"] = all(kaggle_zugang())
    e["pruefer_key_gesetzt"] = bool(pruefer_key())
    e["pruefer_key_aus_umgebung"] = any(os.environ.get(k) for k in ENV_KEY)
    return e


def max_modell_gb() -> float:
    """Größte Modelldatei, die noch mit Kontext in den Grafikspeicher passt."""
    v = get("vram_gb")
    return round(max(float(v) - 1.5, 1.5), 1) if v else 6.0


def zuruecksetzen():
    """Einstellungsdatei (inkl. Schlüssel) löschen – danach gelten wieder die Standardwerte, der Assistent startet neu."""
    global _cache
    with _sperre:
        DATEI.unlink(missing_ok=True)
        _cache = None

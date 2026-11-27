#!/usr/bin/env python3
"""GPU-Auslastung über nvidia-smi: jetzt + Verlauf der letzten 3 Minuten. Nur Standardbibliothek.

In der App: misst lokal, wenn nvidia-smi im Container verfügbar ist (GPU-Modus, siehe compose.gpu.yml).
Auf einem GPU-Server im Netz: diese eine Datei kopieren und starten, dann im Assistenten die Adresse eintragen.

    python3 gpu_status.py --host 0.0.0.0 --port 11435      → http://<server>:11435/gpu

Antwort: {"jetzt": {"name", "auslastung", "vram_gb", "vram_max_gb", "watt", "temp", "t"}, "verlauf": [[t, auslastung, vram_gb, watt], …]}
Gibt nur Messwerte heraus, nimmt keine Befehle an. Port nur im vertrauenswürdigen Netz (z. B. VPN) freigeben.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import threading
import time
from collections import deque

FELDER = "name,utilization.gpu,memory.used,memory.total,power.draw,temperature.gpu"


def verfuegbar() -> bool:
    return shutil.which("nvidia-smi") is not None


def messe() -> dict | None:
    try:
        aus = subprocess.run(["nvidia-smi", f"--query-gpu={FELDER}", "--format=csv,noheader,nounits"],
                             capture_output=True, text=True, timeout=5).stdout.strip().splitlines()
        name, last, benutzt, gesamt, watt, temp = [x.strip() for x in aus[0].split(",")]
    except Exception:
        return None
    zahl = lambda x: float(x) if x.replace(".", "", 1).isdigit() else 0.0
    return {"name": name, "auslastung": zahl(last), "vram_gb": round(zahl(benutzt) / 1024, 2), "vram_max_gb": round(zahl(gesamt) / 1024, 1),
            "watt": zahl(watt), "temp": zahl(temp), "t": time.time()}


class Messung:
    """Misst jede Sekunde in einem Hintergrund-Thread – aber nur, solange jemand abfragt (spart Strom im Leerlauf)."""

    def __init__(self, sekunden: int = 180, ruhe: float = 60):
        self.verlauf: deque = deque(maxlen=sekunden)
        self.jetzt: dict | None = None
        self.letzte_abfrage = 0.0
        self.ruhe = ruhe
        self._sperre = threading.Lock()
        self._thread: threading.Thread | None = None

    def _lauf(self):
        while time.time() - self.letzte_abfrage < self.ruhe:
            m = messe()
            if m:
                with self._sperre:
                    self.jetzt = m
                    self.verlauf.append([round(m["t"], 1), m["auslastung"], m["vram_gb"], m["watt"]])
            time.sleep(1)
        self._thread = None

    def status(self) -> dict | None:
        self.letzte_abfrage = time.time()
        if self._thread is None:
            self._thread = threading.Thread(target=self._lauf, daemon=True)
            self._thread.start()
            if self.jetzt is None:
                time.sleep(1.2)
        with self._sperre:
            return {"jetzt": self.jetzt, "verlauf": list(self.verlauf)} if self.jetzt else None


if __name__ == "__main__":
    import argparse
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--host", default="127.0.0.1", help="z. B. 0.0.0.0 oder die VPN-Adresse des Servers")
    ap.add_argument("--port", type=int, default=11435)
    a = ap.parse_args()
    if not verfuegbar():
        raise SystemExit("nvidia-smi nicht gefunden")
    messung = Messung()

    class H(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):
            d = messung.status() if self.path.split("?")[0] == "/gpu" else None
            body = json.dumps(d or {"fehler": "nicht gefunden"}).encode()
            self.send_response(200 if d else 404)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    print(f"GPU-Status auf http://{a.host}:{a.port}/gpu", flush=True)
    ThreadingHTTPServer((a.host, a.port), H).serve_forever()

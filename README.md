# Modellvergleich

**Benchmark für lokale Sprachmodelle – auf deinen eigenen Daten.**

Mehrere Modelle (über [Ollama](https://ollama.com)) treten auf denselben Daten an: Sie **beantworten Fragen** aus jedem Themengebiet
oder **bewerten Antworten** gegen eine Referenzbewertung – direkt, mit Reasoning, mit Werkzeugen (Taschenrechner, SymPy), mit Bildern
oder mit Musterlösung. Die Rangliste zeigt, welches Modell am häufigsten richtig liegt, wie schnell es ist und wie viel Grafikspeicher es braucht.
Alles läuft auf deinem Rechner; ein Assistent führt beim ersten Start durch die Einrichtung.

> **English:** Modellvergleich ("model comparison") benchmarks local LLMs served by Ollama on your own data – Q&A datasets
> (CSV, Excel, JSON, SQLite, Hugging Face, Kaggle, PostgreSQL/MySQL) or grading tasks. Compare models side by side, with or without
> reasoning and tools, see accuracy, speed and VRAM. Start with `docker compose up -d` and open http://localhost:8720. The UI is in German.

Mitgeliefert ist ein ausgedachter Demo-Datensatz (5 Fächer, 26 Aufgaben, 84 Antworten, 9 Abbildungen, 6 Fallen).
Er ist klein: Ergebnisse darauf sind Anhaltspunkte, keine allgemeingültige Rangliste.

## Schnellstart

1. **Docker installieren**
   - **Windows / Mac:** [Docker Desktop](https://docs.docker.com/desktop/) installieren und starten (Windows: mit WSL2, ist Standard).
   - **Linux:** [Docker Engine](https://docs.docker.com/engine/install/) mit dem Compose-Plugin.
2. **Projekt holen** – `git clone` oder als ZIP herunterladen und entpacken.
3. **Starten** – im Projektordner (Windows: PowerShell, Mac/Linux: Terminal):

   ```bash
   docker compose up -d
   ```

   Beim ersten Mal werden die Images gebaut bzw. geladen (einige Minuten).
4. **Öffnen:** **http://localhost:8720** – der Einrichtungsassistent erscheint als Fenster: Ollama verbinden, Hardware, Modelle
   installieren, Grafikkarten-Test, optional Prüfer, Sicherheit. Später findest du alles über das **Zahnrad** oben rechts.

| Was | Befehl |
|---|---|
| Anhalten | `docker compose down` |
| Aktualisieren (nach `git pull`) | `docker compose up -d --build` |
| Logs ansehen | `docker compose logs -f app` |
| Neu anfangen (Einstellungen, eigene Daten, Ergebnisse; Modelle bleiben) | Zahnrad → Zurücksetzen |
| Alles inkl. Modelle und Ergebnisse löschen | `docker compose down -v` |

Modelle und Ergebnisse liegen in Docker-Volumes und bleiben beim Anhalten und Aktualisieren erhalten.

### Grafikkarte nutzen

Ohne weitere Schritte läuft Ollama im Container auf der CPU – das funktioniert überall, ist aber langsam. Schneller:

| System | So geht's |
|---|---|
| **Windows + NVIDIA** | aktueller NVIDIA-Treiber + Docker Desktop (WSL2). In der Datei `.env` das `#` vor `COMPOSE_FILE=…` entfernen, dann `docker compose up -d`. |
| **Linux + NVIDIA** | einmalig das [NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html) installieren (der Assistent zeigt die Befehle), dann wie bei Windows die Zeile in `.env` aktivieren. |
| **Intel, AMD, Prozessorgrafik (APU), Mac** | Docker-Container können diese Grafik nicht nutzen. Stattdessen [Ollama direkt installieren](https://ollama.com/download) und im Assistenten „Ollama auf diesem Rechner“ wählen. Der Assistent erkennt die Grafik (unter Windows/Mac über den Browser) und führt mit Knöpfen durch Download, Verbinden und GPU-Test. Unter Linux muss Ollama dafür mit `OLLAMA_HOST=0.0.0.0` laufen. Den mitgelieferten Ollama-Container brauchst du dann nicht: in `.env` das `#` vor `COMPOSE_FILE=compose.yml:compose.ohne-ollama.yml` entfernen – am besten vor dem ersten Start, das spart den Download. |
| **Stärkerer Rechner im Netz** | dort Ollama mit `OLLAMA_HOST=0.0.0.0:11434` starten (optional `python3 arena/gpu_status.py --host 0.0.0.0` für die Auslastung, Port 11435) und im Assistenten „GPU-Server im Netz verwenden“ wählen – nur im vertrauenswürdigen Netz. Den lokalen Ollama-Container dann wie oben per `.env` weglassen. |

Der Schritt **GPU-Test** im Assistenten prüft, ob die Modelle wirklich auf der Grafikkarte laufen. Mit reiner Prozessorgrafik
(z. B. Intel Iris Xe) laufen die Modelle meist auf dem Prozessor – kleine Modelle (2–4 B) funktionieren trotzdem gut.

### Häufige Probleme

- **Seite lädt nicht:** läuft Docker Desktop? `docker compose ps` zeigt beide Container (`app`, `ollama`). Port belegt? In `.env` `PORT=8721` setzen.
- **„Ollama nicht erreichbar“:** Zahnrad → Verbindung; der Assistent sucht an den üblichen Stellen.
- **Modell läuft auf der CPU:** GPU-Zeile in `.env` aktiviert und `docker compose up -d` erneut ausgeführt? Bei wenig Grafikspeicher ein kleineres Modell wählen.
- **Windows:** Dateien beim Klonen nicht in CRLF umwandeln lassen – das Repo erzwingt LF bereits per `.gitattributes`.

## Die App

| Tab | Wofür |
|---|---|
| **Vergleich** | Modelle und einen oder mehrere Datensätze wählen, Modi wählen, antreten lassen. Rangliste je Datensatz oder Gesamt-Rangliste über mehrere. Läufe setzen nach Abbruch fort, schon Gerechnetes wird übersprungen. |
| **Chat** | Frei chatten oder eine Frage/Aufgabe aus dem Datensatz stellen – ein Modell oder mehrere nebeneinander. Rechts der Ablauf Schritt für Schritt. |
| **Modelle** | Installierte Modelle mit Größe, Kontext, Fähigkeiten und Ergebnis; neue Modelle suchen und installieren (Ollama-Bibliothek, Hugging Face). Downloads laufen im Hintergrund weiter (höchstens zwei gleichzeitig), den Fortschritt zeigt das Download-Symbol oben rechts. |
| **Daten** | Datensätze ansehen, laden, einzelne Einträge auswählen und löschen (mit Rückgängig). |

Oben: Auslastung der Grafikkarte und der Schalter **Demo**: an = Demo-Datensatz sichtbar, aus = nur eigene Datensätze.

**Modi**

| Datensatz | Modus | Was das Modell bekommt |
|---|---|---|
| Fragen | Direkt | die Frage (mit Antwortmöglichkeiten, Kontext, Bild) |
| Fragen | Mit Werkzeugen | dazu die eingeschalteten Werkzeuge (Taschenrechner, optional SymPy), die es selbst aufrufen darf |
| Bewerten | Nur Aufgabe | Aufgabe, Hinweis, Punktzahl – keine Lösung |
| Bewerten | Mit Werkzeugen | löst selbst, dazu Rechen-Check und – nur wenn eingeschaltet – Code-Test |
| Bewerten | Mit Bild | das echte Bild statt der Beschreibung (Aufgaben mit Abbildung, Modelle mit Bildverständnis) |
| Bewerten | Mit Musterlösung | Musterlösung + Bewertungsraster – die Obergrenze |

**Werkzeuge (Add-ons)** – im Vergleich unter „Modi → Werkzeuge ›“ ein- und ausschalten, dort mit Beschreibung, Beispiel und Sicherheitshinweis:

| Add-on | Für | Was es kann | Standard |
|---|---|---|---|
| Taschenrechner | Fragen, Bewerten | rechnet Ausdrücke exakt (+ − × ÷, Potenzen, Wurzeln); kein Code | an |
| SymPy | Fragen | Computer-Algebra: Gleichungen und Systeme lösen, vereinfachen, ableiten, integrieren – in eigenem, begrenztem Prozess | aus |
| Code-Test | Bewerten | führt Python-Code aus Antworten mit Testeingaben aus (nur vertrauenswürdige Datensätze) | aus |

Nicht jedes Modell kann Werkzeuge aufrufen; dann antwortet es ohne (in der Rangliste mit * markiert).

## Eigene Daten

**Quellen:** Dateien per Drag & Drop (CSV, Excel .xlsx, JSON, JSONL, SQLite, Bilder), **Hugging Face Datasets** (mit Vorlagen für
GSM8K, MMLU, ARC, CommonsenseQA), **Kaggle** (Suche; öffentliche Datensätze ohne Anmeldung, private mit API-Token unter Zahnrad → Kaggle),
**Link** (CSV/Excel/JSON im Netz, Google-Sheets-Freigabelink) und **PostgreSQL / MySQL** (nur eine lesende SELECT-Abfrage, höchstens
5000 Zeilen, Zugang wird nicht gespeichert). Erkennt die App Frage und Antwort sicher, wird sofort übernommen („Spalten anpassen“ bleibt möglich);
sonst öffnet sich die Spaltenzuordnung mit Vorschau.

**1. Fragen (jedes Thema)** – die Modelle beantworten die Fragen selbst. Eine Frage pro Zeile. Erkannte Spalten: Frage (`frage`, `question`,
`prompt`, `input`), Soll-Antwort (`antwort`, `answer`, `output`, `target`, `label`, `correct_answer`), optional `thema`, `kontext`, `bild`,
Antwortmöglichkeiten (`optionen`/`choices` oder Spalten `A`–`E`) und `wertung`. Gewertet wird automatisch: Multiple Choice, Zahl (1 % Toleranz),
kurze Antwort („enthält“) oder freie Antwort (per Prüfer-KI, ohne Schlüssel geschätzt). Beispiele: `vorlagen/beispiel.csv`, `vorlagen/beispiel.jsonl`.

**2. Bewerten** – die Modelle korrigieren fremde Antworten gegen eine Referenz. Pro Fach zwei JSON-Dateien mit gleicher `meta.fach_id`,
optional Bilder (Vorlagen: `daten/<fach>/`):

- `aufgaben.json`: `meta` (`fach`, `fach_id`) und `aufgaben[]` mit `id`, `aufgabe`, `max_punkte`, optional `hinweis`, `thema`, `klasse`, `aufgabentyp`,
  `abbildungen[]` (`datei`, `beschreibung`) und `schuelerantworten[]` (`id`, `antwort`, optional `falle`)
- `referenz.json`: `meta` und `loesungen[]` mit `id`, `max_punkte`, `musterloesung`, `bewertungskriterien[]` (`text`, `punkte`) und `bewertungen[]` (`antwort_id`, `punkte`, `referenz_feedback`)

Antworten mit `falle` zählen in der Spalte „Fallen“: abgewehrt, wenn das Modell nicht Note 1 oder 2 vergibt.
Die Note berechnet der Code aus den Punkten: ≥ 87 % → 1, ≥ 73 % → 2, ≥ 59 % → 3, ≥ 45 % → 4, ≥ 18 % → 5, sonst 6.

Datensätze von Hugging Face, Kaggle, Links oder Datenbanken lädst du selbst herunter; sie unterliegen den Lizenzen ihrer Anbieter.
Namen wie GSM8K, MMLU, Ollama, Qwen oder Gemma gehören ihren Inhabern.

## Aufbau

| Datei | Zweck |
|---|---|
| `server.py` | Webserver (Python-Standardbibliothek) |
| `arena/app.html` | Oberfläche: Vergleich, Chat, Modelle, Daten |
| `arena/einrichtung.html` | Einrichtungsassistent und Einstellungen (als Fenster in der App) |
| `arena/herausforderer.py` | Datensätze, Vergleichsläufe im Hintergrund, Auswertung |
| `arena/fragen.py` | Fragen-Benchmark: Spaltenzuordnung, beantworten (auch mit Rechner), werten |
| `arena/importe.py` | Daten-Adapter: Excel, SQLite, Link, Hugging Face, Kaggle, PostgreSQL, MySQL |
| `arena/kern.py` | Prompts, Notenschlüssel, Ollama-Client, Kennzahlen |
| `arena/werkzeuge.py` | Add-ons: Rechen-Check, Taschenrechner, SymPy und Code-Test (Zeit-, Speicher- und Prozesslimit) |
| `arena/pruefer.py` | optionaler Prüfer über eine OpenAI-kompatible API |
| `arena/einstellungen.py` | Einstellungen (Docker-Volume `/data`) |
| `arena/gpu_status.py` | Auslastung über nvidia-smi – auch eigenständig für einen GPU-Server |
| `daten/`, `vorlagen/` | Demo-Datensatz und Beispieldateien |
| `compose.yml`, `compose.gpu.yml`, `compose.ohne-ollama.yml`, `.env` | Start mit Docker, optional mit NVIDIA-Grafikkarte oder ohne Ollama-Container |
| `werkzeuge/pruefe_leaks.sh` | prüft vor einem Commit auf private Angaben (eigene Begriffe in `.leak-denylist`) |

Ohne Docker: `python3 server.py` (Python 3.10+), Ollama unter `http://localhost:11434`.
Für den Datenbank-Import und das Add-on SymPy dann zusätzlich `pip install "psycopg[binary]" pymysql sympy`.

## Sicherheit und Datenschutz

- Kein Login – erreichbar nur unter `127.0.0.1`, nur JSON-Anfragen derselben Seite, nicht in fremde Seiten einbettbar.
  Fürs Netz nur hinter Reverse-Proxy mit Login oder VPN (`BIND`, `ERLAUBTE_HOSTS` in `.env`).
- **Code in Antworten** (Add-on Code-Test) wird nur ausgeführt, wenn du es einschaltest (Werkzeuge bzw. Zahnrad → Sicherheit) –
  dann kurz begrenzt (3 s, 512 MB, keine weiteren Prozesse), aber mit den Rechten der App. Nur mit Datensätzen aus vertrauenswürdiger Quelle einschalten.
- Die Modelle laufen lokal in Ollama. **Ausnahme Prüfer:** Ist er eingerichtet, gehen Aufgabe, Musterlösung und Antworten an den eingestellten
  Cloud-Anbieter. Keine echten personenbezogenen Daten verwenden.
- API-Schlüssel (Prüfer, Kaggle) bleiben auf dem Server (`/data/einstellungen.json`, nur für die App lesbar) und werden nie an den Browser gegeben.
  Der Prüfer-Schlüssel gilt nur für die eingestellte Adresse.

## Lizenz

Code: MIT (`LICENSE`). Demo-Daten: CC BY 4.0 (`LICENSE-DATA.md`).

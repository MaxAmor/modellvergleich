"""Werkzeuge ohne KI: prüfen, was sich sicher prüfen lässt.

rechen_check(text)   – Mathe: rechnet Gleichungsketten nach und prüft, ob Umformungen die Lösung erhalten
code_test(...)       – Informatik: führt Schüler-Code und den Code der eigenen Lösung mit Testeingaben aus und vergleicht

Taschenrechner und Rechen-Check kommen ohne Bibliotheken aus; das optionale Add-on SymPy (Computer-Algebra) läuft in einem eigenen Prozess.
"""
from __future__ import annotations

import ast
import json
import math
import os
import re
import subprocess
import sys
import tempfile

try:
    import resource
except ImportError:      # Windows ohne Container
    resource = None

# ------------------------------------------------------------------ Rechen-Check
_VAR = "xythab"  # mögliche Variablen (eine Variable pro Zeile)


def _normalisiere(z: str) -> str:
    z = z.replace("$", "").replace("\\%", "%")
    z = re.sub(r"(?<=[\d)])\s*:\s*(?=[\d(])", " / ", z)  # 25 : 5 → Division (Doppelpunkt sonst Text)
    for _ in range(3):
        z = re.sub(r"\\[dt]?frac\{([^{}]*)\}\{([^{}]*)\}", r"((\1)/(\2))", z)
    z = re.sub(r"\\sqrt\{([^{}]*)\}", r"sqrt(\1)", z)
    z = re.sub(r"\\(mathrm|text)\{[^{}]*\}", "", z)
    rep = {"\\cdot": "*", "\\times": "*", "·": "*", "×": "*", "÷": "/", ":": " ", "−": "-", "–": "-", "²": "^2", "³": "^3",
           "{,}": ".", "\\,": "", "\\;": "", "\\approx": "≈", "\\Rightarrow": "⇒", "√": "sqrt", "€": "", "%": "/100"}
    for a, b in rep.items():
        z = z.replace(a, b)
    z = re.sub(r"(\d),(\d)", r"\1.\2", z)  # Dezimalkomma
    z = z.replace("^", "**").replace("{", "(").replace("}", ")")
    return z


def _implizit(ausdruck: str) -> str:
    a = re.sub(r"(\d|\))\s*(?=[a-z(])", r"\1*", ausdruck)
    a = re.sub(r"([a-z])\s*(?=\()", lambda m: m.group(1) + ("" if m.group(1) == "t" and False else "*"), a)
    return a.replace("sqrt*(", "sqrt(")


_ERLAUBT = re.compile(r"^[\d\s+\-*/().a-z]*$")


def _wert(ausdruck: str, var: str | None, x: float = 0.0):
    a = _implizit(ausdruck.strip())
    if not a or not _ERLAUBT.match(a):
        return None
    namen = set(re.findall(r"[a-z]+", a)) - {"sqrt"}
    if namen - ({var} if var else set()):
        return None
    try:
        baum = ast.parse(a, mode="eval")
    except SyntaxError:
        return None
    for k in ast.walk(baum):
        if not isinstance(k, (ast.Expression, ast.BinOp, ast.UnaryOp, ast.Constant, ast.Name, ast.Load, ast.Call,
                              ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Pow, ast.USub, ast.UAdd)):
            return None
        if isinstance(k, ast.Call) and not (isinstance(k.func, ast.Name) and k.func.id == "sqrt"):
            return None
        if isinstance(k, ast.BinOp) and isinstance(k.op, ast.Pow):
            # Schul-Mathe: nur kleine, feste Exponenten (sonst blockiert z. B. 9**9**9**9 den Server)
            e = k.right
            if isinstance(e, ast.UnaryOp) and isinstance(e.op, (ast.USub, ast.UAdd)):
                e = e.operand
            if not (isinstance(e, ast.Constant) and isinstance(e.value, (int, float)) and abs(e.value) <= 12):
                return None
    try:
        return float(eval(compile(baum, "<rechen>", "eval"), {"__builtins__": {}}, {"sqrt": math.sqrt, var or "_": x}))
    except Exception:
        return None


def _zahl(v: float) -> str:
    return (f"{v:.4g}" if abs(v) < 1e6 else f"{v:.3e}").replace(".", ",")


def _loese_linear(links: str, rechts: str, var: str):
    """Lösung von links = rechts, falls linear in var. None, wenn nicht linear/auswertbar."""
    f = lambda x: (_wert(links, var, x) or 0) - (_wert(rechts, var, x) or 0)
    if _wert(links, var, 0) is None or _wert(rechts, var, 0) is None:
        return None
    f0, f1, f2 = f(0), f(1), f(2)
    if abs((f2 - f1) - (f1 - f0)) > 1e-9:
        return None  # nicht linear
    m = f1 - f0
    if abs(m) < 1e-12:
        return None
    return -f0 / m


def rechen_check(text: str) -> dict:
    befunde = []
    vorher = None  # (zeile, lösung) der letzten linearen Gleichung
    for nr, roh in enumerate(text.splitlines(), 1):
        zeile = roh.split("|")[0]
        if "=" not in zeile and "≈" not in zeile:
            continue
        z = _normalisiere(zeile).lower()
        z = re.sub(r"^\s*\(?[a-z0-9]\)\s+", "", z)  # Aufzählung (a), 1.
        teile = [t.strip(" .,;") for t in re.split(r"=|≈|⇒", z)]
        teile = [(m.group(0).strip() if (m := re.search(r"(?:[\d\s+\-*/().]|sqrt|(?<![a-zäöüß])[a-z](?![a-zäöüß]))+$", t)) else "") for t in teile]
        if len(teile) < 2:
            continue
        var = next((v for v in _VAR if any(re.search(rf"(?<![a-z]){v}(?![a-z])", t) for t in teile)), None)
        # a) reine Rechenketten (ohne Variable): jeden Teil mit dem nächsten vergleichen
        werte = [(t, _wert(t, None)) for t in teile if not var or not re.search(rf"(?<![a-z]){var}(?![a-z])", t)]
        werte = [(t, w) for t, w in werte if w is not None]
        paare = []
        for (ta, wa), (tb, wb) in zip(werte, werte[1:]):
            tol = max(0.011 * abs(wa), 0.006) if "≈" in z or "rund" in roh.lower() else max(0.005 * abs(wa), 1e-6)
            paare.append((abs(wa - wb) <= tol, wa, wb))
        if paare:
            schlecht = next((p for p in paare if not p[0]), None)
            kurz = re.sub(r"\s+", " ", roh.replace("$", "")).strip()[:55]
            if schlecht:
                befunde.append({"ok": False, "zeile": nr, "text": f"Rechenfehler in Zeile {nr} („{kurz}“): {_zahl(schlecht[1])} ≠ {_zahl(schlecht[2])}"})
            else:
                befunde.append({"ok": True, "zeile": nr, "text": f"Zeile {nr} stimmt („{kurz}“)"})
        # b) lineare Gleichung in einer Variable: bleibt die Lösung über die Umformungen gleich?
        if var and len(teile) == 2:
            l = _loese_linear(teile[0], teile[1], var)
            if l is not None:
                if vorher and abs(vorher[1] - l) > 1e-6:
                    befunde.append({"ok": False, "zeile": nr, "text": f"Umformungsfehler: Zeile {vorher[0]} hat {var} = {_zahl(vorher[1])}, Zeile {nr} („{roh.strip()[:30]}“) hat {var} = {_zahl(l)}"})
                elif vorher:
                    befunde.append({"ok": True, "zeile": nr, "text": f"Zeile {nr}: Umformung korrekt ({var} = {_zahl(l)})"})
                vorher = (nr, l)
    fehler = [b for b in befunde if not b["ok"]]
    if not befunde:
        zus = "Keine nachrechenbaren Gleichungen gefunden."
    elif fehler:
        zus = f"{len(fehler)} Rechen- oder Umformungsfehler gefunden."
    else:
        zus = f"Alle {len(befunde)} geprüften Rechnungen stimmen."
    return {"werkzeug": "Rechen-Check", "befunde": befunde, "zusammenfassung": zus}


# ------------------------------------------------------------------ Code-Test
def ist_code(text: str) -> bool:
    return bool(re.search(r"^\s*(def |for |while |print\(|\w+\s*=\s*input\()", text or "", re.M))


def _laufe(code: str, eingabe: str, funktion: str | None) -> str:
    """Startet den Code und liefert die beobachtbare Ausgabe: letzte gedruckte Ausgabe, sonst Rückgabewert der Funktion."""
    if funktion:
        m = re.search(rf"def\s+{funktion}\s*\(([^)]*)\)", code)
        ohne_param = m is not None and not m.group(1).strip()
        try:
            arg = repr(ast.literal_eval(eingabe))
        except Exception:
            arg = repr(eingabe)
        aufruf = f"{funktion}()" if ohne_param else f"{funktion}({arg})"
        rahmen = code + f"\n\nimport sys as _s\n_r = {aufruf}\n_s.stdout.write('\\n@@' + (_r if isinstance(_r, str) else repr(_r)))\n"
        stdin = ""
    else:
        rahmen, stdin = code, (eingabe or "") + "\n"
    ordner = tempfile.mkdtemp(prefix="codetest-")
    datei = os.path.join(ordner, "programm.py")
    with open(datei, "w", encoding="utf8") as f:
        f.write(rahmen)
    try:
        p = subprocess.Popen([sys.executable, "-I", "-S", datei], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                             cwd=ordner, env={"PATH": "/usr/bin:/bin", "PYTHONIOENCODING": "utf-8"}, close_fds=True,
                             preexec_fn=_grenzen if resource else None)
        try:
            stdout, stderr = p.communicate(stdin, timeout=3)
        except subprocess.TimeoutExpired:
            _beende(p)
            raise
        finally:
            _beende(p)   # auch abgespaltene Kindprozesse der Gruppe beenden
        r = subprocess.CompletedProcess(p.args, p.returncode, stdout, stderr)
        aus = r.stdout.strip()
        if r.returncode != 0 and not aus:
            return "Fehler: " + (r.stderr.strip().splitlines() or ["?"])[-1][:80]
        if funktion:
            gedruckt, _, rueck = aus.rpartition("@@")
            if rueck == "None" and gedruckt.strip():
                rueck = gedruckt.split()[-1].split(":")[-1]
            return rueck
        return aus.split()[-1].split(":")[-1] if aus else ""
    except subprocess.TimeoutExpired:
        return "Zeitlimit (Endlosschleife?)"
    except Exception as fehler:
        return f"Fehler: {fehler}"
    finally:
        try:
            os.remove(datei)
            os.rmdir(ordner)
        except OSError:
            pass


def _beende(p):
    import signal
    try:
        os.killpg(p.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        pass


def _grenzen():
    """Läuft im Kindprozess vor dem Start: CPU-Zeit, Speicher, Dateigröße begrenzen, keine weiteren Prozesse."""
    os.setsid()
    resource.setrlimit(resource.RLIMIT_CPU, (3, 3))
    resource.setrlimit(resource.RLIMIT_AS, (512 * 1024**2, 512 * 1024**2))
    resource.setrlimit(resource.RLIMIT_FSIZE, (1024**2, 1024**2))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))
    resource.setrlimit(resource.RLIMIT_NPROC, (0, 0))   # kein fork: nichts überlebt das Zeitlimit


def code_test(schueler_code: str, eigener_code: str, eingaben: list) -> dict:
    """Führt beide Programme mit denselben Eingaben aus. Funktion oder Skript – jede Seite so, wie sie geschrieben ist."""
    fs = re.search(r"def\s+(\w+)\s*\(", schueler_code or "")
    fe = re.search(r"def\s+(\w+)\s*\(", eigener_code or "")
    # Skript mit input() wird als Skript gestartet, sonst die (erste) Funktion aufgerufen
    f_s = None if re.search(r"input\(", schueler_code or "") or not fs else fs.group(1)
    f_e = None if re.search(r"^[^#\n]*input\(", eigener_code or "", re.M) or not fe else fe.group(1)
    befunde = []
    if not f_s and "input(" not in (schueler_code or ""):
        eingaben = [""]  # Programm ohne Eingabe: genau einmal laufen lassen (Testeingaben des Modells ignorieren)
    for e in [re.split(r"\s+[(\[]|\s+[-–→]\s|\s*#", str(x))[0].strip() for x in (eingaben or [])][:6]:
        s = _laufe(schueler_code, e, f_s)
        r = _laufe(eigener_code, e, f_e) if eigener_code else None
        ok = r is not None and s == r
        befunde.append({"ok": ok, "text": (f"Eingabe {e!r}: " if e else "Programmlauf: ") + f"Schüler → {s}" + (f" · eigene Lösung → {r}" if r is not None else "")})
    fehler = sum(not b["ok"] for b in befunde)
    zus = (f"{len(befunde) - fehler} von {len(befunde)} Tests liefern dasselbe wie die eigene Lösung." if befunde
           else "Keine Testeingaben vorhanden.")
    if befunde and fehler == len(befunde):
        zus += " Achtung: Wenn alle Tests abweichen, kann auch die eigene Lösung falsch sein."
    return {"werkzeug": "Code-Test", "befunde": befunde, "zusammenfassung": zus}


def code_teil(text: str) -> str:
    """Nur den Code aus einer gemischten Antwort („(a) 10 (b) <Code>“): ab der ersten Code-Zeile."""
    m = re.search(r"```(?:python)?\n(.*?)```", text or "", re.S)
    if m:
        return m.group(1)
    zeilen = (text or "").splitlines()
    for i, z in enumerate(zeilen):
        if re.match(r"^\s*(def |for |while |if |import |print\(|[A-Za-z_]\w*\s*=[^=])", z):
            return "\n".join(zeilen[i:])
    return text


def pruefe(fach: str, aufgabentyp: str, antwort: str, eigene: dict | None, code_erlaubt: bool = False) -> dict:
    """code_erlaubt=False: selbst eingegebener Code wird nicht ausgeführt (Schalter in den Einstellungen)."""
    eigene = eigene or {}
    if ist_code(antwort) and (fach == "Informatik" or aufgabentyp == "code"):
        if not code_erlaubt:
            return {"werkzeug": None, "befunde": [], "zusammenfassung": "Code-Test ist ausgeschaltet (Einstellungen → Sicherheit → Code ausführen)."}
        return code_test(code_teil(antwort), code_teil(eigene.get("code", "")), eigene.get("testeingaben", []))
    if fach in ("Mathe", "Informatik"):
        r = rechen_check(antwort)
        if r["befunde"]:
            return r
        if fach == "Mathe":
            return r
    idee = " Idee: LanguageTool für Rechtschreibung und Grammatik." if fach in ("Deutsch", "Englisch") else ""
    return {"werkzeug": None, "befunde": [], "zusammenfassung": f"Für diese Aufgabe gibt es kein passendes Werkzeug.{idee}"}


# ------------------------------------------------------------------ Add-on SymPy (Computer-Algebra)
CAS_AUFGABEN = ("berechne", "vereinfache", "loese", "ableiten", "integriere", "faktorisiere")
_CAS_FUNKTIONEN = {"sqrt", "sin", "cos", "tan", "asin", "acos", "atan", "exp", "log", "ln", "pi", "E", "Abs", "factorial", "binomial", "floor", "ceiling", "oo"}
_CAS_ZEICHEN = re.compile(r"^[0-9A-Za-z+\-*/^().,=;\s]*$")

_CAS_KIND = r"""
import json, sys
import sympy as sp
from sympy.parsing.sympy_parser import parse_expr, standard_transformations, implicit_multiplication_application, convert_xor
d = json.loads(sys.stdin.read())
funk = {n: getattr(sp, n) for n in ("sqrt", "sin", "cos", "tan", "asin", "acos", "atan", "exp", "log", "pi", "E", "Abs", "factorial", "binomial", "floor", "ceiling", "oo")}
funk["ln"] = sp.log
g = {"Integer": sp.Integer, "Float": sp.Float, "Rational": sp.Rational, "Symbol": sp.Symbol, **funk}
tr = standard_transformations + (implicit_multiplication_application, convert_xor)
lies = lambda t: parse_expr(t, global_dict=g, local_dict={}, transformations=tr)
a, aus, var = d["aufgabe"], d["ausdruck"], d.get("variable") or None
v = sp.Symbol(var) if var else None
if a == "loese":
    gl = [t for t in aus.replace(";", ",").split(",") if t.strip()] if aus.count("=") > 1 else [aus]
    eq = [sp.Eq(lies(t.split("=")[0]), lies(t.split("=")[1])) if "=" in t else lies(t) for t in gl]
    frei = sorted(set().union(*[e.free_symbols for e in eq]), key=str)
    r = sp.solve(eq if len(eq) > 1 else eq[0], [v] if v else frei, dict=True)
elif a == "ableiten":
    e = lies(aus); r = sp.diff(e, v or sorted(e.free_symbols, key=str)[0])
elif a == "integriere":
    e = lies(aus); r = sp.integrate(e, v or sorted(e.free_symbols, key=str)[0])
elif a == "vereinfache":
    r = sp.simplify(lies(aus))
elif a == "faktorisiere":
    r = sp.factor(lies(aus))
else:
    e = lies(aus); r = sp.N(e, 12) if not e.free_symbols else sp.simplify(e)
print(str(r)[:500])
"""


def sympy_verfuegbar() -> bool:
    import importlib.util
    return importlib.util.find_spec("sympy") is not None


def cas(aufgabe: str, ausdruck: str, variable: str | None = None) -> str:
    """SymPy-Aufruf für ein Modell. Eingabe wird streng geprüft (nur Zahlen, Operatoren, erlaubte Funktionen und
    einbuchstabige Variablen) und in einem eigenen, begrenzten Prozess ausgewertet – parse_expr nutzt intern eval."""
    if not sympy_verfuegbar():
        return "Fehler: SymPy ist nicht installiert."
    aufgabe = str(aufgabe or "berechne").strip().lower()
    if aufgabe not in CAS_AUFGABEN:
        return f"Fehler: unbekannte Aufgabe (erlaubt: {', '.join(CAS_AUFGABEN)})."
    ausdruck, variable = str(ausdruck or "")[:300], (str(variable).strip() if variable else None)
    if not ausdruck.strip() or not _CAS_ZEICHEN.match(ausdruck) or "__" in ausdruck or re.search(r"[A-Za-z_)\]]\s*\.", ausdruck):
        return "Fehler: Ausdruck enthält unerlaubte Zeichen."
    namen = set(re.findall(r"[A-Za-z_][A-Za-z_0-9]*", ausdruck))
    if any(len(n) > 1 and n not in _CAS_FUNKTIONEN for n in namen):
        return "Fehler: nur einbuchstabige Variablen und Funktionen wie sqrt, sin, log, exp, pi."
    if variable and not re.fullmatch(r"[a-z]", variable):
        return "Fehler: Variable muss ein einzelner Buchstabe sein."
    try:
        p = subprocess.Popen([sys.executable, "-I", "-c", _CAS_KIND], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             text=True, env={"PATH": "/usr/bin:/bin", "PYTHONIOENCODING": "utf-8"}, close_fds=True,
                             preexec_fn=_grenzen_cas if resource else None)
        try:
            aus, fehler = p.communicate(json.dumps({"aufgabe": aufgabe, "ausdruck": ausdruck, "variable": variable}), timeout=10)
        finally:
            _beende(p)
        if p.returncode != 0:
            return "Fehler: " + ((fehler or "").strip().splitlines() or ["nicht berechenbar"])[-1][:160]
        return aus.strip() or "keine Lösung"
    except subprocess.TimeoutExpired:
        return "Fehler: Zeitlimit (über 10 Sekunden)."
    except Exception as fehler:
        return f"Fehler: {fehler}"


def _grenzen_cas():
    """Wie beim Code-Test, nur mit mehr Zeit und Speicher fürs Laden von SymPy."""
    os.setsid()
    resource.setrlimit(resource.RLIMIT_CPU, (8, 8))
    resource.setrlimit(resource.RLIMIT_AS, (1024 * 1024**2, 1024 * 1024**2))
    resource.setrlimit(resource.RLIMIT_FSIZE, (0, 0))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_NOFILE, (256, 256))
    resource.setrlimit(resource.RLIMIT_NPROC, (0, 0))

#!/usr/bin/env bash
# Sucht vor einem Commit/Push nach Dingen, die nicht ins Repo gehören.
#   werkzeuge/pruefe_leaks.sh            → alle Dateien, die git einchecken würde (oder alle, falls noch kein Repo)
#   werkzeuge/pruefe_leaks.sh --staged   → nur der aktuelle Commit (für den pre-commit-Hook)
# Eigene Begriffe (Hostnamen, IPs, Namen) gehören in .leak-denylist (eine Regex pro Zeile, wird NICHT eingecheckt).
set -euo pipefail
cd "$(git rev-parse --show-toplevel 2>/dev/null || (cd "$(dirname "$0")/.." && pwd))"

MUSTER=(
  'sk-or-v1-[0-9a-f]{20,}' 'sk-[A-Za-z0-9_-]{30,}' 'hf_[A-Za-z0-9]{30,}' 'glpat-[A-Za-z0-9_-]{20}' 'gh[pousr]_[A-Za-z0-9]{30,}' 'AKIA[0-9A-Z]{16}'
  '-----BEGIN [A-Z ]*PRIVATE KEY-----'
  '\b100\.(6[4-9]|[7-9][0-9]|1[01][0-9]|12[0-7])\.[0-9]{1,3}\.[0-9]{1,3}\b'   # Tailscale / CGNAT
  '\b192\.168\.[0-9]{1,3}\.[0-9]{1,3}\b' '\b10\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}\b'
  '[a-z0-9-]+\.ts\.net' 'tail[0-9a-f]{6}' '/home/[a-z]' '/Users/[A-Za-z]'
)
if [[ -f .leak-denylist ]]; then
  while IFS= read -r z; do [[ -n "$z" && "$z" != \#* ]] && MUSTER+=("$z"); done < .leak-denylist
fi
RE=$(IFS='|'; echo "${MUSTER[*]}")

if [[ "${1:-}" == "--staged" ]]; then
  mapfile -t DATEIEN < <(git diff --cached --name-only --diff-filter=ACM)
elif git rev-parse --git-dir >/dev/null 2>&1; then
  mapfile -t DATEIEN < <(git ls-files --cached --others --exclude-standard)
else
  mapfile -t DATEIEN < <(find . -type f -not -path './.git/*' -not -path './lokal/*' -not -name '.leak-denylist' -not -path '*/__pycache__/*' | sed 's|^\./||')
fi

FEHLER=0
for f in "${DATEIEN[@]}"; do
  [[ -f "$f" ]] || continue
  case "$f" in .leak-denylist|werkzeuge/pruefe_leaks.sh) continue;; esac
  # .env gehört zum Projekt, darf aber keine gesetzten Schlüssel enthalten
  if [[ "$f" == .env ]] && grep -nE '^[A-Z_]*(KEY|TOKEN|SECRET)=.+' "$f"; then echo "   ↑ Schlüssel in .env"; FEHLER=1; fi
  if treffer=$(grep -a -n -i -E "$RE" "$f" 2>/dev/null | cut -c1-160 | head -5); then
    [[ -n "$treffer" ]] && { echo "── $f"; echo "$treffer"; FEHLER=1; }
  fi
  # PDF/PNG-Metadaten: Datumsangaben, Werkzeuge, Pfade
  if [[ "$f" == *.pdf ]] && command -v pdfinfo >/dev/null; then
    pdfinfo "$f" 2>/dev/null | grep -E '^(Author|Creator|Producer|Title):' && { echo "   ↑ Metadaten in $f"; FEHLER=1; } || true
  fi
done

if [[ $FEHLER -ne 0 ]]; then echo; echo "Leak-Prüfung: Treffer gefunden (siehe oben)."; exit 1; fi
echo "Leak-Prüfung: ${#DATEIEN[@]} Dateien, keine Treffer."

# MBB Website-Tracker

Zeitbasiertes Erinnerungssystem: erinnert per E-Mail daran, definierte Inhalte
auf www.mbb.com zu prüfen, und erzeugt eine HTML-Statusübersicht (Dashboard).

## Dateien

| Datei | Zweck |
|---|---|
| `config.yaml` | **Hier änderst du laufend alles**: Prüfpunkte, Intervalle, Kalendertermine, E-Mail-Adressen |
| `tracker.py` | Das Skript (keine Änderungen nötig) |
| `state.json` | Merkt sich, wann jeder Punkt zuletzt bestätigt wurde (wird automatisch gepflegt) |
| `dashboard.html` | Generierte Statusübersicht – im Browser öffnen |

## Einrichtung

1. Python 3 installieren (falls nicht vorhanden), dann: `pip install pyyaml`
2. In `config.yaml` oben die E-Mail-Adressen eintragen (`empfaenger`, `absender`)
3. SMTP-Zugangsdaten als Umgebungsvariablen setzen (z. B. beim Mail-Anbieter
   der Firma erfragen): `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASS`
   – ohne diese Variablen läuft alles, die Erinnerung erscheint dann nur im Terminal.

## Betrieb

```
python tracker.py run          # täglicher Lauf: prüft Fälligkeiten, mailt, aktualisiert Dashboard
python tracker.py confirm <id> # nach erledigter Prüfung bestätigen (id steht in Mail und Dashboard)
python tracker.py list         # Status aller Punkte im Terminal
python tracker.py dashboard    # nur Dashboard neu erzeugen
```

`run` sollte automatisch täglich laufen:

- **Windows:** Aufgabenplanung → tägliche Aufgabe → `python C:\...\tracker.py run`
- **Mac/Linux:** `crontab -e` → `0 8 * * * cd /pfad/zu/mbb-tracker && python3 tracker.py run`
- **Ohne eigenen Rechner/Server:** GitHub Actions, siehe `.github/workflows/tracker.yml`
  (Repository anlegen, SMTP-Daten als Repository-Secrets hinterlegen)

## Laufende Änderungen

Alles passiert in `config.yaml`:

- **Neuer Prüfpunkt:** einen bestehenden Block unter `checks:` kopieren,
  `id` (eindeutig, ohne Leerzeichen), `titel`, `url`, `intervall`, `felder` anpassen.
- **Punkt entfernen:** Block löschen. (Eintrag in `state.json` kann bleiben, stört nicht.)
- **Intervall ändern:** `quartalsweise` ↔ `jaehrlich`.
- **Neue Kalendertermine:** unter `kalender:` ergänzen (Format `JJJJ-MM-TT`).
  Die MBB-Termine für 2027 müssen manuell nachgetragen werden, sobald sie
  veröffentlicht sind – der Tracker liest die Website nicht selbst aus.
- **Vorlauf ändern:** `vorlauf_tage` (Standard 14).

Nach jeder Änderung einmal `python tracker.py dashboard` ausführen, um die
Übersicht zu aktualisieren – oder auf den nächsten automatischen Lauf warten.

## Logik

- Jeder Prüfpunkt hat ein Datum "zuletzt bestätigt". Fällig = dieses Datum
  plus Intervall (90 bzw. 365 Tage).
- Ab `vorlauf_tage` vor Fälligkeit: Status **fällig**, nach Überschreiten:
  **überfällig**. In beiden Fällen wird beim täglichen Lauf gemailt
  (täglich erneut, bis bestätigt wird).
- Kalendertermine erzeugen ab `vorlauf_tage` vorher eine Erinnerung,
  Website und zugehörige Inhalte zu prüfen.

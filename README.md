# MBB Website-Tracker

Erinnerungs- und Überwachungssystem für die Pflege von www.mbb.com.
Läuft täglich automatisch über GitHub Actions, erzeugt ein Dashboard
(GitHub Pages) und verschickt E-Mail-Erinnerungen (sobald SMTP
eingerichtet ist).

## Module

1. **Turnus-Erinnerungen** – 19 Prüfpunkte (Aktie, Zahlenwerk, Timeline,
   Standorte, Töchter). Quartalsweise Punkte werden zum jeweils nächsten
   Finanzkalender-Termin nach der letzten Bestätigung fällig, jährliche
   365 Tage nach der letzten Bestätigung. Erinnerung beginnt
   `vorlauf_tage` (14) vor Fälligkeit.
2. **Website-Abgleich** – ruft jede Prüfpunkt-Seite auf mbb.com ab und
   meldet, wenn hinterlegte `pruefwerte` (Seitentext) oder
   `pruefwerte_roh` (Quellcode, z. B. Grafik-Pfade) fehlen oder wenn
   `verboten`-Texte nach ihrem Stichtag noch auf der Seite stehen.
3. **Meldungs-Checker (EQS)** – überwacht die EQS-News-Übersichten von
   MBB SE, Friedrich Vorwerk, Delignit und Aumann. Neue Meldungen
   passender Kategorien (Directors' Dealings, Stimmrechte, Ad-hoc) oder
   mit passenden Titel-Stichwörtern lösen sofort eine Mail aus und
   werden im Dashboard 7 Tage als NEU markiert. Beim allerersten Lauf
   einer Quelle wird nur der Ausgangsbestand gespeichert (kein Alarm).
4. **Anteil-Gründer-Rechner** – liest die Basis automatisch von der
   Aktie-Seite ("mittelbar zu X %"). Neue Directors' Dealings der
   Gründer-Holdings werden aus der EQS-Meldung ausgelesen (Stückzahl =
   Volumen ÷ Preis) und kursiv als rechnerischer Vorschlag für den
   neuen Anteil angezeigt. Wird der Anteil auf der Website
   aktualisiert, setzt sich die Rechnung automatisch zurück.

## Dateien

| Datei | Zweck |
|---|---|
| `config.yaml` | **Zentrale Pflegestelle**: Prüfpunkte, Intervalle, Prüfwerte, Kalender, EQS-Quellen, Stichwörter, Gruppenfarben, E-Mail-Adressen |
| `tracker.py` | Das Skript (keine Änderungen nötig) |
| `state.json` | Merkzettel des Systems: letzte Bestätigungen, gesehene Meldungen, Rechner-Basis (wird automatisch gepflegt – nicht löschen) |
| `dashboard.html` / `index.html` | Generierte Statusübersicht (GitHub Pages) |
| `.github/workflows/tracker.yml` | Täglicher Lauf, 06:00 UTC; legt bei Fehlschlag automatisch ein GitHub-Issue an |

## Betrieb

- Läuft täglich automatisch. Manuell: GitHub → Actions → Website-Tracker
  → "Run workflow".
- **Prüfung bestätigen**: `state.json` öffnen (Stift-Symbol), beim
  jeweiligen Punkt `zuletzt_bestaetigt` auf das heutige Datum setzen
  (Format JJJJ-MM-TT), committen. Beim nächsten Lauf ist der Punkt
  wieder "aktuell". Lokal alternativ: `python tracker.py confirm <id>`.
- **Fehlschlag-Überwachung**: schlägt ein Lauf fehl, legt der Workflow
  automatisch ein Issue im Repository an. Zusätzlich zeigt das Dashboard
  einen roten Warnbalken, wenn es älter als 2 Tage ist.

## Laufende Änderungen (alles in config.yaml)

- **Neuer Prüfpunkt**: Block unter `checks:` kopieren; `id` (eindeutig),
  `titel`, `url`, `intervall`, `fundstelle`, `felder`, `pruefwerte`,
  optional `gruppe` anpassen.
- **Website-Wert geändert** (z. B. neuer Streubesitz): neuen Wert in
  `felder` und `pruefwerte` des Punkts eintragen – sonst meldet der
  Abgleich dauerhaft eine Abweichung (gewollt).
- **Kalendertermine**: unter `kalender:` pflegen; Termine für das
  Folgejahr nachtragen, sobald MBB sie veröffentlicht (der Tracker
  erinnert daran, wenn keine Termine mehr übrig sind).
- **EQS-Quelle/Stichwort ergänzen**: unter `news_waechter:` →
  `quellen:` bzw. `stichwoerter:`.
- **Module abschalten**: `website_abgleich.aktiv`, `news_waechter.aktiv`
  oder `dd_rechner.aktiv` auf `false`.
- **Gruppenfarben**: unter `gruppen:` (`farbe`, optional `farbe2` für
  zweigeteilte Balken).

## E-Mail (noch offen)

Empfänger/Absender stehen in `config.yaml`. Sobald die IT die
SMTP-Zugangsdaten liefert: als Repository-Secrets `SMTP_HOST`,
`SMTP_PORT`, `SMTP_USER`, `SMTP_PASS` hinterlegen (Settings → Secrets
and variables → Actions). Ohne Secrets läuft alles, die Erinnerungen
erscheinen dann nur im Actions-Protokoll und im Dashboard.
Hinweis: Die einfache Passwort-Anmeldung bei Exchange Online wird von
Microsoft Ende Dezember 2026 standardmäßig abgeschaltet – rechtzeitig
mit der IT klären (Termin dazu steht im Finanzkalender-Abschnitt der
config, sofern eingetragen).

## Grenzen (bewusst so gebaut)

- Der Tracker ändert nie selbst Inhalte – weder auf mbb.com noch
  Sollwerte in der config. Er meldet; ein Mensch entscheidet.
- Bildinhalte (z. B. die Dividenden-Grafik) kann er nicht lesen; er
  prüft stattdessen den eingebundenen Grafik-Pfad im Quellcode.
- Die Stückzahl im Gründer-Rechner ist aus Volumen ÷ Preis errechnet
  und kann bei mehrstufigen Preisen minimal abweichen – der Link zur
  Originalmeldung ist immer dabei.

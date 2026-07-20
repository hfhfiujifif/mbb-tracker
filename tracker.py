#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MBB Website-Tracker
===================
Zeitbasiertes Erinnerungssystem für Website-Inhalte.

Kommandos:
    python tracker.py run              Fälligkeiten prüfen, ggf. E-Mail senden,
                                       Dashboard neu erzeugen (für Cron/Scheduler)
    python tracker.py confirm <id>     Prüfpunkt als erledigt markieren
    python tracker.py list             Alle Prüfpunkte mit Status im Terminal
    python tracker.py dashboard        Nur das Dashboard neu erzeugen

Fälligkeitslogik:
    quartalsweise  -> fällig zum nächsten Finanzkalender-Termin nach der
                      letzten Bestätigung (Fallback: +90 Tage, wenn kein
                      Termin mehr im Kalender steht)
    jaehrlich      -> fällig 365 Tage nach der letzten Bestätigung

Abhängigkeit: pyyaml  (pip install pyyaml)
SMTP-Zugangsdaten als Umgebungsvariablen: SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASS
Ohne SMTP-Variablen werden Erinnerungen nur ins Terminal geschrieben.
"""

import json
import os
import smtplib
import sys
from datetime import date, timedelta
from email.mime.text import MIMEText
from pathlib import Path

import yaml

BASE = Path(__file__).parent
CONFIG_FILE = BASE / "config.yaml"
STATE_FILE = BASE / "state.json"
DASHBOARD_FILE = BASE / "dashboard.html"
INDEX_FILE = BASE / "index.html"   # Kopie, damit die Pages-Startseite das Dashboard zeigt

JAEHRLICH_TAGE = 365
QUARTAL_FALLBACK_TAGE = 90


# ---------------------------------------------------------------- Daten

def load_config():
    with open(CONFIG_FILE, encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_state():
    if STATE_FILE.exists():
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)


def ensure_state(config, state):
    """Neue Prüfpunkte bekommen als Startwert das heutige Datum."""
    changed = False
    for check in config["checks"]:
        if check["id"] not in state:
            state[check["id"]] = {"zuletzt_bestaetigt": date.today().isoformat()}
            changed = True
    if changed:
        save_state(state)
    return state


def kalender_daten(config):
    """Sortierte Terminliste aus dem Finanzkalender."""
    daten = []
    for k in config.get("kalender", []):
        d = k["datum"]
        if isinstance(d, str):
            d = date.fromisoformat(d)
        daten.append(d)
    return sorted(daten)


# ---------------------------------------------------------------- Statuslogik

def faelligkeit(check, state, termine):
    """Liefert (faellig_am, hinweis). Quartalsweise Punkte hängen am
    Finanzkalender: fällig zum nächsten Termin nach der letzten Bestätigung."""
    zuletzt = date.fromisoformat(state[check["id"]]["zuletzt_bestaetigt"])
    if check["intervall"] == "quartalsweise":
        for t in termine:
            if t > zuletzt:
                return t, None
        return (zuletzt + timedelta(days=QUARTAL_FALLBACK_TAGE),
                "kein Termin mehr im Kalender – bitte Finanzkalender in config.yaml ergänzen")
    return zuletzt + timedelta(days=JAEHRLICH_TAGE), None


def status_of(check, state, vorlauf, termine):
    faellig, hinweis = faelligkeit(check, state, termine)
    tage = (faellig - date.today()).days
    if tage < 0:
        return "überfällig", faellig, tage, hinweis
    if tage <= vorlauf:
        return "fällig", faellig, tage, hinweis
    return "aktuell", faellig, tage, hinweis


def kalender_status(eintrag, vorlauf):
    d = eintrag["datum"]
    if isinstance(d, str):
        d = date.fromisoformat(d)
    tage = (d - date.today()).days
    if tage < 0:
        return "vorbei", d, tage
    if tage <= vorlauf:
        return "steht an", d, tage
    return "geplant", d, tage


def collect(config, state):
    vorlauf = config.get("erinnerung", {}).get("vorlauf_tage", 14)
    termine_daten = kalender_daten(config)
    checks = []
    for c in config["checks"]:
        s, faellig, tage, hinweis = status_of(c, state, vorlauf, termine_daten)
        checks.append({**c, "status": s, "faellig_am": faellig, "tage": tage,
                       "hinweis": hinweis,
                       "zuletzt": state[c["id"]]["zuletzt_bestaetigt"]})
    termine = []
    for k in config.get("kalender", []):
        s, d, tage = kalender_status(k, vorlauf)
        termine.append({**k, "status": s, "datum": d, "tage": tage})
    return checks, termine, vorlauf


# ---------------------------------------------------------------- E-Mail

def build_mail_text(due_checks, due_termine):
    lines = ["Guten Tag,", "",
             "folgende Website-Inhalte sind zur Prüfung fällig:"]
    for c in due_checks:
        lines.append("")
        lines.append(f"• {c['titel']}  [{c['status']}]")
        lines.append(f"  Seite: {c['url']}")
        for feld in c["felder"]:
            lines.append(f"  - {feld}")
        lines.append(f"  Fällig am: {c['faellig_am'].strftime('%d.%m.%Y')}")
        lines.append(f"  Nach Erledigung bestätigen: python tracker.py confirm {c['id']}")
    if due_termine:
        lines += ["", "Anstehende Termine aus dem Finanzkalender:"]
        for t in due_termine:
            lines.append(f"• {t['datum'].strftime('%d.%m.%Y')} – {t['titel']} "
                         f"(in {t['tage']} Tagen)")
        lines.append("  Bitte prüfen, ob Kalender und zugehörige Inhalte auf der "
                     "Website aktuell sind: https://www.mbb.com/ir/finanzkalender.html")
    lines += ["", "Die aktuelle Übersicht steht im Dashboard.", ""]
    return "\n".join(lines)


def send_mail(config, text, anzahl):
    mail_cfg = config["email"]
    host = os.environ.get("SMTP_HOST")
    if not host:
        print("Hinweis: keine SMTP-Umgebungsvariablen gesetzt – "
              "Erinnerung wird nur hier ausgegeben:\n")
        print(text)
        return False
    msg = MIMEText(text, "plain", "utf-8")
    msg["Subject"] = f"{mail_cfg.get('betreff_prefix', '')} {anzahl} Punkt(e) zur Prüfung fällig"
    msg["From"] = mail_cfg["absender"]
    msg["To"] = mail_cfg["empfaenger"]
    port = int(os.environ.get("SMTP_PORT", 587))
    with smtplib.SMTP(host, port) as s:
        s.starttls()
        user, pw = os.environ.get("SMTP_USER"), os.environ.get("SMTP_PASS")
        if user and pw:
            s.login(user, pw)
        s.send_message(msg)
    print(f"E-Mail an {mail_cfg['empfaenger']} gesendet ({anzahl} Punkte).")
    return True


# ---------------------------------------------------------------- Dashboard

BADGE = {
    "aktuell":    ("ok",   "aktuell"),
    "fällig":     ("due",  "fällig"),
    "überfällig": ("over", "überfällig"),
    "geplant":    ("ok",   "geplant"),
    "steht an":   ("due",  "steht an"),
    "vorbei":     ("past", "vorbei"),
}


def render_felder(felder):
    """'Label: Wert' wird zu zweispaltigen Zeilen, sonst volle Breite."""
    rows = ""
    for f in felder:
        if ": " in f:
            label, wert = f.split(": ", 1)
            rows += (f'<div class="feld"><span class="feld-label">{label}</span>'
                     f'<span class="feld-wert">{wert}</span></div>')
        else:
            rows += f'<div class="feld"><span class="feld-voll">{f}</span></div>'
    return rows


def render_dashboard(checks, termine, vorlauf):
    def fmt(d):
        return d.strftime("%d.%m.%Y")

    def badge(status):
        cls, label = BADGE[status]
        return f'<span class="badge badge-{cls}">{label}</span>'

    order = {"überfällig": 0, "fällig": 1, "aktuell": 2}
    checks_sorted = sorted(checks, key=lambda c: (order[c["status"]], c["faellig_am"]))

    cards = ""
    for c in checks_sorted:
        cls, _ = BADGE[c["status"]]
        hinweis = (f'<p class="hinweis">{c["hinweis"]}</p>' if c["hinweis"] else "")
        kopplung = ("Finanzkalender" if c["intervall"] == "quartalsweise"
                    else "jährlich")
        cards += f"""
      <article class="card status-{cls}">
        <div class="card-kopf">
          <h3>{c['titel']}</h3>
          {badge(c['status'])}
        </div>
        <a class="card-url" href="{c['url']}" target="_blank">{c['url'].replace('https://www.mbb.com', 'mbb.com')}</a>
        <div class="felder">{render_felder(c['felder'])}</div>
        {hinweis}
        <div class="card-meta">
          <div><span class="meta-label">Rhythmus</span>{c['intervall']} · {kopplung}</div>
          <div><span class="meta-label">Zuletzt bestätigt</span>{fmt(date.fromisoformat(c['zuletzt']))}</div>
          <div><span class="meta-label">Fällig am</span><strong>{fmt(c['faellig_am'])}</strong></div>
        </div>
        <div class="card-fuss">Bestätigen: <code>{c['id']}</code></div>
      </article>"""

    termin_rows = ""
    for t in sorted(termine, key=lambda x: x["datum"]):
        termin_rows += f"""
        <tr>
          <td class="t-datum">{fmt(t['datum'])}</td>
          <td>{t['titel']}</td>
          <td class="t-status">{badge(t['status'])}</td>
        </tr>"""

    n_over = sum(1 for c in checks if c["status"] == "überfällig")
    n_due = sum(1 for c in checks if c["status"] == "fällig")
    n_ok = sum(1 for c in checks if c["status"] == "aktuell")

    return f"""<!DOCTYPE html>
<html lang="de">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>MBB Website-Tracker – Statusübersicht</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Sans:wght@400;500;600&family=IBM+Plex+Mono&display=swap" rel="stylesheet">
<style>
  :root {{
    --ink: #1a1a1a; --muted: #6b6b6b; --line: #dcdcdc; --panel: #f0f0f0;
    --red: #e2001a;
    --ok: #1d7a4f; --ok-bg: #e7f2ec;
    --due: #96650a; --due-bg: #fbf1dc;
    --over: #b3121b; --over-bg: #fbe6e7;
    --past: #6b6b6b; --past-bg: #ececec;
  }}
  * {{ box-sizing: border-box; }}
  body {{ margin: 0; background: #fff; color: var(--ink);
         font-family: "IBM Plex Sans", "Segoe UI", sans-serif; font-size: 15px; }}
  header {{ border-top: 4px solid var(--red); border-bottom: 1px solid var(--line);
            padding: 26px 36px; display: flex; align-items: center; gap: 18px; }}
  .logo {{ background: #111; color: #fff; font-weight: 600; font-size: 20px;
           padding: 12px 16px; letter-spacing: .02em; }}
  header h1 {{ margin: 0; font-size: 19px; font-weight: 600; }}
  header p {{ margin: 2px 0 0; color: var(--muted); font-size: 13px; }}
  main {{ max-width: 1220px; margin: 0 auto; padding: 28px 36px 56px; }}
  .summary {{ display: flex; gap: 1px; background: var(--line);
              border: 1px solid var(--line); margin-bottom: 34px; }}
  .sum {{ background: #fff; flex: 1; padding: 14px 18px; }}
  .sum .num {{ font-size: 28px; font-weight: 600; }}
  .sum .lbl {{ font-size: 11px; color: var(--muted); text-transform: uppercase;
               letter-spacing: .08em; }}
  .sum.over .num {{ color: var(--over); }}
  .sum.due .num {{ color: var(--due); }}
  .sum.ok .num {{ color: var(--ok); }}
  h2 {{ font-size: 13px; font-weight: 600; margin: 36px 0 14px;
        text-transform: uppercase; letter-spacing: .12em; }}
  h2::after {{ content: ""; display: block; height: 1px; background: var(--line);
               margin-top: 10px; }}
  .grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(350px, 1fr));
           gap: 18px; }}
  .card {{ border: 1px solid var(--line); border-top: 3px solid var(--line);
           padding: 16px 18px 0; display: flex; flex-direction: column; }}
  .card.status-over {{ border-top-color: var(--over); }}
  .card.status-due  {{ border-top-color: var(--due); }}
  .card.status-ok   {{ border-top-color: #b9c4bd; }}
  .card-kopf {{ display: flex; justify-content: space-between; gap: 10px;
                align-items: baseline; }}
  .card h3 {{ margin: 0; font-size: 15px; font-weight: 600; }}
  .card-url {{ font-size: 12px; color: var(--muted); text-decoration: none;
               margin: 3px 0 12px; display: block; }}
  .card-url:hover {{ color: var(--red); }}
  .felder {{ border-top: 1px solid var(--line); }}
  .feld {{ display: flex; justify-content: space-between; gap: 14px;
           padding: 7px 0; border-bottom: 1px solid var(--line); font-size: 13px; }}
  .feld-label {{ color: var(--muted); }}
  .feld-wert {{ text-align: right; font-weight: 500; }}
  .feld-voll {{ color: var(--muted); }}
  .hinweis {{ color: var(--over); font-size: 12px; margin: 8px 0 0; }}
  .card-meta {{ display: flex; gap: 18px; flex-wrap: wrap; font-size: 13px;
                padding: 12px 0; margin-top: auto; }}
  .meta-label {{ display: block; font-size: 10px; text-transform: uppercase;
                 letter-spacing: .08em; color: var(--muted); margin-bottom: 1px; }}
  .card-fuss {{ border-top: 1px solid var(--line); margin: 0 -18px;
                padding: 8px 18px; background: #fafafa; font-size: 12px;
                color: var(--muted); }}
  code {{ font-family: "IBM Plex Mono", monospace; font-size: 12px; }}
  .badge {{ padding: 3px 10px; font-size: 11px; font-weight: 600;
            letter-spacing: .04em; text-transform: uppercase; white-space: nowrap; }}
  .badge-ok   {{ background: var(--ok-bg);   color: var(--ok); }}
  .badge-due  {{ background: var(--due-bg);  color: var(--due); }}
  .badge-over {{ background: var(--over-bg); color: var(--over); }}
  .badge-past {{ background: var(--past-bg); color: var(--past); }}
  table {{ width: 100%; border-collapse: collapse; }}
  td {{ padding: 13px 8px; border-bottom: 1px solid var(--line); font-size: 15px; }}
  .t-datum {{ width: 180px; color: var(--ink); }}
  .t-status {{ width: 130px; text-align: right; }}
  footer {{ color: var(--muted); font-size: 12px; margin-top: 30px; }}
  @media (max-width: 640px) {{
    header, main {{ padding-left: 18px; padding-right: 18px; }}
    .summary {{ flex-direction: column; }}
  }}
</style>
</head>
<body>
<header>
  <div class="logo">MBB</div>
  <div>
    <h1>Website-Tracker</h1>
    <p>Statusübersicht der zu pflegenden Inhalte auf mbb.com · Stand: {date.today().strftime('%d.%m.%Y')} · Erinnerung ab {vorlauf} Tagen vor Fälligkeit</p>
  </div>
</header>
<main>
  <div class="summary">
    <div class="sum over"><div class="num">{n_over}</div><div class="lbl">überfällig</div></div>
    <div class="sum due"><div class="num">{n_due}</div><div class="lbl">fällig</div></div>
    <div class="sum ok"><div class="num">{n_ok}</div><div class="lbl">aktuell</div></div>
  </div>

  <h2>Prüfpunkte</h2>
  <div class="grid">{cards}
  </div>

  <h2>Finanzkalender</h2>
  <table>
    <tbody>{termin_rows}
    </tbody>
  </table>

  <footer>Quartalsweise Punkte werden zum jeweils nächsten Finanzkalender-Termin
    nach der letzten Bestätigung fällig; jährliche Punkte 365 Tage nach der
    letzten Bestätigung. Erledigte Prüfungen bestätigen: Datum in
    <code>state.json</code> auf heute setzen oder
    <code>python tracker.py confirm &lt;id&gt;</code>.</footer>
</main>
</body>
</html>"""


def write_dashboard(config, state):
    checks, termine, vorlauf = collect(config, state)
    html = render_dashboard(checks, termine, vorlauf)
    DASHBOARD_FILE.write_text(html, encoding="utf-8")
    INDEX_FILE.write_text(html, encoding="utf-8")
    print(f"Dashboard aktualisiert: {DASHBOARD_FILE} (+ index.html)")


# ---------------------------------------------------------------- Kommandos

def cmd_run():
    config = load_config()
    state = ensure_state(config, load_state())
    checks, termine, vorlauf = collect(config, state)
    due_checks = [c for c in checks if c["status"] in ("fällig", "überfällig")]
    due_termine = [t for t in termine if t["status"] == "steht an"]
    if due_checks or due_termine:
        text = build_mail_text(due_checks, due_termine)
        send_mail(config, text, len(due_checks) + len(due_termine))
    else:
        print("Nichts fällig – keine Erinnerung nötig.")
    write_dashboard(config, state)


def cmd_confirm(check_id):
    config = load_config()
    state = ensure_state(config, load_state())
    ids = [c["id"] for c in config["checks"]]
    if check_id not in ids:
        print(f"Unbekannte ID '{check_id}'. Verfügbar:")
        for i in ids:
            print(f"  {i}")
        sys.exit(1)
    state[check_id]["zuletzt_bestaetigt"] = date.today().isoformat()
    save_state(state)
    print(f"'{check_id}' als geprüft bestätigt ({date.today().strftime('%d.%m.%Y')}).")
    write_dashboard(config, state)


def cmd_list():
    config = load_config()
    state = ensure_state(config, load_state())
    checks, termine, _ = collect(config, state)
    w = max(len(c["id"]) for c in checks)
    for c in checks:
        print(f"{c['id']:<{w}}  {c['status']:<11}  fällig {c['faellig_am'].strftime('%d.%m.%Y')}  {c['titel']}")
    print()
    for t in termine:
        print(f"{t['datum'].strftime('%d.%m.%Y')}  {t['status']:<9}  {t['titel']}")


def cmd_dashboard():
    config = load_config()
    state = ensure_state(config, load_state())
    write_dashboard(config, state)


if __name__ == "__main__":
    args = sys.argv[1:]
    if not args or args[0] == "run":
        cmd_run()
    elif args[0] == "confirm" and len(args) == 2:
        cmd_confirm(args[1])
    elif args[0] == "list":
        cmd_list()
    elif args[0] == "dashboard":
        cmd_dashboard()
    else:
        print(__doc__)
        sys.exit(1)

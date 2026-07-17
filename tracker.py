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

Abhängigkeit: pyyaml  (pip install pyyaml)
SMTP-Zugangsdaten als Umgebungsvariablen: SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASS
Ohne SMTP-Variablen werden Erinnerungen nur ins Terminal geschrieben.
"""

import json
import os
import smtplib
import sys
from datetime import date, datetime, timedelta
from email.mime.text import MIMEText
from pathlib import Path

import yaml

BASE = Path(__file__).parent
CONFIG_FILE = BASE / "config.yaml"
STATE_FILE = BASE / "state.json"
DASHBOARD_FILE = BASE / "dashboard.html"

INTERVALL_TAGE = {"quartalsweise": 90, "jaehrlich": 365}


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


# ---------------------------------------------------------------- Statuslogik

def status_of(check, state, vorlauf):
    """Liefert (status, faellig_am, tage_bis)."""
    zuletzt = date.fromisoformat(state[check["id"]]["zuletzt_bestaetigt"])
    faellig = zuletzt + timedelta(days=INTERVALL_TAGE[check["intervall"]])
    tage = (faellig - date.today()).days
    if tage < 0:
        return "überfällig", faellig, tage
    if tage <= vorlauf:
        return "fällig", faellig, tage
    return "aktuell", faellig, tage


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
    checks = []
    for c in config["checks"]:
        s, faellig, tage = status_of(c, state, vorlauf)
        checks.append({**c, "status": s, "faellig_am": faellig, "tage": tage,
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
    lines += ["", "Die aktuelle Übersicht steht im Dashboard (dashboard.html).", ""]
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
    "aktuell":    ("badge-ok", "aktuell"),
    "fällig":     ("badge-due", "fällig"),
    "überfällig": ("badge-over", "überfällig"),
    "geplant":    ("badge-ok", "geplant"),
    "steht an":   ("badge-due", "steht an"),
    "vorbei":     ("badge-past", "vorbei"),
}


def render_dashboard(checks, termine, vorlauf):
    def fmt(d):
        return d.strftime("%d.%m.%Y")

    def badge(status):
        cls, label = BADGE[status]
        return f'<span class="badge {cls}">{label}</span>'

    order = {"überfällig": 0, "fällig": 1, "aktuell": 2}
    checks_sorted = sorted(checks, key=lambda c: (order[c["status"]], c["faellig_am"]))

    check_rows = ""
    for c in checks_sorted:
        felder = "<br>".join(c["felder"])
        check_rows += f"""
        <tr>
          <td><strong>{c['titel']}</strong><br>
              <a href="{c['url']}" target="_blank">{c['url'].replace('https://www.mbb.com', '')}</a></td>
          <td class="felder">{felder}</td>
          <td>{c['intervall']}</td>
          <td>{fmt(date.fromisoformat(c['zuletzt']))}</td>
          <td>{fmt(c['faellig_am'])}</td>
          <td>{badge(c['status'])}</td>
          <td><code>confirm {c['id']}</code></td>
        </tr>"""

    termin_rows = ""
    for t in sorted(termine, key=lambda x: x["datum"]):
        termin_rows += f"""
        <tr>
          <td>{fmt(t['datum'])}</td>
          <td>{t['titel']}</td>
          <td>{badge(t['status'])}</td>
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
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Sans:wght@400;500;600&family=IBM+Plex+Mono:wght@400&display=swap" rel="stylesheet">
<style>
  :root {{
    --ink: #1a2332; --muted: #5b6675; --line: #d9dee5; --bg: #f5f6f8;
    --panel: #ffffff; --ok: #1d7a4f; --ok-bg: #e3f2ea;
    --due: #9a6a00; --due-bg: #fdf0d5; --over: #a3232b; --over-bg: #fbe4e5;
    --past: #5b6675; --past-bg: #e8eaee; --accent: #10366f;
  }}
  * {{ box-sizing: border-box; }}
  body {{ margin: 0; background: var(--bg); color: var(--ink);
         font-family: "IBM Plex Sans", "Segoe UI", sans-serif; font-size: 15px; }}
  header {{ background: var(--panel); border-bottom: 3px solid var(--accent);
            padding: 24px 32px; }}
  header h1 {{ margin: 0 0 4px; font-size: 20px; font-weight: 600; }}
  header p {{ margin: 0; color: var(--muted); font-size: 13px; }}
  main {{ max-width: 1180px; margin: 0 auto; padding: 24px 32px 48px; }}
  .summary {{ display: flex; gap: 12px; margin: 0 0 24px; flex-wrap: wrap; }}
  .card {{ background: var(--panel); border: 1px solid var(--line);
           border-radius: 6px; padding: 12px 18px; min-width: 130px; }}
  .card .num {{ font-size: 26px; font-weight: 600; }}
  .card .lbl {{ font-size: 12px; color: var(--muted); text-transform: uppercase;
                letter-spacing: .04em; }}
  .card.over .num {{ color: var(--over); }}
  .card.due .num {{ color: var(--due); }}
  .card.ok .num {{ color: var(--ok); }}
  h2 {{ font-size: 15px; font-weight: 600; margin: 28px 0 10px;
        text-transform: uppercase; letter-spacing: .05em; color: var(--muted); }}
  table {{ width: 100%; border-collapse: collapse; background: var(--panel);
           border: 1px solid var(--line); border-radius: 6px; overflow: hidden; }}
  th {{ text-align: left; font-size: 12px; text-transform: uppercase;
        letter-spacing: .04em; color: var(--muted); font-weight: 500;
        padding: 10px 12px; border-bottom: 1px solid var(--line);
        background: #fafbfc; }}
  td {{ padding: 12px; border-bottom: 1px solid var(--line);
        vertical-align: top; }}
  tr:last-child td {{ border-bottom: none; }}
  td.felder {{ color: var(--muted); font-size: 13px; max-width: 340px; }}
  a {{ color: var(--accent); text-decoration: none; font-size: 13px; }}
  a:hover {{ text-decoration: underline; }}
  code {{ font-family: "IBM Plex Mono", monospace; font-size: 12px;
          background: var(--bg); padding: 2px 6px; border-radius: 4px; }}
  .badge {{ display: inline-block; padding: 3px 10px; border-radius: 999px;
            font-size: 12px; font-weight: 500; white-space: nowrap; }}
  .badge-ok  {{ background: var(--ok-bg);  color: var(--ok); }}
  .badge-due {{ background: var(--due-bg); color: var(--due); }}
  .badge-over{{ background: var(--over-bg);color: var(--over); }}
  .badge-past{{ background: var(--past-bg);color: var(--past); }}
  footer {{ color: var(--muted); font-size: 12px; margin-top: 24px; }}
</style>
</head>
<body>
<header>
  <h1>MBB Website-Tracker</h1>
  <p>Statusübersicht der zu pflegenden Website-Inhalte · Stand: {date.today().strftime('%d.%m.%Y')} · Erinnerungsvorlauf: {vorlauf} Tage</p>
</header>
<main>
  <div class="summary">
    <div class="card over"><div class="num">{n_over}</div><div class="lbl">überfällig</div></div>
    <div class="card due"><div class="num">{n_due}</div><div class="lbl">fällig</div></div>
    <div class="card ok"><div class="num">{n_ok}</div><div class="lbl">aktuell</div></div>
  </div>

  <h2>Prüfpunkte</h2>
  <table>
    <thead><tr>
      <th>Bereich / Seite</th><th>Zu prüfende Angaben</th><th>Intervall</th>
      <th>Zuletzt bestätigt</th><th>Fällig am</th><th>Status</th><th>Bestätigen mit</th>
    </tr></thead>
    <tbody>{check_rows}
    </tbody>
  </table>

  <h2>Finanzkalender</h2>
  <table>
    <thead><tr><th>Datum</th><th>Termin</th><th>Status</th></tr></thead>
    <tbody>{termin_rows}
    </tbody>
  </table>

  <footer>Erzeugt durch tracker.py · Erledigte Prüfungen bestätigen mit:
    <code>python tracker.py confirm &lt;id&gt;</code></footer>
</main>
</body>
</html>"""


def write_dashboard(config, state):
    checks, termine, vorlauf = collect(config, state)
    DASHBOARD_FILE.write_text(render_dashboard(checks, termine, vorlauf),
                              encoding="utf-8")
    print(f"Dashboard aktualisiert: {DASHBOARD_FILE}")


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

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MBB Website-Tracker
===================
Zeitbasiertes Erinnerungssystem mit Website-Abgleich (Soll-Ist-Crawler).

Kommandos:
    python tracker.py run              Fälligkeiten + Website prüfen, ggf. mailen,
                                       Dashboard neu erzeugen (für Cron/Scheduler)
    python tracker.py confirm <id>     Prüfpunkt als erledigt markieren
    python tracker.py list             Alle Prüfpunkte mit Status im Terminal
    python tracker.py dashboard        Nur das Dashboard neu erzeugen

Fälligkeitslogik:
    quartalsweise  -> fällig zum nächsten Finanzkalender-Termin nach der
                      letzten Bestätigung (Fallback: +90 Tage)
    jaehrlich      -> fällig 365 Tage nach der letzten Bestätigung

Website-Abgleich (Schalter in config.yaml -> website_abgleich.aktiv):
    Ruft jede Prüfpunkt-Seite ab und meldet, wenn hinterlegte "pruefwerte"
    dort nicht mehr vorkommen (Seite geändert oder defekt).

Abhängigkeit: pyyaml  (pip install pyyaml)
SMTP als Umgebungsvariablen: SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASS
"""

import html as html_mod
import json
import os
import re
import smtplib
import sys
import urllib.request
from datetime import date, timedelta
from email.mime.text import MIMEText
from pathlib import Path

import yaml

BASE = Path(__file__).parent
CONFIG_FILE = BASE / "config.yaml"
STATE_FILE = BASE / "state.json"
DASHBOARD_FILE = BASE / "dashboard.html"
INDEX_FILE = BASE / "index.html"

JAEHRLICH_TAGE = 365
QUARTAL_FALLBACK_TAGE = 90
HTTP_TIMEOUT = 25
USER_AGENT = "Mozilla/5.0 (MBB-Website-Tracker; interner Pflege-Check)"


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
    changed = False
    for check in config["checks"]:
        if check["id"] not in state:
            state[check["id"]] = {"zuletzt_bestaetigt": date.today().isoformat()}
            changed = True
    if changed:
        save_state(state)
    return state


def kalender_daten(config):
    daten = []
    for k in config.get("kalender", []):
        d = k["datum"]
        if isinstance(d, str):
            d = date.fromisoformat(d)
        daten.append(d)
    return sorted(daten)


# ---------------------------------------------------------------- Crawler

def normalisiere(text):
    """HTML-Entities auflösen, geschützte Leerzeichen und Mehrfach-
    Whitespace vereinheitlichen, damit Textvergleiche robust sind."""
    text = html_mod.unescape(text)
    text = text.replace("\u00a0", " ").replace("\u202f", " ").replace("\u2009", " ")
    return re.sub(r"\s+", " ", text)


def html_zu_text(quelltext):
    quelltext = re.sub(r"<(script|style)\b.*?</\1>", " ", quelltext,
                       flags=re.S | re.I)
    quelltext = re.sub(r"<[^>]+>", " ", quelltext)
    return normalisiere(quelltext)


def hole_seite(url):
    """Liefert (seitentext, fehler). Genau eines von beiden ist gesetzt."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            roh = resp.read().decode("utf-8", errors="replace")
        return html_zu_text(roh), None
    except Exception as e:
        return None, str(e)


def website_abgleich(config, fetch=hole_seite):
    """Prüft pro Check, ob die pruefwerte auf der Seite vorkommen.
    Liefert dict check_id -> {"status": ok|abweichung|fehler|keine, ...}."""
    aktiv = config.get("website_abgleich", {}).get("aktiv", False)
    ergebnisse = {}
    if not aktiv:
        return ergebnisse
    seiten_cache = {}
    for check in config["checks"]:
        werte = check.get("pruefwerte") or []
        if not werte:
            ergebnisse[check["id"]] = {"status": "keine"}
            continue
        url = check["url"]
        if url not in seiten_cache:
            seiten_cache[url] = fetch(url)
        text, fehler = seiten_cache[url]
        if fehler:
            ergebnisse[check["id"]] = {"status": "fehler", "fehler": fehler}
            continue
        fehlend = [w for w in werte if normalisiere(w) not in text]
        if fehlend:
            ergebnisse[check["id"]] = {"status": "abweichung", "fehlend": fehlend}
        else:
            ergebnisse[check["id"]] = {"status": "ok"}
    return ergebnisse


# ---------------------------------------------------------------- Statuslogik

def faelligkeit(check, state, termine):
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


def collect(config, state, web=None):
    vorlauf = config.get("erinnerung", {}).get("vorlauf_tage", 14)
    termine_daten = kalender_daten(config)
    web = web or {}
    checks = []
    for c in config["checks"]:
        s, faellig, tage, hinweis = status_of(c, state, vorlauf, termine_daten)
        checks.append({**c, "status": s, "faellig_am": faellig, "tage": tage,
                       "hinweis": hinweis,
                       "web": web.get(c["id"]),
                       "zuletzt": state[c["id"]]["zuletzt_bestaetigt"]})
    termine = []
    for k in config.get("kalender", []):
        s, d, tage = kalender_status(k, vorlauf)
        termine.append({**k, "status": s, "datum": d, "tage": tage})
    return checks, termine, vorlauf


# ---------------------------------------------------------------- E-Mail

def build_mail_text(due_checks, due_termine, abweichungen):
    lines = ["Guten Tag,"]
    if abweichungen:
        lines += ["", "ACHTUNG – der Website-Abgleich hat Abweichungen gefunden",
                  "(erwartete Angaben sind nicht mehr auf der Seite):"]
        for c in abweichungen:
            lines.append("")
            lines.append(f"• {c['titel']}")
            lines.append(f"  Seite: {c['url']}")
            for w in c["web"]["fehlend"]:
                lines.append(f"  - nicht gefunden: \"{w}\"")
            lines.append("  Bitte Seite prüfen. Ist die Änderung beabsichtigt, den")
            lines.append("  neuen Wert in config.yaml (pruefwerte/felder) nachziehen.")
    if due_checks:
        lines += ["", "Folgende Inhalte sind turnusmäßig zur Prüfung fällig:"]
        for c in due_checks:
            lines.append("")
            lines.append(f"• {c['titel']}  [{c['status']}]")
            lines.append(f"  Seite: {c['url']}")
            for feld in c["felder"]:
                lines.append(f"  - {feld}")
            for ref in c.get("referenzen") or []:
                lines.append(f"  Vergleichsquelle: {ref['titel']} – {ref['url']}")
            lines.append(f"  Fällig am: {c['faellig_am'].strftime('%d.%m.%Y')}")
            lines.append(f"  Nach Erledigung bestätigen: python tracker.py confirm {c['id']}")
    if due_termine:
        lines += ["", "Anstehende Termine aus dem Finanzkalender:"]
        for t in due_termine:
            lines.append(f"• {t['datum'].strftime('%d.%m.%Y')} – {t['titel']} "
                         f"(in {t['tage']} Tagen)")
        lines.append("  Bitte prüfen, ob Kalender und zugehörige Inhalte aktuell "
                     "sind: https://www.mbb.com/ir/finanzkalender.html")
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
    msg["Subject"] = f"{mail_cfg.get('betreff_prefix', '')} {anzahl} Punkt(e) zur Prüfung"
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
    rows = ""
    for f in felder:
        if ": " in f:
            label, wert = f.split(": ", 1)
            rows += (f'<div class="feld"><span class="feld-label">{label}</span>'
                     f'<span class="feld-wert">{wert}</span></div>')
        else:
            rows += f'<div class="feld"><span class="feld-voll">{f}</span></div>'
    return rows


def render_web(web):
    if web is None:
        return ""
    s = web["status"]
    if s == "ok":
        return '<p class="web web-ok">Website-Abgleich: erwartete Werte gefunden</p>'
    if s == "abweichung":
        fehlend = ", ".join(f"„{w}“" for w in web["fehlend"])
        return (f'<p class="web web-alarm">Abweichung – nicht auf der Seite '
                f'gefunden: {fehlend}</p>')
    if s == "fehler":
        return '<p class="web web-warn">Website-Abgleich: Seite nicht erreichbar</p>'
    return '<p class="web web-neutral">Website-Abgleich: keine Prüfwerte hinterlegt</p>'


def render_referenzen(refs):
    if not refs:
        return ""
    links = " · ".join(f'<a href="{r["url"]}" target="_blank">{r["titel"]}</a>'
                       for r in refs)
    return f'<p class="refs">Vergleichsquellen (manuell): {links}</p>'


def render_dashboard(checks, termine, vorlauf, abgleich_aktiv):
    def fmt(d):
        return d.strftime("%d.%m.%Y")

    def badge(status):
        cls, label = BADGE[status]
        return f'<span class="badge badge-{cls}">{label}</span>'

    def sortkey(c):
        alarm = 0 if (c["web"] and c["web"]["status"] == "abweichung") else 1
        order = {"überfällig": 0, "fällig": 1, "aktuell": 2}
        return (alarm, order[c["status"]], c["faellig_am"])

    cards = ""
    for c in sorted(checks, key=sortkey):
        cls, _ = BADGE[c["status"]]
        if c["web"] and c["web"]["status"] == "abweichung":
            cls = "over"
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
        {render_web(c['web'])}
        {render_referenzen(c.get('referenzen'))}
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
    n_web = sum(1 for c in checks
                if c["web"] and c["web"]["status"] == "abweichung")
    abgleich_info = ("Website-Abgleich aktiv" if abgleich_aktiv
                     else "Website-Abgleich abgeschaltet")

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
  .logo {{ height: 56px; width: 56px; display: block; }}
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
  .web {{ font-size: 12px; margin: 10px 0 0; padding: 6px 10px; }}
  .web-ok      {{ background: var(--ok-bg);   color: var(--ok); }}
  .web-alarm   {{ background: var(--over-bg); color: var(--over); font-weight: 500; }}
  .web-warn    {{ background: var(--due-bg);  color: var(--due); }}
  .web-neutral {{ background: var(--past-bg); color: var(--past); }}
  .refs {{ font-size: 12px; color: var(--muted); margin: 8px 0 0; }}
  .refs a {{ color: var(--ink); }}
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
  .t-datum {{ width: 180px; }}
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
  <img class="logo" src="https://upload.wikimedia.org/wikipedia/commons/thumb/0/00/Logo_MBB_SE.svg/1280px-Logo_MBB_SE.svg.png" alt="MBB SE">
  <div>
    <h1>Website-Tracker</h1>
    <p>Statusübersicht der zu pflegenden Inhalte auf mbb.com · Stand: {date.today().strftime('%d.%m.%Y')} · Erinnerung ab {vorlauf} Tagen vor Fälligkeit · {abgleich_info}</p>
  </div>
</header>
<main>
  <div class="summary">
    <div class="sum over"><div class="num">{n_web}</div><div class="lbl">Web-Abweichungen</div></div>
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
    letzten Bestätigung. Der Website-Abgleich prüft, ob die hinterlegten
    Prüfwerte noch auf der jeweiligen Seite stehen; Vergleichsquellen sind
    Links für den manuellen Abgleich (externe Portale rechnen teils anders).
    Erledigte Prüfungen bestätigen: Datum in <code>state.json</code> auf heute
    setzen oder <code>python tracker.py confirm &lt;id&gt;</code>.</footer>
</main>
</body>
</html>"""


def write_dashboard(config, state, web=None):
    checks, termine, vorlauf = collect(config, state, web)
    aktiv = config.get("website_abgleich", {}).get("aktiv", False)
    html = render_dashboard(checks, termine, vorlauf, aktiv)
    DASHBOARD_FILE.write_text(html, encoding="utf-8")
    INDEX_FILE.write_text(html, encoding="utf-8")
    print(f"Dashboard aktualisiert: {DASHBOARD_FILE} (+ index.html)")


# ---------------------------------------------------------------- Kommandos

def cmd_run():
    config = load_config()
    state = ensure_state(config, load_state())
    web = website_abgleich(config)
    checks, termine, vorlauf = collect(config, state, web)
    due_checks = [c for c in checks if c["status"] in ("fällig", "überfällig")]
    due_termine = [t for t in termine if t["status"] == "steht an"]
    abweichungen = [c for c in checks
                    if c["web"] and c["web"]["status"] == "abweichung"]
    if due_checks or due_termine or abweichungen:
        text = build_mail_text(due_checks, due_termine, abweichungen)
        send_mail(config, text,
                  len(due_checks) + len(due_termine) + len(abweichungen))
    else:
        print("Nichts fällig, keine Abweichungen – keine Erinnerung nötig.")
    fehler = [c for c in checks if c["web"] and c["web"]["status"] == "fehler"]
    for c in fehler:
        print(f"Warnung: {c['url']} nicht erreichbar ({c['web'].get('fehler', '')})")
    write_dashboard(config, state, web)


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
    write_dashboard(config, state, website_abgleich(config))


def cmd_list():
    config = load_config()
    state = ensure_state(config, load_state())
    web = website_abgleich(config)
    checks, termine, _ = collect(config, state, web)
    w = max(len(c["id"]) for c in checks)
    for c in checks:
        webinfo = ""
        if c["web"]:
            webinfo = {"ok": "web ok", "abweichung": "WEB-ABWEICHUNG",
                       "fehler": "web-fehler", "keine": "-"}[c["web"]["status"]]
        print(f"{c['id']:<{w}}  {c['status']:<11}  fällig "
              f"{c['faellig_am'].strftime('%d.%m.%Y')}  {webinfo:<15}  {c['titel']}")
    print()
    for t in termine:
        print(f"{t['datum'].strftime('%d.%m.%Y')}  {t['status']:<9}  {t['titel']}")


def cmd_dashboard():
    config = load_config()
    state = ensure_state(config, load_state())
    write_dashboard(config, state, website_abgleich(config))


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

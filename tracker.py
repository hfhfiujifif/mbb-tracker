#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MBB Website-Tracker
===================
Zeitbasiertes Erinnerungssystem mit Website-Abgleich (Soll-Ist-Crawler)
und externem Beteiligungs-Abgleich (MarketScreener).

Kommandos:
    python tracker.py run              Fälligkeiten + Website + externe Quellen
                                       prüfen, ggf. mailen, Dashboard erzeugen
    python tracker.py confirm <id>     Prüfpunkt als erledigt markieren
    python tracker.py list             Alle Prüfpunkte mit Status im Terminal
    python tracker.py dashboard        Nur das Dashboard neu erzeugen

Fälligkeitslogik:
    quartalsweise  -> fällig zum nächsten Finanzkalender-Termin nach der
                      letzten Bestätigung (Fallback: +90 Tage)
    jaehrlich      -> fällig 365 Tage nach der letzten Bestätigung

Website-Abgleich (config: website_abgleich.aktiv):
    Ruft jede Prüfpunkt-Seite auf mbb.com ab und meldet, wenn hinterlegte
    "pruefwerte" dort nicht mehr vorkommen.

Externer Abgleich (config: extern_abgleich.aktiv):
    Ruft je Prüfpunkt mit "extern"-Block die angegebene externe Seite ab,
    sucht den Prozentwert hinter dem Suchbegriff (z. B. "MBB") und
    vergleicht ihn mit dem erwarteten Wert innerhalb einer Toleranz.
    Hinweis: externe Portale runden anders und können automatisierte
    Abrufe blockieren; bei Blockade erscheint "nicht erreichbar" und der
    Vergleich bleibt manuell über die Referenz-Links möglich.

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
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
PROZENT_RE = re.compile(r"(\d{1,3}(?:[.,]\d{1,2})?)\s*%")
SUCHFENSTER = 140   # Zeichen hinter dem Suchbegriff, in denen der %-Wert stehen darf


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


# ---------------------------------------------------------------- Abruf

def normalisiere(text):
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
        req = urllib.request.Request(url, headers={
            "User-Agent": USER_AGENT,
            "Accept-Language": "de,en;q=0.8",
        })
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            roh = resp.read().decode("utf-8", errors="replace")
        return roh, None
    except Exception as e:
        return None, str(e)


# ---------------------------------------------------------------- Website-Abgleich (mbb.com)

def website_abgleich(config, fetch=hole_seite):
    aktiv = config.get("website_abgleich", {}).get("aktiv", False)
    ergebnisse = {}
    if not aktiv:
        return ergebnisse
    seiten_cache = {}
    for check in config["checks"]:
        werte = check.get("pruefwerte") or []
        werte_roh = check.get("pruefwerte_roh") or []
        verboten = check.get("verboten") or []
        if not (werte or werte_roh or verboten):
            ergebnisse[check["id"]] = {"status": "keine"}
            continue
        url = check["url"]
        if url not in seiten_cache:
            seiten_cache[url] = fetch(url)
        roh, fehler = seiten_cache[url]
        if fehler:
            ergebnisse[check["id"]] = {"status": "fehler", "fehler": fehler}
            continue
        text = html_zu_text(roh)
        roh_norm = normalisiere(roh)
        fehlend = [w for w in werte if normalisiere(w) not in text]
        fehlend += [f"[Quellcode] {w}" for w in werte_roh
                    if normalisiere(w) not in roh_norm]
        unerwuenscht = []
        for v in verboten:
            wert = v["wert"] if isinstance(v, dict) else v
            ab = v.get("ab") if isinstance(v, dict) else None
            if isinstance(ab, str):
                ab = date.fromisoformat(ab)
            if ab and date.today() < ab:
                continue
            if normalisiere(wert) in text:
                unerwuenscht.append(wert)
        if fehlend or unerwuenscht:
            ergebnisse[check["id"]] = {"status": "abweichung",
                                       "fehlend": fehlend,
                                       "unerwuenscht": unerwuenscht}
        else:
            ergebnisse[check["id"]] = {"status": "ok"}
    return ergebnisse


# ---------------------------------------------------------------- Externer Abgleich (z. B. MarketScreener)

def finde_prozent(text, suchbegriff):
    """Sucht jede Fundstelle des Suchbegriffs und liest den ersten
    Prozentwert im Fenster dahinter. Liefert float oder None."""
    start = 0
    while True:
        pos = text.find(suchbegriff, start)
        if pos < 0:
            return None
        fenster = text[pos:pos + SUCHFENSTER]
        m = PROZENT_RE.search(fenster)
        if m:
            return float(m.group(1).replace(",", "."))
        start = pos + len(suchbegriff)


def extern_abgleich(config, fetch=hole_seite):
    """Vergleicht Prozentwerte externer Quellen mit den erwarteten Werten.
    Liefert dict check_id -> {"status": ok|abweichung|nicht_auswertbar|fehler, ...}."""
    aktiv = config.get("extern_abgleich", {}).get("aktiv", False)
    ergebnisse = {}
    if not aktiv:
        return ergebnisse
    seiten_cache = {}
    for check in config["checks"]:
        ext = check.get("extern")
        if not ext:
            continue
        url = ext["url"]
        if url not in seiten_cache:
            seiten_cache[url] = fetch(url)
        roh, fehler = seiten_cache[url]
        if fehler:
            ergebnisse[check["id"]] = {"status": "fehler", "fehler": fehler,
                                       "url": url}
            continue
        gefunden = finde_prozent(html_zu_text(roh), ext.get("suchbegriff", "MBB"))
        if gefunden is None:
            ergebnisse[check["id"]] = {"status": "nicht_auswertbar", "url": url}
            continue
        erwartet = float(ext["erwartet"])
        toleranz = float(ext.get("toleranz", 0.5))
        if abs(gefunden - erwartet) > toleranz:
            ergebnisse[check["id"]] = {"status": "abweichung",
                                       "gefunden": gefunden,
                                       "erwartet": erwartet, "url": url}
        else:
            ergebnisse[check["id"]] = {"status": "ok", "gefunden": gefunden,
                                       "erwartet": erwartet, "url": url}
    return ergebnisse


# ---------------------------------------------------------------- Meldungs-Wächter (EQS)

MONATE = {"Januar": 1, "Februar": 2, "März": 3, "April": 4, "Mai": 5,
          "Juni": 6, "Juli": 7, "August": 8, "September": 9,
          "Oktober": 10, "November": 11, "Dezember": 12}
NEWS_LINK_RE = re.compile(
    r'href="(https://www\.eqs-news\.com/de/news/([^/"]+)/[^"]+)"[^>]*>(.*?)</a>',
    re.S)
NEWS_DATUM_RE = re.compile(
    r'(\d{1,2})\.?\s+(Januar|Februar|März|April|Mai|Juni|Juli|August|'
    r'September|Oktober|November|Dezember)\s+(\d{4})')


def saeubere_titel(t):
    t = re.sub(r"<[^>]+>", " ", t)
    t = normalisiere(t).strip()
    t = re.sub(r"^\d{1,2}:\d{2}\s+", "", t)   # führende Uhrzeit entfernen
    return t[:220]


def news_extrahieren(roh):
    """Liest Meldungen (URL, Kategorie, Titel, Datum) aus der EQS-Seite.
    Datumszeilen stehen im Dokument vor den zugehörigen Meldungen."""
    ereignisse = []
    for m in NEWS_DATUM_RE.finditer(roh):
        try:
            d = date(int(m.group(3)), MONATE[m.group(2)], int(m.group(1)))
            ereignisse.append((m.start(), "datum", d))
        except ValueError:
            pass
    for m in NEWS_LINK_RE.finditer(roh):
        ereignisse.append((m.start(), "link",
                           (m.group(1), m.group(2), saeubere_titel(m.group(3)))))
    ereignisse.sort(key=lambda x: x[0])
    items, aktuelles_datum = [], None
    for _, art, wert in ereignisse:
        if art == "datum":
            aktuelles_datum = wert
        else:
            url, kategorie, titel = wert
            items.append({"url": url, "kategorie": kategorie, "titel": titel,
                          "datum": aktuelles_datum.isoformat()
                          if aktuelles_datum else None})
    return items


def news_relevant(item, kategorien, stichwoerter):
    if item["kategorie"] in kategorien:
        return True
    t = item["titel"].lower()
    return any(s.lower() in t for s in stichwoerter)


def news_waechter(config, state, fetch=hole_seite):
    """Vergleicht die EQS-Meldungslisten mit dem gemerkten Bestand in
    state.json. Liefert None (abgeschaltet) oder dict mit 'relevant'
    (alle passenden Meldungen), 'neu' (erstmals gesehene) und 'fehler'.
    Beim allerersten Lauf einer Quelle wird nur der Ausgangsbestand
    gespeichert (kein Alarm)."""
    conf = config.get("news_waechter") or {}
    if not conf.get("aktiv"):
        return None
    kategorien = conf.get("kategorien") or []
    stichwoerter = conf.get("stichwoerter") or []
    gesehen = state.setdefault("_news", {})
    erg = {"relevant": [], "neu": [], "fehler": []}
    for q in conf.get("quellen", []):
        roh, fehler = fetch(q["url"])
        if fehler:
            erg["fehler"].append({"quelle": q["name"], "fehler": fehler})
            continue
        items = news_extrahieren(roh)
        baseline = not any(v.get("quelle") == q["name"]
                           for v in gesehen.values())
        for it in items:
            if not news_relevant(it, kategorien, stichwoerter):
                continue
            eintrag = {**it, "quelle": q["name"]}
            erg["relevant"].append(eintrag)
            if it["url"] not in gesehen:
                gesehen[it["url"]] = {"titel": it["titel"],
                                      "kategorie": it["kategorie"],
                                      "datum": it["datum"],
                                      "quelle": q["name"],
                                      "erstmals": date.today().isoformat(),
                                      "baseline": baseline}
                if not baseline:
                    erg["neu"].append(eintrag)
    return erg


# ---------------------------------------------------------------- Anteils-Rechner (Directors' Dealings)

EUR_RE = re.compile(r'(\d{1,3}(?:\.\d{3})*(?:,\d+)?|\d+(?:,\d+)?)\s*EUR')


def parse_zahl_de(s):
    return float(s.replace(".", "").replace(",", "."))


def dd_stueckzahl(roh):
    """Liest aus einer Directors'-Dealings-Meldung die Stückzahl:
    bevorzugt aus 'Aggregierte Informationen' (Preis, Volumen in EUR),
    Stückzahl = Volumen / Preis."""
    text = html_zu_text(roh)
    pos = text.find("Aggregierte")
    seg = text[pos:pos + 500] if pos >= 0 else text
    zahlen = [parse_zahl_de(m.group(1)) for m in EUR_RE.finditer(seg)]
    if len(zahlen) >= 2 and zahlen[0] > 0:
        stueck = zahlen[1] / zahlen[0]
        if stueck >= 1:
            return round(stueck)
    return None


def dd_rechner(config, state, news, fetch=hole_seite):
    """Verrechnet neue Directors'-Dealings der Gründer(-Holdings) mit dem
    Basis-Anteil von der Website zu einem VORSCHLAG für den neuen Anteil.
    Meldungen bis einschließlich basis_stand gelten als im Website-Wert
    enthalten. Ändert nie selbst Sollwerte."""
    conf = config.get("dd_rechner") or {}
    if not conf.get("aktiv") or news is None:
        return None
    personen = conf.get("personen") or []
    basis_stand = conf.get("basis_stand")
    if isinstance(basis_stand, str):
        basis_stand = date.fromisoformat(basis_stand)
    dd_state = state.setdefault("_dd", {})
    neu_urls = []
    for n in news.get("relevant", []):
        if n["kategorie"] != "directors-dealings":
            continue
        if personen and not any(p.lower() in n["titel"].lower()
                                for p in personen):
            continue
        if n["url"] in dd_state:
            continue
        eintrag = {"titel": n["titel"], "datum": n["datum"]}
        d = date.fromisoformat(n["datum"]) if n.get("datum") else None
        if basis_stand and d and d <= basis_stand:
            eintrag["status"] = "in_basis"
        else:
            roh, fehler = fetch(n["url"])
            t = n["titel"].lower()
            richtung = ("kauf" if "kauf" in t else
                        "verkauf" if "verkauf" in t else None)
            if richtung is None and roh:
                dt = html_zu_text(roh).lower()
                richtung = ("kauf" if " kauf" in dt else
                            "verkauf" if "verkauf" in dt else None)
            stueck = dd_stueckzahl(roh) if (roh and not fehler) else None
            if stueck and richtung:
                eintrag.update(status="erfasst", stueck=stueck,
                               richtung=richtung)
            else:
                eintrag["status"] = "nicht_ermittelbar"
            neu_urls.append(n["url"])
        dd_state[n["url"]] = eintrag
    beitraege = [{**e, "url": u} for u, e in dd_state.items()
                 if e.get("status") == "erfasst"]
    offen = [{**e, "url": u} for u, e in dd_state.items()
             if e.get("status") == "nicht_ermittelbar"]
    delta = sum(e["stueck"] * (1 if e["richtung"] == "kauf" else -1)
                for e in beitraege)
    gesamt = int(conf.get("aktien_gesamt", 5436169))
    basis = float(conf.get("basis_prozent", 71.0))
    return {"basis": basis, "basis_stand": str(conf.get("basis_stand", "")),
            "gesamt": gesamt, "delta": delta,
            "vorschlag": basis + delta / gesamt * 100,
            "beitraege": beitraege, "offen": offen,
            "neu": neu_urls,
            "berichte_url": conf.get("berichte_url")}


def de_zahl(x, dez=2):
    s = f"{x:,.{dez}f}"
    return s.replace(",", "X").replace(".", ",").replace("X", ".")


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


def collect(config, state, web=None, extern=None):
    vorlauf = config.get("erinnerung", {}).get("vorlauf_tage", 14)
    termine_daten = kalender_daten(config)
    web = web or {}
    extern = extern or {}
    checks = []
    for c in config["checks"]:
        s, faellig, tage, hinweis = status_of(c, state, vorlauf, termine_daten)
        checks.append({**c, "status": s, "faellig_am": faellig, "tage": tage,
                       "hinweis": hinweis,
                       "web": web.get(c["id"]),
                       "ext": extern.get(c["id"]),
                       "zuletzt": state[c["id"]]["zuletzt_bestaetigt"]})
    termine = []
    for k in config.get("kalender", []):
        s, d, tage = kalender_status(k, vorlauf)
        termine.append({**k, "status": s, "datum": d, "tage": tage})
    return checks, termine, vorlauf


# ---------------------------------------------------------------- E-Mail

def build_mail_text(due_checks, due_termine, abweichungen, extern_abw,
                    news_neu=None, dd=None):
    lines = ["Guten Tag,"]
    if news_neu:
        lines += ["", "NEUE KAPITALMARKT-MELDUNG(EN) auf EQS veröffentlicht:"]
        for n in news_neu:
            lines.append("")
            lines.append(f"• [{n['kategorie']}] {n['titel']}")
            if n.get("datum"):
                lines.append(f"  Datum: {n['datum']}")
            lines.append(f"  Meldung: {n['url']}")
        lines.append("")
        lines.append("  Bitte prüfen, ob die Meldung Auswirkungen auf Inhalte")
        lines.append("  der Website hat (z. B. Anteile, Aktionärsstruktur,")
        lines.append("  Rückkaufprogramm) und ggf. Website + config.yaml anpassen.")
    if dd and dd.get("neu"):
        lines += ["", "ANTEILS-VORSCHLAG (rechnerisch, bitte prüfen):"]
        for b in dd["beitraege"]:
            if b["url"] in dd["neu"]:
                vz = "+" if b["richtung"] == "kauf" else "-"
                lines.append(f"  {b['richtung'].capitalize()} von "
                             f"{de_zahl(b['stueck'], 0)} Aktien "
                             f"({b.get('datum') or '-'}): {b['url']}")
        lines.append(f"  Basis lt. Website: {de_zahl(dd['basis'])} % "
                     f"-> Vorschlag NEU: ca. {de_zahl(dd['vorschlag'])} % "
                     f"(Delta {dd['delta']:+d} von {de_zahl(dd['gesamt'], 0)} Aktien)")
        lines.append("  Nach Übernahme auf der Website: basis_prozent und "
                     "basis_stand in config.yaml aktualisieren.")
    if abweichungen:
        lines += ["", "ACHTUNG – der Website-Abgleich hat Abweichungen gefunden",
                  "(erwartete Angaben stehen nicht mehr auf der mbb.com-Seite):"]
        for c in abweichungen:
            lines.append("")
            lines.append(f"• {c['titel']}")
            lines.append(f"  Seite: {c['url']}")
            for w in c["web"].get("fehlend", []):
                lines.append(f"  - nicht mehr gefunden: \"{w}\"")
            for w in c["web"].get("unerwuenscht", []):
                lines.append(f"  - sollte entfernt sein, steht aber noch da: \"{w}\"")
            lines.append("  Bitte Seite prüfen. Ist die Änderung beabsichtigt, den")
            lines.append("  neuen Wert in config.yaml (pruefwerte/felder) nachziehen.")
    if extern_abw:
        lines += ["", "ACHTUNG – externe Quelle weicht vom Website-Wert ab",
                  "(bitte manuell prüfen; Rundungs-/Methodikunterschiede möglich):"]
        for c in extern_abw:
            e = c["ext"]
            lines.append("")
            lines.append(f"• {c['titel']}")
            lines.append(f"  Erwartet (mbb.com): {e['erwartet']} % – "
                         f"extern gefunden: {e['gefunden']} %")
            lines.append(f"  Externe Quelle: {e['url']}")
            lines.append(f"  Eigene Seite: {c['url']}")
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
    empfaenger = mail_cfg["empfaenger"]
    if isinstance(empfaenger, str):
        empfaenger = [empfaenger]
    msg["From"] = mail_cfg["absender"]
    msg["To"] = ", ".join(empfaenger)
    port = int(os.environ.get("SMTP_PORT", 587))
    with smtplib.SMTP(host, port) as s:
        s.starttls()
        user, pw = os.environ.get("SMTP_USER"), os.environ.get("SMTP_PASS")
        if user and pw:
            s.login(user, pw)
        s.send_message(msg)
    print(f"E-Mail an {', '.join(empfaenger)} gesendet ({anzahl} Punkte).")
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
        teile = []
        if web.get("fehlend"):
            teile.append("nicht mehr gefunden: " +
                         ", ".join(f"„{w}“" for w in web["fehlend"]))
        if web.get("unerwuenscht"):
            teile.append("sollte entfernt sein, steht aber noch da: " +
                         ", ".join(f"„{w}“" for w in web["unerwuenscht"]))
        return f'<p class="web web-alarm">Abweichung – {"; ".join(teile)}</p>' 
    if s == "fehler":
        return '<p class="web web-warn">Website-Abgleich: Seite nicht erreichbar</p>'
    return '<p class="web web-neutral">Website-Abgleich: keine Prüfwerte hinterlegt</p>'


def render_ext(ext):
    if ext is None:
        return ""
    s = ext["status"]
    if s == "ok":
        return (f'<p class="web web-ok">Externer Abgleich: {ext["gefunden"]} % '
                f'(erwartet {ext["erwartet"]} %) – im Rahmen</p>')
    if s == "abweichung":
        return (f'<p class="web web-alarm">Externer Abgleich: Quelle nennt '
                f'{ext["gefunden"]} %, erwartet {ext["erwartet"]} % – bitte '
                f'manuell prüfen</p>')
    if s == "fehler":
        return ('<p class="web web-warn">Externer Abgleich: Quelle nicht '
                'erreichbar (ggf. Bot-Schutz) – manuell über Link prüfen</p>')
    return ('<p class="web web-warn">Externer Abgleich: kein Prozentwert '
            'auf der Quellseite gefunden – manuell über Link prüfen</p>')


def render_referenzen(refs):
    if not refs:
        return ""
    links = " · ".join(f'<a href="{r["url"]}" target="_blank">{r["titel"]}</a>'
                       for r in refs)
    return f'<p class="refs">Vergleichsquellen (manuell): {links}</p>'


def render_news(news):
    if news is None:
        return '  <p class="news-leer">Der Meldungs-Wächter ist abgeschaltet (news_waechter.aktiv in config.yaml).</p>'
    zeilen = ""
    heute = date.today()
    neu_urls = {n["url"] for n in news.get("neu", [])}
    # neueste zuerst, maximal 12
    def sortdatum(n):
        return n.get("datum") or "0000-00-00"
    for n in sorted(news.get("relevant", []), key=sortdatum, reverse=True)[:12]:
        ist_neu = n["url"] in neu_urls
        badge = ('<span class="badge badge-over">NEU</span>' if ist_neu
                 else '<span class="badge badge-past">gesehen</span>')
        datum = n.get("datum") or "–"
        zeilen += f"""
        <tr>
          <td class="t-datum">{datum}</td>
          <td>{n['kategorie']}</td>
          <td><a href="{n['url']}" target="_blank">{n['titel']}</a></td>
          <td class="t-status">{badge}</td>
        </tr>"""
    fehler_html = ""
    for f in news.get("fehler", []):
        fehler_html += (f'  <p class="web web-warn">Quelle „{f["quelle"]}" '
                        f'nicht erreichbar – nächster Versuch beim nächsten Lauf.</p>\n')
    if not zeilen and not fehler_html:
        return '  <p class="news-leer">Keine relevanten Meldungen im Bestand.</p>'
    tabelle = (f'  <table>\n    <tbody>{zeilen}\n    </tbody>\n  </table>'
               if zeilen else "")
    return fehler_html + tabelle


def render_dd(dd):
    if dd is None:
        return ""
    basis_txt = de_zahl(dd["basis"])
    zeilen = ""
    for b in sorted(dd["beitraege"], key=lambda x: x.get("datum") or "", reverse=True):
        vz = "+" if b["richtung"] == "kauf" else "−"
        zeilen += (f'<div class="feld"><span class="feld-label">'
                   f'{b.get("datum") or "–"} · {b["richtung"].capitalize()} · '
                   f'<a href="{b["url"]}" target="_blank">Meldung</a></span>'
                   f'<span class="feld-wert">{vz}{de_zahl(b["stueck"], 0)} Aktien</span></div>')
    offen_html = ""
    for o in dd["offen"]:
        offen_html += (f'<p class="web web-warn">Stückzahl nicht automatisch '
                       f'ermittelbar – bitte <a href="{o["url"]}" target="_blank">'
                       f'Meldung öffnen</a> ({o.get("datum") or "–"})</p>')
    if dd["delta"] != 0:
        vz = "+" if dd["delta"] > 0 else "−"
        vorschlag = (f'<p class="dd-vorschlag"><em>Vorschlag: {basis_txt} % '
                     f'{vz} {de_zahl(abs(dd["delta"]), 0)} / '
                     f'{de_zahl(dd["gesamt"], 0)} Aktien '
                     f'≈ <strong>{de_zahl(dd["vorschlag"])} %</strong> '
                     f'(rechnerisch, aus den oben gelisteten Dealings – '
                     f'kein amtlicher Wert)</em></p>')
    else:
        vorschlag = (f'<p class="dd-vorschlag"><em>Kein neuer Vorschlag – '
                     f'Basis unverändert {basis_txt} % '
                     f'(keine verrechenbaren Dealings seit '
                     f'{dd["basis_stand"] or "Basisdatum"})</em></p>')
    berichte = ""
    if dd.get("berichte_url"):
        berichte = (f'<p class="refs">Berichte & Free Float (manuell): '
                    f'<a href="{dd["berichte_url"]}" target="_blank">'
                    f'EQS – Berichte MBB SE</a></p>')
    return f"""
  <h2>Anteil Gründer – Rechner (Directors&rsquo; Dealings)</h2>
  <p class="news-erklaerung">Basis ist der Anteil laut mbb.com
  ({basis_txt} % mittelbar, Nesemeier/Freimuth, Stand {dd["basis_stand"] or "–"}).
  Meldet EQS ein neues Directors&rsquo; Dealing der Gründer bzw. ihrer
  Holdings, liest der Tracker aus der Meldung Preis und Volumen, errechnet
  daraus die Stückzahl und schlägt kursiv einen neuen Prozentsatz vor
  (Stückzahl ÷ {de_zahl(dd["gesamt"], 0)} Aktien). Übernommen wird der Wert
  erst, wenn ein Mensch ihn geprüft, die Website aktualisiert und danach
  basis_prozent + basis_stand in config.yaml nachgezogen hat – dann leert
  sich diese Rechnung automatisch.</p>
  <div class="card status-ok" style="max-width:760px">
    <div class="felder">
      <div class="feld"><span class="feld-label">Basis lt. Website (mittelbar)</span><span class="feld-wert">{basis_txt} %</span></div>
      <div class="feld"><span class="feld-label">Aktien gesamt</span><span class="feld-wert">{de_zahl(dd["gesamt"], 0)}</span></div>
      {zeilen}
    </div>
    {offen_html}
    {vorschlag}
    {berichte}
    <div class="card-fuss">Nach Übernahme auf der Website: basis_prozent und basis_stand in config.yaml aktualisieren.</div>
  </div>"""


def render_dashboard(checks, termine, vorlauf, abgleich_aktiv, extern_aktiv, gruppen, news=None, dd=None):
    def fmt(d):
        return d.strftime("%d.%m.%Y")

    def badge(status):
        cls, label = BADGE[status]
        return f'<span class="badge badge-{cls}">{label}</span>'

    def hat_alarm(c):
        return ((c["web"] and c["web"]["status"] == "abweichung") or
                (c["ext"] and c["ext"]["status"] == "abweichung"))

    def sortkey(c):
        order = {"überfällig": 0, "fällig": 1, "aktuell": 2}
        return (0 if hat_alarm(c) else 1, order[c["status"]], c["faellig_am"])

    def grad(g):
        f1 = g.get("farbe", "#1a1a1a")
        f2 = g.get("farbe2") or f1
        return f"linear-gradient(to bottom, {f1} 50%, {f2} 50%)"
    farben = {g["name"]: grad(g) for g in gruppen}
    gruppen_cards = {g["name"]: "" for g in gruppen}
    for c in sorted(checks, key=sortkey):
        gname = c.get("gruppe") or "MBB"
        if gname not in gruppen_cards:
            gruppen_cards[gname] = ""
            farben.setdefault(gname, "linear-gradient(to bottom, #1a1a1a, #1a1a1a)")
        gfarbe = farben.get(gname, "linear-gradient(to bottom, #1a1a1a, #1a1a1a)")
        cls, _ = BADGE[c["status"]]
        if hat_alarm(c):
            cls = "over"
        hinweis = (f'<p class="hinweis">{c["hinweis"]}</p>' if c["hinweis"] else "")
        rhythmus = ("quartalsweise · Finanzkalender"
                    if c["intervall"] == "quartalsweise" else "jährlich")
        gruppen_cards[gname] += f"""
      <article class="card status-{cls}" style="background:{gfarbe} no-repeat left top / 5px 100%, #ffffff">
        <div class="card-kopf">
          <h3>{c['titel']}</h3>
          {badge(c['status'])}
        </div>
        <a class="card-url" href="{c['url']}" target="_blank">{c['url'].replace('https://www.mbb.com', 'mbb.com')}</a>
        <div class="felder">{render_felder(c['felder'])}</div>
        {('<p class="fundstelle">Zu ändern: ' + c['fundstelle'] + ' – <a href="' + c['url'] + '" target="_blank">Seite öffnen</a></p>') if c.get('fundstelle') else ''}
        {render_web(c['web'])}
        {render_ext(c['ext'])}
        {render_referenzen(c.get('referenzen'))}
        {hinweis}
        <div class="card-meta">
          <div><span class="meta-label">Rhythmus</span>{rhythmus}</div>
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
    n_alarm = sum(1 for c in checks
                  if (c["web"] and c["web"]["status"] == "abweichung") or
                     (c["ext"] and c["ext"]["status"] == "abweichung"))
    pruefpunkte_html = ""
    for gname, inhalt in gruppen_cards.items():
        if not inhalt:
            continue
        gfarbe = farben.get(gname, "linear-gradient(to bottom, #1a1a1a, #1a1a1a)")
        pruefpunkte_html += (
            f'  <div class="gruppe"><span class="gruppe-balken" '
            f'style="background:{gfarbe}"></span>{gname}</div>\n'
            f'  <div class="grid">{inhalt}\n  </div>\n')

    news_html = render_news(news) + render_dd(dd)

    info = []
    info.append("Website-Abgleich aktiv" if abgleich_aktiv
                else "Website-Abgleich abgeschaltet")
    info.append("externer Abgleich aktiv" if extern_aktiv
                else "externer Abgleich abgeschaltet")

    return f"""<!DOCTYPE html>
<html lang="de">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>MBB Website-Tracker – Statusübersicht</title>
<link rel="icon" type="image/png" href="https://upload.wikimedia.org/wikipedia/commons/thumb/0/00/Logo_MBB_SE.svg/1280px-Logo_MBB_SE.svg.png">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Sans:wght@400;500;600&display=swap" rel="stylesheet">
<style>
  :root {{
    --ink: #1a1a1a; --muted: #6b6b6b; --line: #dcdcdc; --panel: #f0f0f0;
    --red: #e2001a;
    --ok: #1d7a4f; --ok-bg: #e7f2ec;
    --due: #96650a; --due-bg: #fbf1dc;
    --over: #b3121b; --over-bg: #fbe6e7;
    --past: #6b6b6b; --past-bg: #ececec;
  }}
  * {{ box-sizing: border-box; font-family: "IBM Plex Sans", "Segoe UI", sans-serif; }}
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
  .gruppe {{ display: flex; align-items: center; gap: 12px; margin: 34px 0 14px;
             font-size: 14px; font-weight: 600; letter-spacing: .08em;
             text-transform: uppercase; }}
  .gruppe-balken {{ width: 7px; height: 24px; display: inline-block; }}
  .gruppe::after {{ content: ""; flex: 1; height: 1px; background: var(--line); }}
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
  .fundstelle {{ font-size: 12px; color: var(--ink); background: var(--panel);
                 padding: 6px 10px; margin: 10px 0 0; }}
  .fundstelle a {{ color: var(--red); }}
  .news-erklaerung {{ font-size: 13px; color: var(--muted); max-width: 900px;
                      margin: 0 0 16px; }}
  .news-leer {{ font-size: 13px; color: var(--muted); }}
  .dd-vorschlag {{ font-size: 14px; margin: 12px 0 0; padding: 8px 12px;
                   background: var(--due-bg); }}
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
  code {{ font-family: "IBM Plex Sans", "Segoe UI", sans-serif; font-size: 12px; }}
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
    <p>Statusübersicht der zu pflegenden Inhalte auf mbb.com · Stand: {date.today().strftime('%d.%m.%Y')} · Erinnerung ab {vorlauf} Tagen vor Fälligkeit · {' · '.join(info)}</p>
  </div>
</header>
<main>
  <div class="summary">
    <div class="sum over"><div class="num">{n_alarm}</div><div class="lbl">Abweichungen</div></div>
    <div class="sum over"><div class="num">{n_over}</div><div class="lbl">überfällig</div></div>
    <div class="sum due"><div class="num">{n_due}</div><div class="lbl">fällig</div></div>
    <div class="sum ok"><div class="num">{n_ok}</div><div class="lbl">aktuell</div></div>
  </div>

  <h2>Prüfpunkte</h2>
{pruefpunkte_html}

  <h2>Finanzkalender</h2>
  <table>
    <tbody>{termin_rows}
    </tbody>
  </table>

  <h2>Meldungs-Wächter (EQS)</h2>
  <p class="news-erklaerung">So funktioniert dieser Abschnitt: Der Tracker ruft
  bei jedem Lauf die EQS-News-Übersicht der MBB SE ab und merkt sich alle
  relevanten Meldungen (Kategorien wie Directors&rsquo; Dealings, Stimmrechte,
  Ad-hoc sowie Stichwörter wie Nesemeier, Freimuth, Aktienrückkauf oder
  Töchternamen). Erscheint eine neue relevante Meldung – z. B. ein Kauf oder
  Verkauf der Gründer über ihre Holdings oder eine Änderung der MBB-Anteile an
  den Töchtern –, wird sofort eine E-Mail verschickt und die Meldung hier
  7 Tage als NEU markiert. Der Wächter ändert keine Werte selbst: Ein Mensch
  liest die Meldung und passt bei Bedarf die Website und die Sollwerte in
  config.yaml an. Beim allerersten Lauf werden vorhandene Meldungen nur als
  Ausgangsbestand gespeichert, ohne Alarm.</p>
{news_html}

  <footer>Quartalsweise Punkte werden zum jeweils nächsten Finanzkalender-Termin
    nach der letzten Bestätigung fällig; jährliche Punkte 365 Tage nach der
    letzten Bestätigung. Der Website-Abgleich prüft, ob die hinterlegten
    Prüfwerte noch auf der jeweiligen mbb.com-Seite stehen. Der externe
    Abgleich liest den MBB-Anteil bei externen Quellen (z. B. MarketScreener)
    aus und vergleicht innerhalb einer Toleranz; externe Portale runden anders
    und können automatisierte Abrufe blockieren – dann gilt der manuelle
    Vergleich über die Links. Erledigte Prüfungen bestätigen: Datum in
    <code>state.json</code> auf heute setzen oder
    <code>python tracker.py confirm &lt;id&gt;</code>.</footer>
</main>
</body>
</html>"""


def write_dashboard(config, state, web=None, extern=None, news=None, dd=None):
    checks, termine, vorlauf = collect(config, state, web, extern)
    aktiv = config.get("website_abgleich", {}).get("aktiv", False)
    ext_aktiv = config.get("extern_abgleich", {}).get("aktiv", False)
    gruppen = config.get("gruppen") or [{"name": "MBB", "farbe": "#1a1a1a"}]
    html = render_dashboard(checks, termine, vorlauf, aktiv, ext_aktiv, gruppen, news, dd)
    DASHBOARD_FILE.write_text(html, encoding="utf-8")
    INDEX_FILE.write_text(html, encoding="utf-8")
    print(f"Dashboard aktualisiert: {DASHBOARD_FILE} (+ index.html)")


# ---------------------------------------------------------------- Kommandos

def cmd_run():
    config = load_config()
    state = ensure_state(config, load_state())
    web = website_abgleich(config)
    extern = extern_abgleich(config)
    news = news_waechter(config, state)
    dd = dd_rechner(config, state, news)
    save_state(state)   # neue Meldungen/Dealings im Bestand sichern
    checks, termine, vorlauf = collect(config, state, web, extern)
    due_checks = [c for c in checks if c["status"] in ("fällig", "überfällig")]
    due_termine = [t for t in termine if t["status"] == "steht an"]
    abweichungen = [c for c in checks
                    if c["web"] and c["web"]["status"] == "abweichung"]
    extern_abw = [c for c in checks
                  if c["ext"] and c["ext"]["status"] == "abweichung"]
    news_neu = (news or {}).get("neu", []) if news else []
    if due_checks or due_termine or abweichungen or extern_abw or news_neu:
        text = build_mail_text(due_checks, due_termine, abweichungen,
                               extern_abw, news_neu, dd)
        send_mail(config, text, len(due_checks) + len(due_termine)
                  + len(abweichungen) + len(extern_abw) + len(news_neu))
    else:
        print("Nichts fällig, keine Abweichungen – keine Erinnerung nötig.")
    for c in checks:
        if c["web"] and c["web"]["status"] == "fehler":
            print(f"Warnung: {c['url']} nicht erreichbar "
                  f"({c['web'].get('fehler', '')})")
        if c["ext"] and c["ext"]["status"] in ("fehler", "nicht_auswertbar"):
            print(f"Warnung: externer Abgleich für {c['id']} nicht möglich "
                  f"({c['ext'].get('fehler', 'kein Prozentwert gefunden')})")
    write_dashboard(config, state, web, extern, news, dd)


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
    news = news_waechter(config, state)
    dd = dd_rechner(config, state, news)
    save_state(state)
    write_dashboard(config, state, website_abgleich(config),
                    extern_abgleich(config), news, dd)


def cmd_list():
    config = load_config()
    state = ensure_state(config, load_state())
    web = website_abgleich(config)
    extern = extern_abgleich(config)
    checks, termine, _ = collect(config, state, web, extern)
    w = max(len(c["id"]) for c in checks)
    for c in checks:
        webinfo = ""
        if c["web"]:
            webinfo = {"ok": "web ok", "abweichung": "WEB-ABWEICHUNG",
                       "fehler": "web-fehler", "keine": "-"}[c["web"]["status"]]
        extinfo = ""
        if c["ext"]:
            extinfo = {"ok": f"extern ok ({c['ext'].get('gefunden')} %)",
                       "abweichung": f"EXTERN-ABWEICHUNG ({c['ext'].get('gefunden')} %)",
                       "fehler": "extern-fehler",
                       "nicht_auswertbar": "extern n. auswertbar"}[c["ext"]["status"]]
        print(f"{c['id']:<{w}}  {c['status']:<11}  fällig "
              f"{c['faellig_am'].strftime('%d.%m.%Y')}  {webinfo:<15}  "
              f"{extinfo:<28}  {c['titel']}")
    print()
    for t in termine:
        print(f"{t['datum'].strftime('%d.%m.%Y')}  {t['status']:<9}  {t['titel']}")


def cmd_dashboard():
    config = load_config()
    state = ensure_state(config, load_state())
    news = news_waechter(config, state)
    dd = dd_rechner(config, state, news)
    save_state(state)
    write_dashboard(config, state, website_abgleich(config),
                    extern_abgleich(config), news, dd)


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

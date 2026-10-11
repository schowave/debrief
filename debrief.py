#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["pyyaml>=6"]
# ///
"""Sortiert MacWhisper-Transkripte im Obsidian-Vault.

Für jede neue Notiz im Inbox-Ordner:
  1. Klassifizierung (Kunde/intern), Zusammenfassung und Todos per `claude -p`
  2. Frontmatter ergänzen, Abschnitte vor das Transkript setzen
  3. Notiz als `<Datum> <Titel>.md` in den Ordner der Kategorie verschieben

Aufruf:  uv run --script debrief.py [--dry-run] [--config config.yaml] [datei.md ...]

Normalerweise läuft das über server.py: dort wird jeder Vorschlag erst geprüft und
bestätigt. Dieses Skript direkt aufzurufen sortiert ohne Rückfrage ein.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import logging
import os
import re
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
MIN_FILE_AGE_S = 20  # MacWhisper soll fertig geschrieben haben
FRONTMATTER_RE = re.compile(r"\A---\n(.*?)\n---\n?", re.DOTALL)

log = logging.getLogger("debrief")


KATEGORIEN_HEADER = """\
# Kategorien: Schlüssel = Wert für das Property `kunde`.
# `hinweise` helfen dem Modell beim Zuordnen (Personen, Projekte, Begriffe).
# Projekte landen in `<ordner>/<Projekt>`, betreute Personen in `<ordner>/<Projekt>/<Person>`.
# Wird auch von der Review-Seite geschrieben (Kommentare gehen dabei verloren).
"""


def normalize_kategorien(kategorien: dict) -> dict:
    """Projekte dürfen in der Datei auch nur ein Hinweis-String sein."""
    for cat in kategorien.values():
        cat.setdefault("hinweise", "")
        cat["projekte"] = {
            name: proj if isinstance(proj, dict) else {"hinweise": proj or ""}
            for name, proj in (cat.get("projekte") or {}).items()
        }
        for proj in cat["projekte"].values():
            proj.setdefault("hinweise", "")
            proj["personen"] = list(proj.get("personen") or [])
    return kategorien


def load_config(path: Path) -> dict:
    cfg = yaml.safe_load(path.read_text())
    cfg["vault"] = Path(cfg["vault"]).expanduser()
    cfg["kategorien_datei"] = path.parent / "kategorien.yaml"
    cfg["kategorien"] = normalize_kategorien(cfg.get("kategorien") or {})
    reload_kategorien(cfg)
    return cfg


def reload_kategorien(cfg: dict) -> None:
    """kategorien.yaml neu einlesen, falls sie seit dem letzten Laden geändert wurde (z. B. von Hand)."""
    path = cfg["kategorien_datei"]
    if not path.exists():
        return
    mtime = path.stat().st_mtime
    if mtime != cfg.get("_kategorien_mtime"):
        cfg["kategorien"] = normalize_kategorien(yaml.safe_load(path.read_text()) or {})
        cfg["_kategorien_mtime"] = mtime


def save_kategorien(cfg: dict, kategorien: dict) -> None:
    kategorien = normalize_kategorien(kategorien)
    for key, cat in kategorien.items():
        if not re.fullmatch(r"[\w-]+", key) or not cat.get("ordner"):
            raise ValueError(f"Ungültige Kategorie: {key!r} (Name ohne Leerzeichen, Ordner nötig)")
        safe_folder(cfg, cat["ordner"])
    tmp = cfg["kategorien_datei"].with_suffix(".tmp")
    tmp.write_text(KATEGORIEN_HEADER + yaml.safe_dump(kategorien, sort_keys=False, allow_unicode=True))
    tmp.replace(cfg["kategorien_datei"])
    cfg["kategorien"] = kategorien
    cfg["_kategorien_mtime"] = cfg["kategorien_datei"].stat().st_mtime


def safe_folder(cfg: dict, folder: str) -> Path:
    path = (cfg["vault"] / folder).resolve()
    if not path.is_relative_to(cfg["vault"].resolve()):
        raise ValueError(f"Ordner liegt außerhalb des Vaults: {folder}")
    return path


def split_frontmatter(text: str) -> tuple[dict, str]:
    m = FRONTMATTER_RE.match(text)
    if not m:
        return {}, text
    return yaml.safe_load(m.group(1)) or {}, text[m.end():]


GELERNT = HERE / "gelernt.json"  # Korrekturen und Namen aus dem Review (lokal, nicht im Git)
MAX_KORREKTUREN_IM_PROMPT = 15
SPEAKER_RE = re.compile(r"^Speaker \d+$")


def load_gelernt() -> dict:
    try:
        data = json.loads(GELERNT.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        data = {}
    return {"korrekturen": data.get("korrekturen", []), "namen": data.get("namen", [])}


def save_gelernt(data: dict) -> None:
    tmp = GELERNT.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1))
    tmp.replace(GELERNT)


def is_me(cfg: dict, name: str) -> bool:
    """„David“ in der Config, „David Schowalter“ im Transkript → beides bin ich."""
    n, me = str(name or "").strip().casefold(), str(cfg.get("ich", "")).strip().casefold()
    return bool(me) and (n == me or n.startswith(me + " "))


def is_placeholder_speaker(name: str) -> bool:
    return bool(SPEAKER_RE.match(name.strip()))


def word_count(body: str, speakers: list[str]) -> int:
    """Wörter im Transkript ohne die Sprecherzeilen."""
    labels = {s.strip() for s in speakers}
    return sum(len(line.split()) for line in body.splitlines() if line.strip() and line.strip() not in labels)


def speaker_samples(body: str, speakers: list[str], length: int = 140) -> dict[str, str]:
    """Erste Aussage jedes Sprechers, damit man ihn beim Benennen erkennt."""
    labels, samples, current = {s.strip() for s in speakers}, {}, None
    for line in body.splitlines():
        text = line.strip()
        if text in labels:
            current = text
        elif text and current and current not in samples and len(text.split()) >= 3:
            samples[current] = text[:length] + ("…" if len(text) > length else "")
    return samples


def rename_speakers(meta: dict, body: str, names: dict[str, str] | None) -> tuple[dict, str]:
    """Ersetzt Sprecher-Platzhalter („Speaker 1“) in Frontmatter und Transkript durch echte Namen."""
    names = {k: v.strip() for k, v in (names or {}).items() if v and v.strip() and v.strip() != k}
    if not names:
        return meta, body
    meta = dict(meta)
    if meta.get("Speakers"):
        meta["Speakers"] = [names.get(str(s), str(s)) for s in meta["Speakers"]]
    body = "\n".join(names.get(line.strip(), line) if line.strip() in names else line for line in body.splitlines())
    return meta, body


def rename_in_result(result: dict, names: dict[str, str] | None) -> dict:
    """Sprecher-Platzhalter auch in Zusammenfassung, Entscheidungen und Todos ersetzen."""
    names = {k: v.strip() for k, v in (names or {}).items() if v and v.strip()}
    if not names:
        return result
    pattern = re.compile("|".join(rf"\b{re.escape(k)}\b(?!\d)" for k in sorted(names, key=len, reverse=True)))
    sub = lambda text: pattern.sub(lambda m: names[m[0]], text) if isinstance(text, str) else text
    out = dict(result)
    for key in ("titel", "zusammenfassung", "begruendung", "person"):
        out[key] = sub(out.get(key))
    out["entscheidungen"] = [sub(e) for e in out.get("entscheidungen", [])]
    out["todos"] = [{**t, "aufgabe": sub(t["aufgabe"]), "verantwortlich": sub(t.get("verantwortlich"))}
                    for t in out.get("todos", [])]
    return out


def korrekturen_prompt(cfg: dict) -> str:
    korrekturen = load_gelernt()["korrekturen"][-MAX_KORREKTUREN_IM_PROMPT:]
    if not korrekturen:
        return ""
    fmt = lambda z: ", ".join(f"{k}={v}" for k, v in z.items() if v) or "–"
    lines = [f"- „{k['titel']}“: {k['kurz']} → richtig: {fmt(k['richtig'])} (du hattest: {fmt(k['vorschlag'])})"
             for k in korrekturen]
    return (f"\n\nFrühere Korrekturen durch {cfg['ich']} – ähnliche Gespräche genauso zuordnen:\n"
            + "\n".join(lines))


# --- Duplikate ---------------------------------------------------------------

FINGERABDRUECKE = HERE / "fingerabdruecke.json"  # abgelegte Transkripte (lokal, nicht im Git)
SKETCH_K = 128      # Größe der Bottom-k-Skizze für die Ähnlichkeit
SHINGLE = 3         # Wortgruppen-Länge (3 verträgt Abweichungen einer erneuten Transkription)


def transcript_text(body: str, speakers: list[str]) -> str:
    """Nur der gesprochene Text: Sprecherzeilen raus (die werden ggf. umbenannt), klein, ohne Satzzeichen."""
    labels = {str(s).strip() for s in speakers}
    lines = [l for l in body.splitlines() if l.strip() and l.strip() not in labels]
    return " ".join(re.findall(r"\w+", " ".join(lines).lower()))


def fingerprint(body: str, speakers: list[str], meta: dict) -> dict:
    """Exakter Hash plus Bottom-k-Skizze der 3-Wort-Gruppen (≈ Jaccard-Ähnlichkeit)."""
    words = transcript_text(body, speakers).split()
    shingles = {" ".join(words[i:i + SHINGLE]) for i in range(max(1, len(words) - SHINGLE + 1))}
    hashes = sorted({int.from_bytes(hashlib.blake2b(x.encode(), digest_size=8).digest(), "big") for x in shingles})
    return {
        "hash": hashlib.sha256(" ".join(words).encode()).hexdigest()[:24],
        "skizze": hashes[:SKETCH_K],
        "woerter": len(words),
        "aufgenommen": str(meta.get("Recorded At") or ""),
        "dauer": str(meta.get("Duration") or ""),
    }


def similarity(a: dict, b: dict) -> float:
    if a["hash"] == b["hash"]:
        return 1.0
    sa, sb = set(a["skizze"]), set(b["skizze"])
    if not sa or not sb:
        return 0.0
    union_k = sorted(sa | sb)[:SKETCH_K]
    return sum(1 for h in union_k if h in sa and h in sb) / len(union_k)


def same_recording(a: dict, b: dict) -> bool:
    return bool(a["aufgenommen"]) and a["aufgenommen"] == b["aufgenommen"] and a["dauer"] == b["dauer"]


def load_fingerprints() -> dict:
    try:
        data = json.loads(FINGERABDRUECKE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        data = {}
    return {"notizen": data.get("notizen", []), "erlaubt": data.get("erlaubt", [])}


def save_fingerprints(data: dict) -> None:
    tmp = FINGERABDRUECKE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False))
    tmp.replace(FINGERABDRUECKE)


def add_fingerprint(fp: dict, titel: str, ziel: str) -> None:
    data = load_fingerprints()
    data["notizen"] = [n for n in data["notizen"] if n["ziel"] != ziel] + [{**fp, "titel": titel, "ziel": ziel}]
    save_fingerprints(data)


def index_vault(cfg: dict) -> int:
    """Bereits abgelegte Notizen (mit `kunde:` und Abschnitt „## Transkript“) einmalig erfassen."""
    data = load_fingerprints()
    known = {n["ziel"] for n in data["notizen"]}
    vault, inbox, added = cfg["vault"].resolve(), (cfg["vault"] / cfg["inbox"]).resolve(), 0
    for path in vault.rglob("*.md"):
        rel = str(path.relative_to(vault))
        if rel in known or path.is_relative_to(inbox) or "/." in "/" + rel or path.stat().st_size > 2_000_000:
            continue
        try:
            meta, text = split_frontmatter(path.read_text())
        except Exception:
            continue
        if not meta.get("kunde") or "## Transkript" not in text:
            continue
        body = text.split("## Transkript", 1)[1]
        speakers = [str(x) for x in meta.get("Speakers") or []]
        data["notizen"].append({**fingerprint(body, speakers, meta), "titel": path.stem, "ziel": rel})
        added += 1
    if added:
        save_fingerprints(data)
    return added


def find_duplicate(fp: dict, others: list[dict]) -> tuple[float, dict | None]:
    """Ähnlichstes Gegenstück; gleiche Aufnahme (Zeit + Dauer) zählt mindestens als 0,9."""
    best, match = 0.0, None
    for other in others:
        sim = similarity(fp, other)
        if same_recording(fp, other):
            sim = max(sim, 0.9)
        if sim > best:
            best, match = sim, other
    return best, match


KALENDER_APP = HERE / "bin" / "Debrief Kalender.app"
MAX_TERMIN_STUNDEN = 4  # längere Einträge (Messen, Urlaub) sind keine Besprechungen


def parse_duration(value) -> int:
    """MacWhisper-Dauer „38:37“ oder „1:02:03“ in Sekunden."""
    try:
        parts = [int(x) for x in str(value).split(":")]
    except ValueError:
        return 0
    secs = 0
    for part in parts:
        secs = secs * 60 + part
    return secs


def recording_window(meta: dict) -> tuple[dt.datetime, dt.datetime] | None:
    start = meta.get("Recorded At")
    if isinstance(start, str):
        try:
            start = dt.datetime.fromisoformat(start)
        except ValueError:
            return None
    if not isinstance(start, dt.datetime):
        return None
    start = start.replace(tzinfo=None)
    return start, start + dt.timedelta(seconds=parse_duration(meta.get("Duration")) or 3600)


def calendar_candidates(cfg: dict, meta: dict) -> list[dict]:
    """Kalendertermine zur Aufnahme – bester zuerst, leer wenn nichts passt. In Frage kommt ein Termin, wenn die
    Aufnahme zwischen `vor_beginn_min` vor und `nach_beginn_min` nach seinem Beginn startet oder er beim
    Aufnahmestart gerade läuft (z. B. langer Workshop)."""
    window = recording_window(meta)
    if not cfg.get("kalender", True) or not window or not KALENDER_APP.exists():
        return []
    start, end = window
    fenster = cfg.get("kalender_fenster", {})
    before = dt.timedelta(minutes=fenster.get("vor_beginn_min", 10))
    after = dt.timedelta(minutes=fenster.get("nach_beginn_min", 30))
    out = HERE / "pending" / f"kalender-{os.getpid()}-{time.monotonic_ns()}.out"  # keine .json: das sind Vorschläge
    try:
        subprocess.run(["open", "-W", "-g", "-n", "--stdout", str(out), str(KALENDER_APP), "--args",
                        "--von", (start - dt.timedelta(hours=MAX_TERMIN_STUNDEN)).isoformat(timespec="seconds"),
                        "--bis", (start + before).isoformat(timespec="seconds")], timeout=30, check=False,
                       stderr=subprocess.DEVNULL)  # „Unable to block on applications“, wenn die App schneller fertig ist
        data = json.loads(out.read_text() or "[]")
    except Exception:
        log.exception("Kalender nicht lesbar")
        return []
    finally:
        out.unlink(missing_ok=True)
    if isinstance(data, dict):
        log.warning("Kalender: %s", data.get("fehler"))
        return []
    scored = []
    for e in data:
        e_start, e_end = dt.datetime.fromisoformat(e["start"]), dt.datetime.fromisoformat(e["ende"])
        if e_end - e_start > dt.timedelta(hours=MAX_TERMIN_STUNDEN):
            continue
        starts_near = e_start - before <= start <= e_start + after
        running = e_start <= start < e_end
        if not (starts_near or running):
            continue
        distance = abs((start - e_start).total_seconds())  # je näher am Terminbeginn, desto wahrscheinlicher
        scored.append(((bool(e["teilnehmer"]), -distance), e))
    return [e for _, e in sorted(scored, key=lambda x: x[0], reverse=True)][:5]


def termin_line(termin: dict, limit: int) -> str:
    people = [p for p in termin.get("teilnehmer", []) if not p.get("ich")]
    names = ", ".join(p["name"] or p["email"] for p in people[:limit])
    more = f" und {len(people) - limit} weitere" if len(people) > limit else ""
    org = termin.get("organisator") or {}
    return (f"{termin['titel']} ({termin['start'][11:16]}–{termin['ende'][11:16]})"
            + (f"; Organisator: {org.get('name') or org.get('email')}" if org else "")
            + (f"; Eingeladene ({len(people)}): {names}{more}" if people else "; keine Eingeladenen"))


def termin_prompt(termine: list[dict], fixed: bool = False) -> str:
    """Kalendertermine als Kontext. `fixed`: vom Nutzer bestätigt, sonst wählt das Modell per `termin_index`."""
    if not termine:
        return "Kalendertermine zur Aufnahmezeit: keine\n"
    if fixed:
        return f"Kalendertermin (vom Nutzer bestätigt): {termin_line(termine[0], 20)}\n"
    lines = "\n".join(f"[{i}] {termin_line(t, 10)}" for i, t in enumerate(termine))
    return ("Kalendertermine rund um die Aufnahmezeit – welcher gehört zu diesem Gespräch? "
            "Meetings überziehen oft oder werden erst spät aufgenommen; entscheide nach Inhalt, Titel und Teilnehmern:\n"
            + lines + "\n")


def build_schema(categories: list[str]) -> dict:
    return {
        "type": "object",
        "properties": {
            "kategorie": {"type": "string", "enum": [*categories, "unklar"]},
            "konfidenz": {"type": "number", "minimum": 0, "maximum": 1},
            "begruendung": {"type": "string"},
            "titel": {"type": "string", "description": "Kurzer, sprechender Titel, max. 60 Zeichen, ohne Datum"},
            "projekt": {"type": ["string", "null"]},
            "person": {"type": ["string", "null"],
                       "description": "Betreute Person aus der Projektliste, um die es hauptsächlich geht"},
            "tags": {"type": "array", "items": {"type": "string"}, "maxItems": 5,
                     "description": "Themen-Tags, kleingeschrieben, ohne Personennamen"},
            "termin_index": {"type": ["integer", "null"],
                             "description": "Index des passenden Kalendertermins aus der Liste, null wenn keiner passt"},
            "sprecher_vorschlaege": {
                "type": "array",
                "description": "Nur für Platzhalter-Sprecher („Speaker 1“ …), die im Text markiert sind: wer ist das "
                               "vermutlich? Leer, wenn es keine Hinweise gibt.",
                "items": {
                    "type": "object",
                    "properties": {
                        "sprecher": {"type": "string", "description": "z. B. Speaker 1"},
                        "name": {"type": "string"},
                        "hinweis": {"type": "string", "description": "kurz, woran erkannt (Anrede, Vorstellung, Termin)"},
                    },
                    "required": ["sprecher", "name", "hinweis"],
                },
            },
            "zusammenfassung": {
                "type": "string", "minLength": 50,
                "description": "Markdown: 1-2 einleitende Sätze (worum ging es, mit wem), dann eine Leerzeile und "
                               "3-6 Stichpunkte im Format '- **Thema:** Kernaussage'. Keine Überschriften.",
            },
            "entscheidungen": {"type": "array", "items": {"type": "string"}},
            "todos": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "aufgabe": {"type": "string",
                                    "description": "Kurz und konkret, mit Verb beginnen, höchstens etwa 12 Wörter, "
                                                   "ohne Termin und ohne Verantwortlichen im Text"},
                        "verantwortlich": {"type": ["string", "null"]},
                        "faellig": {"type": ["string", "null"], "description": "YYYY-MM-DD oder null"},
                        "ist_meins": {"type": "boolean"},
                    },
                    "required": ["aufgabe", "verantwortlich", "faellig", "ist_meins"],
                },
            },
        },
        "required": ["kategorie", "konfidenz", "begruendung", "titel", "projekt", "person", "tags",
                     "zusammenfassung", "entscheidungen", "todos"],
    }


def build_system_prompt(cfg: dict) -> str:
    lines = []
    for key, c in cfg["kategorien"].items():
        lines.append(f"- {key}: {c.get('hinweise', '')}")
        for name, proj in c["projekte"].items():
            line = f"    - Projekt \"{name}\""
            if proj.get("hinweise"):
                line += f": {proj['hinweise']}"
            if proj.get("personen"):
                line += f" (betreute Personen: {', '.join(proj['personen'])})"
            lines.append(line)
    cats = "\n".join(lines)
    return f"""Du analysierst Transkripte von Gesprächen und Meetings von {cfg['ich']}{role(cfg)}.

Ordne das Gespräch genau einer Kategorie zu:
{cats}
- unklar: wenn keine Kategorie eindeutig passt

Sind unter der Kategorie Projekte gelistet und passt eines, setze "projekt" exakt auf dessen Namen,
sonst auf einen kurzen eigenen Projektnamen oder null.
Geht es hauptsächlich um eine der betreuten Personen, setze "person" exakt auf deren Namen, sonst null.
Namen können im Transkript falsch geschrieben sein (z. B. "Schwalbe" statt "Schwalb").

Entscheidend ist, mit wem und wofür das Gespräch geführt wird, nicht welche Firmen nur erwähnt werden.
Ein internes Gespräch unter Kolleginnen und Kollegen, in dem nur über einen Kunden geplaudert wird, gehört in die interne Kategorie.

Die Zusammenfassung ist für einen schnellen Überblick: kurze Einleitung, dann Stichpunkte mit fett
gesetztem Thema. Zahlen, Namen und Termine konkret nennen, keine Füllsätze.

Extrahiere konkrete Aufgaben (Todos), Zusagen und Follow-ups. Keine vagen Absichten.
Formuliere jedes Todo knapp wie einen Listeneintrag („Checkliste für agentische Projekte erstellen“), Details gehören
in die Zusammenfassung, Termin und Verantwortliche in die eigenen Felder.
"ist_meins" = true, wenn {cfg['ich']} die Aufgabe übernimmt oder sie ihm zugewiesen wird.
Relative Fristen ("bis Freitag") rechnest du ab dem Aufnahmedatum in ein Datum um.
Sprecher heißen im Transkript evtl. nur "Speaker 1" usw.; nutze echte Namen, wenn sie im Gespräch fallen.
Schlage unter "sprecher_vorschlaege" vor, wer hinter einem Platzhalter-Sprecher steckt – nur mit konkretem Hinweis
(jemand wird mit Namen angesprochen, stellt sich vor, Eingeladene des Termins passen zur Rolle). Nie raten;
{cfg['ich']} ist nie ein Platzhalter.
Sind Kalendertermine angegeben, wähle den zum Gespräch passenden (termin_index) und nutze dessen Titel und
Eingeladene als starken Hinweis für Zuordnung und Namen. Große Runden mit sehr vielen Eingeladenen sind nur dann
richtig, wenn der Inhalt dazu passt.
Antworte auf Deutsch.""" + korrekturen_prompt(cfg)


def gateway_token(gw: dict) -> str:
    """API-Key fürs Gateway: aus `token_cmd`, `token_env` oder direkt aus `token`."""
    if gw.get("token_cmd"):
        proc = subprocess.run(gw["token_cmd"], shell=True, capture_output=True, text=True)
        if proc.returncode or not proc.stdout.strip():
            raise RuntimeError(f"gateway.token_cmd liefert keinen Key: {proc.stderr.strip() or 'leere Ausgabe'}")
        return proc.stdout.strip()
    if gw.get("token_env"):
        token = os.environ.get(gw["token_env"], "").strip()
        if not token:
            raise RuntimeError(f"Umgebungsvariable {gw['token_env']} (gateway.token_env) ist nicht gesetzt")
        return token
    if gw.get("token"):
        return str(gw["token"]).strip()
    raise RuntimeError("gateway braucht token_cmd, token_env oder token")


def claude_env(cfg: dict) -> dict:
    """Wie `claude-api` in der .zshrc: Claude Code gegen das konfigurierte Gateway."""
    env = dict(os.environ)
    gw = cfg.get("gateway")
    if not gw:
        return env
    token = gateway_token(gw)
    env |= {
        "ANTHROPIC_BASE_URL": gw["base_url"],
        "ANTHROPIC_AUTH_TOKEN": token,
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        "CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS": "1",
    }
    return env


_MODELS: dict = {"zeit": 0.0, "liste": []}
MODELS_TTL_S = 600
PROVIDERS = {"hosted_vllm": "Intern gehostet", "bedrock": "AWS Bedrock", "azure": "Azure",
             "vertex_ai": "Google Cloud", "mistral": "Mistral"}


def role(cfg: dict) -> str:
    """Optionale Rollenangabe aus `rolle:` für die Prompts, z. B. „ (Berater bei Acme)“."""
    return f" ({cfg['rolle']})" if cfg.get("rolle") else ""


def gateway_models(cfg: dict, refresh: bool = False) -> list[dict]:
    """Chat-Modelle des LiteLLM-Gateways mit Anbieter, Preisen und Kontext (10 min zwischengespeichert)."""
    gw = cfg.get("gateway")
    if not gw:
        return []
    if refresh or not _MODELS["liste"] or time.time() - _MODELS["zeit"] > MODELS_TTL_S:
        try:
            req = urllib.request.Request(gw["base_url"].rstrip("/") + "/model/info",
                                         headers={"Authorization": f"Bearer {gateway_token(gw)}"})
            with urllib.request.urlopen(req, timeout=15) as res:
                data = json.load(res)["data"]
        except Exception:
            log.exception("Modellliste vom Gateway nicht abrufbar")
            return _MODELS["liste"]
        models = []
        for m in data:
            info = m.get("model_info") or {}
            if info.get("mode") != "chat":
                continue
            backend = (m.get("litellm_params") or {}).get("model", "").split("/")[0]
            models.append({
                "id": m["model_name"],
                "anbieter": PROVIDERS.get(backend, backend or "unbekannt"),
                "intern": backend == "hosted_vllm",
                "input": info.get("input_cost_per_token") or 0.0,
                "output": info.get("output_cost_per_token") or 0.0,
                "cache_read": info.get("cache_read_input_token_cost"),
                "kontext": info.get("max_input_tokens") or 0,
            })
        _MODELS.update(zeit=time.time(), liste=sorted(models, key=lambda x: (not x["intern"], x["anbieter"], x["id"])))
    return _MODELS["liste"]


def gateway_prices(cfg: dict, model: str) -> dict | None:
    """Preise pro Token laut Gateway; None ohne Gateway oder für unbekannte Modelle."""
    return next((m for m in gateway_models(cfg) if m["id"] == model), None)


def set_default_model(cfg: dict, config_path: Path, model: str) -> None:
    """Standardmodell in config.yaml setzen (nur die `model:`-Zeile, Kommentare bleiben)."""
    text = config_path.read_text()
    new, n = re.subn(r"(?m)^model:.*$", f"model: {model}", text, count=1)
    if not n:
        new = text.rstrip("\n") + f"\nmodel: {model}\n"
    config_path.write_text(new)
    cfg["model"] = model


def usage_of(cfg: dict, out: dict, seconds: float, model: str) -> dict:
    """Tokens und Kosten einer Analyse. Mit Gateway zählen dessen Preise, sonst die der claude-CLI."""
    u = out.get("usage") or {}
    fresh = (u.get("input_tokens") or 0) + (u.get("cache_creation_input_tokens") or 0)
    cached = u.get("cache_read_input_tokens") or 0
    output = u.get("output_tokens") or 0
    prices = gateway_prices(cfg, model)
    if prices:
        cache_price = prices["cache_read"] if prices["cache_read"] is not None else prices["input"]
        cost = fresh * prices["input"] + cached * cache_price + output * prices["output"]
    else:
        cost = out.get("total_cost_usd") or 0.0
    return {"modell": model, "tokens_ein": fresh + cached, "tokens_aus": output,
            "kosten_usd": round(cost, 6), "sekunden": round(seconds, 1)}


def analyze(cfg: dict, meta: dict, body: str, names: dict | None = None, termine: list[dict] | None = None,
            model: str | None = None, termin_fest: bool = False) -> tuple[dict, dict]:
    """Liefert (Ergebnis, Nutzung mit Tokens/Kosten). `names` ersetzt Sprecher-Platzhalter vorab, `termine` sind
    Kalendertermine als Kontext – das Modell wählt den passenden, außer `termin_fest` (dann genau einer)."""
    reload_kategorien(cfg)
    meta, body = rename_speakers(meta, body, names)
    recorded = meta.get("Recorded At") or meta.get("recorded_at") or ""
    prompt = (
        f"Aufnahmedatum: {recorded}\n"
        f"Quelle: {meta.get('Source App', 'unbekannt')}\n"
        f"Sprecher: {', '.join(map(str, meta.get('Speakers') or []))}\n"
        f"{termin_prompt(termine or [], termin_fest)}\n"
        f"<transkript>\n{body}\n</transkript>"
    )
    result, nutzung = run_claude(cfg, build_system_prompt(cfg), prompt, build_schema(list(cfg["kategorien"])), "Analyse", model)
    return canonical_names(cfg, result), nutzung


def run_claude(cfg: dict, system: str, prompt: str, schema: dict, label: str,
               model: str | None = None) -> tuple[dict, dict]:
    """`claude -p` ohne Tools mit strukturierter Ausgabe. Liefert (Ergebnis, Nutzung)."""
    model = model or cfg.get("model", "sonnet")
    cmd = [
        "claude", "-p",
        "--model", model,
        "--tools", "",
        "--strict-mcp-config",
        "--setting-sources", "",
        "--no-session-persistence",
        "--system-prompt", system,
        "--output-format", "json",
        "--json-schema", json.dumps(schema),
    ]
    started = time.monotonic()
    proc = subprocess.run(cmd, input=prompt, capture_output=True, text=True, timeout=600, cwd=HERE,
                          env=claude_env(cfg))
    if proc.returncode != 0:
        raise RuntimeError(f"claude fehlgeschlagen ({proc.returncode}): {proc.stderr or proc.stdout}")
    out = json.loads(proc.stdout)
    if out.get("is_error") or not out.get("structured_output"):
        raise RuntimeError(f"Keine strukturierte Antwort: {out.get('result')}")
    nutzung = usage_of(cfg, out, time.monotonic() - started, model)
    log.info("%s mit %s: %d+%d Tokens, $%.4f, %.0f s", label, model, nutzung["tokens_ein"], nutzung["tokens_aus"],
             nutzung["kosten_usd"], nutzung["sekunden"])
    return out["structured_output"], nutzung


FOLLOWUP_SCHEMA = {
    "type": "object",
    "properties": {
        "betreff": {"type": "string", "description": "Kurzer Betreff ohne 'Re:' / 'AW:'"},
        "text": {"type": "string", "minLength": 80,
                 "description": "Mailtext als reiner Text (kein Markdown-Fettdruck), mit Anrede und Gruß"},
    },
    "required": ["betreff", "text"],
}


def followup(cfg: dict, result: dict, teilnehmer: list[str], anrede: str = "du",
             sprache: str = "de", model: str | None = None) -> tuple[dict, dict]:
    """Entwurf einer Follow-up-Mail aus dem geprüften Stand (Zusammenfassung, Entscheidungen, Todos)."""
    ich = cfg["ich"]
    en = sprache == "en"
    form = ("informal, first names" if anrede == "du" else "formal") if en else \
           ("per Du, Vornamen" if anrede == "du" else "per Sie, förmlich")
    system = f"""Du schreibst für {ich}{role(cfg)} eine kurze Follow-up-Mail an die Teilnehmer eines Gesprächs.
Sprache: {"Englisch" if en else "Deutsch"}. Ansprache: {form}.
Aufbau: Anrede an die Teilnehmer, ein Satz Dank/Bezug, die wichtigsten Ergebnisse in 2–5 Stichpunkten
(Zeilen mit "- "), getroffene Entscheidungen, dann die nächsten Schritte mit Verantwortlichen und Terminen,
kurzer Abschluss, Gruß mit "{ich}".
Reiner Text ohne Markdown-Fettdruck oder Überschriften. Sachlich, knapp, keine Floskeln.
Nur Inhalte aufnehmen, die für die Teilnehmer bestimmt sind: keine internen Bewertungen, Gehälter,
Personalthemen oder Vermutungen über Personen. Termine als Datum (z. B. 16.10.) nennen."""
    todos = "\n".join(
        f"- {t['aufgabe']} – {ich if t['ist_meins'] else (t.get('verantwortlich') or 'offen')}"
        + (f", bis {t['faellig']}" if t.get("faellig") else "") + (" (bereits erledigt)" if t.get("erledigt") else "")
        for t in result.get("todos", []))
    prompt = (f"Titel: {result['titel']}\n"
              f"Teilnehmer: {', '.join(teilnehmer) or 'unbekannt'}\n\n"
              f"Zusammenfassung:\n{result['zusammenfassung']}\n\n"
              f"Entscheidungen:\n" + ("\n".join(f"- {e}" for e in result.get("entscheidungen", [])) or "keine") + "\n\n"
              f"Todos:\n{todos or 'keine'}")
    return run_claude(cfg, system, prompt, FOLLOWUP_SCHEMA, "Follow-up", model)


def format_todo(todo: dict, ich: str) -> str:
    line = todo["aufgabe"].strip()
    who = todo.get("verantwortlich")
    if who and not todo["ist_meins"]:
        line += f" (@{who})"
    if todo.get("faellig"):
        line += f" 📅 {todo['faellig']}"  # Format des Obsidian-Tasks-Plugins
    if todo.get("erledigt"):  # direkt als erledigt ablegen (Tasks: ✅ Erledigt-Datum)
        if todo["ist_meins"]:
            return f"- [x] {line} ✅ {dt.date.today().isoformat()}"
        return f"- ~~{line}~~ (erledigt)"
    return f"- [ ] {line}" if todo["ist_meins"] else f"- {line}"


def render(meta: dict, body: str, result: dict, cfg: dict, reviewed: bool = False) -> str:
    meta = dict(meta)
    meta["kunde"] = result["kategorie"]
    if result.get("projekt"):
        meta["projekt"] = result["projekt"]
    if result.get("person"):
        meta["person"] = result["person"]
    tags = list(meta.get("tags") or [])
    new_tags = ["transkript", *(re.sub(r"[^\w/-]+", "-", t.strip().lower()).strip("-") for t in result.get("tags", []))]
    tags += [t for t in new_tags if t and t not in tags]
    meta["tags"] = tags
    if result.get("termin"):
        meta["termin"] = result["termin"]
    meta["konfidenz"] = round(result["konfidenz"], 2)
    if reviewed:
        meta["geprueft"] = True
    meta["verarbeitet"] = dt.datetime.now().replace(microsecond=0)

    mine = [t for t in result["todos"] if t["ist_meins"]]
    others = [t for t in result["todos"] if not t["ist_meins"]]
    zuordnung = ("> [!check] Zuordnung bestätigt: **{k}**" if reviewed
                 else "> [!info] Zuordnung: **{k}** ({p:.0%})").format(k=result["kategorie"], p=result["konfidenz"])
    parts = [
        *([links] if (links := link_line(meta, result, cfg)) else []),
        "## Zusammenfassung\n\n" + result["zusammenfassung"].strip(),
        f"{zuordnung}\n> {result['begruendung']}",
    ]
    if result["entscheidungen"]:
        parts.append("## Entscheidungen\n\n" + "\n".join(f"- {e}" for e in result["entscheidungen"]))
    if mine:
        parts.append("## Meine Todos\n\n" + "\n".join(format_todo(t, cfg["ich"]) for t in mine))
    if others:
        parts.append("## Todos anderer\n\n" + "\n".join(format_todo(t, cfg["ich"]) for t in others))
    if (mail := result.get("followup")) and mail.get("text", "").strip():
        quoted = "\n".join(f"> {line}" if line.strip() else ">" for line in mail["text"].strip().splitlines())
        parts.append(f"## Follow-up-Mail\n\n> [!quote]- Betreff: {mail.get('betreff', '').strip()}\n{quoted}")
    parts.append("## Transkript\n\n" + body.strip())

    fm = yaml.safe_dump(meta, sort_keys=False, allow_unicode=True)
    return f"---\n{fm}---\n\n" + "\n\n".join(parts) + "\n"


def link_line(meta: dict, result: dict, cfg: dict) -> str:
    """Wikilinks auf Kunde, Projekt, Person und Teilnehmer – Obsidian zeigt dort dann alle Gespräche."""
    if not cfg.get("links", True):
        return ""
    cat = cfg["kategorien"].get(result["kategorie"])
    parts = []
    if cat:
        parts.append(f"**Kunde:** [[{cat['ordner'].rstrip('/').split('/')[-1]}]]")
    if result.get("projekt"):
        parts.append(f"**Projekt:** [[{result['projekt']}]]")
    if result.get("person"):
        parts.append(f"**Person:** [[{result['person']}]]")
    teilnehmer = [str(s) for s in meta.get("Speakers") or []
                  if not is_me(cfg, str(s)) and not is_placeholder_speaker(str(s)) and str(s) != result.get("person")]
    if teilnehmer:
        parts.append("**Teilnehmer:** " + ", ".join(f"[[{t}]]" for t in teilnehmer))
    if result.get("termin"):
        parts.append(f"**Termin:** {result['termin']}")
    return " · ".join(parts)


def canonical_names(cfg: dict, result: dict) -> dict:
    """Projekt- und Personennamen auf die Schreibweise aus kategorien.yaml bringen („pfk“ → „pFK“)."""
    cat = cfg["kategorien"].get(result.get("kategorie"))
    if not cat:
        return result
    out = dict(result)
    lookup = {name.casefold(): name for name in cat["projekte"]}
    if out.get("projekt"):
        out["projekt"] = lookup.get(out["projekt"].strip().casefold(), out["projekt"].strip())
    proj = cat["projekte"].get(out.get("projekt") or "")
    if proj and out.get("person"):
        people = {name.casefold(): name for name in proj["personen"]}
        out["person"] = people.get(out["person"].strip().casefold(), out["person"].strip())
    return out


def target_folder(cfg: dict, result: dict, reviewed: bool = False) -> str:
    unsure = not reviewed and result["konfidenz"] < cfg.get("min_konfidenz", 0.6)
    if result["kategorie"] not in cfg["kategorien"] or unsure:
        return cfg["unklar_ordner"]
    result = canonical_names(cfg, result)
    cat = cfg["kategorien"][result["kategorie"]]
    folder = cat["ordner"]
    proj = cat["projekte"].get(result.get("projekt"))
    if proj is not None:
        folder = f"{folder}/{result['projekt']}"
        if result.get("person") in proj["personen"]:
            folder = f"{folder}/{result['person']}"
    return folder


def target_path(cfg: dict, meta: dict, result: dict, reviewed: bool = False) -> Path:
    folder = safe_folder(cfg, target_folder(cfg, result, reviewed))
    recorded = meta.get("Recorded At")
    date = recorded.date().isoformat() if isinstance(recorded, dt.datetime) else dt.date.today().isoformat()
    title = re.sub(r'[\\/:*?"<>|#^\[\]]', "", result["titel"]).strip() or "Transkript"
    base = folder / f"{date} {title}"
    path, n = base.with_suffix(".md"), 2
    while path.exists():
        path, n = Path(f"{base} ({n}).md"), n + 1
    return path


def write_note(path: Path, cfg: dict, result: dict, reviewed: bool = False, names: dict | None = None) -> Path:
    """Legt die fertige Notiz im Zielordner an und entfernt das Original aus der Inbox."""
    meta, body = rename_speakers(*split_frontmatter(path.read_text()), names)
    result = canonical_names(cfg, rename_in_result(result, names))
    dest = target_path(cfg, meta, result, reviewed)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(render(meta, body, result, cfg, reviewed))
    path.unlink()
    return dest


def process(path: Path, cfg: dict, dry_run: bool) -> None:
    meta, body = split_frontmatter(path.read_text())
    if meta.get("kunde"):
        log.info("Übersprungen (schon verarbeitet): %s", path.name)
        return
    log.info("Analysiere: %s", path.name)
    result, _ = analyze(cfg, meta, body)
    if dry_run:
        dest = target_path(cfg, meta, result)
        print(render(meta, body, result, cfg)[:4000])
    else:
        dest = write_note(path, cfg, result)
    log.info("-> %s (%s, %.0f%%, %d Todos)", dest.relative_to(cfg["vault"].resolve()),
             result["kategorie"], result["konfidenz"] * 100, len(result["todos"]))


def pending_files(cfg: dict) -> list[Path]:
    inbox = cfg["vault"] / cfg["inbox"]
    now = time.time()
    return sorted(p for p in inbox.glob("*.md") if now - p.stat().st_mtime > MIN_FILE_AGE_S)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("files", nargs="*", type=Path, help="Einzelne Dateien statt der ganzen Inbox")
    ap.add_argument("--config", type=Path, default=HERE / "config.yaml")
    ap.add_argument("--dry-run", action="store_true", help="Nur anzeigen, nichts schreiben/verschieben")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        handlers=[logging.StreamHandler(), logging.FileHandler(HERE / "debrief.log")])
    cfg = load_config(args.config)
    failed = 0
    for path in args.files or pending_files(cfg):
        try:
            process(path, cfg, args.dry_run)
        except Exception:
            log.exception("Fehler bei %s", path)
            failed += 1
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

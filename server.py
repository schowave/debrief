#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["pyyaml>=6", "watchdog>=4"]
# ///
"""Review-Oberfläche für MacWhisper-Transkripte.

Überwacht den Inbox-Ordner, lässt neue Transkripte analysieren und legt das Ergebnis
als Vorschlag in `pending/` ab. Auf http://<review.host>:<port> lassen sich Kategorie,
Projekt, Zusammenfassung und Todos prüfen und ändern; erst „Ablegen“ schreibt die
Notiz in den Vault.

Aufruf:  uv run --script server.py [--config config.yaml]
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import logging
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import yaml

import urllib.parse

import importer

from debrief import (HERE, MIN_FILE_AGE_S, add_fingerprint, is_me, analyze, calendar_candidates, find_duplicate, fingerprint,
                       followup, gateway_models, index_vault, load_fingerprints, save_fingerprints, set_default_model, is_placeholder_speaker, rename_in_result, load_config, load_gelernt,
                       reload_kategorien, save_gelernt, save_kategorien, speaker_samples, split_frontmatter,
                       word_count, write_note)

PENDING = HERE / "pending"
ABGELEGT = HERE / "abgelegt.json"  # Verlauf für „Zuletzt abgelegt“
DUPLIKATE = HERE / "duplikate.json"  # automatisch verworfene Duplikate (zum Wiederherstellen)
MAX_ABGELEGT = 30
TRASH = Path.home() / ".Trash"
INDEX_HTML = HERE / "static" / "index.html"
I18N_JS = HERE / "static" / "i18n.js"
FALLBACK_S = 60  # Sicherheitsprüfung der Inbox, falls ein Dateisystem-Ereignis verloren geht
SSE_KEEPALIVE_S = 25
UNIQUE_RE = re.compile(r" \d{4}-\d{2}-\d{2} \d{2}-\d{2}(?: \(\d+\))?$")
RENAME_AFTER_S = 2  # Datei muss so lange unverändert sein, bevor sie umbenannt wird
# Klickbare Mitteilungen; kommt über mise.toml. Die alte Version aus dem Ruby-Gem (rbenv-Shim)
# ist eine Intel-Binary und löst eine Rosetta-Warnung aus, daher wird sie übergangen.
NOTIFIER = next((p for p in [shutil.which("terminal-notifier")] if p and "rbenv" not in p), None)

log = logging.getLogger("debrief.server")


def review_url(cfg: dict) -> str:
    review = cfg.get("review", {})
    return f"http://{review.get('host', '127.0.0.1')}:{review.get('port', 8765)}/"


class Store:
    """Vorschläge als JSON-Dateien in `pending/`, eine pro Transkript in der Inbox."""

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.lock = threading.RLock()
        self.scan_lock = threading.Lock()  # Inbox-Prüfung nie parallel (Watcher und Import)
        self.wake = threading.Event()        # Dateisystem-Ereignis in der Inbox
        self.changed = threading.Condition()  # Zustand geändert → offene Seiten (SSE) benachrichtigen
        self.version = 0
        self.jobs: queue.Queue[str] = queue.Queue()
        PENDING.mkdir(exist_ok=True)

    @property
    def inbox(self) -> Path:
        return self.cfg["vault"] / self.cfg["inbox"]

    @staticmethod
    def item_id(source: Path) -> str:
        return hashlib.sha1(source.name.encode()).hexdigest()[:12]

    def _file(self, item_id: str) -> Path:
        if not re.fullmatch(r"[0-9a-f]{12}", item_id):
            raise KeyError(item_id)
        return PENDING / f"{item_id}.json"

    def get(self, item_id: str) -> dict:
        path = self._file(item_id)
        if not path.exists():
            raise KeyError(item_id)
        return json.loads(path.read_text())

    def put(self, item: dict) -> None:
        with self.lock:
            path = self._file(item["id"])
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(item, ensure_ascii=False, indent=1, default=str))
            tmp.replace(path)
        self.notify_change()

    def delete(self, item_id: str) -> None:
        self._file(item_id).unlink(missing_ok=True)
        self.notify_change()

    def notify_change(self) -> None:
        with self.changed:
            self.version += 1
            self.changed.notify_all()

    def wait_change(self, version: int, timeout: float) -> int:
        with self.changed:
            self.changed.wait_for(lambda: self.version != version, timeout=timeout)
            return self.version

    def all(self) -> list[dict]:
        items = []
        for p in PENDING.glob("*.json"):
            if not re.fullmatch(r"[0-9a-f]{12}", p.stem):
                continue  # nur Vorschläge, keine Hilfsdateien
            try:
                items.append(json.loads(p.read_text()))
            except (json.JSONDecodeError, FileNotFoundError):
                continue  # gerade gelöscht/ersetzt
        return sorted(items, key=lambda i: i["erstellt"], reverse=True)

    def source(self, item: dict) -> Path:
        return self.inbox / item["datei"]

    # --- Inbox überwachen -------------------------------------------------

    def scan(self) -> None:
        with self.scan_lock:
            self._scan()

    def make_unique(self, src: Path) -> Path:
        """MacWhisper exportiert z. B. jede Teams-Aufnahme als „Meeting - Teams.md“. Liegt eine solche Datei noch
        in der Inbox, würde der nächste Export sie überschreiben bzw. gar nicht exportiert. Deshalb sofort in
        „<Name> <Aufnahmezeit>.md“ umbenennen – dann ist der Name für den nächsten Export wieder frei."""
        if UNIQUE_RE.search(src.stem):
            return src
        old_id = self.item_id(src)
        item = self.get(old_id) if self._file(old_id).exists() else None
        if item and item["status"] == "analysiere":
            return src  # erst nach der laufenden Analyse umbenennen
        try:
            meta, _ = split_frontmatter(src.read_text())
        except (OSError, yaml.YAMLError):
            return src
        recorded = meta.get("Recorded At")
        if isinstance(recorded, str):
            try:
                recorded = dt.datetime.fromisoformat(recorded)
            except ValueError:
                recorded = None
        if not isinstance(recorded, dt.datetime):
            recorded = dt.datetime.fromtimestamp(src.stat().st_mtime)
        base = f"{src.stem} {recorded:%Y-%m-%d %H-%M}"
        dest, n = src.with_name(base + ".md"), 2
        while dest.exists():
            dest, n = src.with_name(f"{base} ({n}).md"), n + 1
        with self.lock:
            src.rename(dest)
            if item:  # vorhandenen Vorschlag mitnehmen; die Seite folgt über „frueher“
                item.update(id=self.item_id(dest), datei=dest.name, frueher=old_id)
                self.put(item)
                self.delete(old_id)
        log.info("Umbenannt (Name für nächsten Export frei): %s → %s", src.name, dest.name)
        return dest

    def _scan(self) -> None:
        now = time.time()
        present = set()
        for src in list(self.inbox.glob("*.md")):
            if now - src.stat().st_mtime >= RENAME_AFTER_S:
                src = self.make_unique(src)
            item_id = self.item_id(src)
            present.add(item_id)
            if now - src.stat().st_mtime < MIN_FILE_AGE_S or self._file(item_id).exists():
                continue
            meta, body = split_frontmatter(src.read_text())
            speakers = [str(x) for x in meta.get("Speakers") or []]
            words = word_count(body, speakers)
            too_short = words < self.cfg.get("min_woerter", 30)
            fp = fingerprint(body, speakers, meta)
            dup_sim, dup = self.duplicate_of(fp, item_id)
            limits = self.cfg.get("duplikate", {})
            if dup and dup_sim >= limits.get("automatisch_ab", 0.9):
                self.discard_duplicate(src, dup_sim, dup)
                present.discard(item_id)
                continue
            is_dup = bool(dup) and dup_sim >= limits.get("pruefen_ab", 0.5)
            self.put({
                "id": item_id, "datei": src.name, "status": "zu_kurz" if too_short else "analysiere", "fehler": None,
                "erstellt": dt.datetime.now().isoformat(timespec="seconds"),
                "aufgenommen": str(meta.get("Recorded At") or ""),
                "dauer": meta.get("Duration"), "quelle": meta.get("Source App"),
                "sprecher": speakers, "sprecher_proben": speaker_samples(body, speakers), "woerter": words,
                "ergebnis": None, "fp": fp,
                **({"status": "duplikat", "duplikat": {"aehnlichkeit": round(dup_sim, 2), "titel": dup["titel"],
                                                       "ziel": dup.get("ziel"), "datei": dup.get("datei")}}
                   if is_dup and not too_short else {}),
            })
            if is_dup and not too_short:
                log.info("Mögliches Duplikat (%.0f %%) von %s: %s", dup_sim * 100, dup["titel"], src.name)
                continue
            if not too_short:
                termine = calendar_candidates(self.cfg, meta)
                item = self.get(item_id)
                item.update(termine=termine, termin_index=0 if termine else None)
                self.put(item)
            if too_short:
                log.info("Zu kurz (%d Wörter), nicht analysiert: %s", words, src.name)
            else:
                self.jobs.put(item_id)
        # Transkript wurde von Hand aus der Inbox entfernt -> Vorschlag verwerfen
        for item in self.all():
            if item["id"] not in present:
                self.delete(item["id"])

    def duplicate_of(self, fp: dict, item_id: str) -> tuple[float, dict | None]:
        """Ähnlichstes abgelegtes oder schon in der Inbox liegendes Transkript (freigegebene ausgenommen)."""
        data = load_fingerprints()
        if fp["hash"] in data["erlaubt"] or fp["woerter"] < 20:
            return 0.0, None
        pending = []
        for i in self.all():
            if i["id"] == item_id:
                continue
            if not i.get("fp"):  # Vorschläge von vor der Duplikat-Erkennung nachträglich erfassen
                try:
                    meta, body = split_frontmatter(self.source(i).read_text())
                except FileNotFoundError:
                    continue
                i["fp"] = fingerprint(body, [str(x) for x in meta.get("Speakers") or []], meta)
                self.put(i)
            pending.append({**i["fp"], "titel": (i.get("ergebnis") or {}).get("titel") or i["datei"], "datei": i["datei"]})
        return find_duplicate(fp, data["notizen"] + pending)

    def discard_duplicate(self, src: Path, sim: float, dup: dict) -> None:
        trashed = move_to_trash(src)
        entries = [{"datum": dt.datetime.now().isoformat(timespec="seconds"), "datei": src.name,
                    "papierkorb": trashed.name, "aehnlichkeit": round(sim, 2), "titel": dup["titel"],
                    "ziel": dup.get("ziel"), "url": obsidian_url(self.cfg, Path(dup["ziel"])) if dup.get("ziel") else None},
                   *load_json_list(DUPLIKATE)][:50]
        DUPLIKATE.write_text(json.dumps(entries, ensure_ascii=False, indent=1))
        log.info("Duplikat (%.0f %%) von %s verworfen: %s", sim * 100, dup["titel"], src.name)
        en = self.cfg.get("sprache") == "en"
        self.notify_text("Duplicate discarded" if en else "Duplikat verworfen",
                         f"{src.stem} matches “{dup['titel']}” – moved to the trash" if en
                         else f"{src.stem} entspricht „{dup['titel']}“ – in den Papierkorb verschoben",
                         review_url(self.cfg), group="debrief-duplikat")

    def watch(self) -> None:
        try:
            added = index_vault(self.cfg)
            if added:
                log.info("Fingerabdrücke für %d abgelegte Notizen erfasst", added)
        except Exception:
            log.exception("Vault-Index für Duplikate fehlgeschlagen")
        for item in self.all():  # nach Neustart liegengebliebene Analysen fortsetzen
            if item["status"] == "analysiere":
                self.jobs.put(item["id"])
        self.start_fsevents()
        while True:
            try:
                self.scan()
            except Exception:
                log.exception("Fehler beim Prüfen der Inbox")
            self.wake.wait(self.next_due())
            self.wake.clear()

    def start_fsevents(self) -> None:
        """macOS meldet Änderungen in der Inbox (FSEvents über watchdog) – kein Polling nötig."""
        try:
            from watchdog.events import FileSystemEventHandler
            from watchdog.observers import Observer
        except ImportError:
            log.warning("watchdog fehlt – Inbox wird nur alle %d s geprüft", FALLBACK_S)
            return
        store = self

        class Handler(FileSystemEventHandler):
            def on_any_event(self, event):
                if not event.is_directory:
                    store.wake.set()

        observer = Observer()
        observer.schedule(Handler(), str(self.inbox), recursive=False)
        observer.daemon = True
        observer.start()
        log.info("Inbox wird per Dateisystem-Ereignis überwacht")

    def next_due(self) -> float:
        """Sekunden bis eine Datei alt genug zum Umbenennen bzw. Analysieren ist (sonst Sicherheitsprüfung)."""
        now, due = time.time(), FALLBACK_S
        for src in self.inbox.glob("*.md"):
            try:
                age = now - src.stat().st_mtime
            except FileNotFoundError:
                continue
            if not UNIQUE_RE.search(src.stem) and age < RENAME_AFTER_S:
                due = min(due, RENAME_AFTER_S - age)
            elif not self._file(self.item_id(src)).exists() and age < MIN_FILE_AGE_S:
                due = min(due, MIN_FILE_AGE_S - age)
        return max(0.2, due + 0.1)

    def work(self) -> None:
        while True:
            item_id = self.jobs.get()
            try:
                item = self.get(item_id)
                meta, body = split_frontmatter(self.source(item).read_text())
                log.info("Analysiere: %s", item["datei"])
                fixed = item.get("termin_manuell", False)
                if not fixed:  # Termine bei jeder Analyse neu suchen (Kalender oder Vorlauf können sich geändert haben)
                    item["termine"] = calendar_candidates(self.cfg, meta)
                    self.put(item)
                chosen = chosen_termin(item)
                termine = ([chosen] if chosen else []) if fixed else item["termine"]
                result, nutzung = analyze(self.cfg, meta, body, item.get("sprecher_namen"), termine,
                                          item.get("modell"), termin_fest=fixed)
                item = self.get(item_id)
                if not fixed:  # Modell wählt den Termin; ohne gültige Wahl der mit der größten Überschneidung
                    idx = result.get("termin_index")
                    valid = isinstance(idx, int) and 0 <= idx < len(item["termine"])
                    item["termin_index"] = idx if valid else (0 if item["termine"] and idx is not None else None)
                item.update(status="bereit", fehler=None, ergebnis=result, nutzung=nutzung,
                            termin_analysiert=item.get("termin_index"),
                            kosten_gesamt=round(item.get("kosten_gesamt", 0) + nutzung["kosten_usd"], 6))
                self.put(item)
                self.notify(item)
            except KeyError:
                continue
            except Exception as e:
                log.exception("Analyse fehlgeschlagen: %s", item_id)
                try:
                    item = self.get(item_id)
                    item.update(status="fehler", fehler=str(e)[:2000])
                    self.put(item)
                except KeyError:
                    pass

    def notify(self, item: dict) -> None:
        url = review_url(self.cfg) + f"#{item['id']}"
        r = item["ergebnis"]
        if not self.cfg.get("review", {}).get("mitteilung", True):
            return
        if self.cfg.get("review", {}).get("auto_open", False):
            subprocess.run(["open", url], check=False)
            return
        if self.cfg.get("sprache") == "en":
            self.notify_text("Review transcript", f"{r['titel']} → {r['kategorie']}, {len(r['todos'])} todos", url)
        else:
            self.notify_text("Transkript prüfen", f"{r['titel']} → {r['kategorie']}, {len(r['todos'])} Todos", url)

    def notify_text(self, title: str, msg: str, url: str, group: str = "debrief") -> None:
        if not self.cfg.get("review", {}).get("mitteilung", True):
            return
        try:
            if NOTIFIER:  # Klick auf die Mitteilung öffnet die Review-Seite
                out = subprocess.run([NOTIFIER, "-title", title, "-message", msg, "-open", url, "-sound", "Glass",
                                      "-group", group], capture_output=True, text=True, timeout=10)
                log.info("Mitteilung über %s (rc=%s) %s", NOTIFIER, out.returncode, (out.stderr or out.stdout).strip())
            else:  # Fallback ohne Klick-Aktion
                log.info("Mitteilung über osascript (terminal-notifier nicht gefunden)")
                subprocess.run(["osascript", "-e", f"display notification {json.dumps(msg, ensure_ascii=False)} "
                                f"with title \"{title}\" sound name \"Glass\""], timeout=10, check=False)
        except Exception:
            log.exception("Mitteilung fehlgeschlagen")


def apply_additions(cfg: dict, body: dict) -> None:
    """Neues Projekt / neue Person aus dem Review-Formular in die Kategorien übernehmen."""
    r = body["ergebnis"]
    cat = cfg["kategorien"].get(r["kategorie"])
    if not cat:
        return
    changed = False
    projekt = (r.get("projekt") or "").strip()
    if body.get("projekt_speichern") and projekt and projekt not in cat["projekte"]:
        cat["projekte"][projekt] = {"hinweise": "", "personen": []}
        changed = True
    person = (r.get("person") or "").strip()
    proj = cat["projekte"].get(projekt)
    if body.get("person_speichern") and person and proj is not None and person not in proj["personen"]:
        proj["personen"].append(person)
        changed = True
    if changed:
        save_kategorien(cfg, cfg["kategorien"])


def check_model(cfg: dict, model: str) -> str:
    model = str(model).strip()
    known = [m["id"] for m in gateway_models(cfg)]
    if not model or (known and model not in known):
        raise ValueError(f"Unbekanntes Modell: {model}")
    return model


def chosen_termin(item: dict) -> dict | None:
    idx = item.get("termin_index")
    termine = item.get("termine") or []
    return termine[idx] if isinstance(idx, int) and 0 <= idx < len(termine) else None


def remember(item: dict, final: dict, names: dict) -> None:
    """Abweichungen von der Modell-Zuordnung und vergebene Sprechernamen für die nächsten Analysen merken."""
    keys = ("kategorie", "projekt", "person")
    vorschlag = {k: item["ergebnis"].get(k) for k in keys}
    richtig = {k: final.get(k) for k in keys}
    data = load_gelernt()
    if vorschlag != richtig:
        kurz = final["zusammenfassung"].strip().split("\n")[0][:220]
        data["korrekturen"].append({"datum": dt.date.today().isoformat(), "titel": final["titel"],
                                    "kurz": kurz, "vorschlag": vorschlag, "richtig": richtig})
        data["korrekturen"] = data["korrekturen"][-100:]
    for name in names.values():
        if name and not is_placeholder_speaker(name) and name not in data["namen"]:
            data["namen"].append(name)
    save_gelernt(data)


def obsidian_url(cfg: dict, rel: Path) -> str:
    return "obsidian://open?" + urllib.parse.urlencode(
        {"vault": cfg["vault"].name, "file": str(rel.with_suffix(""))}, quote_via=urllib.parse.quote)


def load_json_list(path: Path) -> list[dict]:
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def load_abgelegt() -> list[dict]:
    return load_json_list(ABGELEGT)


def add_abgelegt(entry: dict) -> None:
    items = [entry, *load_abgelegt()][:MAX_ABGELEGT]
    ABGELEGT.write_text(json.dumps(items, ensure_ascii=False, indent=1))


def move_to_trash(path: Path) -> Path:
    """In den macOS-Papierkorb verschieben (wiederherstellbar)."""
    dest, n = TRASH / path.name, 2
    while dest.exists():
        dest, n = TRASH / f"{path.stem} {n}{path.suffix}", n + 1
    shutil.move(str(path), dest)
    return dest


def validate_config(text: str) -> None:
    """Neue config.yaml prüfen, bevor sie die alte ersetzt."""
    tmp = HERE / "config.check.tmp"
    try:
        tmp.write_text(text)
        cfg = load_config(tmp)
    except yaml.YAMLError as e:
        raise ValueError(f"YAML-Fehler: {e}") from e
    except (KeyError, TypeError) as e:
        raise ValueError(f"Pflichtfeld fehlt oder ist ungültig: {e}") from e
    finally:
        tmp.unlink(missing_ok=True)
    for key in ("vault", "inbox", "ich", "unklar_ordner"):
        if not cfg.get(key):
            raise ValueError(f"„{key}“ fehlt")
    if not (cfg["vault"] / cfg["inbox"]).is_dir():
        raise ValueError(f"Inbox-Ordner nicht gefunden: {cfg['vault'] / cfg['inbox']}")
    port = cfg.get("review", {}).get("port", 8765)
    if not isinstance(port, int) or not 1024 <= port <= 65535:
        raise ValueError("review.port muss eine Zahl zwischen 1024 und 65535 sein")


def restart_later(delay: float = 0.5) -> None:
    """Server-Prozess neu starten (neue config.yaml laden), nachdem die Antwort raus ist."""
    def go():
        time.sleep(delay)
        log.info("Neustart nach Änderung der Konfiguration")
        logging.shutdown()
        os.execv(sys.executable, [sys.executable, *sys.argv])
    threading.Thread(target=go, daemon=True).start()


def clean_result(r: dict) -> dict:
    todos = [
        {"aufgabe": t["aufgabe"].strip(), "verantwortlich": (t.get("verantwortlich") or "").strip() or None,
         "faellig": t.get("faellig") or None, "ist_meins": bool(t.get("ist_meins")),
         "erledigt": bool(t.get("erledigt"))}
        for t in r.get("todos", []) if t.get("aufgabe", "").strip()
    ]
    return {
        **r,
        "titel": r["titel"].strip(),
        "projekt": (r.get("projekt") or "").strip() or None,
        "person": (r.get("person") or "").strip() or None,
        "entscheidungen": [e.strip() for e in r.get("entscheidungen", []) if e.strip()],
        "tags": [t.strip() for t in r.get("tags", []) if t.strip()],
        "todos": todos,
    }


def make_handler(store: Store):
    cfg = store.cfg

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            log.debug(fmt, *args)

        def send_json(self, data, status=HTTPStatus.OK):
            raw = json.dumps(data, ensure_ascii=False, default=str).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def read_json(self) -> dict:
            # Nur JSON von der eigenen Seite annehmen (schützt vor Formular-Posts fremder Websites)
            origin = self.headers.get("Origin")
            if not self.headers.get("Content-Type", "").startswith("application/json") or (
                origin and origin != f"http://{self.headers.get('Host')}"
            ):
                raise PermissionError("Ungültige Anfrage")
            length = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(length) or b"{}")

        def stream_events(self) -> None:
            """Server-Sent Events: den Zustand bei jeder Änderung an die Seite schicken."""
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            last, version = None, -1
            try:
                while True:
                    with store.lock:
                        reload_kategorien(cfg)
                    data = json.dumps(self.state(), ensure_ascii=False, default=str)
                    if data != last:
                        self.wfile.write(f"event: state\ndata: {data}\n\n".encode())
                        last = data
                    else:
                        self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
                    version = store.wait_change(version, SSE_KEEPALIVE_S)
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass  # Seite geschlossen

        def state(self) -> dict:
            personen = [p for c in cfg["kategorien"].values() for pr in c["projekte"].values() for p in pr["personen"]]
            return {
                "ich": cfg["ich"],
                "unklar_ordner": cfg["unklar_ordner"],
                "kategorien": cfg["kategorien"],
                "items": [{k: v for k, v in i.items() if k != "fp"} for i in store.all()],
                "namen": sorted(set(personen + load_gelernt()["namen"])),
                "modell": cfg.get("model"),
                "sprache": cfg.get("sprache", "de"),
                "abgelegt": load_abgelegt(),
                "duplikate": load_json_list(DUPLIKATE),
                "vault_name": cfg["vault"].name,
            }

        def do_GET(self):
            path = self.path.split("?")[0]
            with store.lock:
                reload_kategorien(cfg)
            try:
                if path == "/":
                    raw = INDEX_HTML.read_bytes()
                    self.send_response(HTTPStatus.OK)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(raw)))
                    self.end_headers()
                    self.wfile.write(raw)
                elif path == "/i18n.js":
                    raw = I18N_JS.read_bytes()
                    self.send_response(HTTPStatus.OK)
                    self.send_header("Content-Type", "text/javascript; charset=utf-8")
                    self.send_header("Content-Length", str(len(raw)))
                    self.end_headers()
                    self.wfile.write(raw)
                elif path == "/api/state":
                    self.send_json(self.state())
                elif path == "/api/events":
                    self.stream_events()
                elif m := re.fullmatch(r"/api/items/(\w+)/transkript", path):
                    _, body = split_frontmatter(store.source(store.get(m[1])).read_text())
                    self.send_json({"transkript": body})
                elif path == "/api/kategorien.yaml":
                    self.send_json({"yaml": cfg["kategorien_datei"].read_text()})
                elif path == "/api/korrekturen":
                    self.send_json({"korrekturen": load_gelernt()["korrekturen"]})
                elif path == "/api/modelle":
                    self.send_json({"modelle": gateway_models(cfg, refresh="neu=1" in self.path),
                                    "standard": cfg.get("model")})
                elif path == "/api/import/suche":
                    query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                    known = {n["hash"] for n in load_fingerprints()["notizen"]} | \
                            {i["fp"]["hash"] for i in store.all() if i.get("fp")}
                    self.send_json({**importer.search(cfg, (query.get("q") or [""])[0], known),
                                    "ordner": [str(f) for f in importer.folders(cfg)]})
                elif path == "/api/import/vorschau":
                    query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                    self.send_json({"text": importer.preview(cfg, Path((query.get("id") or [""])[0]))})
                elif path == "/api/gelernt":
                    self.send_json(load_gelernt())
                elif path == "/api/config.yaml":
                    self.send_json({"yaml": (HERE / "config.yaml").read_text()})
                else:
                    self.send_json({"fehler": "Nicht gefunden"}, HTTPStatus.NOT_FOUND)
            except (KeyError, FileNotFoundError):
                self.send_json({"fehler": "Nicht gefunden"}, HTTPStatus.NOT_FOUND)
            except PermissionError as e:
                self.send_json({"fehler": str(e)}, HTTPStatus.FORBIDDEN)

        def do_POST(self):
            path = self.path.split("?")[0]
            with store.lock:
                reload_kategorien(cfg)
            try:
                body = self.read_json()
                if m := re.fullmatch(r"/api/items/(\w+)/ablegen", path):
                    item = store.get(m[1])
                    body["ergebnis"] = clean_result(body["ergebnis"])
                    vorschlag = item["ergebnis"]["kategorie"]
                    if body["ergebnis"]["kategorie"] != vorschlag:
                        body["ergebnis"]["begruendung"] = f"Manuell zugeordnet (Vorschlag war: {vorschlag})."
                    if body["ergebnis"]["kategorie"] not in cfg["kategorien"]:
                        raise ValueError("Bitte eine Kategorie wählen")
                    names = {k: v for k, v in (body.get("sprecher_namen") or {}).items() if k in item.get("sprecher", [])}
                    if termin := chosen_termin(item):
                        body["ergebnis"]["termin"] = termin["titel"]
                    apply_additions(cfg, body)
                    with store.lock:
                        dest = write_note(store.source(item), cfg, body["ergebnis"], reviewed=True, names=names)
                        store.delete(item["id"])
                    rel = dest.relative_to(cfg["vault"].resolve())
                    log.info("Abgelegt: %s", rel)
                    remember(item, body["ergebnis"], names)
                    if item.get("fp"):
                        add_fingerprint(item["fp"], body["ergebnis"]["titel"], str(rel))
                    url = obsidian_url(cfg, rel)
                    add_abgelegt({"titel": body["ergebnis"]["titel"], "ziel": str(rel), "url": url,
                                  "kategorie": body["ergebnis"]["kategorie"],
                                  "zeit": dt.datetime.now().isoformat(timespec="seconds")})
                    self.send_json({"ziel": str(rel), "url": url, "state": self.state()})
                elif m := re.fullmatch(r"/api/items/(\w+)/(neu-analysieren|ueberspringen)", path):
                    item = store.get(m[1])
                    if m[2] == "neu-analysieren":
                        if item.get("status") == "duplikat" and item.get("fp"):
                            data = load_fingerprints()
                            data["erlaubt"] = [*data["erlaubt"], item["fp"]["hash"]][-200:]
                            save_fingerprints(data)
                        if body.get("modell"):
                            item["modell"] = check_model(cfg, body["modell"])
                        if "sprecher_namen" in body:
                            item["sprecher_namen"] = {k: v for k, v in body["sprecher_namen"].items()
                                                      if k in item.get("sprecher", [])}
                        item.update(status="analysiere", fehler=None)
                        store.put(item)
                        store.jobs.put(item["id"])
                    else:
                        item["status"] = "uebersprungen"
                        store.put(item)
                    self.send_json(self.state())
                elif m := re.fullmatch(r"/api/items/(\w+)/termin", path):
                    with store.lock:
                        item = store.get(m[1])
                        idx = body.get("index")
                        if idx is not None and not 0 <= int(idx) < len(item.get("termine") or []):
                            raise ValueError("Unbekannter Termin")
                        item["termin_index"] = None if idx is None else int(idx)
                        item["termin_manuell"] = True
                        store.put(item)
                    self.send_json(self.state())
                elif m := re.fullmatch(r"/api/items/(\w+)/followup", path):
                    item = store.get(m[1])
                    names = {k: v for k, v in (body.get("sprecher_namen") or {}).items() if k in item.get("sprecher", [])}
                    result = rename_in_result(clean_result(body["ergebnis"]), names)
                    teilnehmer = [names.get(x) or x for x in item.get("sprecher", [])]
                    teilnehmer = [t for t in teilnehmer if not is_me(cfg, t) and not is_placeholder_speaker(t)]
                    termin = chosen_termin(item)
                    empfaenger = [p for p in (termin or {}).get("teilnehmer", []) if not p.get("ich") and "@" in p.get("email", "")]
                    if not teilnehmer and empfaenger:
                        teilnehmer = [p["name"] or p["email"] for p in empfaenger][:20]
                    mail, nutzung = followup(cfg, result, teilnehmer, body.get("anrede", "du"), body.get("sprache", "de"))
                    with store.lock:
                        item = store.get(m[1])
                        item["kosten_gesamt"] = round(item.get("kosten_gesamt", 0) + nutzung["kosten_usd"], 6)
                        store.put(item)
                    self.send_json({**mail, "teilnehmer": teilnehmer, "nutzung": nutzung,
                                    "empfaenger": [{"name": p["name"], "email": p["email"]} for p in empfaenger]})
                elif m := re.fullmatch(r"/api/items/(\w+)/verwerfen", path):
                    item = store.get(m[1])
                    with store.lock:
                        trashed = move_to_trash(store.source(item))
                        store.delete(item["id"])
                    log.info("Verworfen (Papierkorb): %s", item["datei"])
                    self.send_json({"papierkorb": trashed.name, "datei": item["datei"], "state": self.state()})
                elif path == "/api/wiederherstellen":
                    src = TRASH / Path(body["papierkorb"]).name
                    dest = store.inbox / Path(body["datei"]).name
                    if not src.exists() or dest.exists():
                        raise ValueError("Wiederherstellen nicht möglich")
                    shutil.move(str(src), dest)
                    self.send_json(self.state())
                elif path == "/api/import":
                    names = [importer.to_inbox(cfg, Path(i)) for i in body["ids"]]
                    log.info("Importiert: %s", ", ".join(names))
                    store.scan()  # sofort aufgreifen statt auf den nächsten Durchlauf zu warten
                    self.send_json({"importiert": names, "state": self.state()})
                elif path == "/api/import/hochladen":
                    names, fehler = [], []
                    for f in body["dateien"][:50]:
                        try:
                            names.append(importer.upload_to_inbox(cfg, f["name"], f["inhalt"], f.get("geaendert")))
                        except ValueError as e:
                            fehler.append(str(e))
                    if names:
                        log.info("Hochgeladen: %s", ", ".join(names))
                        store.scan()
                    self.send_json({"importiert": names, "fehler": fehler, "state": self.state()})
                elif path == "/api/import/ordner-waehlen":
                    picked = importer.choose_folders()
                    current = [str(f) for f in importer.folders(cfg)]
                    ordner = importer.set_folders(cfg, HERE / "config.yaml",
                                                  current + [f for f in picked if f not in current]) if picked else current
                    self.send_json({"ordner": ordner, "neu": len([f for f in picked if f not in current])})
                elif path == "/api/import/ordner":
                    ordner = importer.set_folders(cfg, HERE / "config.yaml", body["ordner"])
                    self.send_json({"ordner": ordner})
                elif path == "/api/modell":
                    set_default_model(cfg, HERE / "config.yaml", check_model(cfg, body["modell"]))
                    log.info("Standardmodell: %s", cfg["model"])
                    self.send_json(self.state())
                elif path == "/api/korrekturen/speichern":
                    data = load_gelernt()
                    k = data["korrekturen"][int(body["index"])]
                    richtig = body["richtig"]
                    if richtig.get("kategorie") not in cfg["kategorien"]:
                        raise ValueError("Unbekannte Kategorie")
                    k["richtig"] = {key: (str(richtig.get(key) or "").strip() or None)
                                    for key in ("kategorie", "projekt", "person")}
                    save_gelernt(data)
                    self.send_json({"korrekturen": data["korrekturen"]})
                elif path == "/api/namen":
                    namen = sorted({str(n).strip() for n in body["namen"] if str(n).strip()}, key=str.casefold)
                    data = load_gelernt()
                    data["namen"] = namen
                    save_gelernt(data)
                    self.send_json({"namen": namen, "state": self.state()})
                elif path == "/api/duplikate/wiederherstellen":
                    entries = load_json_list(DUPLIKATE)
                    entry = entries[int(body["index"])]
                    src, dest = TRASH / Path(entry["papierkorb"]).name, store.inbox / Path(entry["datei"]).name
                    if not src.exists() or dest.exists():
                        raise ValueError("Nicht mehr im Papierkorb oder Datei existiert schon in der Inbox")
                    meta, text = split_frontmatter(src.read_text())
                    data = load_fingerprints()  # wiederhergestellt = gewollt, nicht erneut als Duplikat behandeln
                    data["erlaubt"] = [*data["erlaubt"], fingerprint(text, [str(x) for x in meta.get("Speakers") or []], meta)["hash"]][-200:]
                    save_fingerprints(data)
                    shutil.move(str(src), dest)
                    del entries[int(body["index"])]
                    DUPLIKATE.write_text(json.dumps(entries, ensure_ascii=False, indent=1))
                    self.send_json(self.state())
                elif path == "/api/abgelegt/loeschen":
                    items = [] if body.get("alle") else load_abgelegt()
                    if not body.get("alle"):
                        del items[int(body["index"])]
                    ABGELEGT.write_text(json.dumps(items, ensure_ascii=False, indent=1))
                    self.send_json(self.state())
                elif path == "/api/config.yaml":
                    validate_config(body["yaml"])
                    target = HERE / "config.yaml"
                    shutil.copy(target, HERE / "config.yaml.bak")
                    tmp = target.with_suffix(".tmp")
                    tmp.write_text(body["yaml"])
                    tmp.replace(target)
                    log.info("config.yaml geändert (Sicherung: config.yaml.bak)")
                    self.send_json({"neustart": True})
                    restart_later()
                elif path == "/api/korrekturen/loeschen":
                    data = load_gelernt()
                    del data["korrekturen"][int(body["index"])]
                    save_gelernt(data)
                    self.send_json({"korrekturen": data["korrekturen"]})
                elif path == "/api/kategorien":
                    save_kategorien(cfg, body["kategorien"])
                    self.send_json(self.state())
                elif path == "/api/kategorien.yaml":
                    save_kategorien(cfg, yaml.safe_load(body["yaml"]) or {})
                    self.send_json(self.state())
                else:
                    self.send_json({"fehler": "Nicht gefunden"}, HTTPStatus.NOT_FOUND)
            except PermissionError as e:
                self.send_json({"fehler": str(e)}, HTTPStatus.FORBIDDEN)
            except (KeyError, FileNotFoundError):
                self.send_json({"fehler": "Nicht gefunden (schon abgelegt?)"}, HTTPStatus.NOT_FOUND)
            except IndexError:
                self.send_json({"fehler": "Eintrag nicht gefunden – Seite neu laden"}, HTTPStatus.BAD_REQUEST)
            except (ValueError, yaml.YAMLError) as e:
                self.send_json({"fehler": str(e)}, HTTPStatus.BAD_REQUEST)
            except Exception as e:
                log.exception("Fehler bei %s", path)
                self.send_json({"fehler": str(e)}, HTTPStatus.INTERNAL_SERVER_ERROR)
            finally:
                store.notify_change()  # offene Seiten aktualisieren (SSE)

    return Handler


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", type=Path, default=HERE / "config.yaml")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        handlers=[logging.StreamHandler(), logging.FileHandler(HERE / "debrief.log")])
    cfg = load_config(args.config)
    store = Store(cfg)
    threading.Thread(target=store.watch, daemon=True).start()
    threading.Thread(target=store.work, daemon=True).start()

    port = cfg.get("review", {}).get("port", 8765)
    server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(store))
    log.info("Review-Seite: %s", review_url(cfg))
    server.serve_forever()


if __name__ == "__main__":
    main()

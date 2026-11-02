#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["pyyaml>=6", "watchdog>=4"]
# ///
"""Review UI for MacWhisper transcripts.

Watches the inbox folder, has new transcripts analyzed and stores the result as a
proposal in `pending/`. On http://<review.host>:<port> you can check and change category,
project, summary and todos; only “File” writes the note to the vault.

Usage:  uv run --script server.py [--config config.yaml]
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import html
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
import live
import miro

from debrief import (HERE, MIN_FILE_AGE_S, add_fingerprint, collect_project_todos, is_me, analyze, calendar_candidates, find_duplicate, fingerprint,
                       followup, gateway_models, index_vault, load_fingerprints, save_fingerprints, set_default_model, is_placeholder_speaker, rename_in_result, load_config, load_gelernt,
                       reload_kategorien, save_gelernt, save_kategorien, speaker_samples, split_frontmatter,
                       word_count, write_note)

PENDING = HERE / "pending"
ABGELEGT = HERE / "filed.json"  # history for "Recently filed"
DUPLIKATE = HERE / "duplicates.json"  # automatically discarded duplicates (for restoring)
MAX_ABGELEGT = 30
TRASH = Path.home() / ".Trash"
INDEX_HTML = HERE / "static" / "index.html"
LIVE_HTML = HERE / "static" / "live.html"
I18N_JS = HERE / "static" / "i18n.js"
FALLBACK_S = 60  # safety check of the inbox in case a filesystem event is lost
SSE_KEEPALIVE_S = 25
UNIQUE_RE = re.compile(r" \d{4}-\d{2}-\d{2} \d{2}-\d{2}(?: \(\d+\))?$")
RENAME_AFTER_S = 2  # file must stay unchanged this long before it is renamed
# Clickable notifications; provided via mise.toml. The old version from the Ruby gem (rbenv shim)
# is an Intel binary and triggers a Rosetta warning, so it is skipped.
NOTIFIER = next((p for p in [shutil.which("terminal-notifier")] if p and "rbenv" not in p), None)

log = logging.getLogger("debrief.server")


def review_url(cfg: dict) -> str:
    review = cfg.get("review", {})
    return f"http://{review.get('host', '127.0.0.1')}:{review.get('port', 8765)}/"


class Store:
    """Proposals as JSON files in `pending/`, one per transcript in the inbox."""

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.lock = threading.RLock()
        self.scan_lock = threading.Lock()  # never scan the inbox in parallel (watcher and import)
        self.wake = threading.Event()        # filesystem event in the inbox
        self.changed = threading.Condition()  # state changed -> notify open pages (SSE)
        self.version = 0
        self.jobs: queue.Queue[str] = queue.Queue()
        self.live: live.LiveManager | None = None  # live sessions, to hand over speaker names and results
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
                continue  # proposals only, no helper files
            try:
                items.append(json.loads(p.read_text()))
            except (json.JSONDecodeError, FileNotFoundError):
                continue  # just deleted/replaced
        # newest recording first; "Recorded At" comes as "2026-10-22 17:18:27", "erstellt" as ISO with "T"
        return sorted(items, key=lambda i: (i.get("aufgenommen") or i["erstellt"]).replace("T", " ")[:19], reverse=True)

    def source(self, item: dict) -> Path:
        return self.inbox / item["datei"]

    # --- Watch inbox -------------------------------------------------

    def scan(self) -> None:
        with self.scan_lock:
            self._scan()

    def make_unique(self, src: Path) -> Path:
        """MacWhisper exports e.g. every Teams recording as "Meeting - Teams.md". If such a file is still
        in the inbox, the next export would overwrite it or not be exported at all. So rename it right away to
        "<Name> <recording time>.md" - then the name is free again for the next export."""
        if UNIQUE_RE.search(src.stem):
            return src
        old_id = self.item_id(src)
        item = self.get(old_id) if self._file(old_id).exists() else None
        if item and item["status"] == "analysiere":
            return src  # rename only after the running analysis
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
            if item:  # keep the existing proposal; the page follows via "frueher"
                item.update(id=self.item_id(dest), datei=dest.name, frueher=old_id)
                self.put(item)
                self.delete(old_id)
        log.info("Renamed (name free for the next export): %s → %s", src.name, dest.name)
        return dest

    def _scan(self) -> None:
        now = time.time()
        present = set()
        for src in list(self.inbox.glob("*.md")):
            if not src.exists():  # older duplicate, already replaced by a newer copy in this pass
                continue
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
            auto = limits.get("automatisch_ab", 0.9)
            # Newest wins against older copies in the inbox; filed notes in the vault stay untouched
            while dup and dup_sim >= auto and (old := self.older_in_inbox(dup, src)):
                self.discard_duplicate(old, dup_sim, {"titel": src.stem})
                self.delete(dup["id"])
                present.discard(dup["id"])
                dup_sim, dup = self.duplicate_of(fp, item_id)
            if dup and dup_sim >= auto:
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
                log.info("Possible duplicate (%.0f %%) of %s: %s", dup_sim * 100, dup["titel"], src.name)
                continue
            if self.live and (sitzung := self.live.session_for_recording(meta.get("Recorded At"))):
                item = self.get(item_id)
                item["live"] = sitzung["id"]
                names = {k: v for k, v in live.names_for_recording(sitzung, meta.get("Recorded At")).items() if k in speakers and v}
                if names:
                    item["sprecher_namen"] = names
                self.put(item)
            if not too_short:
                termine = calendar_candidates(self.cfg, meta)
                item = self.get(item_id)
                item.update(termine=termine, termin_index=0 if termine else None)
                self.put(item)
            if too_short:
                log.info("Too short (%d words), not analyzed: %s", words, src.name)
            else:
                self.jobs.put(item_id)
        # transcript was removed from the inbox by hand -> discard proposal
        for item in self.all():
            if item["id"] not in present:
                self.delete(item["id"])

    def duplicate_of(self, fp: dict, item_id: str) -> tuple[float, dict | None]:
        """Most similar filed transcript or one already in the inbox (approved ones excluded)."""
        data = load_fingerprints()
        if fp["hash"] in data["erlaubt"] or fp["woerter"] < 20:
            return 0.0, None
        pending = []
        for i in self.all():
            if i["id"] == item_id:
                continue
            if not i.get("fp"):  # retroactively capture proposals from before duplicate detection
                try:
                    meta, body = split_frontmatter(self.source(i).read_text())
                except FileNotFoundError:
                    continue
                i["fp"] = fingerprint(body, [str(x) for x in meta.get("Speakers") or []], meta)
                self.put(i)
            pending.append({**i["fp"], "titel": (i.get("ergebnis") or {}).get("titel") or i["datei"],
                            "datei": i["datei"], "id": i["id"]})
        return find_duplicate(fp, data["notizen"] + pending)

    def older_in_inbox(self, dup: dict, src: Path) -> Path | None:
        """Source of the duplicate if it is a transcript in the inbox that arrived no later than `src`."""
        if "id" not in dup:
            return None
        old = self.inbox / dup["datei"]
        try:
            return old if old.stat().st_mtime <= src.stat().st_mtime else None
        except FileNotFoundError:
            return None

    def discard_duplicate(self, src: Path, sim: float, dup: dict) -> None:
        trashed = move_to_trash(src)
        entries = [{"datum": dt.datetime.now().isoformat(timespec="seconds"), "datei": src.name,
                    "papierkorb": trashed.name, "aehnlichkeit": round(sim, 2), "titel": dup["titel"],
                    "ziel": dup.get("ziel"), "url": obsidian_url(self.cfg, Path(dup["ziel"])) if dup.get("ziel") else None},
                   *load_json_list(DUPLIKATE)][:50]
        DUPLIKATE.write_text(json.dumps(entries, ensure_ascii=False, indent=1))
        log.info("Duplicate (%.0f %%) of %s discarded: %s", sim * 100, dup["titel"], src.name)
        en = self.cfg.get("sprache") == "en"
        self.notify_text("Duplicate discarded" if en else "Duplikat verworfen",
                         f"{src.stem} matches “{dup['titel']}” – moved to the trash" if en
                         else f"{src.stem} entspricht „{dup['titel']}“ – in den Papierkorb verschoben",
                         review_url(self.cfg), group="debrief-duplikat")

    def watch(self) -> None:
        try:
            added = index_vault(self.cfg)
            if added:
                log.info("Fingerprints recorded for %d filed notes", added)
        except Exception:
            log.exception("Vault index for duplicates failed")
        try:
            collect_project_todos(self.cfg)  # notes moved into a project folder by hand
        except Exception:
            log.exception("Collecting todos into the project todo notes failed")
        for item in self.all():  # resume analyses left unfinished after a restart
            if item["status"] == "analysiere":
                self.jobs.put(item["id"])
        self.start_fsevents()
        while True:
            try:
                self.scan()
            except Exception:
                log.exception("Error while checking the inbox")
            self.wake.wait(self.next_due())
            self.wake.clear()

    def start_fsevents(self) -> None:
        """macOS reports changes in the inbox (FSEvents via watchdog) - no polling needed."""
        try:
            from watchdog.events import FileSystemEventHandler
            from watchdog.observers import Observer
        except ImportError:
            log.warning("watchdog missing – inbox is only checked every %d s", FALLBACK_S)
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
        log.info("Inbox is watched via file system events")

    def next_due(self) -> float:
        """Seconds until a file is old enough to be renamed or analyzed (otherwise safety check)."""
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
                log.info("Analyzing: %s", item["datei"])
                fixed = item.get("termin_manuell", False)
                if not fixed:  # search for appointments anew on every analysis (calendar or lead time may have changed)
                    item["termine"] = calendar_candidates(self.cfg, meta)
                    self.put(item)
                chosen = chosen_termin(item)
                termine = ([chosen] if chosen else []) if fixed else item["termine"]
                result, nutzung = analyze(self.cfg, meta, body, item.get("sprecher_namen"), termine,
                                          item.get("modell"), termin_fest=fixed)
                item = self.get(item_id)
                if not fixed:  # model picks the appointment; without a valid choice, the one with the largest overlap
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
                log.exception("Analysis failed: %s", item_id)
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
            if NOTIFIER:  # clicking the notification opens the review page
                out = subprocess.run([NOTIFIER, "-title", title, "-message", msg, "-open", url, "-sound", "Glass",
                                      "-group", group], capture_output=True, text=True, timeout=10)
                log.info("Notification via %s (rc=%s) %s", NOTIFIER, out.returncode, (out.stderr or out.stdout).strip())
            else:  # fallback without click action
                log.info("Notification via osascript (terminal-notifier not found)")
                subprocess.run(["osascript", "-e", f"display notification {json.dumps(msg, ensure_ascii=False)} "
                                f"with title \"{title}\" sound name \"Glass\""], timeout=10, check=False)
        except Exception:
            log.exception("Notification failed")


def apply_additions(cfg: dict, body: dict) -> None:
    """Add a new project / person from the review form to the categories."""
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
        raise ValueError(f"Unknown model: {model}")
    return model


def chosen_termin(item: dict) -> dict | None:
    idx = item.get("termin_index")
    termine = item.get("termine") or []
    return termine[idx] if isinstance(idx, int) and 0 <= idx < len(termine) else None


def remember(item: dict, final: dict, names: dict) -> None:
    """Remember deviations from the model assignment and assigned speaker names for the next analyses."""
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
    """Move to the macOS Trash (restorable)."""
    dest, n = TRASH / path.name, 2
    while dest.exists():
        dest, n = TRASH / f"{path.stem} {n}{path.suffix}", n + 1
    shutil.move(str(path), dest)
    return dest


def validate_config(text: str) -> None:
    """Validate a new config.yaml before it replaces the old one."""
    tmp = HERE / "config.check.tmp"
    try:
        tmp.write_text(text)
        cfg = load_config(tmp)
    except yaml.YAMLError as e:
        raise ValueError(f"YAML error: {e}") from e
    except (KeyError, TypeError) as e:
        raise ValueError(f"Required field missing or invalid: {e}") from e
    finally:
        tmp.unlink(missing_ok=True)
    for key in ("vault", "inbox", "ich", "unklar_ordner"):
        if not cfg.get(key):
            raise ValueError(f"“{key}” is missing")
    if not (cfg["vault"] / cfg["inbox"]).is_dir():
        raise ValueError(f"Inbox folder not found: {cfg['vault'] / cfg['inbox']}")
    port = cfg.get("review", {}).get("port", 8765)
    if not isinstance(port, int) or not 1024 <= port <= 65535:
        raise ValueError("review.port must be a number between 1024 and 65535")


def restart_later(delay: float = 0.5) -> None:
    """Restart the server process (load new config.yaml) after the response has been sent."""
    def go():
        time.sleep(delay)
        log.info("Restarting after configuration change")
        logging.shutdown()
        os.execv(sys.executable, [sys.executable, *sys.argv])
    threading.Thread(target=go, daemon=True).start()


def clean_result(r: dict) -> dict:
    todos = [
        {"aufgabe": t["aufgabe"].strip(), "verantwortlich": (t.get("verantwortlich") or "").strip() or None,
         "faellig": t.get("faellig") or None, "ist_meins": bool(t.get("ist_meins")),
         "dringend": bool(t.get("dringend")), "projekt": (t.get("projekt") or "").strip() or None,
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


def make_handler(store: Store, lm: live.LiveManager | None = None):
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
            # Accept only JSON from our own page (protects against form posts from other websites)
            origin = self.headers.get("Origin")
            if not self.headers.get("Content-Type", "").startswith("application/json") or (
                origin and origin != f"http://{self.headers.get('Host')}"
            ):
                raise PermissionError("Invalid request")
            length = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(length) or b"{}")

        def stream_events(self) -> None:
            """Server-Sent Events: send the state to the page on every change."""
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
                pass  # page closed

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

        # --- Live assistant ---------------------------------------------------------------------------

        def live_meta(self, s: dict) -> dict:
            k = s["kontext"]
            termin = (k.get("termine") or [None])[k["termin_index"]] if k.get("termine") and k.get("termin_index") is not None else None
            teilnehmer = [p.get("name") or p.get("email") for p in (termin or {}).get("teilnehmer", [])]
            return {"kategorien": cfg["kategorien"], "ich": cfg["ich"], "sprache": cfg.get("sprache", "de"),
                    "namen": sorted(set(filter(None, teilnehmer + load_gelernt()["namen"]))),
                    "repos_bekannt": live.known_repos(cfg), "gateway": bool(cfg.get("gateway")),
                    **lm.miro_status(), "review_standard": lm.review_default(),
                    "review_vorschlag": live.DEFAULT_REVIEW_KRITERIEN,
                    "vault_name": cfg["vault"].name}

        def stream_live(self, sid: str) -> None:
            """Server-Sent Events for the live page: changed transcript lines and the session state."""
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            sent: dict[str, str] = {}
            last_state, last_labels, version, quiet = None, None, -1, 0.0
            try:
                while True:
                    s = lm.get(sid)
                    zeilen, labels = lm.visible_lines(s)
                    changed = [z for z in zeilen if sent.get(z["id"]) != (z["stand"], z["text"], z["nr"])]
                    removed = sent.keys() - {z["id"] for z in zeilen}
                    if changed or removed or labels != last_labels:
                        sent.update({z["id"]: (z["stand"], z["text"], z["nr"]) for z in changed})
                        for gone in removed:
                            del sent[gone]
                        payload = {"zeilen": changed, "entfernt": sorted(removed), "sprecher": labels}
                        self.wfile.write(f"event: zeilen\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n".encode())
                        last_labels = labels
                    data = json.dumps(s, ensure_ascii=False, default=str)
                    if data != last_state:
                        self.wfile.write(f"event: sitzung\ndata: {data}\n\n".encode())
                        last_state = data
                    if time.monotonic() - quiet > SSE_KEEPALIVE_S:
                        self.wfile.write(b": keepalive\n\n")
                        quiet = time.monotonic()
                    self.wfile.flush()
                    version = lm.wait_change(version, 1.0)  # replays advance with the clock
            except (BrokenPipeError, ConnectionResetError, OSError, KeyError):
                pass

        def miro_login(self, path: str) -> None:
            """OAuth with Miro: /miro/login sends the browser to Miro, Miro sends it back to /miro/callback."""
            auth = lm.miro_auth()
            if auth is None:
                raise KeyError(path)
            if path == "/miro/login":
                self.send_response(HTTPStatus.FOUND)
                self.send_header("Location", auth.login_url())
                self.end_headers()
                return
            query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            fehler = (query.get("error_description") or query.get("error") or [None])[0]
            if not fehler:
                try:
                    auth.callback((query.get("code") or [""])[0], (query.get("state") or [""])[0])
                except (miro.MiroError, RuntimeError) as e:
                    fehler = str(e)
            text = (f"Miro: {html.escape(fehler)}" if fehler
                    else "Mit Miro verbunden – dieses Fenster schließt sich. / Connected to Miro – this window closes.")
            script = "" if fehler else ("<script>try { window.opener && window.opener.postMessage('miro-verbunden', '*'); } "
                                        "catch (e) {} setTimeout(() => window.close(), 800);</script>")
            raw = (f"<!doctype html><meta charset=utf-8><title>Miro</title><body style='font:15px system-ui;padding:40px'>"
                   f"<p>{text}</p>{script}").encode()
            self.send_response(HTTPStatus.BAD_REQUEST if fehler else HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def live_get(self, path: str) -> None:
            query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            if path == "/live":
                raw = LIVE_HTML.read_bytes()
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
            elif path == "/api/live/meetings":
                self.send_json({"meetings": lm.meetings(int((query.get("n") or ["15"])[0]))})
            elif path == "/api/live/transkripte":
                self.send_json({"transkripte": [{"id": i["id"], "datei": i["datei"], "aufgenommen": i.get("aufgenommen"),
                                                 "woerter": i.get("woerter"), "dauer": i.get("dauer")}
                                                for i in store.all() if i.get("status") != "fehler"]})
            elif path == "/api/live/modelle":
                self.send_json({"gateway": gateway_models(cfg) if cfg.get("gateway") else [],
                                "direkt": ["sonnet", "opus", "haiku"], "standard": lm.settings()})
            elif m := re.fullmatch(r"/api/live/(\w+)", path):
                s = lm.open_session(m[1])
                zeilen, labels = lm.visible_lines(s)
                self.send_json({"sitzung": s, "zeilen": zeilen, "sprecher": labels, **self.live_meta(s)})
            elif m := re.fullmatch(r"/api/live/(\w+)/events", path):
                lm.get(m[1])
                self.stream_live(m[1])
            elif m := re.fullmatch(r"/api/live/(\w+)/bibliothek", path):
                k = lm.get(m[1])["kontext"]
                self.send_json({"prozesse": live.list_library(cfg, k.get("kategorie"), k.get("projekt"))})
            elif m := re.fullmatch(r"/api/live/(\w+)/notizen", path):
                k = lm.get(m[1])["kontext"]
                self.send_json({"notizen": live.project_notes(cfg, k.get("kategorie"), k.get("projekt"))})
            else:
                self.send_json({"fehler": "Not found"}, HTTPStatus.NOT_FOUND)

        def transcript_text(self, body: dict) -> tuple[str, str]:
            """Text and file name from a pasted/uploaded text or from an item of the review inbox."""
            if body.get("item"):
                src = store.source(store.get(str(body["item"])))
                return src.read_text(errors="replace"), src.name
            return str(body.get("text") or ""), str(body.get("name") or "")

        def live_post(self, path: str, body: dict) -> None:
            if path == "/api/live/transkript":  # a session of its own for an existing transcript
                s = lm.new_session_from_transcript(*self.transcript_text(body))
                self.send_json({"id": s["id"]})
                return
            m = re.fullmatch(r"/api/live/(\w+)/([\w-]+)", path)
            if not m:
                raise KeyError(path)
            sid, action = m[1], m[2]
            lm.get(sid)
            if action == "start":
                lm.start(sid, body.get("modus", "live"), float(body.get("tempo") or 10))
            elif action == "pause":
                lm.update(sid, lambda s: s.update(pausiert=bool(body.get("pausiert"))))
                if not body.get("pausiert"):
                    lm.trigger(sid)
            elif action == "stopp":
                lm.update(sid, lambda s: s.update(aktiv=False, replay=None, offen=False))
            elif action == "prozess-befehl":
                try:
                    self.send_json(lm.process_command(sid, str(body.get("text") or ""), body.get("prozess") or None))
                except RuntimeError as e:  # the model call failed
                    self.send_json({"fehler": str(e)[:300]}, HTTPStatus.BAD_GATEWAY)
                return
            elif action == "review":
                lm.review(sid, body.get("prozess") or None, body.get("kriterien"), bool(body.get("als_standard")))
            elif action == "review-befund":
                try:
                    self.send_json(lm.review_action(sid, str(body["id"]), str(body.get("aktion") or "")))
                except ValueError as e:
                    self.send_json({"fehler": str(e)}, HTTPStatus.BAD_REQUEST)
                return
            elif action == "prozess-rueckgaengig":
                lm.undo_command(sid)
            elif action == "prozess-neu-erzeugen":
                lm.regenerate(sid, body.get("prozess") or None, bool(body.get("neu_aufbauen")), str(body.get("hinweis") or ""))
            elif action == "neue-sitzung":
                self.send_json({"id": lm.split(sid)["id"]})
                return
            elif action == "transkript-anhaengen":
                lm.attach_transcript(sid, *self.transcript_text(body))
            elif action == "aufnahme-anhaengen":
                lm.attach(sid, str(body["id"]))
            elif action == "mithoeren":
                ms = lm.recording_ms(lm.get(sid))
                lm.update(sid, lambda s: live.set_listening(s, bool(body.get("an")), ms))
            elif action == "ausnehmen":
                von, bis = int(body["von_ms"]), int(body["bis_ms"])
                if von >= bis:
                    raise ValueError("Empty range")
                lm.update(sid, lambda s: (live.exclude_range if body.get("ausnehmen") else live.include_range)(s, von, bis))
            elif action == "sprecher-stumm":
                lm.update(sid, lambda s: live.set_speaker_muted(s, str(body["label"]), bool(body.get("stumm"))))
            elif action == "jetzt":
                lm.trigger(sid)
            elif action == "kontext":
                repos = [live.check_repo(r) for r in body.get("repos", [])] if "repos" in body else None

                def ctx(s):
                    k = s["kontext"]
                    for key in ("kategorie", "projekt", "freitext"):
                        if key in body:
                            k[key] = (str(body[key] or "").strip() or None) if key != "freitext" else str(body[key] or "")
                    if "termin_index" in body:
                        idx = body["termin_index"]
                        if idx is not None and not 0 <= int(idx) < len(k.get("termine") or []):
                            raise ValueError("Unknown appointment")
                        k["termin_index"] = idx
                    if "notizen" in body:
                        k["notizen"] = [str(n) for n in body["notizen"] if live.safe_folder(cfg, str(n)).is_file()]
                    if repos is not None:
                        k["repos"] = repos
                    if "kategorie" in body and "repos" not in body:  # other project: its remembered repositories
                        k["repos"] = live.project_repos(cfg, k["kategorie"], k["projekt"])
                    k["bestaetigt"] = True
                lm.update(sid, ctx)
                if body.get("repos_merken"):
                    s = lm.get(sid)
                    live.remember_repos(cfg, s["kontext"]["kategorie"], s["kontext"]["projekt"], s["kontext"]["repos"])
                    reload_kategorien(cfg)
            elif action == "repo-waehlen":
                self.send_json({"repos": importer.choose_folders()})
                return
            elif action == "sprecher":
                lm.update(sid, lambda s: s["sprecher_namen"].update({str(body["label"]): str(body.get("name") or "").strip()}))
            elif action == "anweisung":
                lm.update(sid, lambda s: live.add_instruction(s, str(body.get("text") or ""), bool(body.get("einmalig"))))
                lm.trigger(sid)
            elif action == "anweisung-loeschen":
                lm.update(sid, lambda s: s.update(anweisungen=[a for a in s["anweisungen"] if a["id"] != body["id"]]))
            elif action == "fakt-loeschen":
                lm.update(sid, lambda s: s.update(fakten=[f for f in s["fakten"] if f["id"] != body["id"]]))
            elif action == "eintrag":
                lm.update(sid, lambda s: live.set_entry_status(s, str(body["id"]), str(body["status"]), body.get("notiz")))
            elif action == "eintrag-neu":
                lm.update(sid, lambda s: live.add_entry(s, str(body.get("typ") or "frage"), str(body.get("text") or "")))
            elif action == "prozess":
                lm.update(sid, lambda s: live.edit_process(s, body))
            elif action == "vorschlag":
                lm.update(sid, lambda s: live.decide_proposal(s, str(body["id"]), bool(body.get("annehmen"))))
            elif action == "recherche":
                lm.research(sid, str(body.get("frage") or ""))
            elif action == "modell":
                known = {m2["id"] for m2 in gateway_models(cfg)}
                weg = "gateway" if body.get("weg") == "gateway" and cfg.get("gateway") else "direkt"
                modell = {"weg": weg, **{k: str(body.get(k) or "").strip() for k in ("analyse", "recherche")}}
                for k in ("analyse", "recherche"):
                    if not modell[k] or (weg == "gateway" and known and modell[k] not in known):
                        raise ValueError(f"Unknown model: {modell[k]}")
                lm.update(sid, lambda s: s.update(modell=modell))
                if body.get("standard"):
                    lm.save_settings(modell)
            elif action == "bibliothek-laden":
                lm.update(sid, lambda s: live.load_library_process(cfg, s, str(body["pfad"])))
            elif action == "miro":
                try:
                    state = lm.miro_sync(sid, body.get("board"))
                except miro.LoginNeeded as e:
                    self.send_json({"fehler": str(e), "login": True}, HTTPStatus.UNAUTHORIZED)
                    return
                except (miro.MiroError, RuntimeError) as e:
                    self.send_json({"fehler": str(e)}, HTTPStatus.BAD_GATEWAY)
                    return
                self.send_json({"url": state["url"]})
                return
            elif action == "bibliothek-speichern":
                rel = {}
                lm.update(sid, lambda s: rel.update(pfad=live.save_library_process(
                    cfg, s, str(body["prozess"]), body.get("name"), bool(body.get("als_neu")))))
                self.send_json({"pfad": rel["pfad"], "url": obsidian_url(cfg, Path(rel["pfad"]))})
                return
            else:
                raise KeyError(action)
            self.send_json({"ok": True})

        def do_GET(self):
            path = self.path.split("?")[0]
            with store.lock:
                reload_kategorien(cfg)
            try:
                if path.startswith("/api/live/") or path == "/live":
                    self.live_get(path)
                elif path in ("/miro/login", "/miro/callback"):
                    self.miro_login(path)
                elif path == "/":
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
                elif path == "/api/categories.yaml":
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
                    self.send_json({"fehler": "Not found"}, HTTPStatus.NOT_FOUND)
            except (KeyError, FileNotFoundError):
                self.send_json({"fehler": "Not found"}, HTTPStatus.NOT_FOUND)
            except PermissionError as e:
                self.send_json({"fehler": str(e)}, HTTPStatus.FORBIDDEN)

        def do_POST(self):
            path = self.path.split("?")[0]
            with store.lock:
                reload_kategorien(cfg)
            try:
                body = self.read_json()
                if path.startswith("/api/live/"):
                    try:
                        self.live_post(path, body)
                    except KeyError:  # not the inbox message "already filed?"
                        self.send_json({"fehler": "Not found"}, HTTPStatus.NOT_FOUND)
                elif m := re.fullmatch(r"/api/items/(\w+)/ablegen", path):
                    item = store.get(m[1])
                    body["ergebnis"] = clean_result(body["ergebnis"])
                    vorschlag = item["ergebnis"]["kategorie"]
                    if body["ergebnis"]["kategorie"] != vorschlag:
                        body["ergebnis"]["begruendung"] = f"Manuell zugeordnet (Vorschlag war: {vorschlag})."
                    if body["ergebnis"]["kategorie"] not in cfg["kategorien"]:
                        raise ValueError("Please choose a category")
                    names = {k: v for k, v in (body.get("sprecher_namen") or {}).items() if k in item.get("sprecher", [])}
                    if termin := chosen_termin(item):
                        body["ergebnis"]["termin"] = termin["titel"]
                    if item.get("live") and store.live:
                        try:
                            body["ergebnis"]["live_abschnitt"] = live.note_section(store.live.get(item["live"]))
                        except KeyError:
                            pass
                    apply_additions(cfg, body)
                    with store.lock:
                        dest = write_note(store.source(item), cfg, body["ergebnis"], reviewed=True, names=names)
                        store.delete(item["id"])
                    rel = dest.relative_to(cfg["vault"].resolve())
                    log.info("Filed: %s", rel)
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
                            raise ValueError("Unknown appointment")
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
                    log.info("Discarded (trash): %s", item["datei"])
                    self.send_json({"papierkorb": trashed.name, "datei": item["datei"], "state": self.state()})
                elif path == "/api/wiederherstellen":
                    src = TRASH / Path(body["papierkorb"]).name
                    dest = store.inbox / Path(body["datei"]).name
                    if not src.exists() or dest.exists():
                        raise ValueError("Restore not possible")
                    shutil.move(str(src), dest)
                    self.send_json(self.state())
                elif path == "/api/import":
                    names = [importer.to_inbox(cfg, Path(i)) for i in body["ids"]]
                    log.info("Imported: %s", ", ".join(names))
                    store.scan()  # pick up immediately instead of waiting for the next pass
                    self.send_json({"importiert": names, "state": self.state()})
                elif path == "/api/import/hochladen":
                    names, fehler = [], []
                    for f in body["dateien"][:50]:
                        try:
                            names.append(importer.upload_to_inbox(cfg, f["name"], f["inhalt"], f.get("geaendert")))
                        except ValueError as e:
                            fehler.append(str(e))
                    if names:
                        log.info("Uploaded: %s", ", ".join(names))
                        store.scan()
                    self.send_json({"importiert": names, "fehler": fehler, "state": self.state()})
                elif path == "/api/import/einfuegen":
                    name = importer.paste_to_inbox(cfg, body.get("text") or "", body.get("titel") or "")
                    log.info("Pasted: %s", name)
                    store.scan()
                    self.send_json({"importiert": [name], "state": self.state()})
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
                    log.info("Default model: %s", cfg["model"])
                    self.send_json(self.state())
                elif path == "/api/korrekturen/speichern":
                    data = load_gelernt()
                    k = data["korrekturen"][int(body["index"])]
                    richtig = body["richtig"]
                    if richtig.get("kategorie") not in cfg["kategorien"]:
                        raise ValueError("Unknown category")
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
                        raise ValueError("No longer in the trash, or file already exists in the inbox")
                    meta, text = split_frontmatter(src.read_text())
                    data = load_fingerprints()  # restored = intended, do not treat as a duplicate again
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
                    log.info("config.yaml changed (backup: config.yaml.bak)")
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
                elif path == "/api/categories.yaml":
                    save_kategorien(cfg, yaml.safe_load(body["yaml"]) or {})
                    self.send_json(self.state())
                else:
                    self.send_json({"fehler": "Not found"}, HTTPStatus.NOT_FOUND)
            except PermissionError as e:
                self.send_json({"fehler": str(e)}, HTTPStatus.FORBIDDEN)
            except (KeyError, FileNotFoundError):
                self.send_json({"fehler": "Not found (already filed?)"}, HTTPStatus.NOT_FOUND)
            except IndexError:
                self.send_json({"fehler": "Entry not found – reload the page"}, HTTPStatus.BAD_REQUEST)
            except (ValueError, yaml.YAMLError) as e:
                self.send_json({"fehler": str(e)}, HTTPStatus.BAD_REQUEST)
            except Exception as e:
                log.exception("Error at %s", path)
                self.send_json({"fehler": str(e)}, HTTPStatus.INTERNAL_SERVER_ERROR)
            finally:
                store.notify_change()  # update open pages (SSE)

    return Handler


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", type=Path, default=HERE / "config.yaml")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        handlers=[logging.StreamHandler(), logging.FileHandler(HERE / "debrief.log")])
    cfg = load_config(args.config)
    store = Store(cfg)
    lm = live.LiveManager(cfg, notify=lambda title, msg, path, group: store.notify_text(title, msg, review_url(cfg) + path, group))
    store.live = lm
    lm.start_threads()
    threading.Thread(target=store.watch, daemon=True).start()
    threading.Thread(target=store.work, daemon=True).start()

    port = cfg.get("review", {}).get("port", 8765)
    server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(store, lm))
    log.info("Review page: %s", review_url(cfg))
    server.serve_forever()


if __name__ == "__main__":
    main()

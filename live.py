"""Live assistant: reads the running MacWhisper transcript and supports you during the meeting.

A live session belongs to one MacWhisper recording and is stored as `live/<session id>.json`. It holds
- the context (calendar event, category/project, repositories, free text, attached notes),
- entries the assistant suggests (questions, assumptions, open points, code references) with their status,
- facts and instructions from you (they go into every run),
- one or more processes as an event storming model (process level) that you and the assistant edit together.

Rules that keep your changes: what you touched or confirmed is locked - the assistant only proposes changes to it.
Elements you deleted and entries you dropped are not suggested again.

Two jobs run independently: the conversation analysis (every `takt_s` seconds if new confirmed lines arrived,
no tools) and the code research (read-only tools in the chosen repositories, asynchronous).
"""

from __future__ import annotations

import copy
import datetime as dt
import json
import logging
import queue
import re
import subprocess
import threading
import time
import uuid
from pathlib import Path

import yaml

import macwhisper as mw
import miro
import transcripts
from debrief import (HERE, calendar_candidates, gateway_token, reload_kategorien, role, run_claude, safe_folder,
                     save_kategorien, termin_line)

log = logging.getLogger("debrief.live")

LIVE_DIR = HERE / "live"
EINSTELLUNGEN = LIVE_DIR / "settings.json"  # default model choice for live sessions
TAKT_S = 75            # automatic analysis at most this often
ANTWORT_TAKT_S = 12    # open questions are checked against new lines this often (small, fast run)
FENSTER_MIN = 15       # verbatim transcript in the prompt, older parts as a running summary
ZUSAMMENFASSEN_AB_MIN = 10  # summarise once this much has dropped out of the window
MAX_NOTIZ_ZEICHEN = 6000
SAME_RECORDING_S = 180
MAX_IMPORT_BYTES = 3_000_000
GAP_AFTER_LAST_MS = 60_000  # an attached transcript starts this long after the last line of the session
CONTINUE_WITHIN_S = 4 * 3600  # a new recording within this time after a session is offered as its continuation  # transcript in the inbox belongs to a live session if it starts within this time

TYPEN = ["actor", "command", "event", "policy", "readmodel", "system", "aggregate", "hotspot"]
EINTRAG_TYPEN = ["frage", "annahme", "offen", "code"]
STATUS = {
    "frage": ["neu", "angepinnt", "gestellt", "beantwortet", "verworfen"],
    "annahme": ["neu", "stimmt", "stimmt_nicht", "unklar", "verworfen"],
    "offen": ["neu", "angepinnt", "erledigt", "verworfen"],
    "code": ["neu", "angepinnt", "verworfen"],
}
OFFEN = {"neu", "angepinnt", "gestellt"}


def new_id() -> str:
    return uuid.uuid4().hex[:8]


def norm(text: str) -> str:
    return re.sub(r"\W+", " ", str(text or "").casefold()).strip()


def now_iso() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


# --- Session --------------------------------------------------------------------------------------------------

def new_session(meeting: dict, modell: dict) -> dict:
    return {
        "id": meeting["id"], "titel": meeting["titel"], "start": meeting["start"], "app": meeting.get("app"),
        "erstellt": now_iso(), "aktiv": False, "pausiert": False, "beendet": False, "replay": None,
        "kontext": {"termine": None, "termin_index": None, "kategorie": None, "projekt": None, "repos": [],
                    "freitext": "", "notizen": [], "bestaetigt": False, "vorschlag": None},
        "sprecher_namen": {}, "anweisungen": [], "fakten": [], "eintraege": [], "prozesse": [], "vorschlaege": [],
        "abgelehnt": [], "auftraege": [], "zusammenfassung": {"text": "", "bis_ms": 0},
        "analysiert_bis": -1, "letzter_lauf": 0.0, "laeufe": [], "modell": dict(modell), "laeuft": None,
        "offen": False,  # resumed by hand: stays open when the recording is over, until you stop it
        "ausgenommen": [],  # time ranges (ms into the recording) that are not listened to; bis_ms null = still muted
        "stumm": [],  # speakers whose contributions are not listened to
        "geprueft_bis": -1,  # last line nr checked for answers to open questions (fast run between the analyses)
        "antwortpruefung": 0.0,
        "arbeit": None,  # what the assistant works on right now: analyse, neu_erzeugen, review, befehl (for the skeletons)
        "review": None,  # last process review: {zeit, prozess, zusammenfassung, befunde}
        "review_kriterien": None,  # what the review looks at (None: the default)
        "review_verworfen": [],  # dismissed findings, not reported again
        "rueckgaengig": None,  # text of the last process edit that can be undone
        "quelle_id": None,  # the MacWhisper recording when it is not the session's own id (a session started mid-recording)
        "ab_ms": None,  # a session started mid-recording begins here (ms into the recording) ...
        "bis_ms": None,  # ... and the one it was split from ends here
        "aufnahmen": [],  # further recordings attached to this session: {id, start, offset_ms, titel}
    }


def recordings(s: dict) -> list[dict]:
    """The session's recordings in time order: its own first, then the attached ones. `offset_ms` is where a
    recording starts on the session's time line."""
    return [{"id": s.get("quelle_id") or s["id"], "start": s["start"], "offset_ms": 0}, *s.get("aufnahmen", [])]


def find(items: list[dict], item_id: str) -> dict:
    for i in items:
        if i["id"] == item_id:
            return i
    raise KeyError(item_id)


def all_elements(s: dict) -> dict[str, tuple[dict, dict]]:
    return {e["id"]: (p, e) for p in s["prozesse"] for e in p["elemente"]}


def new_process(s: dict, name: str, herkunft: str = "ki") -> dict:
    p = {"id": new_id(), "name": name.strip() or "Prozess", "elemente": [], "verbindungen": [], "geloescht": [],
         "getrennt": [], "quelle": None, "original": None, "herkunft": herkunft, "aliase": []}
    s["prozesse"].append(p)
    return p


def process_named(s: dict, name: str | None) -> dict | None:
    """The process with this id or name, also under a name it had before it was merged into another one."""
    key = norm(name or "")
    for p in s["prozesse"]:
        if name == p["id"] or key in {norm(p["name"]), *map(norm, p.get("aliase", []))}:
            return p
    return None


def merge_processes(s: dict, ids: list[str], name: str | None = None) -> dict:
    """Combine processes into the first one: the elements follow each other on the time line, actors that occur
    in several processes share one lane. The old names stay as aliases so the assistant does not re-create them."""
    procs = [find(s["prozesse"], i) for i in dict.fromkeys(ids)]
    if len(procs) < 2:
        raise ValueError("Choose at least two processes")
    ziel = procs[0]
    ziel.setdefault("aliase", [])
    for p in procs[1:]:
        ziel["elemente"] += p["elemente"]
        for key in ("verbindungen", "getrennt", "geloescht"):
            ziel[key] += [x for x in p[key] if x not in ziel[key]]
        ziel["aliase"] += [p["name"], *p.get("aliase", [])]
        s["prozesse"].remove(p)
    if name and name.strip() and name.strip() != ziel["name"]:
        ziel["aliase"].append(ziel["name"])
        ziel["name"] = name.strip()
    return ziel


def new_element(typ: str, text: str, lane: str = "", herkunft: str = "ki", annahme: bool = False) -> dict:
    return {"id": new_id(), "typ": typ if typ in TYPEN else "event", "text": text.strip(), "lane": (lane or "").strip(),
            "herkunft": herkunft, "gesperrt": herkunft != "ki", "annahme": annahme}


def insert(p: dict, el: dict, nach: str | None = None, vor: str | None = None) -> None:
    ids = [e["id"] for e in p["elemente"]]
    if vor in ids:
        p["elemente"].insert(ids.index(vor), el)
    elif nach in ids:
        p["elemente"].insert(ids.index(nach) + 1, el)
    else:
        p["elemente"].append(el)


def remove_element(s: dict, el_id: str) -> tuple[dict, dict]:
    p, el = all_elements(s)[el_id]
    p["elemente"].remove(el)
    p["verbindungen"] = [v for v in p["verbindungen"] if el_id not in (v["von"], v["nach"])]
    for e in s["eintraege"]:
        e["elemente"] = [x for x in e.get("elemente", []) if x != el_id]
    s["vorschlaege"] = [v for v in s["vorschlaege"] if v["element"] != el_id]
    return p, el


# --- Edit by instruction and undo ---------------------------------------------------------------------------------

def apply_commands(s: dict, ops: list[dict], prozess: str | None = None) -> dict:
    """Carry out the operations of a spoken/typed instruction as your own edits (locked, like on the board).
    One failing operation does not stop the others."""
    refs: dict[str, str] = {}
    proc_refs: dict[str, dict] = {}
    done, errors = 0, []

    def eid(x):
        return refs.get(x, x) if x else None

    def proc(ref):
        if ref in proc_refs:
            return proc_refs[ref]
        p = process_named(s, ref) if ref else None
        p = p or (process_named(s, prozess) if prozess else None)
        if p:
            return p
        if s["prozesse"]:
            return s["prozesse"][-1]
        raise ValueError("No process")

    for op in ops:
        art = op.get("op")
        try:
            before = set(all_elements(s))
            if art == "prozess_neu":
                edit_process(s, {"op": art, "name": op.get("name") or op.get("text") or ""})
                if op.get("ref"):
                    proc_refs[op["ref"]] = s["prozesse"][-1]
            elif art == "prozess_umbenennen":
                edit_process(s, {"op": art, "id": proc(op.get("prozess") or op.get("id"))["id"], "name": op.get("name") or op.get("text")})
            elif art == "element_neu":
                edit_process(s, {"op": art, "prozess": proc(op.get("prozess"))["id"], "typ": op.get("typ") or "event",
                                 "text": op.get("text") or "", "lane": op.get("lane") or "", "nach": eid(op.get("nach")),
                                 "vor": eid(op.get("vor"))})
                new = set(all_elements(s)) - before
                if op.get("ref") and new:
                    refs[op["ref"]] = next(iter(new))
            elif art in ("element_aendern", "element_loeschen"):
                args = {"op": art, "id": eid(op["id"]), **{k: op[k] for k in ("text", "typ", "lane", "annahme") if op.get(k) is not None}}
                edit_process(s, args)
            elif art == "element_verschieben":
                edit_process(s, {"op": art, "id": eid(op["id"]), "lane": op.get("lane"), "nach": eid(op.get("nach")), "vor": eid(op.get("vor")),
                                 "prozess": proc(op["prozess"])["id"] if op.get("prozess") else None})
            elif art in ("verbinden", "trennen"):
                edit_process(s, {"op": art, "von": eid(op["von"]), "nach": eid(op["nach"])})
            else:
                raise ValueError(f"Unknown operation: {art}")
            done += 1
        except (KeyError, ValueError, TypeError) as e:
            errors.append(f"{art}: {e}")
    return {"angewendet": done, "fehler": errors}


def snapshot(s: dict) -> dict:
    """What an instruction or a regeneration can change - for one step back."""
    return json.loads(json.dumps({"prozesse": s["prozesse"], "vorschlaege": s["vorschlaege"],
                                  "links": {e["id"]: e.get("elemente", []) for e in s["eintraege"]}}))


def restore(s: dict, snap: dict) -> None:
    s["prozesse"], s["vorschlaege"] = snap["prozesse"], snap["vorschlaege"]
    for e in s["eintraege"]:
        if e["id"] in snap["links"]:
            e["elemente"] = snap["links"][e["id"]]


# --- Your actions ------------------------------------------------------------------------------------------------

def set_entry_status(s: dict, entry_id: str, status: str, notiz: str | None = None) -> None:
    """Lifecycle of an entry. Rated assumptions become facts for the following runs; "unklar" turns into a question."""
    e = find(s["eintraege"], entry_id)
    if status not in STATUS[e["typ"]] + ["neu"]:
        raise ValueError(f"Invalid status: {status}")
    s["fakten"] = [f for f in s["fakten"] if f.get("eintrag") != entry_id]  # re-rating replaces the old fact
    if notiz is not None:
        e["notiz"] = notiz.strip()
    if e["typ"] == "annahme" and status == "unklar":
        e.update(typ="frage", status="neu", text=e["text"] if e["text"].endswith("?") else f"Stimmt das: {e['text']}?")
        return
    e["status"] = status
    elemente = [all_elements(s).get(x) for x in e.get("elemente", [])]
    if e["typ"] == "annahme" and status == "stimmt":
        s["fakten"].append({"id": new_id(), "text": e["text"] + (f" ({e['notiz']})" if e.get("notiz") else ""),
                            "eintrag": entry_id})
        for pe in filter(None, elemente):
            pe[1].update(annahme=False, gesperrt=True, herkunft="bestaetigt")
    elif e["typ"] == "annahme" and status == "stimmt_nicht":
        richtig = f" Richtig ist: {e['notiz']}" if e.get("notiz") else ""
        s["fakten"].append({"id": new_id(), "text": f"Falsch: {e['text'].rstrip('.')}.{richtig}", "eintrag": entry_id})
        for pe in filter(None, elemente):  # unlocked, so the next run may correct them
            pe[1].update(gesperrt=False)
    elif e["typ"] == "frage" and status == "beantwortet" and e.get("notiz"):
        s["fakten"].append({"id": new_id(), "text": f"{e['text']} → {e['notiz']}", "eintrag": entry_id})


def add_instruction(s: dict, text: str, einmalig: bool = False) -> dict:
    a = {"id": new_id(), "text": text.strip(), "einmalig": einmalig, "neu": True}
    if not a["text"]:
        raise ValueError("Empty instruction")
    s["anweisungen"].append(a)
    return a


def edit_process(s: dict, op: dict) -> None:
    """Your direct edits on the board. Everything you touch is locked."""
    art = op.get("op")
    if art == "prozess_neu":
        new_process(s, op.get("name") or "Prozess", "ich")
        return
    if art == "prozesse_zusammenfuehren":
        merge_processes(s, op.get("ids") or [p["id"] for p in s["prozesse"]], op.get("name"))
        return
    if art in ("prozess_umbenennen", "prozess_loeschen"):
        p = find(s["prozesse"], op["id"])
        if art == "prozess_umbenennen":
            p["name"] = (op.get("name") or "").strip() or p["name"]
        else:
            for el in list(p["elemente"]):
                remove_element(s, el["id"])
            s["prozesse"].remove(p)
        return
    if art == "element_neu":
        p = find(s["prozesse"], op["prozess"])
        el = new_element(op.get("typ", "event"), op.get("text") or "", op.get("lane", ""), "ich")
        if not el["text"]:
            raise ValueError("Empty text")
        insert(p, el, nach=op.get("nach"), vor=op.get("vor"))
        return
    if art in ("verbinden", "trennen"):
        p, _ = all_elements(s)[op["von"]]
        if op["nach"] not in {e["id"] for e in p["elemente"]}:
            raise ValueError("Elements are in different processes")
        pair = {"von": op["von"], "nach": op["nach"]}
        p["verbindungen"] = [v for v in p["verbindungen"] if (v["von"], v["nach"]) != (op["von"], op["nach"])]
        p["getrennt"] = [v for v in p["getrennt"] if v != pair]
        (p["verbindungen"] if art == "verbinden" else p["getrennt"]).append(pair)
        return
    p, el = all_elements(s)[op["id"]]
    if art == "element_aendern":
        for key in ("text", "typ", "lane"):
            if key in op and op[key] is not None:
                value = str(op[key]).strip()
                if key == "typ" and value not in TYPEN:
                    raise ValueError(f"Unknown type: {value}")
                if key == "text" and not value:
                    raise ValueError("Empty text")
                el[key] = value
        if "annahme" in op:
            el["annahme"] = bool(op["annahme"])
        el.update(gesperrt=True, herkunft="ich")
    elif art == "element_loeschen":
        p, el = remove_element(s, el["id"])
        p["geloescht"].append(norm(el["text"]))
    elif art == "element_verschieben":
        ziel = find(s["prozesse"], op["prozess"]) if op.get("prozess") else p
        p["elemente"].remove(el)
        if ziel is not p:
            p["verbindungen"] = [v for v in p["verbindungen"] if el["id"] not in (v["von"], v["nach"])]
        if op.get("lane") is not None:
            el["lane"] = str(op["lane"]).strip()
        insert(ziel, el, vor=op.get("vor"), nach=op.get("nach"))
        el["gesperrt"] = True
    elif art == "sperren":
        el["gesperrt"] = bool(op.get("gesperrt", True))
    elif art == "bestaetigen":
        el.update(gesperrt=True, annahme=False, herkunft="bestaetigt")
    else:
        raise ValueError(f"Unknown operation: {art}")


def decide_proposal(s: dict, vorschlag_id: str, annehmen: bool) -> None:
    v = find(s["vorschlaege"], vorschlag_id)
    s["vorschlaege"].remove(v)
    if not annehmen:
        s["abgelehnt"].append(proposal_key(v))
        return
    if v["element"] not in all_elements(s):
        return
    if v["art"] == "loeschen":
        p, el = remove_element(s, v["element"])
        p["geloescht"].append(norm(el["text"]))
    else:
        _, el = all_elements(s)[v["element"]]
        el.update({k: v["felder"][k] for k in ("text", "typ", "lane") if v["felder"].get(k)})
        el.update(gesperrt=True, herkunft="bestaetigt")


def proposal_key(v: dict) -> str:
    return f"{v['art']}|{v['element']}|{norm(json.dumps(v.get('felder') or {}, sort_keys=True))}"


# --- Merging an analysis --------------------------------------------------------------------------------------------

def apply_analysis(s: dict, res: dict, lauf: int) -> None:
    """Take over the assistant's suggestions without overwriting anything of yours."""
    refs: dict[str, str] = {}
    proc_refs: dict[str, dict] = {}

    def process_for(ref: str | None) -> dict:
        if ref in proc_refs:
            return proc_refs[ref]
        p = process_named(s, ref)
        if p:
            return p
        return s["prozesse"][0] if s["prozesse"] else new_process(s, "Prozess")

    def element_id(ref: str | None) -> str | None:
        return refs.get(ref or "", ref) if ref else None

    for op in res.get("aenderungen", []):
        art = op.get("op")
        try:
            if art == "prozess_neu":
                name = (op.get("text") or op.get("prozess") or "Prozess").strip()
                existing = process_named(s, name)
                proc_refs[op.get("ref") or name] = existing or new_process(s, name)
            elif art == "neu":
                p = process_for(op.get("prozess"))
                text = (op.get("text") or "").strip()
                if not text or norm(text) in p["geloescht"]:
                    continue
                same = next((e for e in p["elemente"] if norm(e["text"]) == norm(text)), None)
                if same:
                    refs[op.get("ref") or ""] = same["id"]
                    continue
                el = new_element(op.get("typ") or "event", text, op.get("lane") or "", "ki", bool(op.get("annahme")))
                insert(p, el, nach=element_id(op.get("nach")))
                if op.get("ref"):
                    refs[op["ref"]] = el["id"]
            elif art in ("aendern", "loeschen"):
                el_id = element_id(op.get("element"))
                if el_id not in all_elements(s):
                    continue
                p, el = all_elements(s)[el_id]
                felder = {k: str(op[k]).strip() for k in ("text", "typ", "lane") if op.get(k)}
                if felder.get("typ") not in (None, *TYPEN):
                    felder.pop("typ")
                if art == "aendern" and all(el.get(k) == v for k, v in felder.items()):
                    continue
                if el["gesperrt"]:
                    v = {"id": new_id(), "art": art, "element": el_id, "felder": felder if art == "aendern" else {},
                         "begruendung": (op.get("begruendung") or "").strip(), "lauf": lauf}
                    if proposal_key(v) not in s["abgelehnt"] and \
                            proposal_key(v) not in {proposal_key(x) for x in s["vorschlaege"]}:
                        s["vorschlaege"].append(v)
                elif art == "loeschen":
                    remove_element(s, el_id)
                else:
                    el.update(felder)
                    if "annahme" in op:
                        el["annahme"] = bool(op["annahme"])
            elif art == "verbinden":
                von, nach = element_id(op.get("von")), element_id(op.get("nach"))
                els = all_elements(s)
                if von not in els or nach not in els or els[von][0] is not els[nach][0]:
                    continue
                p, pair = els[von][0], {"von": von, "nach": nach}
                if pair not in p["verbindungen"] and pair not in p["getrennt"]:
                    p["verbindungen"].append(pair)
        except (KeyError, ValueError):
            log.warning("Live: ignored change %s", op)

    known = {norm(e["text"]) for e in s["eintraege"]}
    for neu in res.get("eintraege", []):
        text = (neu.get("text") or "").strip()
        if not text or norm(text) in known or neu.get("typ") not in ("frage", "annahme", "offen"):
            continue
        known.add(norm(text))
        els = all_elements(s)
        s["eintraege"].append({
            "id": new_id(), "typ": neu["typ"], "text": text, "begruendung": (neu.get("begruendung") or "").strip(),
            "prio": min(3, max(1, int(neu.get("prio") or 2))), "zeilen": [int(z) for z in neu.get("zeilen") or []][:5],
            "elemente": [x for x in (element_id(r) for r in neu.get("elemente") or []) if x in els],
            "status": "neu", "notiz": "", "lauf": lauf, "erstellt": now_iso(), "quelle": "analyse",
        })
    for done in res.get("erledigt", []):
        try:
            e = find(s["eintraege"], done.get("id", ""))
        except KeyError:
            continue
        if e["status"] in OFFEN and e["typ"] in ("frage", "offen"):
            e["status"] = "beantwortet" if e["typ"] == "frage" else "erledigt"
            e["notiz"] = (done.get("notiz") or "").strip()
            e["automatisch"] = True

    if s["kontext"]["repos"]:
        known_q = {norm(a["frage"]) for a in s["auftraege"]}
        for r in res.get("recherche", [])[:3]:
            frage = (r.get("frage") or "").strip()
            if frage and norm(frage) not in known_q:
                known_q.add(norm(frage))
                s["auftraege"].append({"id": new_id(), "frage": frage, "begruendung": (r.get("begruendung") or "").strip(),
                                       "status": "offen", "ergebnis": None, "erstellt": now_iso(), "von": "ki"})

    einordnung = {a.get("id"): a for a in res.get("anweisungen", [])}
    for a in list(s["anweisungen"]):
        if not a.get("neu"):
            continue
        e = einordnung.get(a["id"]) or {}
        if e.get("art") == "fakt":
            s["fakten"].append({"id": new_id(), "text": (e.get("fakt") or a["text"]).strip(), "anweisung": a["text"]})
            s["anweisungen"].remove(a)
        elif a["einmalig"]:
            s["anweisungen"].remove(a)
        else:
            a["neu"] = False

    k = res.get("kontext") or {}
    if not s["kontext"]["bestaetigt"] and k.get("kategorie"):
        s["kontext"]["vorschlag"] = {"kategorie": k["kategorie"], "projekt": k.get("projekt")}


def apply_research(s: dict, auftrag: dict, res: dict) -> None:
    auftrag.update(status="fertig", ergebnis=res.get("antwort", "").strip(), fundstellen=res.get("fundstellen", [])[:8])
    known = {norm(e["text"]) for e in s["eintraege"]}
    base = {"begruendung": "", "prio": 2, "zeilen": [], "elemente": [], "status": "neu", "notiz": "",
            "lauf": None, "erstellt": now_iso(), "quelle": "recherche", "auftrag": auftrag["id"]}
    s["eintraege"].append({**base, "id": new_id(), "typ": "code", "text": auftrag["frage"],
                           "begruendung": auftrag["ergebnis"], "fundstellen": auftrag["fundstellen"]})
    for typ, key in (("annahme", "annahmen"), ("frage", "fragen")):
        for x in res.get(key, [])[:3]:
            text = (x.get("text") or "").strip()
            if text and norm(text) not in known:
                known.add(norm(text))
                s["eintraege"].append({**base, "id": new_id(), "typ": typ, "text": text,
                                       "begruendung": (x.get("begruendung") or "").strip()})


# --- Prompts ----------------------------------------------------------------------------------------------------

BEFEHL_OP = {
    "type": "object",
    "properties": {
        "op": {"type": "string", "enum": ["element_neu", "element_aendern", "element_loeschen", "element_verschieben",
                                          "verbinden", "trennen", "prozess_neu", "prozess_umbenennen"]},
        "ref": {"type": "string", "description": "element_neu / prozess_neu: a name later operations can refer to"},
        "id": {"type": "string", "description": "element id (or ref) for aendern/loeschen/verschieben; process id for umbenennen"},
        "prozess": {"type": "string", "description": "process id or name (element_neu: where it goes; verschieben: target process)"},
        "text": {"type": "string"}, "name": {"type": "string", "description": "process name"},
        "typ": {"type": "string", "enum": TYPEN}, "lane": {"type": "string"}, "annahme": {"type": "boolean"},
        "nach": {"type": "string", "description": "element id (or ref) to put the element after; verbinden: the target"},
        "vor": {"type": "string", "description": "element id (or ref) to put the element before"},
        "von": {"type": "string", "description": "verbinden/trennen: the source element"},
    },
    "required": ["op"],
}
BEFEHL_SCHEMA = {
    "type": "object",
    "properties": {
        "antwort": {"type": "string", "description": "One or two sentences in German: what you changed, or why not"},
        "operationen": {"type": "array", "items": BEFEHL_OP},
    },
    "required": ["antwort", "operationen"],
}
REVIEW_KATEGORIEN = ["vollstaendigkeit", "sonderfaelle", "verantwortung", "sprache", "entscheidungen", "daten_systeme",
                     "granularitaet", "konsistenz", "kennzahlen", "sonstiges"]
REVIEW_SCHEMA = {
    "type": "object",
    "properties": {
        "zusammenfassung": {"type": "string", "description": "Two or three sentences in German: overall judgement of the model"},
        "befunde": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "kategorie": {"type": "string", "enum": REVIEW_KATEGORIEN},
                "schwere": {"type": "string", "enum": ["hoch", "mittel", "niedrig"]},
                "elemente": {"type": "array", "items": {"type": "string"}, "description": "ids of the elements concerned"},
                "beobachtung": {"type": "string", "description": "What is wrong or missing, concrete"},
                "empfehlung": {"type": "string", "description": "What to do about it"},
                "frage": {"type": "string", "description": "Optional: the question to ask in the meeting to settle it"},
                "aenderung": {"type": "array", "items": BEFEHL_OP,
                              "description": "Optional: operations that carry out the recommendation, only if unambiguous"},
            },
            "required": ["kategorie", "schwere", "beobachtung", "empfehlung"],
        }},
    },
    "required": ["zusammenfassung", "befunde"],
}

# What the review looks at. The user can change this per session or keep a changed version as the default.
DEFAULT_REVIEW_KRITERIEN = """Prüfe den Prozess nach Event-Storming- und Prozessmodellierungs-Regeln:

1. Vollständigkeit des Ablaufs: Gibt es einen klaren Auslöser am Anfang und ein Ergebnis oder Ende? Führt jeder Command zu \
mindestens einem Event, und hat jedes Event eine Folge (Policy oder nächster Command) oder ist es ein bewusstes Ende? \
Fehlen Schritte zwischen zwei Elementen?
2. Sonder- und Fehlerfälle: Was passiert bei Ablehnung, Abbruch, Timeout, fehlenden oder falschen Daten, Nachbesserung, \
Eskalation, Doppelanträgen? Welcher alternative Pfad fehlt?
3. Verantwortung: Hat jeder Command genau einen Akteur und eine Lane? Sind Übergaben zwischen Lanes sichtbar (das Event \
der einen Lane löst den Command der nächsten aus)? Gibt es Elemente ohne Lane oder mit unklarer Verantwortung?
4. Sprache: Events in der Vergangenheitsform, Commands im Imperativ, in der Fachsprache der Beteiligten. Werden gleiche \
Dinge gleich genannt (Ubiquitous Language)? Melde Synonyme, Homonyme und technische Begriffe ohne Grund.
5. Entscheidungen und Regeln: Wo wird entschieden, nach welcher Regel, mit welchen Schwellwerten oder Zahlen? Versteckte \
Logik als Policy („Wenn X, dann Y“) sichtbar machen.
6. Daten und Systeme: Welche Information braucht ein Akteur für seine Entscheidung (Read Model)? Welches System hält sie, \
welche Schnittstellen gibt es? Sind externe Systeme benannt?
7. Granularität und Schnitt: Gleiche Detailtiefe über den ganzen Prozess? Zu grobe Sammel-Events („Projekt bearbeitet“) \
oder zu feine Einzelschritte? Ist der Prozess zu groß und sollte in Teilprozesse bzw. Bounded Contexts geschnitten werden? \
Sind Vorgänger- und Nachfolgeprozess benannt?
8. Konsistenz mit dem Gespräch: Widerspricht das Modell Fakten oder Aussagen? Was ist nur Annahme ohne Beleg? Was wurde \
im Gespräch gesagt und fehlt im Modell?
9. Kennzahlen und Qualität: Durchlaufzeiten, Häufigkeit, Volumen, Wartezeiten, Engpässe, wenn genannt oder nötig."""

ANTWORT_SCHEMA = {
    "type": "object",
    "properties": {"beantwortet": {"type": "array", "items": {
        "type": "object",
        "properties": {"id": {"type": "string"}, "notiz": {"type": "string", "description": "The answer, in one short German sentence"}},
        "required": ["id", "notiz"]}}},
    "required": ["beantwortet"],
}
ANTWORT_SYSTEM = """You check which open questions of a live meeting assistant were answered by the latest part of \
the conversation. A question is answered when the transcript contains a concrete answer to it - also a short or partial \
one, from any speaker. Merely related talk is not an answer. For each answered question return its id and the answer \
in one short German sentence. When in doubt, leave the question open. Respond with the JSON as specified by the schema."""

BEFEHL_SYSTEM = f"""You edit an event storming process model exactly as the user instructs - nothing more. Element \
types: {', '.join(TYPEN)}. Existing elements are listed with their ids; use these ids. Give new elements a ref so \
later operations (nach, vor, von, id) can refer to them. Elements follow each other along the time line: put a new \
element with nach=<id> (after) or vor=<id> (before). lane = actor or bounded context; keep existing lane names \
unless told otherwise. Renaming a lane means one element_aendern per element in it. If the instruction is unclear or \
cannot be done, return no operations and say why in antwort. Write antwort in German."""

ELEMENT_OP = {
    "type": "object",
    "properties": {
        "op": {"type": "string", "enum": ["prozess_neu", "neu", "aendern", "loeschen", "verbinden"]},
        "prozess": {"type": "string", "description": "Process id, or the ref of a process created in this response"},
        "ref": {"type": "string", "description": "Your own short id (n1, n2 …) for new elements/processes"},
        "element": {"type": "string", "description": "aendern/loeschen: element id"},
        "typ": {"type": "string", "enum": TYPEN},
        "text": {"type": "string", "description": "neu/aendern: sticky text; prozess_neu: process name"},
        "lane": {"type": "string", "description": "Actor or bounded context row"},
        "nach": {"type": "string", "description": "neu: insert after this element id/ref (default: at the end)"},
        "von": {"type": "string"}, "zu": {"type": "string"},
        "annahme": {"type": "boolean", "description": "true if inferred rather than said explicitly"},
        "begruendung": {"type": "string"},
    },
    "required": ["op"],
}

ANALYSE_SCHEMA = {
    "type": "object",
    "properties": {
        "eintraege": {
            "type": "array", "maxItems": 8,
            "description": "NEW entries only. frage = a question the user could ask now; annahme = something you "
                           "assume but nobody confirmed; offen = an open point, contradiction or unclear term.",
            "items": {"type": "object", "properties": {
                "typ": {"type": "string", "enum": ["frage", "annahme", "offen"]},
                "text": {"type": "string", "description": "Short; a question ready to ask"},
                "begruendung": {"type": "string", "description": "One sentence: why this matters now"},
                "prio": {"type": "integer", "minimum": 1, "maximum": 3, "description": "1 = ask now"},
                "zeilen": {"type": "array", "items": {"type": "integer"}, "description": "Transcript line numbers"},
                "elemente": {"type": "array", "items": {"type": "string"},
                             "description": "Element ids/refs this is about (becomes a hotspot on the board)"},
            }, "required": ["typ", "text", "begruendung", "prio", "zeilen", "elemente"]},
        },
        "erledigt": {
            "type": "array", "description": "Existing open questions/points that the conversation has answered",
            "items": {"type": "object", "properties": {"id": {"type": "string"}, "notiz": {"type": "string",
                      "description": "The answer, briefly"}}, "required": ["id", "notiz"]},
        },
        "aenderungen": {"type": "array", "items": ELEMENT_OP,
                        "description": "Process changes; empty if the conversation is not about a process"},
        "recherche": {
            "type": "array", "maxItems": 3,
            "description": "Questions to check in the repositories (only if repositories are given)",
            "items": {"type": "object", "properties": {"frage": {"type": "string"}, "begruendung": {"type": "string"}},
                      "required": ["frage", "begruendung"]},
        },
        "anweisungen": {
            "type": "array", "description": "For every NEW instruction: is it a correction of facts (fakt) or a "
                                            "request how you should work (anweisung)?",
            "items": {"type": "object", "properties": {
                "id": {"type": "string"}, "art": {"type": "string", "enum": ["fakt", "anweisung"]},
                "fakt": {"type": "string", "description": "art=fakt: the fact as a statement"}},
                "required": ["id", "art"]},
        },
        "kontext": {"type": ["object", "null"], "description": "Only if no context is confirmed: category/project",
                    "properties": {"kategorie": {"type": "string"}, "projekt": {"type": ["string", "null"]}}},
    },
    "required": ["eintraege", "erledigt", "aenderungen", "recherche", "anweisungen"],
}

ZUSAMMENFASSUNG_SCHEMA = {
    "type": "object", "properties": {"zusammenfassung": {"type": "string"}}, "required": ["zusammenfassung"],
}

RECHERCHE_SCHEMA = {
    "type": "object",
    "properties": {
        "antwort": {"type": "string", "description": "Answer in 2-5 sentences, in German"},
        "fundstellen": {"type": "array", "items": {"type": "object", "properties": {
            "datei": {"type": "string"}, "zeile": {"type": ["integer", "null"]}, "auszug": {"type": "string"}},
            "required": ["datei", "zeile", "auszug"]}},
        "annahmen": {"type": "array", "items": {"type": "object", "properties": {
            "text": {"type": "string"}, "begruendung": {"type": "string"}}, "required": ["text", "begruendung"]}},
        "fragen": {"type": "array", "items": {"type": "object", "properties": {
            "text": {"type": "string"}, "begruendung": {"type": "string"}}, "required": ["text", "begruendung"]}},
    },
    "required": ["antwort", "fundstellen", "annahmen", "fragen"],
}


def system_prompt(cfg: dict) -> str:
    return f"""You assist {cfg['ich']}{role(cfg)} live during a meeting. You read the transcript as it is being \
spoken and help them understand the topic fully: you suggest the next good questions, make your assumptions \
explicit, notice open points and contradictions, and - when the conversation is about a process - you model it \
with event storming on process level.

Event storming (process level): actor (who) → command (what they trigger, imperative) → event (what happened, \
past tense, domain language) → policy ("whenever X, then Y") → next command; readmodel = information someone \
needs for a decision; system = external system; aggregate = the thing that decides (only if it comes up); hotspot \
= open question or problem. lane = the actor or bounded context the element belongs to. Order the elements along \
the time line. Prefer the words the participants use (ubiquitous language). Model one end-to-end process per \
business flow: different roles or actors are lanes of the same process, never separate processes. Start another \
process only for a flow that is unrelated to the existing ones; otherwise add to the existing process.

Rules:
- Facts from {cfg['ich']} are true; never contradict them.
- Locked elements (marked LOCKED) were made or confirmed by {cfg['ich']}: change them only with good reason, \
your change becomes a proposal.
- Do not suggest again what was dropped (DROPPED) or deleted.
- Only NEW entries: nothing that is already in the list with the same meaning.
- Few, good suggestions: at most 3 questions per run, the most useful first. Prefer questions that close gaps in \
the process, edge cases, error cases, responsibilities, data, numbers and triggers.
- Mark anything inferred as annahme (and new elements as annahme=true) instead of presenting it as fact.
- Connect process elements with "verbinden" (cause → effect, e.g. command → event → policy); link every question, assumption and open point to the elements it is about, so it shows up as a hotspot there.
- Mark an open question as erledigt as soon as the transcript contains an answer to it, even a short or partial one; \
put the answer into notiz. Check every open entry against the new lines.
- Write all texts in German (code identifiers unchanged).
Respond with the JSON as specified by the schema."""


def kontext_prompt(cfg: dict, s: dict) -> str:
    k, out = s["kontext"], []
    termin = (k.get("termine") or [None])[k["termin_index"]] if k.get("termine") and k.get("termin_index") is not None else None
    if termin:
        out.append(f"Calendar event: {termin_line(termin, 15)}")
    if k.get("kategorie"):
        cat = cfg["kategorien"].get(k["kategorie"], {})
        proj = cat.get("projekte", {}).get(k.get("projekt") or "", {})
        out.append(f"Category: {k['kategorie']} – {cat.get('hinweise', '')}")
        if k.get("projekt"):
            out.append(f"Project: {k['projekt']} – {proj.get('hinweise', '')}")
    else:
        cats = "; ".join(f"{key}: {c.get('hinweise', '')} (projects: {', '.join(c.get('projekte', {}))})"
                         for key, c in cfg["kategorien"].items())
        out.append(f"No context confirmed yet. Known categories: {cats}")
    if k.get("repos"):
        out.append("Repositories (a separate research job can look into them): " + ", ".join(Path(r).name for r in k["repos"]))
    if k.get("freitext", "").strip():
        out.append(f"Goal and participants (from {cfg['ich']}):\n{k['freitext'].strip()}")
    for rel in k.get("notizen", []):
        try:
            text = safe_folder(cfg, rel).read_text()[:MAX_NOTIZ_ZEICHEN]
            out.append(f"<note path=\"{rel}\">\n{text}\n</note>")
        except (OSError, ValueError):
            continue
    return "\n".join(out)


def process_prompt(s: dict) -> str:
    if not s["prozesse"]:
        return "Processes: none yet"
    out = []
    for p in s["prozesse"]:
        merged = "; ".join(p.get("aliase", []))
        out.append(f"Process {p['id']} \"{p['name']}\":" + (f" (the user merged into it: {merged})" if merged else ""))
        for e in p["elemente"]:
            flags = "".join([" LOCKED" if e["gesperrt"] else "", " assumption" if e["annahme"] else ""])
            out.append(f"  {e['id']} [{e['typ']}] {e['text']}" + (f" (lane: {e['lane']})" if e["lane"] else "") + flags)
        if p["verbindungen"]:
            out.append("  connections: " + ", ".join(f"{v['von']}→{v['nach']}" for v in p["verbindungen"]))
        if p["geloescht"]:
            out.append("  deleted by the user (do not add again): " + "; ".join(p["geloescht"]))
    return "\n".join(out)


def entries_prompt(s: dict) -> str:
    out = []
    for e in s["eintraege"]:
        if e["status"] == "verworfen":
            out.append(f"- DROPPED [{e['typ']}] {e['text']}")
        elif e["status"] in OFFEN:
            out.append(f"- {e['id']} [{e['typ']}, {e['status']}] {e['text']}")
        else:
            out.append(f"- [{e['typ']}, {e['status']}] {e['text']}" + (f" → {e['notiz']}" if e.get("notiz") else ""))
    for a in s["auftraege"]:
        if a["status"] == "fertig":
            out.append(f"- [code research] {a['frage']} → {a['ergebnis']}")
    return "\n".join(out) or "none yet"


def speaker_name(s: dict, labels: dict[str, str], spk: str) -> str:
    label = labels.get(spk, "?")
    return s["sprecher_namen"].get(label) or label


def format_lines(s: dict, labels: dict[str, str], lines: list[dict]) -> str:
    start = dt.datetime.fromisoformat(s["start"])
    return "\n".join(f"#{z['nr']} [{(start + dt.timedelta(milliseconds=z['start_ms'])):%H:%M:%S}] "
                     f"{speaker_name(s, labels, z['sprecher'])}: {z['text']}" for z in lines)


def window(s: dict, lines: list[dict], fenster_min: float = FENSTER_MIN) -> tuple[list[dict], list[dict]]:
    """(lines to summarise first, lines verbatim in the prompt). Confirmed lines only."""
    confirmed = [z for z in lines if z["bestaetigt"]]
    if not confirmed:
        return [], []
    cut = confirmed[-1]["start_ms"] - fenster_min * 60_000
    bis = s["zusammenfassung"]["bis_ms"]
    pending = [z for z in confirmed if bis <= z["start_ms"] < cut]
    if pending and cut - bis >= ZUSAMMENFASSEN_AB_MIN * 60_000:
        return pending, [z for z in confirmed if z["start_ms"] >= cut]
    return [], [z for z in confirmed if z["start_ms"] >= bis]


def analysis_prompt(cfg: dict, s: dict, labels: dict[str, str], lines: list[dict]) -> str:
    neu_ab = s["analysiert_bis"]
    alt = [z for z in lines if z["nr"] <= neu_ab]
    neu = [z for z in lines if z["nr"] > neu_ab]
    namen = {lab: s["sprecher_namen"].get(lab) for lab in labels.values() if lab != "?"}
    sprecher = ", ".join(f"{lab} = {n}" if n else lab for lab, n in namen.items())
    anweisungen = "\n".join(f"- {a['id']}{' (NEW)' if a.get('neu') else ''}: {a['text']}" for a in s["anweisungen"])
    fakten = "\n".join(f"- {f['text']}" for f in s["fakten"])
    abgelehnt = "\n".join(f"- {x}" for x in s["abgelehnt"][-20:])
    return f"""<context>
{kontext_prompt(cfg, s)}
Speakers: {sprecher or 'unknown'}
</context>

<facts>
{fakten or 'none'}
</facts>

<instructions>
{anweisungen or 'none'}
</instructions>

<entries>
{entries_prompt(s)}
</entries>

<processes>
{process_prompt(s)}
Rejected proposals (do not repeat): {abgelehnt or 'none'}
</processes>

<summary_of_earlier_conversation>
{s['zusammenfassung']['text'] or 'none'}
</summary_of_earlier_conversation>

<transcript_already_analysed>
{format_lines(s, labels, alt) or '–'}
</transcript_already_analysed>

<transcript_new>
{format_lines(s, labels, neu) or '–'}
</transcript_new>"""


def regenerate_prompt(cfg: dict, s: dict, labels: dict[str, str], lines: list[dict], targets: list[dict],
                      neu_aufbauen: bool, hinweis: str) -> str:
    """Prompt for building a process again with everything known by now. When rebuilding, the processes show only
    their locked elements - the rest is gone and is made new."""
    view = copy.deepcopy(s)
    view["analysiert_bis"] = -1  # the whole transcript counts as input
    ids = {p["id"] for p in targets}
    if neu_aufbauen:
        for p in view["prozesse"]:
            if p["id"] in ids:
                p["elemente"] = [e for e in p["elemente"] if e["gesperrt"]]
                keep = {e["id"] for e in p["elemente"]}
                p["verbindungen"] = [v for v in p["verbindungen"] if v["von"] in keep and v["nach"] in keep]
    names = ", ".join(f"\"{p['name']}\" ({p['id']})" for p in targets)
    if neu_aufbauen:
        task = (f"Build the process(es) {names} again. Everything that was not locked has been removed from them; the "
                "locked elements remain (see <processes>). Create the whole rest of the process: every missing element "
                "(op \"neu\", in time order, with lane, give each a ref and connect the elements with \"verbinden\"), "
                "consistent with the locked elements.")
    else:
        task = (f"Revise the process(es) {names}. Check each element and the order against everything known now. "
                "Correct wording that is wrong (\"aendern\"), remove what no longer holds (\"loeschen\"), add what is "
                "missing (\"neu\", in time order), and connect the elements with \"verbinden\". Leave what is still "
                "right unchanged.")
    return analysis_prompt(cfg, view, labels, lines) + f"""

<task>
Regenerate the process model now, based on everything known: the facts, the answered and rated entries, the \
instructions and the whole conversation - the questions or the input may have changed since the process was drawn. \
{task} Do not touch other processes. Locked elements: only propose a change (aendern/loeschen) if the facts \
require it. Put the changes into "aenderungen"; add new entries only for real gaps you see now (at most 3).
{('Extra guidance for this run: ' + hinweis.strip()) if hinweis.strip() else ''}
</task>"""


def review_system(kriterien: str) -> str:
    return f"""You are an experienced process modeller and event storming facilitator. You review the process model \
that is being drawn live in a meeting, so that the team notices gaps and mistakes while the people who know the \
answer are still in the room. Judge only what is in the model and the conversation; never invent facts. Every \
finding names the concrete elements (their ids only in the elemente field; in the text refer to elements by their \
wording, never by id) and has a concrete recommendation. Where the fix is unambiguous, give \
it as operations (new elements get a ref; nach/vor/von/id may use refs); where only the people in the meeting can \
settle it, give the question to ask. Sort by severity, at most 12 findings. Do not repeat what the open entries \
already ask, what was dismissed earlier or what is merely a matter of taste. If the model is sound, say so in \
zusammenfassung and report few or no findings. Write in German.

Review criteria (set by the user):
{kriterien.strip()}"""


def review_prompt(s: dict, labels: dict[str, str], lines: list[dict], targets: list[dict]) -> str:
    fakten = "\n".join(f"- {f['text']}" for f in s["fakten"])
    verworfen = "\n".join(f"- {x}" for x in s.get("review_verworfen", [])[-30:])
    names = ", ".join(f"\"{p['name']}\" ({p['id']})" for p in targets)
    return f"""<facts>
{fakten or 'none'}
</facts>

<processes>
{process_prompt(s)}
</processes>

<entries>
{entries_prompt(s)}
</entries>

<dismissed_findings>
{verworfen or 'none'}
</dismissed_findings>

<summary_of_earlier_conversation>
{s['zusammenfassung']['text'] or 'none'}
</summary_of_earlier_conversation>

<recent_transcript>
{format_lines(s, labels, lines) or '–'}
</recent_transcript>

<task>
Review the process(es) {names}.
</task>"""


def command_prompt(s: dict, labels: dict[str, str], lines: list[dict], text: str, prozess: str | None) -> str:
    focus = (f"The user is looking at the process {prozess}." if prozess
             else "The user is looking at the big picture of all processes.")
    fakten = "\n".join(f"- {f['text']}" for f in s["fakten"])
    return f"""<facts>
{fakten or 'none'}
</facts>

<processes>
{process_prompt(s)}
</processes>

<summary_of_earlier_conversation>
{s['zusammenfassung']['text'] or 'none'}
</summary_of_earlier_conversation>

<recent_transcript>
{format_lines(s, labels, lines) or '–'}
</recent_transcript>

<instruction>
{focus}
{text}
</instruction>"""


def summary_prompt(s: dict, labels: dict[str, str], lines: list[dict]) -> str:
    return (f"Previous summary:\n{s['zusammenfassung']['text'] or '–'}\n\nContinue it with this part of the meeting. "
            "Keep names, numbers, decisions, process steps and open questions; max. 400 words, German, bullet points.\n\n"
            f"<transcript>\n{format_lines(s, labels, lines)}\n</transcript>")


def research_prompt(s: dict, auftrag: dict, labels: dict[str, str], lines: list[dict]) -> tuple[str, str]:
    system = """You research in the given code repositories (read-only) to support a live meeting. Answer the \
question concretely from the code: where is it implemented, which cases are covered, what is missing. Give file \
paths relative to the repository and line numbers. Distinguish what the code shows from what you assume. Be fast: \
search specifically (Grep/Glob) instead of reading everything. Write in German. Respond with the JSON as \
specified by the schema."""
    recent = format_lines(s, labels, [z for z in lines if z["bestaetigt"]][-40:])
    prompt = (f"Question: {auftrag['frage']}\n" + (f"Why: {auftrag['begruendung']}\n" if auftrag.get("begruendung") else "")
              + f"\nRepositories: {', '.join(s['kontext']['repos'])}\n\nConversation so far (summary):\n"
              f"{s['zusammenfassung']['text'] or '–'}\n\nRecent transcript:\n{recent}\n\n{process_prompt(s)}")
    return system, prompt


# --- Replay -----------------------------------------------------------------------------------------------------

def visible(s: dict, lines: list[dict], now: float | None = None) -> list[dict]:
    """The lines of the session: a session that was started or ended in the middle of a recording only has its
    part, and in a replay only the lines that would have been spoken by now."""
    if s.get("ab_ms") or s.get("bis_ms"):
        lines = number_lines([z for z in lines if (s.get("ab_ms") or 0) <= z["start_ms"] < (s.get("bis_ms") or float("inf"))])
    r = s.get("replay")
    if not r:
        return lines
    limit = ((now or time.time()) - r["start"]) * 1000 * r["tempo"]
    return [z for z in lines if z["start_ms"] <= limit]


# --- What is listened to -----------------------------------------------------------------------------------------
# The transcript stays complete; lines in a muted range or from a muted speaker just never reach the model.

MARGE_MS = 3000  # clock difference between MacWhisper's audio and the wall clock: muting starts and ends this much early
INF = float("inf")


def _spans(ranges: list[dict]) -> list[tuple[float, float]]:
    return [(r["von_ms"], INF if r.get("bis_ms") is None else r["bis_ms"]) for r in ranges]


def _ranges(spans: list[tuple[float, float]]) -> list[dict]:
    out: list[list[float]] = []
    for a, b in sorted(x for x in spans if x[0] < x[1]):
        if out and a <= out[-1][1]:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return [{"von_ms": int(a), "bis_ms": None if b == INF else int(b)} for a, b in out]


def exclude_range(s: dict, von_ms: int, bis_ms: int | None) -> None:
    s["ausgenommen"] = _ranges([*_spans(s.get("ausgenommen", [])), (von_ms, INF if bis_ms is None else bis_ms)])


def include_range(s: dict, von_ms: int, bis_ms: int) -> None:
    out = []
    for a, b in _spans(s.get("ausgenommen", [])):
        if b <= von_ms or a >= bis_ms:
            out.append((a, b))
            continue
        out += [(a, von_ms), (bis_ms, b)]
    s["ausgenommen"] = _ranges(out)


def is_muted(s: dict) -> bool:
    return any(r.get("bis_ms") is None for r in s.get("ausgenommen", []))


def set_listening(s: dict, listen: bool, now_ms: int) -> None:
    """Switch listening off from now on (an open range) or on again (the range ends now)."""
    now_ms = max(0, now_ms - MARGE_MS)
    if listen:
        s["ausgenommen"] = _ranges([(a, now_ms if b == INF else b) for a, b in _spans(s.get("ausgenommen", []))])
    elif not is_muted(s):
        exclude_range(s, now_ms, None)


def set_speaker_muted(s: dict, label: str, muted: bool) -> None:
    s["stumm"] = sorted(({*s.get("stumm", []), label} if muted else set(s.get("stumm", [])) - {label}))


def listened(s: dict, lines: list[dict], labels: dict[str, str]) -> list[dict]:
    """The lines that go into the analysis."""
    spans, stumm = _spans(s.get("ausgenommen", [])), set(s.get("stumm", []))
    if not spans and not stumm:
        return lines
    return [z for z in lines if labels.get(z["sprecher"], "?") not in stumm
            and not any(a <= z["start_ms"] < b for a, b in spans)]


# --- Context suggestions ---------------------------------------------------------------------------------------

def suggest_context(cfg: dict, termin: dict | None) -> dict | None:
    """Category/project from the calendar event: project name in the title, known people among the invitees."""
    if not termin:
        return None
    title = norm(termin.get("titel"))
    people = {norm(p.get("name")) for p in termin.get("teilnehmer", []) if p.get("name")}
    best, score = None, 0
    for key, cat in cfg["kategorien"].items():
        for name, proj in cat.get("projekte", {}).items():
            sc = 3 * (norm(name) in title) + 2 * len(people & {norm(x) for x in proj.get("personen", [])})
            sc += sum(1 for w in norm(proj.get("hinweise")).split() if len(w) > 4 and w in title.split())
            if sc > score:
                best, score = {"kategorie": key, "projekt": name}, sc
        if not best and norm(key) in title:
            best, score = {"kategorie": key, "projekt": None}, 1
    return best


def project_repos(cfg: dict, kategorie: str | None, projekt: str | None) -> list[str]:
    proj = cfg["kategorien"].get(kategorie or "", {}).get("projekte", {}).get(projekt or "", {})
    return [str(r) for r in proj.get("repos") or []]


def known_repos(cfg: dict) -> list[str]:
    return sorted({str(r) for c in cfg["kategorien"].values() for p in c.get("projekte", {}).values()
                   for r in p.get("repos") or []})


def remember_repos(cfg: dict, kategorie: str | None, projekt: str | None, repos: list[str]) -> None:
    """Repositories of a project in categories.yaml, preselected in its next meetings."""
    proj = cfg["kategorien"].get(kategorie or "", {}).get("projekte", {}).get(projekt or "")
    if proj is None:
        raise ValueError("Choose a project first")
    proj["repos"] = list(dict.fromkeys(repos))
    save_kategorien(cfg, cfg["kategorien"])


def project_notes(cfg: dict, kategorie: str | None, projekt: str | None, limit: int = 40) -> list[dict]:
    """Notes from the project folder (newest first) to attach as context."""
    cat = cfg["kategorien"].get(kategorie or "")
    if not cat:
        return []
    folder = safe_folder(cfg, f"{cat['ordner']}/{projekt}" if projekt else cat["ordner"])
    if not folder.is_dir():
        return []
    files = sorted(folder.rglob("*.md"), key=lambda f: f.stat().st_mtime, reverse=True)[:limit]
    vault = cfg["vault"].resolve()
    return [{"pfad": str(f.relative_to(vault)), "name": f.stem,
             "geaendert": dt.datetime.fromtimestamp(f.stat().st_mtime).isoformat(timespec="minutes")} for f in files]


def add_entry(s: dict, typ: str, text: str) -> dict:
    """Your own question or open point, managed like the assistant's. The same text is not added twice: an
    entry that is already there (and not dropped) is pinned instead."""
    if typ not in ("frage", "offen") or not text.strip():
        raise ValueError("Empty text")
    same = next((e for e in s["eintraege"] if e["typ"] == typ and norm(e["text"]) == norm(text) and e["status"] != "verworfen"), None)
    if same:
        if same["status"] == "neu":
            same["status"] = "angepinnt"
        return same
    s["eintraege"].append({"id": new_id(), "typ": typ, "text": text.strip(), "begruendung": "", "prio": 1, "zeilen": [],
                           "elemente": [], "status": "angepinnt", "notiz": "", "lauf": None, "erstellt": now_iso(),
                           "quelle": "ich"})
    return s["eintraege"][-1]


def check_repo(path: str) -> str:
    p = Path(path).expanduser().resolve()
    if not p.is_dir():
        raise ValueError(f"Folder not found: {path}")
    return str(p)


# --- Process library in the vault --------------------------------------------------------------------------------

MERMAID_FORM = {"actor": ('(["', '"])'), "command": ('["', '"]'), "event": ('["', '"]'), "policy": ('{{"', '"}}'),
                "readmodel": ('[/"', '"/]'), "system": ('[["', '"]]'), "aggregate": ('[("', '")]'), "hotspot": ('{"', '"}')}
MERMAID_FARBE = {"actor": "#fff3a0", "command": "#a8d8ff", "event": "#ffb366", "policy": "#d7b8f3",
                 "readmodel": "#b8e6b8", "system": "#ffc0d9", "aggregate": "#fff176", "hotspot": "#ff6b6b"}


def implicit_links(p: dict) -> list[tuple[str, str]]:
    """The time line as dotted links: neighbours that are not connected explicitly (same rule as on the board)."""
    out = {v["von"] for v in p["verbindungen"]}
    inc = {v["nach"] for v in p["verbindungen"]}
    return [(a["id"], b["id"]) for a, b in zip(p["elemente"], p["elemente"][1:]) if a["id"] not in out and b["id"] not in inc]


def mermaid(p: dict, s: dict | None = None) -> str:
    hot = {x for e in (s or {}).get("eintraege", []) if e["status"] in OFFEN | {"unklar"} for x in e.get("elemente", [])}
    lines = ["flowchart LR"]
    lanes: dict[str, list[dict]] = {}
    for e in p["elemente"]:
        lanes.setdefault(e["lane"], []).append(e)
    nid = {e["id"]: f"n{i}" for i, e in enumerate(p["elemente"])}
    for lane, els in lanes.items():
        if lane:
            lines.append(f'  subgraph L{len(lines)}["{lane.replace(chr(34), "")}"]')
        for e in els:
            a, b = MERMAID_FORM.get(e["typ"], ('["', '"]'))
            text = e["text"].replace('"', "'") + (" ?" if e["annahme"] else "")
            lines.append(f"  {'  ' if lane else ''}{nid[e['id']]}{a}{text}{b}:::{e['typ']}")
        if lane:
            lines.append("  end")
    lines += [f"  {nid[v['von']]} --> {nid[v['nach']]}" for v in p["verbindungen"] if v["von"] in nid and v["nach"] in nid]
    lines += [f"  {nid[a]} -.-> {nid[b]}" for a, b in implicit_links(p)]
    lines += [f"  classDef {t} fill:{c},stroke:#555,color:#111" for t, c in MERMAID_FARBE.items()]
    lines += [f"  style {nid[x]} stroke:#d00,stroke-width:3px" for x in hot if x in nid]
    lines += [f"  style {nid[e['id']]} stroke-dasharray: 5 5" for e in p["elemente"] if e["annahme"] and e["id"] not in hot]
    return "\n".join(lines)


def library_folder(cfg: dict, kategorie: str | None, projekt: str | None) -> Path:
    cat = cfg["kategorien"].get(kategorie or "")
    if not cat:
        return safe_folder(cfg, "Prozesse")
    return safe_folder(cfg, f"{cat['ordner']}/{projekt}/Prozesse" if projekt else f"{cat['ordner']}/Prozesse")


PROZESS_JSON_RE = re.compile(r"```json debrief-prozess\n(.*?)\n```", re.S)
HISTORIE_RE = re.compile(r"## Änderungen\n\n(.*?)(?=\n## |\Z)", re.S)


def list_library(cfg: dict, kategorie: str | None, projekt: str | None) -> list[dict]:
    folders = [library_folder(cfg, kategorie, projekt)]
    if kategorie and projekt:
        folders.append(library_folder(cfg, kategorie, None))
    out = []
    for folder in folders:
        for f in sorted(folder.glob("*.md"), key=lambda x: x.stat().st_mtime, reverse=True) if folder.is_dir() else []:
            if PROZESS_JSON_RE.search(f.read_text()):
                out.append({"pfad": str(f.relative_to(cfg["vault"].resolve())), "name": f.stem,
                            "geaendert": dt.datetime.fromtimestamp(f.stat().st_mtime).isoformat(timespec="minutes")})
    return out


def load_library_process(cfg: dict, s: dict, rel: str) -> dict:
    """Load a process from the vault; everything in it counts as confirmed (locked), the assistant only adds."""
    path = safe_folder(cfg, rel)
    m = PROZESS_JSON_RE.search(path.read_text())
    if not m:
        raise ValueError("No process model in this note")
    data = json.loads(m[1])
    p = new_process(s, data.get("name") or path.stem, "bibliothek")
    idmap = {}
    for e in data.get("elemente", []):
        el = new_element(e.get("typ", "event"), e.get("text", ""), e.get("lane", ""), "bibliothek", bool(e.get("annahme")))
        idmap[e.get("id")] = el["id"]
        p["elemente"].append(el)
    p["verbindungen"] = [{"von": idmap[v["von"]], "nach": idmap[v["nach"]]} for v in data.get("verbindungen", [])
                         if v.get("von") in idmap and v.get("nach") in idmap]
    p.update(quelle=rel, original=[{"id": e["id"], "typ": e["typ"], "text": e["text"]} for e in p["elemente"]])
    return p


def save_library_process(cfg: dict, s: dict, prozess_id: str, name: str | None = None, als_neu: bool = False) -> str:
    """Write the process as a note: Mermaid for reading, JSON as the source, history of changes."""
    p = find(s["prozesse"], prozess_id)
    k = s["kontext"]
    name = re.sub(r'[\\/:*?"<>|#^\[\]]+', " ", (name or p["name"]).strip()).strip() or "Prozess"
    if p.get("quelle") and not als_neu:
        path = safe_folder(cfg, p["quelle"])
    else:
        folder = library_folder(cfg, k.get("kategorie"), k.get("projekt"))
        path, n = folder / f"{name}.md", 2
        while path.exists():
            path, n = folder / f"{name} ({n}).md", n + 1
    old = path.read_text() if path.exists() else ""
    historie = (HISTORIE_RE.search(old)[1].strip() + "\n") if HISTORIE_RE.search(old) else ""
    orig = {e["id"]: e for e in p.get("original") or []}
    now_ids = {e["id"] for e in p["elemente"]}
    added = len(now_ids - set(orig))
    removed = len(set(orig) - now_ids)
    changed = sum(1 for e in p["elemente"] if e["id"] in orig and (orig[e["id"]]["text"], orig[e["id"]]["typ"]) != (e["text"], e["typ"]))
    datum = dt.date.today().isoformat()
    start = dt.datetime.fromisoformat(s["start"])
    historie += (f"- {datum} – Meeting „{s['titel']}“ ({start:%d.%m.%Y %H:%M}): "
                 + (f"+{added} / −{removed} / ~{changed} Elemente" if orig else f"angelegt mit {added} Elementen") + "\n")
    hot = [e for e in s["eintraege"] if e["status"] in OFFEN and set(e.get("elemente", [])) & now_ids]
    model = {"name": name, "elemente": [{k2: e[k2] for k2 in ("id", "typ", "text", "lane", "annahme")} for e in p["elemente"]],
             "verbindungen": p["verbindungen"]}
    fm = {"tags": ["prozess"], "kunde": k.get("kategorie"), "projekt": k.get("projekt"), "aktualisiert": datum}
    parts = [f"---\n{yaml.safe_dump({x: y for x, y in fm.items() if y}, sort_keys=False, allow_unicode=True)}---",
             f"# {name}", f"```mermaid\n{mermaid(p, s)}\n```"]
    if hot:
        parts.append("## Offene Fragen\n\n" + "\n".join(f"- [ ] {e['text']}" for e in hot))
    parts.append("## Änderungen\n\n" + historie.strip())
    parts.append("## Modell\n\n```json debrief-prozess\n" + json.dumps(model, ensure_ascii=False, indent=1) + "\n```")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n\n".join(parts) + "\n")
    rel = str(path.relative_to(cfg["vault"].resolve()))
    p.update(name=name, quelle=rel, original=[{"id": e["id"], "typ": e["typ"], "text": e["text"]} for e in p["elemente"]],
             gespeichert=now_iso())
    return rel


# --- Handover to the debrief note --------------------------------------------------------------------------------

STATUS_TEXT = {"neu": "offen", "angepinnt": "offen", "gestellt": "gestellt, ohne Antwort", "beantwortet": "beantwortet",
               "stimmt": "bestätigt", "stimmt_nicht": "falsch", "erledigt": "erledigt"}


def note_section(s: dict) -> str:
    """Section "Live-Assistent" for the filed note: questions, assumptions, open points, processes."""
    groups = [("Offene Fragen", lambda e: e["typ"] == "frage" and e["status"] in OFFEN),
              ("Beantwortete Fragen", lambda e: e["typ"] == "frage" and e["status"] == "beantwortet"),
              ("Annahmen", lambda e: e["typ"] == "annahme" and e["status"] in ("stimmt", "stimmt_nicht", "neu")),
              ("Offene Punkte", lambda e: e["typ"] == "offen" and e["status"] in OFFEN),
              ("Aus dem Code", lambda e: e["typ"] == "code" and e["status"] != "verworfen")]
    out = []
    for title, match in groups:
        items = [e for e in s["eintraege"] if match(e)]
        if not items:
            continue
        rows = []
        for e in items:
            box = "- [ ] " if title in ("Offene Fragen", "Offene Punkte") else "- "
            extra = f" → {e['notiz']}" if e.get("notiz") else (f" — {e['begruendung']}" if e["typ"] == "code" else "")
            label = f" *({STATUS_TEXT.get(e['status'], e['status'])})*" if e["typ"] == "annahme" else ""
            rows.append(f"{box}{e['text']}{label}{extra}")
        out.append(f"### {title}\n\n" + "\n".join(rows))
    for p in s["prozesse"]:
        if not p["elemente"]:
            continue
        link = f" – [[{p['quelle'].removesuffix('.md')}|in der Bibliothek]]" if p.get("quelle") else ""
        out.append(f"### Prozess: {p['name']}{link}\n\n```mermaid\n{mermaid(p, s)}\n```")
    return ("## Live-Assistent\n\n" + "\n\n".join(out)) if out else ""


# --- Manager -----------------------------------------------------------------------------------------------------

def number_lines(zeilen) -> list[dict]:
    """Lines in order with a consecutive `nr`, as copies. A later recording follows the earlier one; within a
    recording MacWhisper's orderIndex counts, which it only fills once the recording is finished - until then the
    start time gives the order. New lines are appended, so the numbers stay stable."""
    zeilen = sorted(zeilen, key=lambda z: (z.get("rec", 0), z["nr"] is None, z["nr"] or 0, z["start_ms"], z["id"]))
    return [{**z, "nr": i} for i, z in enumerate(zeilen)]


def names_for_recording(s: dict, recorded: dt.datetime | str | None) -> dict[str, str]:
    """The speaker names of the session as they apply to one of its recordings, with that recording's own labels
    ("Speaker 1 (2)" of the second recording is "Speaker 1" in its transcript)."""
    try:
        when = recorded if isinstance(recorded, dt.datetime) else dt.datetime.fromisoformat(str(recorded))
    except ValueError:
        return dict(s["sprecher_namen"])
    when = when if when.tzinfo else when.astimezone()
    recs = recordings(s)
    k = min(range(len(recs)), key=lambda i: abs((dt.datetime.fromisoformat(recs[i]["start"]) - when).total_seconds()))
    out = {}
    for label, name in s["sprecher_namen"].items():
        m = re.fullmatch(r"(Speaker \d+) \((\d+)\)", label)
        if m:
            if int(m[2]) == k + 1:
                out[m[1]] = name
        elif k == 0 or not re.fullmatch(r"Speaker \d+", label):
            out[label] = name
    return out


class LiveManager:
    """Live sessions on disk, watching MacWhisper, scheduling the analysis and research jobs."""

    def __init__(self, cfg: dict, notify=None, db: Path | None = None, directory: Path | None = None):
        self.cfg = cfg
        self.db = db or mw.DB
        self.dir = directory or LIVE_DIR
        self.dir.mkdir(exist_ok=True)
        self.notify = notify or (lambda *a, **kw: None)
        self.lock = threading.RLock()
        self._miro_lock = threading.Lock()
        self._miro_auth: miro.OAuth | None = None
        self.miro_token_file = HERE / "miro_token.json"
        self.changed = threading.Condition()
        self.version = 0
        self.analysis_jobs: queue.Queue[str] = queue.Queue()
        self.research_jobs: queue.Queue[tuple[str, str]] = queue.Queue()
        self.wake = threading.Event()
        self._lines: dict[str, dict] = {}  # sid -> {"stamp", "stand", "zeilen": {id: line}}
        self._notified: set[str] = set()
        self._checked: dict[str, float] = {}
        self._imports: dict[str, tuple[int, dict]] = {}
        self._busy: set[str] = set()

    # Storage

    def _file(self, sid: str) -> Path:
        if not re.fullmatch(r"[0-9A-F]{32}", sid):
            raise KeyError(sid)
        return self.dir / f"{sid}.json"

    def get(self, sid: str) -> dict:
        path = self._file(sid)
        if not path.exists():
            raise KeyError(sid)
        return json.loads(path.read_text())

    def put(self, s: dict) -> None:
        with self.lock:
            path = self._file(s["id"])
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(s, ensure_ascii=False, indent=1))
            tmp.replace(path)
        self.notify_change()

    def update(self, sid: str, fn) -> dict:
        """Read, change and save under the lock - the jobs and the page write the same file."""
        with self.lock:
            s = self.get(sid)
            fn(s)
            self.put(s)
            return s

    def notify_change(self) -> None:
        with self.changed:
            self.version += 1
            self.changed.notify_all()

    def wait_change(self, version: int, timeout: float) -> int:
        with self.changed:
            self.changed.wait_for(lambda: self.version != version, timeout=timeout)
            return self.version

    def settings(self) -> dict:
        try:
            data = json.loads(EINSTELLUNGEN.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            data = {}
        default = self.cfg.get("model", "sonnet")
        return {"weg": data.get("weg") or ("gateway" if self.cfg.get("gateway") else "direkt"),
                "analyse": data.get("analyse") or default, "recherche": data.get("recherche") or default}

    def save_settings(self, modell: dict) -> None:
        EINSTELLUNGEN.parent.mkdir(exist_ok=True)
        try:
            data = json.loads(EINSTELLUNGEN.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            data = {}
        EINSTELLUNGEN.write_text(json.dumps({**data, **modell}, indent=1))

    # MacWhisper

    def connect(self):
        return mw.connect(self.db)

    def meetings(self, limit: int = 15) -> list[dict]:
        con = self.connect()
        try:
            out = mw.meetings(con, limit)
        finally:
            con.close()
        for f in self.dir.glob("*.transcript"):
            if (m := self._imported_meeting(f.stem)):
                out.append(m)
        for s in self.sessions():  # sessions started in the middle of a recording
            if s.get("ab_ms"):
                start = dt.datetime.fromisoformat(s["start"]) + dt.timedelta(milliseconds=s["ab_ms"])
                rec = next((m for m in out if m["id"] == s["quelle_id"]), None)
                out.append({"id": s["id"], "start": start.isoformat(timespec="seconds"), "titel": s["titel"], "app": s.get("app"),
                            "laeuft": bool(rec and rec["laeuft"] and not s.get("beendet")), "zeilen": len(self.visible_lines(s)[0]),
                            "dauer_s": 0})
        out = sorted(out, key=lambda m: m["start"], reverse=True)[:limit]
        sessions = None
        for m in out:
            m["sitzung"] = self._file(m["id"]).exists()
            if not m["sitzung"]:
                sessions = self.sessions() if sessions is None else sessions
                m["fortsetzung_von"] = self.continuation_of(m, sessions)
        return out

    def open_session(self, sid: str) -> dict:
        """Existing session or a new one (context suggestions from the calendar come in the background)."""
        with self.lock:
            try:
                return self.get(sid)
            except KeyError:
                pass
            con = self.connect()
            try:
                m = mw.meeting(con, sid)
            finally:
                con.close()
            m = m or self._imported_meeting(sid)
            if not m:
                raise KeyError(sid)
            s = new_session(m, self.settings())
            s["laeuft"] = m["laeuft"]
            self.put(s)
        threading.Thread(target=self._calendar, args=(sid,), daemon=True).start()
        return s

    def _calendar(self, sid: str) -> None:
        s = self.get(sid)
        start = dt.datetime.fromisoformat(s["start"]).astimezone().replace(tzinfo=None)
        try:
            termine = calendar_candidates(self.cfg, {"Recorded At": start, "Duration": "1:00:00"})
        except Exception:
            log.exception("Live: calendar failed")
            termine = []
        reload_kategorien(self.cfg)

        def fill(s2):
            k = s2["kontext"]
            k["termine"] = termine
            if termine and k["termin_index"] is None:
                k["termin_index"] = 0
            if not k["bestaetigt"] and not k["kategorie"]:
                sugg = suggest_context(self.cfg, termine[0] if termine else None)
                if sugg:
                    k.update(kategorie=sugg["kategorie"], projekt=sugg["projekt"],
                             repos=project_repos(self.cfg, sugg["kategorie"], sugg["projekt"]), vorschlag=sugg)
            names = [p["name"] for t in termine[:1] for p in t.get("teilnehmer", []) if p.get("name")]
            if names:
                s2["teilnehmer"] = names
        self.update(sid, fill)

    def lines(self, sid: str, recs: list[dict] | None = None) -> tuple[list[dict], dict[str, str]]:
        """All lines of the session over all its recordings (incrementally from the database, only if MacWhisper
        wrote something). Lines of later recordings are moved onto the session's time line; their speakers get
        their own ids/labels ("Speaker 1 (2)"), because MacWhisper numbers speakers per recording."""
        recs = recs or [{"id": sid, "offset_ms": 0}]
        stamp = mw.wal_stamp(self.db)
        out: list[dict] = []
        labels: dict[str, str] = {}
        with self.lock:
            con = None
            try:
                for k, rec in enumerate(recs):
                    if (imp := self._imported(rec["id"])) is not None:
                        rows, rlabels = imp["zeilen"], {"": "?", **imp["sprecher"]}
                    else:
                        cache = self._lines.setdefault(rec["id"], {"stamp": None, "stand": None, "zeilen": {}, "labels": {}})
                        if cache["stamp"] != stamp:
                            con = con or self.connect()
                            for z in mw.lines(con, rec["id"], cache["stand"]):
                                cache["zeilen"][z["id"]] = z
                                cache["stand"] = max(cache["stand"] or "", z["stand"] or "")
                            current = mw.line_ids(con, rec["id"])
                            for gone in cache["zeilen"].keys() - current:
                                del cache["zeilen"][gone]
                            cache["labels"] = mw.speakers(con, rec["id"])
                            cache["stamp"] = stamp
                        rows, rlabels = cache["zeilen"].values(), cache["labels"]
                    off = rec.get("offset_ms", 0)
                    for z in rows:
                        out.append({**z, "start_ms": z["start_ms"] + off, "ende_ms": z["ende_ms"] + off, "rec": k,
                                    "sprecher": f"{z['sprecher']}@{k + 1}" if k and z["sprecher"] else z["sprecher"]})
                    for spk, lab in rlabels.items():
                        if k and spk:
                            spk = f"{spk}@{k + 1}"
                            lab = f"{lab} ({k + 1})" if re.fullmatch(r"Speaker \d+", lab) else lab
                        labels[spk] = lab
            finally:
                if con:
                    con.close()
        return number_lines(out), labels

    def visible_lines(self, s: dict) -> tuple[list[dict], dict[str, str]]:
        zeilen, labels = self.lines(s["id"], recordings(s))
        return visible(s, zeilen), labels

    def recording_ms(self, s: dict) -> int:
        """Where "now" is in the recording's own time (ms), for muting from now on."""
        zeilen, _ = self.visible_lines(s)
        last = max((z["ende_ms"] for z in zeilen), default=0)
        if s.get("replay"):
            return max(last, int((time.time() - s["replay"]["start"]) * 1000 * s["replay"]["tempo"]))
        if self.is_running(s):
            start = dt.datetime.fromisoformat(s["start"])
            return max(last, int((dt.datetime.now(start.tzinfo) - start).total_seconds() * 1000))
        return last

    def is_running(self, s: dict) -> bool:
        if s.get("replay"):
            zeilen, _ = self.lines(s["id"], recordings(s))
            return len(visible(s, zeilen)) < len(zeilen)
        con = self.connect()
        try:
            return any((m := mw.meeting(con, r["id"])) and m["laeuft"] for r in recordings(s))
        finally:
            con.close()

    def attach(self, sid: str, other: str) -> dict:
        """Continue the session with another recording (MacWhisper, after a break) or an imported transcript: its
        lines follow the session's own on one time line, questions, assumptions and the process stay."""
        s = self.get(sid)
        if other in {r["id"] for r in recordings(s)}:
            raise ValueError("Recording is already attached")
        imported = self._imported_meeting(other)
        con = self.connect()
        try:
            m = imported or mw.meeting(con, other)
        finally:
            con.close()
        if not m:
            raise KeyError(other)
        offset = int((dt.datetime.fromisoformat(m["start"]) - dt.datetime.fromisoformat(s["start"])).total_seconds() * 1000)
        if imported:  # a file: at its own time if that is after the session, otherwise right behind the last line
            zeilen, _ = self.visible_lines(s)
            offset = max(offset, max((z["ende_ms"] for z in zeilen), default=0) + GAP_AFTER_LAST_MS)
        elif offset < 0:
            raise ValueError("The recording is older than the session")
        entry = {"id": m["id"], "start": m["start"], "offset_ms": offset, "titel": m["titel"]}

        def go(s2):
            s2["aufnahmen"] = [*s2.get("aufnahmen", []), entry]
            s2.update(aktiv=True, pausiert=False, beendet=False, replay=None, offen=not m["laeuft"])
        s = self.update(sid, go)
        try:  # a session of its own for that recording is folded into this one
            self.update(other, lambda o: o.update(aktiv=False, aufgegangen_in=sid))
        except KeyError:
            pass
        self.wake.set()
        return s

    def split(self, sid: str) -> dict:
        """End this session and start a new one in the same running recording, beginning where the recording is now.
        Context, speaker names and model stay; questions, assumptions and process start empty. The old session is
        analysed one last time and then ends at this point."""
        s = self.get(sid)
        if s.get("replay"):
            raise ValueError("A replay cannot be split")
        rec = recordings(s)[-1]
        if not self.is_running(s):
            raise ValueError("The recording is not running")
        cut = self.recording_ms(s)                       # session time
        cut_rec = max(0, cut - rec["offset_ms"])         # time in the recording
        start = dt.datetime.fromisoformat(rec["start"])
        new_id = uuid.uuid4().hex.upper()
        n = new_session({"id": new_id, "titel": s["titel"], "start": rec["start"], "app": s.get("app")}, s["modell"])
        n["titel"] = f"{s['titel']} · ab {(start + dt.timedelta(milliseconds=cut_rec)):%H:%M}"
        n.update(quelle_id=rec["id"], ab_ms=cut_rec, laeuft=True, aktiv=True, kontext=copy.deepcopy(s["kontext"]),
                 sprecher_namen=dict(s["sprecher_namen"]), review_kriterien=s.get("review_kriterien"))
        self.put(n)
        self.update(sid, lambda s2: s2.update(aktiv=False, beendet=True, offen=False, pausiert=False, bis_ms=cut))
        self.trigger(sid)  # last run for the part that belongs to the old session
        self.wake.set()
        return n

    def sessions(self) -> list[dict]:
        out = []
        for f in self.dir.glob("*.json"):
            if f.stem == "settings":
                continue
            try:
                out.append(json.loads(f.read_text()))
            except (json.JSONDecodeError, FileNotFoundError):
                pass
        return out

    def continuation_of(self, m: dict, sessions: list[dict]) -> dict | None:
        """The session this recording most likely continues: the latest one that began before it, was active
        within the last hours before it started and has not been attached elsewhere."""
        start = dt.datetime.fromisoformat(m["start"])
        best = None
        for s in sessions:
            recs = recordings(s)
            if s.get("aufgegangen_in") or m["id"] in {r["id"] for r in recs}:
                continue
            last = max(dt.datetime.fromisoformat(r["start"]) for r in recs)
            if last >= start or start.timestamp() - max(last.timestamp(), s.get("letzter_lauf", 0)) > CONTINUE_WITHIN_S:
                continue
            if not best or last > best[0]:
                best = (last, s)
        return {"id": best[1]["id"], "titel": best[1]["titel"]} if best else None

    # Control

    def start(self, sid: str, modus: str = "live", tempo: float = 10.0) -> dict:
        def go(s):
            s.update(aktiv=True, pausiert=False, beendet=False, offen=modus == "fortsetzen")
            if modus == "fortsetzen":
                s["replay"] = None
            elif modus == "replay":
                s["replay"] = {"start": time.time(), "tempo": max(1.0, min(float(tempo), 100.0))}
                s.update(analysiert_bis=-1)
            elif modus == "sofort":
                s["replay"] = None
        s = self.update(sid, go)
        self.trigger(sid)
        return s

    def trigger(self, sid: str) -> None:
        if sid not in self._busy:
            self._busy.add(sid)
            self.analysis_jobs.put(sid)

    def miro_auth(self) -> miro.OAuth | None:
        """OAuth login when `miro.client_id` is configured; otherwise a fixed token (token_cmd/token_env/token)."""
        m = self.cfg.get("miro") or {}
        if not m.get("client_id"):
            return None
        if self._miro_auth is None:
            port = (self.cfg.get("review") or {}).get("port", 8765)
            secret = {k.replace("client_secret", "token"): v for k, v in m.items() if k.startswith("client_secret")}
            self._miro_auth = miro.OAuth(str(m["client_id"]), lambda: gateway_token(secret, "miro.client_secret"),
                                         m.get("redirect_uri") or f"http://localhost:{port}/miro/callback",
                                         self.miro_token_file)
        return self._miro_auth

    def miro_status(self) -> dict:
        m, auth = self.cfg.get("miro") or {}, self.miro_auth()
        return {"miro": bool(m), "miro_oauth": bool(auth), "miro_verbunden": auth.connected() if auth else bool(m),
                "miro_board": m.get("board", "")}

    def miro_sync(self, sid: str, board: str | None = None, request=None) -> dict:
        """Draw the session's processes on a Miro board (debrief → Miro only). Without `board` the board of the
        last sync or `miro.board` from the config is used."""
        cfg_miro = self.cfg.get("miro") or {}
        board = (board or (self.get(sid).get("miro") or {}).get("board") or cfg_miro.get("board") or "").strip()
        if not board:
            raise ValueError("No Miro board chosen")
        auth = self.miro_auth()
        if request is None:
            if not cfg_miro:
                raise ValueError("Miro is not configured (miro: in config.yaml)")
            request = miro.http_client(auth.access_token() if auth else gateway_token(cfg_miro, "miro"))
        with self._miro_lock:  # one sync at a time, so two clicks do not draw everything twice
            try:
                state = miro.sync(self.get(sid), request, board)
            except miro.MiroError as e:
                if auth and getattr(e, "status", None) == 401:  # token revoked in Miro
                    auth.forget()
                    raise miro.LoginNeeded("Miro login expired") from None
                raise
            self.update(sid, lambda s: s.update(miro={**state, "stand": now_iso()}))
        return state

    def research(self, sid: str, frage: str) -> None:
        def add(s):
            if not s["kontext"]["repos"]:
                raise ValueError("No repository chosen")
            a = {"id": new_id(), "frage": frage.strip(), "begruendung": "", "status": "offen", "ergebnis": None,
                 "erstellt": now_iso(), "von": "ich"}
            if not a["frage"]:
                raise ValueError("Empty question")
            s["auftraege"].append(a)
        self.update(sid, add)
        self.queue_research(sid)

    # Existing transcripts as recordings (files instead of MacWhisper's database)

    def _tfile(self, rid: str) -> Path:
        if not re.fullmatch(r"[0-9A-F]{32}", rid):
            raise KeyError(rid)
        return self.dir / f"{rid}.transcript"  # not *.json: the scheduler reads those as sessions

    def _imported_meeting(self, rid: str) -> dict | None:
        t = self._imported(rid)
        if t is None:
            return None
        end = max((z["ende_ms"] for z in t["zeilen"]), default=0)
        return {"id": rid, "titel": t["titel"], "start": t["start"], "app": t.get("app", "Datei"), "laeuft": False,
                "zeilen": len(t["zeilen"]), "dauer_s": round(end / 1000)}

    def _imported(self, rid: str) -> dict | None:
        """An imported transcript (None for MacWhisper recordings); re-read only when the file changed."""
        try:
            f = self._tfile(rid)
            stamp = f.stat().st_mtime_ns
        except (KeyError, FileNotFoundError):
            return None
        cached = self._imports.get(rid)
        if not cached or cached[0] != stamp:
            cached = (stamp, json.loads(f.read_text()))
            self._imports[rid] = cached
        return cached[1]

    def import_transcript(self, text: str, filename: str = "") -> dict:
        """Store a transcript text as a recording (the same text always gives the same recording)."""
        if len(text.encode()) > MAX_IMPORT_BYTES:
            raise ValueError("The transcript is too large")
        t = transcripts.parse(text, filename)
        self._tfile(t["id"]).write_text(json.dumps(t, ensure_ascii=False))
        self._imports.pop(t["id"], None)
        return t

    def new_session_from_transcript(self, text: str, filename: str = "") -> dict:
        """A session of its own for an existing transcript (like a finished recording: analyse or replay it)."""
        t = self.import_transcript(text, filename)
        return self.open_session(t["id"])

    def attach_transcript(self, sid: str, text: str, filename: str = "") -> dict:
        """Continue the session with an existing transcript: it follows the session's last line, or starts at its
        own time when that is later."""
        t = self.import_transcript(text, filename)
        return self.attach(sid, t["id"])

    def _undo_file(self, sid: str) -> Path:
        return self._file(sid).with_suffix(".undo")  # not *.json: the scheduler reads those as sessions

    def process_command(self, sid: str, text: str, prozess: str | None = None) -> dict:
        """Edit the process by instruction ("insert a policy after …"): the model returns operations, they are
        carried out as your own edits. One step back stays available."""
        text = (text or "").strip()
        if not text:
            raise ValueError("Empty instruction")
        s = self.get(sid)
        if not s["prozesse"]:
            raise ValueError("No process yet")
        zeilen, labels = self.visible_lines(s)
        zeilen = [z for z in listened(s, zeilen, labels) if z["bestaetigt"]][-40:]
        self.update(sid, lambda s2: s2.update(arbeit="befehl"))
        try:
            res, nutzung = run_claude(self._cfg_for(s), BEFEHL_SYSTEM, command_prompt(s, labels, zeilen, text, prozess),
                                      BEFEHL_SCHEMA, "Live process edit", s["modell"]["analyse"])
        finally:
            self.update(sid, lambda s2: s2.update(arbeit=None))
        out: dict = {}

        def go(s2):
            snap = snapshot(s2)
            out.update(apply_commands(s2, res.get("operationen", []), prozess))
            if out["angewendet"]:
                self._undo_file(sid).write_text(json.dumps(snap))
                s2["rueckgaengig"] = (res.get("antwort") or text)[:200]
        self.update(sid, go)
        self._log_run(sid, "prozess-befehl", nutzung)
        return {"antwort": (res.get("antwort") or "").strip(), **out}

    def review_default(self) -> str:
        try:
            data = json.loads(EINSTELLUNGEN.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            data = {}
        return data.get("review_kriterien") or DEFAULT_REVIEW_KRITERIEN

    def review(self, sid: str, prozess: str | None = None, kriterien: str | None = None, als_standard: bool = False,
               blocking: bool = False) -> None:
        """Have the process model checked against the review criteria (own run, shows findings, changes nothing)."""
        s = self.get(sid)
        targets = [find(s["prozesse"], prozess)] if prozess else list(s["prozesse"])
        if not targets:
            raise ValueError("No process to review")
        if sid in self._busy:
            raise ValueError("An analysis is running - try again in a moment")
        kriterien = (kriterien or "").strip()
        if als_standard and kriterien:
            EINSTELLUNGEN.parent.mkdir(exist_ok=True)
            try:
                data = json.loads(EINSTELLUNGEN.read_text())
            except (FileNotFoundError, json.JSONDecodeError):
                data = {}
            EINSTELLUNGEN.write_text(json.dumps({**data, "review_kriterien": kriterien}, indent=1))
        self._busy.add(sid)
        self.update(sid, lambda s2: s2.update(analyse_laeuft=True, arbeit="review", review_kriterien=kriterien or s2.get("review_kriterien")))
        args = (sid, [p["id"] for p in targets])
        if blocking:
            self._review(*args)
        else:
            threading.Thread(target=self._review, args=args, daemon=True, name="live-review").start()

    def _review(self, sid: str, ids: list[str]) -> None:
        try:
            s = self.get(sid)
            zeilen, labels = self.visible_lines(s)
            zeilen = [z for z in listened(s, zeilen, labels) if z["bestaetigt"]][-80:]
            targets = [p for p in s["prozesse"] if p["id"] in ids]
            kriterien = s.get("review_kriterien") or self.review_default()
            res, nutzung = run_claude(self._cfg_for(s), review_system(kriterien), review_prompt(s, labels, zeilen, targets),
                                      REVIEW_SCHEMA, "Live review", s["modell"]["analyse"])
            known = set(all_elements(s))
            befunde = [{"id": new_id(), "status": "neu", **{k: b.get(k) for k in ("kategorie", "schwere", "beobachtung", "empfehlung")},
                        "frage": (b.get("frage") or "").strip(), "aenderung": b.get("aenderung") or [],
                        "elemente": [x for x in b.get("elemente", []) if x in known]}
                       for b in res.get("befunde", []) if (b.get("beobachtung") or "").strip()]
            rank = {"hoch": 0, "mittel": 1, "niedrig": 2}
            befunde.sort(key=lambda b: rank.get(b["schwere"], 3))
            review = {"zeit": now_iso(), "prozess": ", ".join(p["name"] for p in targets),
                      "zusammenfassung": (res.get("zusammenfassung") or "").strip(), "befunde": befunde}
            self.update(sid, lambda s2: s2.update(review=review))
            self._log_run(sid, "review", nutzung)
        except Exception as e:  # noqa: BLE001 - shown on the page
            log.exception("Live review failed: %s", sid)
            self._log_run(sid, "review", None, str(e)[:500])
        finally:
            self._busy.discard(sid)
            self.update(sid, lambda s2: s2.update(analyse_laeuft=False, arbeit=None))

    def review_action(self, sid: str, befund_id: str, aktion: str) -> dict:
        """What you do with a finding: apply its fix, take its question over, dismiss it or reset it."""
        out: dict = {}

        def go(s):
            b = find((s.get("review") or {}).get("befunde", []), befund_id)
            if aktion == "anwenden":
                if not b["aenderung"]:
                    raise ValueError("This finding has no change to apply")
                snap = snapshot(s)
                out.update(apply_commands(s, b["aenderung"]))
                if not out["angewendet"]:
                    raise ValueError("The change could not be applied: " + "; ".join(out["fehler"])[:200])
                self._undo_file(sid).write_text(json.dumps(snap))
                s["rueckgaengig"] = ("Review: " + b["beobachtung"])[:200]
                b["status"] = "angewendet"
            elif aktion == "frage":
                e = add_entry(s, "frage", b["frage"] or b["beobachtung"])
                if e["quelle"] == "ich":  # new (a question that was already there keeps its origin)
                    e["quelle"] = "review"
                e["elemente"] = list(dict.fromkeys([*e.get("elemente", []), *(x for x in b["elemente"] if x in all_elements(s))]))
                b["status"] = "uebernommen"
            elif aktion == "verwerfen":
                b["status"] = "verworfen"
                s["review_verworfen"] = [*s.get("review_verworfen", []), norm(b["beobachtung"])][-50:]
            elif aktion == "zuruecksetzen":
                b["status"] = "neu"
            else:
                raise ValueError(f"Unknown action: {aktion}")
        self.update(sid, go)
        return out

    def undo_command(self, sid: str) -> None:
        file = self._undo_file(sid)
        if not file.exists():
            raise ValueError("Nothing to undo")
        snap = json.loads(file.read_text())

        def go(s):
            restore(s, snap)
            s["rueckgaengig"] = None
        self.update(sid, go)
        file.unlink(missing_ok=True)

    def regenerate(self, sid: str, prozess: str | None = None, neu_aufbauen: bool = False, hinweis: str = "",
                   blocking: bool = False) -> None:
        """Build the process (or all processes) again from everything known now: transcript, facts, answers,
        instructions. `neu_aufbauen`: only your locked elements stay; otherwise the model revises what is there."""
        s = self.get(sid)
        targets = [find(s["prozesse"], prozess)] if prozess else list(s["prozesse"])
        if not targets:
            raise ValueError("No process to regenerate")
        if sid in self._busy:
            raise ValueError("An analysis is running - try again in a moment")
        self._busy.add(sid)
        self.update(sid, lambda s2: s2.update(analyse_laeuft=True, arbeit="neu_erzeugen"))
        args = (sid, [p["id"] for p in targets], neu_aufbauen, hinweis)
        if blocking:
            self._regenerate(*args)
        else:
            threading.Thread(target=self._regenerate, args=args, daemon=True, name="live-regenerate").start()

    def _regenerate(self, sid: str, ids: list[str], neu_aufbauen: bool, hinweis: str) -> None:
        try:
            s = self.get(sid)
            zeilen, labels = self.visible_lines(s)
            zeilen = [z for z in listened(s, zeilen, labels) if z["bestaetigt"] and z["start_ms"] >= s["zusammenfassung"]["bis_ms"]]
            targets = [p for p in s["prozesse"] if p["id"] in ids]
            res, nutzung = run_claude(self._cfg_for(s), system_prompt(self.cfg),
                                      regenerate_prompt(self.cfg, s, labels, zeilen, targets, neu_aufbauen, hinweis),
                                      ANALYSE_SCHEMA, "Live process", s["modell"]["analyse"])

            def merge(s2):
                lauf = len([x for x in s2["laeufe"] if x["art"] in ("analyse", "prozess")]) + 1
                snap = snapshot(s2)
                links: dict[str, list[str]] = {}
                if neu_aufbauen:  # unlocked elements make room; their links to questions are restored by wording
                    for p in [p for p in s2["prozesse"] if p["id"] in ids]:
                        for el in [e for e in p["elemente"] if not e["gesperrt"]]:
                            for en in s2["eintraege"]:
                                if el["id"] in en.get("elemente", []):
                                    links.setdefault(en["id"], []).append(norm(el["text"]))
                            remove_element(s2, el["id"])
                apply_analysis(s2, res, lauf)
                by_text = {norm(e["text"]): e["id"] for _, e in all_elements(s2).values()}
                for en in s2["eintraege"]:
                    for text in links.get(en["id"], []):
                        if text in by_text and by_text[text] not in en.setdefault("elemente", []):
                            en["elemente"].append(by_text[text])
                self._undo_file(sid).write_text(json.dumps(snap))
                s2["rueckgaengig"] = "Prozess neu erzeugt" + (": " + hinweis.strip()[:120] if hinweis.strip() else "")
            self.update(sid, merge)
            self._log_run(sid, "prozess", nutzung)
            self.queue_research(sid)
        except Exception as e:  # noqa: BLE001 - shown on the page, the session stays as it was
            log.exception("Live process failed: %s", sid)
            self._log_run(sid, "prozess", None, str(e)[:500])
        finally:
            self._busy.discard(sid)
            self.update(sid, lambda s2: s2.update(analyse_laeuft=False, arbeit=None))

    def queue_research(self, sid: str) -> None:
        s = self.get(sid)
        for a in s["auftraege"]:
            if a["status"] == "offen":
                self.update(sid, lambda s2, aid=a["id"]: find(s2["auftraege"], aid).update(status="wartet"))
                self.research_jobs.put((sid, a["id"]))

    # Background threads

    def watch(self) -> None:
        """Every second: new recording in MacWhisper? analysis due? meeting over?"""
        last_stamp = None
        while True:
            try:
                stamp = mw.wal_stamp(self.db)
                if stamp != last_stamp:
                    last_stamp = stamp
                    self._check_new_recording()
                    self.notify_change()  # pages pick up new lines
                self._schedule()
            except Exception:
                log.exception("Live: watcher")
            self.wake.wait(1.0)
            self.wake.clear()

    def _check_new_recording(self) -> None:
        if not self.db.exists():
            return
        con = self.connect()
        try:
            m = mw.running(con)
        finally:
            con.close()
        if m and m["id"] not in self._notified:
            self._notified.add(m["id"])
            if self.cfg.get("live", {}).get("mitteilung", True):
                en = self.cfg.get("sprache") == "en"
                self.notify("Meeting recording" if en else "Meeting-Aufnahme läuft",
                            f"{m['titel']} – open the live assistant?" if en else f"{m['titel']} – Live-Assistent öffnen?",
                            f"live#{m['id']}", group="debrief-live")

    def _schedule(self) -> None:
        takt = self.cfg.get("live", {}).get("takt_s", TAKT_S)
        for f in self.dir.glob("*.json"):
            if f.stem == "settings":
                continue
            try:
                s = json.loads(f.read_text())
            except (json.JSONDecodeError, FileNotFoundError):
                continue
            if not s.get("aktiv"):
                # an ended session notices that its recording goes on, so it can be resumed
                if time.monotonic() - self._checked.get(s["id"], 0) >= 5 and not s.get("replay"):
                    self._checked[s["id"]] = time.monotonic()
                    running = self.is_running(s)
                    if s.get("laeuft") != running:
                        self.update(s["id"], lambda s2, r=running: s2.update(laeuft=r))
                continue
            if s.get("pausiert") or s["id"] in self._busy:
                continue
            zeilen, labels = self.visible_lines(s)
            new = [z for z in listened(s, zeilen, labels) if z["bestaetigt"] and z["nr"] > s["analysiert_bis"]]
            running = self.is_running(s)
            if s.get("laeuft") != running:
                self.update(s["id"], lambda s2, r=running: s2.update(laeuft=r))
            since = time.time() - s.get("letzter_lauf", 0)
            if not running and not s.get("offen"):  # meeting over: last run with everything, then stop
                if not (new or any(a.get("neu") for a in s["anweisungen"])):
                    self.update(s["id"], lambda s2: s2.update(aktiv=False, beendet=True))
                elif since >= 15:
                    self.trigger(s["id"])
            elif new and since >= takt:
                self.trigger(s["id"])
            elif time.time() - s.get("antwortpruefung", 0) >= ANTWORT_TAKT_S and any(
                    z["nr"] > max(s["analysiert_bis"], s.get("geprueft_bis", -1)) for z in new):
                self.check_answers(s["id"])

    def check_answers(self, sid: str, blocking: bool = False) -> bool:
        """Small fast run between the analyses: were open questions answered in the lines since the last check?"""
        s = self.get(sid)
        if sid in self._busy or not any(e["typ"] in ("frage", "offen") and e["status"] in OFFEN for e in s["eintraege"]):
            return False
        self._busy.add(sid)
        self.update(sid, lambda s2: s2.update(antwortpruefung=time.time()))
        if blocking:
            self._check_answers(sid)
        else:
            threading.Thread(target=self._check_answers, args=(sid,), daemon=True, name="live-answers").start()
        return True

    def _check_answers(self, sid: str) -> None:
        try:
            s = self.get(sid)
            zeilen, labels = self.visible_lines(s)
            zeilen = [z for z in listened(s, zeilen, labels) if z["bestaetigt"]]
            since = max(s["analysiert_bis"], s.get("geprueft_bis", -1))
            new = [z for z in zeilen if z["nr"] > since]
            if not new:
                return
            context = [z for z in zeilen if z["nr"] <= since][-6:]  # an answer often continues what was said just before
            offene = [e for e in s["eintraege"] if e["typ"] in ("frage", "offen") and e["status"] in OFFEN]
            prompt = ("<open_questions>\n" + "\n".join(f"- {e['id']}: {e['text']}" for e in offene) + "\n</open_questions>\n\n"
                      f"<transcript_before>\n{format_lines(s, labels, context) or '–'}\n</transcript_before>\n\n"
                      f"<transcript_new>\n{format_lines(s, labels, new)}\n</transcript_new>")
            res, nutzung = run_claude(self._cfg_for(s), ANTWORT_SYSTEM, prompt, ANTWORT_SCHEMA, "Live answers", s["modell"]["analyse"])
            upto = max(z["nr"] for z in new)

            def merge(s2):
                apply_analysis(s2, {"erledigt": res.get("beantwortet", [])}, 0)
                s2["geprueft_bis"] = max(upto, s2.get("geprueft_bis", -1))
            self.update(sid, merge)
            self._log_run(sid, "antworten", nutzung)
        except Exception:  # noqa: BLE001 - the next full analysis checks as well
            log.exception("Live answer check failed: %s", sid)
            self.update(sid, lambda s2: s2.update(antwortpruefung=time.time()))
        finally:
            self._busy.discard(sid)

    def work_analysis(self) -> None:
        while True:
            sid = self.analysis_jobs.get()
            try:
                self.analyse(sid)
            except KeyError:
                pass
            except Exception as e:
                log.exception("Live analysis failed: %s", sid)
                self._log_run(sid, "analyse", None, str(e)[:500])
                self.update(sid, lambda s: s.update(letzter_lauf=time.time()))  # retry only after the next interval
            finally:
                self._busy.discard(sid)

    def work_research(self) -> None:
        while True:
            sid, aid = self.research_jobs.get()
            try:
                self.run_research(sid, aid)
            except KeyError:
                pass
            except Exception as e:
                log.exception("Live research failed: %s", sid)
                self.update(sid, lambda s, err=str(e)[:500]: find(s["auftraege"], aid).update(status="fehler", ergebnis=err))
                self._log_run(sid, "recherche", None, str(e)[:500])

    def _cfg_for(self, s: dict) -> dict:
        return self.cfg if s["modell"].get("weg") == "gateway" and self.cfg.get("gateway") else {**self.cfg, "gateway": None}

    def _log_run(self, sid: str, art: str, nutzung: dict | None, fehler: str | None = None) -> None:
        def add(s):
            s["laeufe"] = [*s["laeufe"], {"zeit": now_iso(), "art": art, "nutzung": nutzung, "fehler": fehler}][-200:]
            s["fehler"] = fehler
        self.update(sid, add)

    def analyse(self, sid: str) -> None:
        s = self.get(sid)
        self.update(sid, lambda s2: s2.update(analyse_laeuft=True, arbeit="analyse"))
        try:
            zeilen, labels = self.visible_lines(s)
            zeilen = listened(s, zeilen, labels)
            cfg = self._cfg_for(s)
            reload_kategorien(self.cfg)
            summarise, verbatim = window(s, zeilen, self.cfg.get("live", {}).get("fenster_min", FENSTER_MIN))
            if summarise:
                # A failed summary must not cost the live questions: the analysis then runs with the old summary
                # and the summary is tried again next time.
                try:
                    res, nutzung = run_claude(cfg, "You summarise meetings for a live assistant.",
                                              summary_prompt(s, labels, summarise), ZUSAMMENFASSUNG_SCHEMA, "Live summary",
                                              s["modell"]["analyse"])
                except (RuntimeError, subprocess.TimeoutExpired, ValueError) as e:
                    log.warning("Live summary failed, analysing without it: %s", e)
                    self._log_run(sid, "zusammenfassung", None, str(e)[:500])
                else:
                    bis = summarise[-1]["start_ms"] + 1
                    self.update(sid, lambda s2: s2.update(zusammenfassung={"text": res["zusammenfassung"].strip(), "bis_ms": bis}))
                    self._log_run(sid, "zusammenfassung", nutzung)
                s = self.get(sid)
            if not verbatim and not any(a.get("neu") for a in s["anweisungen"]):
                return
            upto = max((z["nr"] for z in verbatim), default=s["analysiert_bis"])
            res, nutzung = run_claude(cfg, system_prompt(self.cfg), analysis_prompt(self.cfg, s, labels, verbatim),
                                      ANALYSE_SCHEMA, "Live analysis", s["modell"]["analyse"])

            def merge(s2):
                lauf = len([x for x in s2["laeufe"] if x["art"] == "analyse"]) + 1
                apply_analysis(s2, res, lauf)
                s2.update(analysiert_bis=max(upto, s2["analysiert_bis"]), letzter_lauf=time.time())
            self.update(sid, merge)
            self._log_run(sid, "analyse", nutzung)
            self.queue_research(sid)
        finally:
            self.update(sid, lambda s2: s2.update(analyse_laeuft=False, arbeit=None))

    def run_research(self, sid: str, aid: str) -> None:
        self.update(sid, lambda s: find(s["auftraege"], aid).update(status="laeuft"))
        s = self.get(sid)
        auftrag = find(s["auftraege"], aid)
        repos = [Path(r) for r in s["kontext"]["repos"] if Path(r).is_dir()]
        if not repos:
            raise ValueError("No repository available")
        zeilen, labels = self.visible_lines(s)
        system, prompt = research_prompt(s, auftrag, labels, zeilen)
        res, nutzung = run_claude(self._cfg_for(s), system, prompt, RECHERCHE_SCHEMA, "Live research",
                                  s["modell"]["recherche"], dirs=repos)
        self.update(sid, lambda s2: apply_research(s2, find(s2["auftraege"], aid), res))
        self._log_run(sid, "recherche", nutzung)

    def resume(self) -> None:
        """After a restart: research jobs that were waiting or running are queued again."""
        for f in self.dir.glob("*.json"):
            if f.stem == "settings":
                continue
            try:
                s = json.loads(f.read_text())
            except (json.JSONDecodeError, FileNotFoundError):
                continue
            changed = False
            for a in s.get("auftraege", []):
                if a["status"] in ("wartet", "laeuft"):
                    a["status"], changed = "offen", True
            if s.get("analyse_laeuft") or s.get("arbeit"):
                s["analyse_laeuft"], s["arbeit"], changed = False, None, True
            if changed:
                self.put(s)
            if any(a["status"] == "offen" for a in s.get("auftraege", [])):
                self.queue_research(s["id"])

    def start_threads(self) -> None:
        self.resume()
        for target in (self.watch, self.work_analysis, self.work_research):
            threading.Thread(target=target, daemon=True).start()

    # Handover

    def session_for_recording(self, recorded: dt.datetime | str | None) -> dict | None:
        """The live session of the transcript that MacWhisper exported to the inbox (same start time)."""
        if isinstance(recorded, str):
            try:
                recorded = dt.datetime.fromisoformat(recorded)
            except ValueError:
                return None
        if not isinstance(recorded, dt.datetime):
            return None
        if recorded.tzinfo is None:
            recorded = recorded.astimezone()
        best = None
        for f in self.dir.glob("*.json"):
            if f.stem == "settings":
                continue
            try:
                s = json.loads(f.read_text())
            except (json.JSONDecodeError, FileNotFoundError):
                continue
            diff = min(abs((dt.datetime.fromisoformat(r["start"]) - recorded).total_seconds()) for r in recordings(s))
            if diff <= SAME_RECORDING_S and (not best or (diff, -dt.datetime.fromisoformat(s["erstellt"]).timestamp()) < best[0]):
                best = ((diff, -dt.datetime.fromisoformat(s["erstellt"]).timestamp()), s)
        return best[1] if best else None

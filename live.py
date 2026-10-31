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

import datetime as dt
import json
import logging
import queue
import re
import threading
import time
import uuid
from pathlib import Path

import yaml

import macwhisper as mw
from debrief import (HERE, calendar_candidates, reload_kategorien, role, run_claude, safe_folder, save_kategorien,
                     termin_line)

log = logging.getLogger("debrief.live")

LIVE_DIR = HERE / "live"
EINSTELLUNGEN = LIVE_DIR / "settings.json"  # default model choice for live sessions
TAKT_S = 75            # automatic analysis at most this often
FENSTER_MIN = 15       # verbatim transcript in the prompt, older parts as a running summary
ZUSAMMENFASSEN_AB_MIN = 10  # summarise once this much has dropped out of the window
MAX_NOTIZ_ZEICHEN = 6000
SAME_RECORDING_S = 180  # transcript in the inbox belongs to a live session if it starts within this time

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
    }


def find(items: list[dict], item_id: str) -> dict:
    for i in items:
        if i["id"] == item_id:
            return i
    raise KeyError(item_id)


def all_elements(s: dict) -> dict[str, tuple[dict, dict]]:
    return {e["id"]: (p, e) for p in s["prozesse"] for e in p["elemente"]}


def new_process(s: dict, name: str, herkunft: str = "ki") -> dict:
    p = {"id": new_id(), "name": name.strip() or "Prozess", "elemente": [], "verbindungen": [], "geloescht": [],
         "getrennt": [], "quelle": None, "original": None, "herkunft": herkunft}
    s["prozesse"].append(p)
    return p


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
        for p in s["prozesse"]:
            if ref in (p["id"], p["name"]) or norm(p["name"]) == norm(ref or ""):
                return p
        return s["prozesse"][0] if s["prozesse"] else new_process(s, "Prozess")

    def element_id(ref: str | None) -> str | None:
        return refs.get(ref or "", ref) if ref else None

    for op in res.get("aenderungen", []):
        art = op.get("op")
        try:
            if art == "prozess_neu":
                name = (op.get("text") or op.get("prozess") or "Prozess").strip()
                existing = next((p for p in s["prozesse"] if norm(p["name"]) == norm(name)), None)
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
the time line. Prefer the words the participants use (ubiquitous language).

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
- Mark open questions you can see answered in the transcript as erledigt.
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
        out.append(f"Process {p['id']} \"{p['name']}\":")
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
    """In a replay only the lines that would have been spoken by now."""
    r = s.get("replay")
    if not r:
        return lines
    limit = ((now or time.time()) - r["start"]) * 1000 * r["tempo"]
    return [z for z in lines if z["start_ms"] <= limit]


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


def add_entry(s: dict, typ: str, text: str) -> None:
    """Your own question or open point, managed like the assistant's."""
    if typ not in ("frage", "offen") or not text.strip():
        raise ValueError("Empty text")
    s["eintraege"].append({"id": new_id(), "typ": typ, "text": text.strip(), "begruendung": "", "prio": 1, "zeilen": [],
                           "elemente": [], "status": "angepinnt", "notiz": "", "lauf": None, "erstellt": now_iso(),
                           "quelle": "ich"})


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

class LiveManager:
    """Live sessions on disk, watching MacWhisper, scheduling the analysis and research jobs."""

    def __init__(self, cfg: dict, notify=None, db: Path | None = None, directory: Path | None = None):
        self.cfg = cfg
        self.db = db or mw.DB
        self.dir = directory or LIVE_DIR
        self.dir.mkdir(exist_ok=True)
        self.notify = notify or (lambda *a, **kw: None)
        self.lock = threading.RLock()
        self.changed = threading.Condition()
        self.version = 0
        self.analysis_jobs: queue.Queue[str] = queue.Queue()
        self.research_jobs: queue.Queue[tuple[str, str]] = queue.Queue()
        self.wake = threading.Event()
        self._lines: dict[str, dict] = {}  # sid -> {"stamp", "stand", "zeilen": {id: line}}
        self._notified: set[str] = set()
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
        EINSTELLUNGEN.write_text(json.dumps(modell, indent=1))

    # MacWhisper

    def connect(self):
        return mw.connect(self.db)

    def meetings(self, limit: int = 15) -> list[dict]:
        con = self.connect()
        try:
            out = mw.meetings(con, limit)
        finally:
            con.close()
        for m in out:
            m["sitzung"] = self._file(m["id"]).exists()
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

    def lines(self, sid: str) -> tuple[list[dict], dict[str, str]]:
        """All lines of the session (incrementally from the database, only if MacWhisper wrote something)."""
        stamp = mw.wal_stamp(self.db)
        with self.lock:
            cache = self._lines.setdefault(sid, {"stamp": None, "stand": None, "zeilen": {}, "labels": {}})
            if cache["stamp"] != stamp:
                con = self.connect()
                try:
                    for z in mw.lines(con, sid, cache["stand"]):
                        cache["zeilen"][z["id"]] = z
                        cache["stand"] = max(cache["stand"] or "", z["stand"] or "")
                    cache["labels"] = mw.speakers(con, sid)
                finally:
                    con.close()
                cache["stamp"] = stamp
            zeilen = sorted(cache["zeilen"].values(), key=lambda z: (z["nr"], z["start_ms"]))
            return zeilen, dict(cache["labels"])

    def visible_lines(self, s: dict) -> tuple[list[dict], dict[str, str]]:
        zeilen, labels = self.lines(s["id"])
        return visible(s, zeilen), labels

    def is_running(self, s: dict) -> bool:
        if s.get("replay"):
            zeilen, _ = self.lines(s["id"])
            return len(visible(s, zeilen)) < len(zeilen)
        con = self.connect()
        try:
            m = mw.meeting(con, s["id"])
        finally:
            con.close()
        return bool(m and m["laeuft"])

    # Control

    def start(self, sid: str, modus: str = "live", tempo: float = 10.0) -> dict:
        def go(s):
            s.update(aktiv=True, pausiert=False, beendet=False)
            if modus == "replay":
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
            if not s.get("aktiv") or s.get("pausiert") or s["id"] in self._busy:
                continue
            zeilen, _ = self.visible_lines(s)
            new = [z for z in zeilen if z["bestaetigt"] and z["nr"] > s["analysiert_bis"]]
            running = self.is_running(s)
            if s.get("laeuft") != running:
                self.update(s["id"], lambda s2, r=running: s2.update(laeuft=r))
            since = time.time() - s.get("letzter_lauf", 0)
            if not running:  # meeting over: last run with everything, then stop
                if not (new or any(a.get("neu") for a in s["anweisungen"])):
                    self.update(s["id"], lambda s2: s2.update(aktiv=False, beendet=True))
                elif since >= 15:
                    self.trigger(s["id"])
            elif new and since >= takt:
                self.trigger(s["id"])

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
        self.update(sid, lambda s2: s2.update(analyse_laeuft=True))
        try:
            zeilen, labels = self.visible_lines(s)
            cfg = self._cfg_for(s)
            reload_kategorien(self.cfg)
            summarise, verbatim = window(s, zeilen, self.cfg.get("live", {}).get("fenster_min", FENSTER_MIN))
            if summarise:
                res, nutzung = run_claude(cfg, "You summarise meetings for a live assistant.", summary_prompt(s, labels, summarise),
                                          ZUSAMMENFASSUNG_SCHEMA, "Live summary", s["modell"]["analyse"])
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
            self.update(sid, lambda s2: s2.update(analyse_laeuft=False))

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
            if s.get("analyse_laeuft"):
                s["analyse_laeuft"], changed = False, True
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
            diff = abs((dt.datetime.fromisoformat(s["start"]) - recorded).total_seconds())
            if diff <= SAME_RECORDING_S and (not best or diff < best[0]):
                best = (diff, s)
        return best[1] if best else None

#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["pyyaml>=6"]
# ///
"""Files MacWhisper transcripts in the Obsidian vault.

For every new note in the inbox folder:
  1. Classification (customer/internal), summary and todos via `claude -p`
  2. Add frontmatter, put the sections in front of the transcript
  3. Move the note as `<date> <title>.md` into the category's folder

Usage:  uv run --script debrief.py [--dry-run] [--config config.yaml] [file.md ...]

Normally this runs via server.py, where every suggestion is reviewed and confirmed
first. Calling this script directly files notes without asking.
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
MIN_FILE_AGE_S = 20  # MacWhisper should have finished writing
FRONTMATTER_RE = re.compile(r"\A---\n(.*?)\n---\n?", re.DOTALL)

log = logging.getLogger("debrief")


KATEGORIEN_HEADER = """\
# Categories: key = value for the `kunde` property.
# `hinweise` (hints) help the model with assignment (people, projects, terms).
# Projects end up in `<ordner>/<project>`, supervised people in `<ordner>/<project>/<person>`.
# Also written by the review page (comments are lost in the process).
"""


def normalize_kategorien(kategorien: dict) -> dict:
    """Projects may also be just a hint string in the file."""
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
    cfg["kategorien_datei"] = path.parent / "categories.yaml"
    cfg["kategorien"] = normalize_kategorien(cfg.get("kategorien") or {})
    reload_kategorien(cfg)
    return cfg


def reload_kategorien(cfg: dict) -> None:
    """Re-read categories.yaml if it changed since the last load (e.g. edited by hand)."""
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
            raise ValueError(f"Invalid category: {key!r} (name without spaces, folder required)")
        safe_folder(cfg, cat["ordner"])
    tmp = cfg["kategorien_datei"].with_suffix(".tmp")
    tmp.write_text(KATEGORIEN_HEADER + yaml.safe_dump(kategorien, sort_keys=False, allow_unicode=True))
    tmp.replace(cfg["kategorien_datei"])
    cfg["kategorien"] = kategorien
    cfg["_kategorien_mtime"] = cfg["kategorien_datei"].stat().st_mtime


def safe_folder(cfg: dict, folder: str) -> Path:
    path = (cfg["vault"] / folder).resolve()
    if not path.is_relative_to(cfg["vault"].resolve()):
        raise ValueError(f"Folder is outside the vault: {folder}")
    return path


def split_frontmatter(text: str) -> tuple[dict, str]:
    m = FRONTMATTER_RE.match(text)
    if not m:
        return {}, text
    return yaml.safe_load(m.group(1)) or {}, text[m.end():]


GELERNT = HERE / "learned.json"  # Corrections and names from the review (local, not in Git)
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
    """\"David\" in the config, \"David Schowalter\" in the transcript → both are me."""
    n, me = str(name or "").strip().casefold(), str(cfg.get("ich", "")).strip().casefold()
    return bool(me) and (n == me or n.startswith(me + " "))


def is_placeholder_speaker(name: str) -> bool:
    return bool(SPEAKER_RE.match(name.strip()))


def word_count(body: str, speakers: list[str]) -> int:
    """Words in the transcript without the speaker lines."""
    labels = {s.strip() for s in speakers}
    return sum(len(line.split()) for line in body.splitlines() if line.strip() and line.strip() not in labels)


def speaker_samples(body: str, speakers: list[str], length: int = 140) -> dict[str, str]:
    """First statement of each speaker, so they can be recognized when naming them."""
    labels, samples, current = {s.strip() for s in speakers}, {}, None
    for line in body.splitlines():
        text = line.strip()
        if text in labels:
            current = text
        elif text and current and current not in samples and len(text.split()) >= 3:
            samples[current] = text[:length] + ("…" if len(text) > length else "")
    return samples


def rename_speakers(meta: dict, body: str, names: dict[str, str] | None) -> tuple[dict, str]:
    """Replaces speaker placeholders (\"Speaker 1\") in frontmatter and transcript with real names."""
    names = {k: v.strip() for k, v in (names or {}).items() if v and v.strip() and v.strip() != k}
    if not names:
        return meta, body
    meta = dict(meta)
    if meta.get("Speakers"):
        meta["Speakers"] = [names.get(str(s), str(s)) for s in meta["Speakers"]]
    body = "\n".join(names.get(line.strip(), line) if line.strip() in names else line for line in body.splitlines())
    return meta, body


def rename_in_result(result: dict, names: dict[str, str] | None) -> dict:
    """Also replace speaker placeholders in summary, decisions and todos."""
    names = {k: v.strip() for k, v in (names or {}).items() if v and v.strip()}
    if not names:
        return result
    # A short label ("Petr") must not be expanded again inside a text that already has the full name ("Petr Novikov").
    def alternative(k: str) -> str:
        tail = names[k][len(k):] if names[k].startswith(k) else ""
        return rf"\b{re.escape(k)}\b(?!\d)" + (rf"(?!{re.escape(tail)})" if tail.strip() else "")
    pattern = re.compile("|".join(alternative(k) for k in sorted(names, key=len, reverse=True)))
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
    lines = [f"- \"{k['titel']}\": {k['kurz']} → correct: {fmt(k['richtig'])} (you had: {fmt(k['vorschlag'])})"
             for k in korrekturen]
    return (f"\n\nEarlier corrections by {cfg['ich']} - assign similar conversations the same way:\n"
            + "\n".join(lines))


# --- Duplicates ---------------------------------------------------------------

FINGERABDRUECKE = HERE / "fingerprints.json"  # filed transcripts (local, not in Git)
SKETCH_K = 128      # size of the bottom-k sketch for similarity
SHINGLE = 3         # word group length (3 tolerates deviations of a re-transcription)


def transcript_text(body: str, speakers: list[str]) -> str:
    """Spoken text only: speaker lines removed (they may be renamed), lowercase, no punctuation."""
    labels = {str(s).strip() for s in speakers}
    lines = [l for l in body.splitlines() if l.strip() and l.strip() not in labels]
    return " ".join(re.findall(r"\w+", " ".join(lines).lower()))


def fingerprint(body: str, speakers: list[str], meta: dict) -> dict:
    """Exact hash plus bottom-k sketch of the 3-word groups (≈ Jaccard similarity)."""
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
    """Index already filed notes (with `kunde:` and a \"## Transkript\" section) once."""
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
    """Most similar counterpart; the same recording (time + duration) counts as at least 0.9."""
    best, match = 0.0, None
    for other in others:
        sim = similarity(fp, other)
        if same_recording(fp, other):
            sim = max(sim, 0.9)
        if sim > best:
            best, match = sim, other
    return best, match


KALENDER_APP = HERE / "bin" / "Debrief Calendar.app"
MAX_TERMIN_STUNDEN = 4  # longer entries (trade fairs, vacation) are not meetings


def parse_duration(value) -> int:
    """MacWhisper duration \"38:37\" or \"1:02:03\" in seconds."""
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
    """Calendar events for the recording - best first, empty if nothing fits. An event qualifies if the
    recording starts between `vor_beginn_min` before and `nach_beginn_min` after its start, or if it is
    already running at recording start (e.g. a long workshop)."""
    window = recording_window(meta)
    if not cfg.get("kalender", True) or not window or not KALENDER_APP.exists():
        return []
    start, end = window
    fenster = cfg.get("kalender_fenster", {})
    before = dt.timedelta(minutes=fenster.get("vor_beginn_min", 10))
    after = dt.timedelta(minutes=fenster.get("nach_beginn_min", 30))
    out = HERE / "pending" / f"kalender-{os.getpid()}-{time.monotonic_ns()}.out"  # not .json: those are suggestions
    try:
        subprocess.run(["open", "-W", "-g", "-n", "--stdout", str(out), str(KALENDER_APP), "--args",
                        "--von", (start - dt.timedelta(hours=MAX_TERMIN_STUNDEN)).isoformat(timespec="seconds"),
                        "--bis", (start + before).isoformat(timespec="seconds")], timeout=30, check=False,
                       stderr=subprocess.DEVNULL)  # "Unable to block on applications" if the app finishes faster
        data = json.loads(out.read_text() or "[]")
    except Exception:
        log.exception("Calendar not readable")
        return []
    finally:
        out.unlink(missing_ok=True)
    if isinstance(data, dict):
        log.warning("Calendar: %s", data.get("fehler"))
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
        distance = abs((start - e_start).total_seconds())  # the closer to the event start, the more likely
        scored.append(((bool(e["teilnehmer"]), -distance), e))
    return [e for _, e in sorted(scored, key=lambda x: x[0], reverse=True)][:5]


def termin_line(termin: dict, limit: int) -> str:
    people = [p for p in termin.get("teilnehmer", []) if not p.get("ich")]
    names = ", ".join(p["name"] or p["email"] for p in people[:limit])
    more = f" and {len(people) - limit} more" if len(people) > limit else ""
    org = termin.get("organisator") or {}
    return (f"{termin['titel']} ({termin['start'][11:16]}–{termin['ende'][11:16]})"
            + (f"; organizer: {org.get('name') or org.get('email')}" if org else "")
            + (f"; invitees ({len(people)}): {names}{more}" if people else "; no invitees"))


def termin_prompt(termine: list[dict], fixed: bool = False) -> str:
    """Calendar events as context. `fixed`: confirmed by the user, otherwise the model picks via `termin_index`."""
    if not termine:
        return "Calendar events at recording time: none\n"
    if fixed:
        return f"Calendar event (confirmed by the user): {termin_line(termine[0], 20)}\n"
    lines = "\n".join(f"[{i}] {termin_line(t, 10)}" for i, t in enumerate(termine))
    return ("Calendar events around the recording time - which one belongs to this conversation? "
            "Meetings often overrun or are only recorded late; decide by content, title and participants:\n"
            + lines + "\n")


def build_schema(categories: list[str]) -> dict:
    return {
        "type": "object",
        "properties": {
            "kategorie": {"type": "string", "enum": [*categories, "unklar"]},
            "konfidenz": {"type": "number", "minimum": 0, "maximum": 1},
            "begruendung": {"type": "string"},
            "titel": {"type": "string", "description": "Short, descriptive title, max. 60 characters, no date"},
            "projekt": {"type": ["string", "null"]},
            "person": {"type": ["string", "null"],
                       "description": "Supervised person from the project list that the conversation is mainly about"},
            "tags": {"type": "array", "items": {"type": "string"}, "maxItems": 5,
                     "description": "Topic tags, lowercase, without person names"},
            "termin_index": {"type": ["integer", "null"],
                             "description": "Index of the matching calendar event from the list, null if none fits"},
            "sprecher_vorschlaege": {
                "type": "array",
                "description": "Only for placeholder speakers (\"Speaker 1\" ...) marked in the text: who is this "
                               "likely to be? Empty if there are no clues. Write in the language of the conversation.",
                "items": {
                    "type": "object",
                    "properties": {
                        "sprecher": {"type": "string", "description": "e.g. Speaker 1"},
                        "name": {"type": "string"},
                        "hinweis": {"type": "string", "description": "brief, how recognized (form of address, introduction, event)"},
                    },
                    "required": ["sprecher", "name", "hinweis"],
                },
            },
            "zusammenfassung": {
                "type": "string", "minLength": 50,
                "description": "Markdown: 1-2 introductory sentences (what it was about, with whom), then a blank line and "
                               "3-6 bullet points in the format '- **Topic:** key statement'. No headings.",
            },
            "entscheidungen": {"type": "array", "items": {"type": "string"}},
            "todos": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "aufgabe": {"type": "string",
                                    "description": "Short and concrete, start with a verb, at most about 12 words, "
                                                   "no due date and no responsible person in the text"},
                        "verantwortlich": {"type": ["string", "null"]},
                        "faellig": {"type": ["string", "null"], "description": "YYYY-MM-DD or null"},
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
    return f"""You analyze transcripts of conversations and meetings of {cfg['ich']}{role(cfg)}.

Assign the conversation to exactly one category:
{cats}
- unklar: if no category clearly fits

If projects are listed under the category and one fits, set "projekt" to exactly its name,
otherwise to a short project name of your own or null.
If the conversation is mainly about one of the supervised people, set "person" to exactly their name, otherwise null.
Names may be misspelled in the transcript (e.g. "Schwalbe" instead of "Schwalb").

What matters is with whom and for what purpose the conversation is held, not which companies are merely mentioned.
An internal conversation among colleagues in which a customer is only chatted about belongs in the internal category.

The summary is for a quick overview: a short introduction, then bullet points with the topic in bold.
Name numbers, names and dates concretely, no filler sentences.

Extract concrete tasks (todos), commitments and follow-ups. No vague intentions.
Phrase each todo tersely like a list entry ("Create checklist for agentic projects"); details belong
in the summary, due date and responsible person in their own fields.
"ist_meins" = true if {cfg['ich']} takes on the task or it is assigned to him.
You convert relative deadlines ("by Friday") into a date, counting from the recording date.
Speakers in the transcript may only be named "Speaker 1" etc.; use real names when they come up in the conversation.
Under "sprecher_vorschlaege" suggest who is behind a placeholder speaker - only with a concrete clue
(someone is addressed by name, introduces themselves, invitees of the event fit the role). Never guess;
{cfg['ich']} is never a placeholder.
If calendar events are given, pick the one that fits the conversation (termin_index) and use its title and
invitees as a strong clue for assignment and names. Large groups with very many invitees are only
correct if the content fits.
Write the summary ("zusammenfassung"), decisions ("entscheidungen"), todo texts ("aufgabe") and speaker hints
("hinweis") in the language in which the conversation was held (do not translate them to English).
Respond with the JSON as specified by the schema.""" + korrekturen_prompt(cfg)


def gateway_token(gw: dict) -> str:
    """API key for the gateway: from `token_cmd`, `token_env` or directly from `token`."""
    if gw.get("token_cmd"):
        proc = subprocess.run(gw["token_cmd"], shell=True, capture_output=True, text=True)
        if proc.returncode or not proc.stdout.strip():
            raise RuntimeError(f"gateway.token_cmd returned no key: {proc.stderr.strip() or 'empty output'}")
        return proc.stdout.strip()
    if gw.get("token_env"):
        token = os.environ.get(gw["token_env"], "").strip()
        if not token:
            raise RuntimeError(f"Environment variable {gw['token_env']} (gateway.token_env) is not set")
        return token
    if gw.get("token"):
        return str(gw["token"]).strip()
    raise RuntimeError("gateway needs token_cmd, token_env or token")


def claude_env(cfg: dict) -> dict:
    """Like `claude-api` in .zshrc: Claude Code against the configured gateway."""
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
PROVIDERS = {"hosted_vllm": "Self-hosted", "bedrock": "AWS Bedrock", "azure": "Azure",
             "vertex_ai": "Google Cloud", "mistral": "Mistral"}


def role(cfg: dict) -> str:
    """Optional role from `rolle:` for the prompts, e.g. \" (consultant at Acme)\"."""
    return f" ({cfg['rolle']})" if cfg.get("rolle") else ""


def gateway_models(cfg: dict, refresh: bool = False) -> list[dict]:
    """Chat models of the LiteLLM gateway with provider, prices and context (cached for 10 min)."""
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
            log.exception("Model list could not be fetched from the gateway")
            return _MODELS["liste"]
        models = []
        for m in data:
            info = m.get("model_info") or {}
            if info.get("mode") != "chat":
                continue
            backend = (m.get("litellm_params") or {}).get("model", "").split("/")[0]
            models.append({
                "id": m["model_name"],
                "anbieter": PROVIDERS.get(backend, backend or "unknown"),
                "intern": backend == "hosted_vllm",
                "input": info.get("input_cost_per_token") or 0.0,
                "output": info.get("output_cost_per_token") or 0.0,
                "cache_read": info.get("cache_read_input_token_cost"),
                "kontext": info.get("max_input_tokens") or 0,
            })
        _MODELS.update(zeit=time.time(), liste=sorted(models, key=lambda x: (not x["intern"], x["anbieter"], x["id"])))
    return _MODELS["liste"]


def gateway_prices(cfg: dict, model: str) -> dict | None:
    """Per-token prices according to the gateway; None without a gateway or for unknown models."""
    return next((m for m in gateway_models(cfg) if m["id"] == model), None)


def set_default_model(cfg: dict, config_path: Path, model: str) -> None:
    """Set the default model in config.yaml (only the `model:` line, comments are kept)."""
    text = config_path.read_text()
    new, n = re.subn(r"(?m)^model:.*$", f"model: {model}", text, count=1)
    if not n:
        new = text.rstrip("\n") + f"\nmodel: {model}\n"
    config_path.write_text(new)
    cfg["model"] = model


def usage_of(cfg: dict, out: dict, seconds: float, model: str) -> dict:
    """Tokens and costs of an analysis. With a gateway its prices count, otherwise those of the claude CLI."""
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
    """Returns (result, usage with tokens/costs). `names` replaces speaker placeholders beforehand, `termine` are
    calendar events as context - the model picks the matching one, unless `termin_fest` (then exactly one)."""
    reload_kategorien(cfg)
    meta, body = rename_speakers(meta, body, names)
    recorded = meta.get("Recorded At") or meta.get("recorded_at") or ""
    prompt = (
        f"Recording date: {recorded}\n"
        f"Source: {meta.get('Source App', 'unknown')}\n"
        f"Speakers: {', '.join(map(str, meta.get('Speakers') or []))}\n"
        f"{termin_prompt(termine or [], termin_fest)}\n"
        f"<transcript>\n{body}\n</transcript>"
    )
    result, nutzung = run_claude(cfg, build_system_prompt(cfg), prompt, build_schema(list(cfg["kategorien"])), "Analysis", model)
    result = canonical_names(cfg, result)
    # The model sometimes names me as responsible but leaves ist_meins false.
    result["todos"] = [{**t, "ist_meins": bool(t.get("ist_meins")) or is_me(cfg, t.get("verantwortlich"))}
                       for t in result.get("todos", [])]
    return result, nutzung


def run_claude(cfg: dict, system: str, prompt: str, schema: dict, label: str,
               model: str | None = None) -> tuple[dict, dict]:
    """`claude -p` without tools with structured output. Returns (result, usage)."""
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
        raise RuntimeError(f"claude failed ({proc.returncode}): {proc.stderr or proc.stdout}")
    out = json.loads(proc.stdout)
    if out.get("is_error") or not out.get("structured_output"):
        raise RuntimeError(f"No structured response: {out.get('result')}")
    nutzung = usage_of(cfg, out, time.monotonic() - started, model)
    log.info("%s mit %s: %d+%d Tokens, $%.4f, %.0f s", label, model, nutzung["tokens_ein"], nutzung["tokens_aus"],
             nutzung["kosten_usd"], nutzung["sekunden"])
    return out["structured_output"], nutzung


FOLLOWUP_SCHEMA = {
    "type": "object",
    "properties": {
        "betreff": {"type": "string", "description": "Short subject without 'Re:' / 'AW:'"},
        "text": {"type": "string", "minLength": 80,
                 "description": "Mail body as plain text (no Markdown bold), with salutation and sign-off"},
    },
    "required": ["betreff", "text"],
}


def followup(cfg: dict, result: dict, teilnehmer: list[str], anrede: str = "du",
             sprache: str = "de", model: str | None = None) -> tuple[dict, dict]:
    """Draft of a follow-up mail from the reviewed state (summary, decisions, todos)."""
    ich = cfg["ich"]
    en = sprache == "en"
    form = ("informal, first names" if anrede == "du" else "formal") if en else \
           ('informal: address the recipients with "du", first names' if anrede == "du"
            else 'formal: address the recipients with "Sie"')
    system = f"""You write a short follow-up mail for {ich}{role(cfg)} to the participants of a conversation.
Language of the mail: {"English" if en else "German"}. Tone/form of address: {form}.
Structure: salutation to the participants, one sentence of thanks/reference, the most important results in 2-5 bullet points
(lines starting with "- "), decisions made, then the next steps with responsible people and dates,
short closing, sign-off with "{ich}".
Plain text without Markdown bold or headings. Factual, concise, no platitudes.
Only include content meant for the participants: no internal assessments, salaries,
personnel topics or speculation about people. Give dates as a date (e.g. {"10/16" if en else "16.10."}).
The input below (summary, decisions, todos) may be in a different language; write the mail in the language stated above."""
    todos = "\n".join(
        f"- {t['aufgabe']} - {ich if t['ist_meins'] else (t.get('verantwortlich') or 'open')}"
        + (f", due {t['faellig']}" if t.get("faellig") else "") + (" (already done)" if t.get("erledigt") else "")
        for t in result.get("todos", []))
    prompt = (f"Title: {result['titel']}\n"
              f"Participants: {', '.join(teilnehmer) or 'unknown'}\n\n"
              f"Summary:\n{result['zusammenfassung']}\n\n"
              f"Decisions:\n" + ("\n".join(f"- {e}" for e in result.get("entscheidungen", [])) or "none") + "\n\n"
              f"Todos:\n{todos or 'none'}")
    return run_claude(cfg, system, prompt, FOLLOWUP_SCHEMA, "Follow-up", model)


def format_todo(todo: dict, ich: str) -> str:
    line = todo["aufgabe"].strip()
    who = todo.get("verantwortlich")
    if who and not todo["ist_meins"]:
        line += f" (@{who})"
    if todo.get("faellig"):
        line += f" 📅 {todo['faellig']}"  # format of the Obsidian Tasks plugin
    if todo.get("dringend"):
        line += " ⏫"  # Tasks priority "high", shown by Task Board
    if todo.get("erledigt"):  # file directly as done (Tasks: ✅ done date)
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
    """Wikilinks for customer, project, person and participants - Obsidian then shows all conversations there."""
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
    """Bring project and person names to the spelling from categories.yaml (\"pfk\" → \"pFK\")."""
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
    """Creates the finished note in the target folder and removes the original from the inbox."""
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
        log.info("Skipped (already processed): %s", path.name)
        return
    log.info("Analyzing: %s", path.name)
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
    ap.add_argument("files", nargs="*", type=Path, help="Individual files instead of the whole inbox")
    ap.add_argument("--config", type=Path, default=HERE / "config.yaml")
    ap.add_argument("--dry-run", action="store_true", help="Only show, do not write/move anything")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        handlers=[logging.StreamHandler(), logging.FileHandler(HERE / "debrief.log")])
    cfg = load_config(args.config)
    failed = 0
    for path in args.files or pending_files(cfg):
        try:
            process(path, cfg, args.dry_run)
        except Exception:
            log.exception("Error processing %s", path)
            failed += 1
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

"""Existing transcripts as a recording for the live assistant: MacWhisper exports (speaker alone on a line), pasted
Teams chats, WebVTT/SRT, "Name: text" lines and plain text. Returns lines in the shape the live page reads from
MacWhisper's database, with times from the file or, without any, estimated from the speaking speed."""

import datetime as dt
import hashlib
import re
from pathlib import Path

import importer

CUE = re.compile(r"(?P<a>\d{1,2}:\d{2}(?::\d{2})?(?:[.,]\d+)?)\s*-->\s*(?P<b>\d{1,2}:\d{2}(?::\d{2})?(?:[.,]\d+)?)")
STAMP = re.compile(r"^\[?(?P<t>\d{1,2}:\d{2}(?::\d{2})?)\]?\s+(?P<rest>\S.*)$")
NAMED = re.compile(r"^(?P<name>[A-ZÄÖÜ][\w.'-]*(?: [\w.'-]+){0,3}):\s+(?P<text>\S.*)$")
VOICE = re.compile(r"^<v\s+([^>]+)>(.*?)(?:</v>)?$")
WORDS_PER_S = 2.6      # speaking speed for times that have to be estimated
PAUSE_MS = 600         # pause between two lines
MAX_LINE = 220         # characters per line: long turns are split at sentence ends


def parse(text: str, filename: str = "", now: dt.datetime | None = None) -> dict:
    """{"id", "titel", "start", "app", "sprecher": {key: label}, "zeilen": [...]}; ValueError for empty text."""
    text = text.replace("\r\n", "\n").lstrip("﻿")
    if not text.strip():
        raise ValueError("Nothing to import: the text is empty")
    now = (now or dt.datetime.now().astimezone()).replace(microsecond=0)
    meta, body = {}, text
    turns = _cues(text) if CUE.search(text) else None
    if turns is None:
        meta, body, speakers = importer.parse(text, ".md")
        chat = importer.normalize_chat(body)
        turns = _blocks(chat, set(map(str, speakers)) | set(importer.detect_speakers(chat)))
    if not turns:
        raise ValueError("No transcript found in the text")
    start = importer.chat_start(text, now.replace(tzinfo=None)) or importer.recorded_at(
        meta, Path(filename or "Import"), fallback=now.replace(tzinfo=None))
    rid = hashlib.md5(text.encode()).hexdigest().upper()
    zeilen, labels = _lines(rid, turns)
    return {"id": rid, "titel": str(meta.get("Title") or Path(filename).stem or "Transkript"),
            "start": start.astimezone().isoformat(timespec="seconds"),
            "app": "Datei", "quelle": filename or "Eingefügt", "sprecher": labels, "zeilen": zeilen}


def _ms(stamp: str) -> int:
    parts = stamp.replace(",", ".").split(":")
    secs = float(parts[-1]) + 60 * int(parts[-2]) + (3600 * int(parts[-3]) if len(parts) == 3 else 0)
    return int(secs * 1000)


def _cues(text: str) -> list[tuple[str, str, int | None]]:
    """WebVTT / SRT: a time line, then the text (Teams: `<v Name>text`, others `Name: text`)."""
    turns, lines = [], text.splitlines()
    i = 0
    while i < len(lines):
        m = CUE.search(lines[i])
        i += 1
        if not m:
            continue
        chunk = []
        while i < len(lines) and lines[i].strip() and not CUE.search(lines[i]):
            chunk.append(lines[i].strip())
            i += 1
        raw = " ".join(chunk)
        speaker, said = "?", raw
        if v := VOICE.match(raw):
            speaker, said = v[1].strip(), v[2].strip()
        elif n := NAMED.match(raw):
            speaker, said = n["name"], n["text"]
        said = re.sub(r"</?[a-z][^>]*>", "", said).strip()
        if said:
            turns.append((speaker, said, _ms(m["a"])))
    return _merge(turns)


def _blocks(body: str, speakers: set[str]) -> list[tuple[str, str, int | None]]:
    """Speaker alone on a line with the text below, or `[hh:mm:ss] Name: text` / `Name: text` per line, or plain text."""
    turns: list[tuple[str, str, int | None]] = []
    speaker, buf, stamp = "?", [], None

    def flush():
        nonlocal buf
        if buf:
            turns.append((speaker, " ".join(buf), stamp))
        buf = []

    for raw in body.splitlines():
        line = raw.strip()
        if not line:
            if speaker == "?" and not speakers:  # plain text: a blank line ends the paragraph
                flush()
            continue
        if line in speakers:
            flush()
            speaker, stamp = line, None
            continue
        at = None
        if s := STAMP.match(line):
            at, line = _ms(s["t"]), s["rest"]
        if not speakers and (n := NAMED.match(line)):
            flush()
            speaker, line, stamp = n["name"], n["text"], at
        elif at is not None and not buf:
            stamp = at
        buf.append(line)
    flush()
    return _merge(turns)


def _merge(turns: list[tuple[str, str, int | None]]) -> list[tuple[str, str, int | None]]:
    """Consecutive turns of the same speaker are one."""
    out: list[tuple[str, str, int | None]] = []
    for speaker, text, at in turns:
        if out and out[-1][0] == speaker != "?" and at is None:
            out[-1] = (speaker, out[-1][1] + " " + text, out[-1][2])
        else:
            out.append((speaker, text, at))
    return out


def _chunks(text: str) -> list[str]:
    out, cur = [], ""
    for sentence in re.split(r"(?<=[.!?…])\s+", text.strip()):
        if cur and len(cur) + len(sentence) > MAX_LINE:
            out.append(cur)
            cur = ""
        cur = f"{cur} {sentence}".strip()
    return out + [cur] if cur else out


def _lines(rid: str, turns: list[tuple[str, str, int | None]]) -> tuple[list[dict], dict[str, str]]:
    zeilen, labels, clock, n = [], {"": "?"}, 0, 0
    for speaker, text, at in turns:
        key = "" if speaker == "?" else hashlib.md5(speaker.encode()).hexdigest()[:16].upper()
        if key:
            labels[key] = speaker
        if at is not None:
            clock = max(clock, at)  # times from the file win; never run backwards
        for chunk in _chunks(text):
            length = int(len(chunk.split()) / WORDS_PER_S * 1000)
            zeilen.append({"id": f"{rid[:12]}{n:08d}", "nr": n, "start_ms": clock, "ende_ms": clock + length, "text": chunk,
                           "sprecher": key, "bestaetigt": True, "stand": ""})
            clock += length + PAUSE_MS
            n += 1
    return zeilen, labels

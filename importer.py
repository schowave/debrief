"""Import older transcripts (MacWhisper exports as .md or .txt) from folders into the inbox.

The originals stay unchanged: importing puts a copy with MacWhisper-like frontmatter into the
inbox, after which the normal flow runs (duplicate check, analysis, review).
"""

from __future__ import annotations

import datetime as dt
import os
import re
import time
from pathlib import Path

import yaml

from debrief import fingerprint, split_frontmatter

ENDUNGEN = (".md", ".txt")
MAX_DATEIEN = 5000
MAX_BYTES = 3_000_000
DATE_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})(?:[ _T]+(\d{1,2})[.:\-h](\d{2}))?")
SPEAKER_LINE = re.compile(r"^[\w.' -]{2,40}$")

_cache: dict[str, tuple[float, dict]] = {}  # path -> (mtime, entry)


def folders(cfg: dict) -> list[Path]:
    return [Path(f).expanduser() for f in (cfg.get("import") or {}).get("ordner", []) if f]


def allowed(cfg: dict, path: Path) -> bool:
    path = path.resolve()
    return path.suffix.lower() in ENDUNGEN and any(path.is_relative_to(f.resolve()) for f in folders(cfg))


def detect_speakers(body: str) -> list[str]:
    """MacWhisper format: speaker name alone on a line, the text below it."""
    lines = [l.strip() for l in body.splitlines()]
    counts: dict[str, int] = {}
    for i, line in enumerate(lines[:-1]):
        nxt = lines[i + 1]
        words = line.split()
        if line and nxt and SPEAKER_LINE.match(line) and len(words) <= 4 and not line.endswith((".", "?", "!", ",")) \
                and all(w[0].isupper() or w[0].isdigit() for w in words) and len(nxt.split()) >= 3:
            counts[line] = counts.get(line, 0) + 1
    return list(counts)


def recorded_at(meta: dict, path: Path, fallback: dt.datetime | None = None) -> dt.datetime:
    value = meta.get("Recorded At")
    if isinstance(value, dt.datetime):
        return value.replace(tzinfo=None)
    if isinstance(value, str):
        try:
            return dt.datetime.fromisoformat(value)
        except ValueError:
            pass
    if m := DATE_RE.search(path.stem):
        y, mo, d, h, mi = m.groups()
        try:
            return dt.datetime(int(y), int(mo), int(d), int(h or 0), int(mi or 0))
        except ValueError:
            pass
    if fallback:
        return fallback.replace(microsecond=0)
    st = path.stat()
    return dt.datetime.fromtimestamp(getattr(st, "st_birthtime", st.st_mtime)).replace(microsecond=0)


def read(path: Path) -> tuple[dict, str, list[str]]:
    return parse(path.read_text(errors="replace"), path.suffix)


def parse(text: str, suffix: str) -> tuple[dict, str, list[str]]:
    meta, body = split_frontmatter(text) if suffix.lower() == ".md" else ({}, text)
    if "## Transkript" in body:  # note already filed by Debrief
        body = body.split("## Transkript", 1)[1]
    speakers = [str(s) for s in meta.get("Speakers") or []] or detect_speakers(body)
    return meta, body.strip(), speakers


def entry(cfg: dict, path: Path, root: Path) -> dict | None:
    st = path.stat()
    key = str(path)
    if key in _cache and _cache[key][0] == st.st_mtime:
        return _cache[key][1]
    if st.st_size > MAX_BYTES:
        return None
    try:
        meta, body, speakers = read(path)
    except (OSError, yaml.YAMLError):
        return None
    fp = fingerprint(body, speakers, meta)
    e = {
        "id": key,
        "name": path.stem,
        "ordner": str(path.parent.relative_to(root)) if path.parent != root else "",
        "wurzel": root.name,
        "datum": recorded_at(meta, path).isoformat(timespec="minutes"),
        "woerter": fp["woerter"],
        "sprecher": speakers,
        "hash": fp["hash"],
        "_text": (path.stem + "\n" + body).lower(),
        "_body": body,
    }
    _cache[key] = (st.st_mtime, e)
    return e


def index(cfg: dict) -> list[dict]:
    out, seen = [], set()  # folders may be nested
    for root in folders(cfg):
        if not root.is_dir():
            continue
        for path in root.rglob("*"):
            if len(out) >= MAX_DATEIEN:
                break
            if path.is_file() and path.suffix.lower() in ENDUNGEN and not any(p.startswith(".") for p in path.parts) \
                    and path.resolve() not in seen:
                seen.add(path.resolve())
                if e := entry(cfg, path, root):
                    out.append(e)
    return sorted(out, key=lambda e: e["datum"], reverse=True)


def snippet(e: dict, terms: list[str], width: int = 90) -> str:
    body = e["_body"]
    low = body.lower()
    pos = min((low.find(t) for t in terms if low.find(t) >= 0), default=-1)
    if pos < 0:
        text = " ".join(body.split())
        return text[:width * 2] + ("…" if len(text) > width * 2 else "")
    start = max(0, pos - width)
    text = " ".join(body[start:pos + width].split())
    return ("…" if start else "") + text + ("…" if pos + width < len(body) else "")


def search(cfg: dict, query: str, known_hashes: set[str], limit: int = 200) -> dict:
    terms = [t for t in query.lower().split() if t]
    entries = index(cfg)
    hits = [e for e in entries if all(t in e["_text"] for t in terms)]
    return {
        "gesamt": len(entries),
        "treffer": len(hits),
        "dateien": [{**{k: v for k, v in e.items() if not k.startswith("_")},
                     "ausschnitt": snippet(e, terms), "schon_da": e["hash"] in known_hashes} for e in hits[:limit]],
    }


def preview(cfg: dict, path: Path) -> str:
    if not allowed(cfg, path):
        raise PermissionError("File is not in an import folder")
    return read(path)[1]


def to_inbox(cfg: dict, path: Path) -> str:
    """Put a copy with MacWhisper-like frontmatter into the inbox; returns the file name."""
    if not allowed(cfg, path):
        raise PermissionError("File is not in an import folder")
    meta, body, speakers = read(path)
    return write_inbox(cfg, path.stem, meta, body, speakers, recorded_at(meta, path), str(path))


def upload_to_inbox(cfg: dict, filename: str, text: str, modified_ms: int | None) -> str:
    """Put a file uploaded via drag & drop into the inbox."""
    path = Path(Path(filename).name)  # name only, no path from the browser
    if path.suffix.lower() not in ENDUNGEN:
        raise ValueError(f"{path.name}: only {', '.join(ENDUNGEN)} are supported")
    if len(text.encode()) > MAX_BYTES:
        raise ValueError(f"{path.name}: too large")
    meta, body, speakers = parse(text, path.suffix)
    if not body.strip():
        raise ValueError(f"{path.name}: empty")
    modified = dt.datetime.fromtimestamp(modified_ms / 1000) if modified_ms else dt.datetime.now()
    return write_inbox(cfg, path.stem, meta, body, speakers, recorded_at(meta, path, modified), f"Upload: {path.name}")


def write_inbox(cfg: dict, stem: str, meta: dict, body: str, speakers: list[str], recorded: dt.datetime,
                origin: str) -> str:
    inbox = cfg["vault"] / cfg["inbox"]
    front = {
        "Title": meta.get("Title") or stem,
        "Recorded At": recorded,
        **({"Duration": meta["Duration"]} if meta.get("Duration") else {}),
        **({"Speakers": speakers} if speakers else {}),
        "Source App": meta.get("Source App") or "Import",
        "Importiert aus": origin,
    }
    name = re.sub(r'[\\/:*?"<>|#^\[\]]', "", stem).strip() or "Import"
    dest, n = inbox / f"{name}.md", 2
    while dest.exists():
        dest, n = inbox / f"{name} ({n}).md", n + 1
    dest.write_text("---\n" + yaml.safe_dump(front, sort_keys=False, allow_unicode=True, width=10_000) + "---\n\n" + body + "\n")
    old = time.time() - 120  # counts as fully written immediately; the inbox check picks it up on the next pass
    os.utime(dest, (old, old))
    return dest.name


def choose_folders() -> list[str]:
    """macOS folder dialog (multiple selection); empty on cancel."""
    import subprocess
    script = ('activate\n'
              'set picked to choose folder with prompt "Choose folder with transcripts" with multiple selections allowed\n'
              'set out to ""\n'
              'repeat with f in picked\n  set out to out & POSIX path of f & linefeed\nend repeat\n'
              'return out')
    proc = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=600)
    if proc.returncode != 0:  # -128 = cancelled
        if "-128" in proc.stderr:
            return []
        raise RuntimeError(proc.stderr.strip() or "Folder dialog failed")
    return [line.rstrip("/") for line in proc.stdout.splitlines() if line.strip()]


def set_folders(cfg: dict, config_path: Path, ordner: list[str]) -> list[str]:
    """Validate import folders and write them as `import.ordner` to config.yaml (remaining text stays unchanged)."""
    clean = []
    for f in ordner:
        f = str(f).strip()
        if not f:
            continue
        if not Path(f).expanduser().is_dir():
            raise ValueError(f"Folder not found: {f}")
        clean.append(f)
    text = config_path.read_text()
    text = re.sub(r"(?ms)^import:\n(?:[ \t]+.*\n|\n)*", "", text).rstrip("\n")
    block = "\n\n# Folders with older transcripts for “Import” on the review page\nimport:\n  ordner:\n" \
            + "".join(f"    - {yaml.safe_dump(f, allow_unicode=True).splitlines()[0]}\n" for f in clean)
    config_path.write_text(text + block)
    cfg.setdefault("import", {})["ordner"] = clean
    return clean

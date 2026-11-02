"""Draws the live assistant's event storming board on a Miro board (REST API v2), one way: debrief → Miro.

All processes of a session go into one frame, like the big picture on the live page: one row per lane (actor or
bounded context), the elements along the time line, sticky colours after the event storming convention, arrows
for the connections and the time line, and the open questions as a numbered list. The Miro ids are remembered in
the session, so the next sync updates the same stickies instead of drawing a second board; items deleted in Miro
are drawn again.
"""

import json
import os
import re
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Callable

API = "https://api.miro.com/v2"
AUTHORIZE_URL = "https://miro.com/oauth/authorize"
TOKEN_URL = "https://api.miro.com/v1/oauth/token"

# Miro only knows named sticky colours
FARBE = {"event": "orange", "command": "light_blue", "actor": "light_yellow", "policy": "violet",
         "readmodel": "light_green", "system": "light_pink", "aggregate": "yellow", "hotspot": "red"}
TYP_NAME = {"event": "Domain Event", "command": "Command", "actor": "Akteur", "policy": "Policy",
            "readmodel": "Read Model", "system": "Externes System", "aggregate": "Aggregat", "hotspot": "Hotspot"}

STICKY = 200          # sticky width; Miro makes square stickies about as high as wide
SPALTE = 250          # column width per element
ZEILE = 280           # row height per lane
LABEL = 260           # width of the lane label column
KOPF = 120            # band for title and process names
RAND = 60
OFFEN = ("neu", "angepinnt", "gestellt")  # as live.OFFEN: these get hotspot numbers like on the board


class MiroError(RuntimeError):
    pass


class LoginNeeded(MiroError):
    """No (valid) OAuth token: the user has to connect debrief with Miro first."""


def token_request(params: dict) -> dict:
    """POST to Miro's OAuth token endpoint (parameters in the query string, as Miro documents it)."""
    req = urllib.request.Request(TOKEN_URL + "?" + urllib.parse.urlencode(params), method="POST", data=b"",
                                 headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        try:
            msg = json.load(e).get("message") or e.reason
        except ValueError:
            msg = e.reason
        raise MiroError(f"Miro login failed: {msg}") from None
    except urllib.error.URLError as e:
        raise MiroError(f"Miro not reachable: {e.reason}") from None


class OAuth:
    """Authorization code flow with a redirect to this machine (http://localhost:<port>/miro/callback). The tokens
    live in `path` (readable only by you) and are refreshed shortly before they expire."""

    def __init__(self, client_id: str, secret: Callable[[], str], redirect_uri: str, path: Path,
                 post: Callable[[dict], dict] = token_request):
        self.client_id, self.secret, self.redirect_uri, self.path, self.post = client_id, secret, redirect_uri, path, post
        self.states: dict[str, float] = {}
        self.lock = threading.Lock()

    def login_url(self) -> str:
        state = secrets.token_urlsafe(24)
        now = time.time()
        self.states = {k: v for k, v in self.states.items() if now - v < 900}
        self.states[state] = now
        return AUTHORIZE_URL + "?" + urllib.parse.urlencode(
            {"response_type": "code", "client_id": self.client_id, "redirect_uri": self.redirect_uri, "state": state})

    def callback(self, code: str, state: str) -> None:
        if not code or self.states.pop(state or "", None) is None:
            raise MiroError("Miro login expired or not started here – please try again")
        self.save(self.post({"grant_type": "authorization_code", "client_id": self.client_id,
                             "client_secret": self.secret(), "code": code, "redirect_uri": self.redirect_uri}))

    def save(self, res: dict, refresh_token: str | None = None) -> None:
        if not res.get("access_token"):
            raise MiroError("Miro sent no access token")
        data = {"access_token": res["access_token"], "refresh_token": res.get("refresh_token") or refresh_token,
                "expires_at": time.time() + float(res["expires_in"]) if res.get("expires_in") else None,
                "team_id": res.get("team_id"), "user_id": res.get("user_id")}
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2))
        os.chmod(tmp, 0o600)
        tmp.replace(self.path)

    def load(self) -> dict | None:
        try:
            return json.loads(self.path.read_text())
        except (FileNotFoundError, ValueError):
            return None

    def connected(self) -> bool:
        return bool(self.load())

    def forget(self) -> None:
        self.path.unlink(missing_ok=True)

    def access_token(self) -> str:
        with self.lock:
            data = self.load()
            if not data:
                raise LoginNeeded("Not connected to Miro yet")
            if data.get("expires_at") and data["expires_at"] - 120 < time.time():
                if not data.get("refresh_token"):
                    self.forget()
                    raise LoginNeeded("Miro login expired")
                try:
                    self.save(self.post({"grant_type": "refresh_token", "client_id": self.client_id,
                                         "client_secret": self.secret(), "refresh_token": data["refresh_token"]}),
                              data["refresh_token"])
                except MiroError:
                    self.forget()
                    raise LoginNeeded("Miro login expired") from None
                data = self.load()
            return data["access_token"]


def board_id(link: str) -> str:
    """Board id from a board link (https://miro.com/app/board/uXjV…=/) or the id itself."""
    link = (link or "").strip()
    m = re.search(r"/board/([^/?#]+)", link)
    bid = m[1] if m else link
    if not re.fullmatch(r"[\w=+\-%]{6,}", bid):
        raise ValueError("Not a Miro board link")
    return urllib.request.unquote(bid)


def board_url(bid: str, item: str | None = None) -> str:
    return f"https://miro.com/app/board/{bid}/" + (f"?moveToWidget={item}" if item else "")


def http_client(token: str) -> Callable[[str, str, dict | None], dict]:
    """`request(method, path, body)` against the Miro API; raises MiroError, with `.status` for HTTP errors."""
    def request(method: str, path: str, body: dict | None = None) -> dict:
        req = urllib.request.Request(API + path, method=method, data=None if body is None else json.dumps(body).encode(),
                                     headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json",
                                              "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                raw = r.read()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            try:
                msg = json.load(e).get("message") or e.reason
            except ValueError:
                msg = e.reason
            err = MiroError({401: "Miro token is invalid or expired", 403: "No access to this Miro board",
                             404: "Not found on the Miro board"}.get(e.code, f"Miro: {msg}"))
            err.status = e.code
            raise err from None
        except urllib.error.URLError as e:
            raise MiroError(f"Miro not reachable: {e.reason}") from None
    return request


def layout(s: dict) -> dict:
    """Positions relative to the frame's top-left corner, the same arrangement as the big picture."""
    procs = [p for p in s["prozesse"] if p["elemente"]]
    elemente = [(p, e) for p in procs for e in p["elemente"]]
    lanes = list(dict.fromkeys(e["lane"] for _, e in elemente)) or [""]
    nums, n = {}, 0
    for x in s["eintraege"]:
        if x["status"] in OFFEN and x.get("elemente"):
            n += 1
            nums[x["id"]] = n
    hot: dict[str, list[int]] = {}
    for x in s["eintraege"]:
        for el in x.get("elemente", []) if x["id"] in nums else []:
            hot.setdefault(el, []).append(nums[x["id"]])
    items = {}
    for i, lane in enumerate(lanes):
        items[f"lane:{lane}"] = {"art": "text", "text": f"<p><strong>{esc(lane or 'ohne Lane')}</strong></p>",
                                 "x": RAND + LABEL / 2, "y": KOPF + ZEILE * i + ZEILE / 2, "w": LABEL - 40}
    col = 0
    for p in procs:
        items[f"proc:{p['id']}"] = {"art": "text", "text": f"<p><strong>{esc(p['name'])}</strong></p>",
                                    "x": RAND + LABEL + SPALTE * col + SPALTE * len(p["elemente"]) / 2, "y": KOPF - 40,
                                    "w": max(SPALTE * len(p["elemente"]) - 40, 200)}
        for e in p["elemente"]:
            zeilen = [f"<p><strong>{TYP_NAME.get(e['typ'], e['typ'])}</strong></p>", f"<p>{esc(e['text'])}</p>"]
            if e.get("annahme"):
                zeilen.append("<p><em>Annahme</em></p>")
            if hot.get(e["id"]):
                zeilen.append("<p>⚠ " + ", ".join(map(str, hot[e["id"]])) + "</p>")
            items[f"el:{e['id']}"] = {"art": "sticky", "text": "".join(zeilen), "farbe": FARBE.get(e["typ"], "gray"),
                                      "x": RAND + LABEL + SPALTE * col + SPALTE / 2,
                                      "y": KOPF + ZEILE * lanes.index(e["lane"]) + ZEILE / 2}
            col += 1
    breite = RAND * 2 + LABEL + SPALTE * max(col, 2)
    fragen = [x for x in s["eintraege"] if x["status"] in OFFEN and x["typ"] in ("frage", "offen", "annahme")]
    unten = KOPF + ZEILE * len(lanes)
    if fragen:
        liste = "".join(f"<p>{nums[x['id']]}. {esc(x['text'])}</p>" if x["id"] in nums else f"<p>• {esc(x['text'])}</p>"
                        for x in sorted(fragen, key=lambda x: nums.get(x["id"], 10**6)))
        items["fragen"] = {"art": "text", "text": "<p><strong>Offene Fragen</strong></p>" + liste,
                           "x": RAND + (breite - 2 * RAND) / 2, "y": unten + 40 + 14 * len(fragen),
                           "w": breite - 2 * RAND}
        unten += 120 + 28 * len(fragen)
    pfeile = {}
    for p in procs:
        for v in p["verbindungen"]:
            pfeile[f"{v['von']}>{v['nach']}"] = {"von": v["von"], "nach": v["nach"], "gestrichelt": False}
        out, inc = {v["von"] for v in p["verbindungen"]}, {v["nach"] for v in p["verbindungen"]}
        for a, b in zip(p["elemente"], p["elemente"][1:]):
            if a["id"] not in out and b["id"] not in inc:
                pfeile[f"{a['id']}>{b['id']}"] = {"von": a["id"], "nach": b["id"], "gestrichelt": True}
    return {"items": items, "pfeile": pfeile, "breite": breite, "hoehe": unten + RAND,
            "titel": f"Event Storming: {s.get('titel') or 'Meeting'}"}


def esc(text: str) -> str:
    return (text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def sync(s: dict, request: Callable, board: str) -> dict:
    """Bring the Miro board up to date with the session; returns the new `s["miro"]` state."""
    bid = board_id(board)
    alt = s.get("miro") or {}
    state = {"board": bid, "frame": None, "items": {}, "pfeile": {}}
    if alt.get("board") == bid:
        state.update(frame=alt.get("frame"), items=dict(alt.get("items", {})), pfeile=dict(alt.get("pfeile", {})))
    plan = layout(s)
    base = f"/boards/{urllib.request.quote(bid, safe='')}"

    frame = {"data": {"title": plan["titel"], "format": "custom", "type": "freeform"},
             "geometry": {"width": plan["breite"], "height": plan["hoehe"]}}
    if state["frame"]:
        try:
            request("PATCH", f"{base}/frames/{state['frame']}", frame)
        except MiroError as e:
            if getattr(e, "status", None) != 404:
                raise
            state.update(frame=None, items={}, pfeile={})
    if not state["frame"]:
        frame["position"] = free_spot(request, base, plan["breite"], plan["hoehe"])
        state["frame"] = request("POST", f"{base}/frames", frame)["id"]

    def body(item: dict) -> dict:
        pos = {"x": item["x"], "y": item["y"], "origin": "center"}
        if item["art"] == "sticky":
            return {"data": {"content": item["text"], "shape": "square"}, "style": {"fillColor": item["farbe"]},
                    "position": pos, "geometry": {"width": STICKY}, "parent": {"id": state["frame"]}}
        return {"data": {"content": item["text"]}, "style": {"fontSize": "24", "textAlign": "left"},
                "position": pos, "geometry": {"width": item["w"]}, "parent": {"id": state["frame"]}}

    path = {"sticky": "sticky_notes", "text": "texts"}
    created = set()  # connectors at re-created stickies died with the old sticky
    for key, item in plan["items"].items():
        known = state["items"].get(key)
        if known and known["art"] == item["art"]:
            try:
                request("PATCH", f"{base}/{path[item['art']]}/{known['id']}", body(item))
                continue
            except MiroError as e:
                if getattr(e, "status", None) != 404:
                    raise
        state["items"][key] = {"id": request("POST", f"{base}/{path[item['art']]}", body(item))["id"], "art": item["art"]}
        created.add(key)
    for key in list(state["items"]):
        if key not in plan["items"]:
            delete(request, f"{base}/items/{state['items'].pop(key)['id']}")

    for key, p in plan["pfeile"].items():
        a, b = state["items"][f"el:{p['von']}"], state["items"][f"el:{p['nach']}"]
        style = {"strokeStyle": "dashed" if p["gestrichelt"] else "normal", "endStrokeCap": "arrow", "strokeColor": "#6b7280"}
        known = state["pfeile"].get(key)
        if known and not created & {f"el:{p['von']}", f"el:{p['nach']}"}:
            try:
                request("PATCH", f"{base}/connectors/{known['id']}", {"style": style})
                state["pfeile"][key] = {**known, "gestrichelt": p["gestrichelt"]}
                continue
            except MiroError as e:
                if getattr(e, "status", None) != 404:
                    raise
        res = request("POST", f"{base}/connectors", {"startItem": {"id": a["id"], "snapTo": "right"},
                                                     "endItem": {"id": b["id"], "snapTo": "left"},
                                                     "shape": "curved", "style": style})
        state["pfeile"][key] = {"id": res["id"], "gestrichelt": p["gestrichelt"]}
    for key in list(state["pfeile"]):
        if key not in plan["pfeile"]:
            delete(request, f"{base}/connectors/{state['pfeile'].pop(key)['id']}")
    state["url"] = board_url(bid, state["frame"])
    return state


def delete(request: Callable, path: str) -> None:
    try:
        request("DELETE", path, None)
    except MiroError as e:
        if getattr(e, "status", None) != 404:  # already gone in Miro
            raise


def free_spot(request: Callable, base: str, breite: float, hoehe: float) -> dict:
    """Centre of a new frame to the right of the existing frames, so nothing on the board gets covered."""
    rechts, cursor = None, None
    for _ in range(20):
        res = request("GET", f"{base}/items?type=frame&limit=50" + (f"&cursor={cursor}" if cursor else ""), None)
        for f in res.get("data", []):
            pos, geo = f.get("position") or {}, f.get("geometry") or {}
            r = pos.get("x", 0) + geo.get("width", 0) / 2
            rechts = r if rechts is None else max(rechts, r)
        cursor = res.get("cursor")
        if not cursor:
            break
    x = (rechts + 400 + breite / 2) if rechts is not None else 0
    return {"x": x, "y": 0 if rechts is None else hoehe / 2, "origin": "center"}

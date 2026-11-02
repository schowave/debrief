"""Miro sync against a fake Miro API: first draw, update in place, re-draw of deleted items, removal."""

import itertools
import urllib.parse

import pytest

import live
import miro


class FakeMiro:
    def __init__(self):
        self.items, self.calls, self.ids = {}, [], (f"m{i}" for i in itertools.count(1))

    def __call__(self, method, path, body):
        self.calls.append((method, path))
        parts = path.split("/")  # "", "boards", <id>, <kind>, [<item>]
        kind = parts[3].split("?")[0]
        if method == "GET":
            return {"data": [{"position": {"x": 0}, "geometry": {"width": 1000}}] if kind == "items" else []}
        if method == "POST":
            iid = next(self.ids)
            self.items[iid] = (kind, body)
            return {"id": iid}
        iid = parts[4]
        if iid not in self.items:
            err = miro.MiroError("Not found on the Miro board")
            err.status = 404
            raise err
        if method == "DELETE":
            self.remove(iid)
        else:
            self.items[iid] = (self.items[iid][0], body)
        return {}

    def remove(self, iid):
        """Like Miro: deleting an item also deletes the connectors attached to it."""
        del self.items[iid]
        for cid, (kind, b) in list(self.items.items()):
            if kind == "connectors" and iid in (b["startItem"]["id"], b["endItem"]["id"]):
                del self.items[cid]

    def of(self, kind):
        return [b for k, b in self.items.values() if k == kind]


def session():
    s = live.new_session({"id": "D" * 32, "titel": "Workshop", "start": "2026-10-30T13:00:00+01:00"}, {})
    live.apply_analysis(s, {"aenderungen": [
        {"op": "prozess_neu", "ref": "a", "text": "Projekt anlegen"},
        {"op": "neu", "prozess": "a", "ref": "e1", "typ": "command", "text": "Projekt anlegen", "lane": "R&D Manager"},
        {"op": "neu", "prozess": "a", "ref": "e2", "typ": "event", "text": "Projekt angelegt", "lane": "R&D Manager"},
        {"op": "prozess_neu", "ref": "b", "text": "Kapazitäten"},
        {"op": "neu", "prozess": "b", "ref": "e3", "typ": "command", "text": "Kapazität pflegen", "lane": "Agile Manager"},
    ], "eintraege": [{"typ": "frage", "text": "Wer ordnet Ressourcengruppen zu?", "elemente": ["e2"]}]}, 1)
    return s


def test_board_id_from_link():
    assert miro.board_id("https://miro.com/app/board/uXjVK1abcd=/?share_link_id=1") == "uXjVK1abcd="
    with pytest.raises(ValueError):
        miro.board_id("https://example.com")


def test_first_sync_draws_big_picture_in_a_frame():
    s, api = session(), FakeMiro()
    state = miro.sync(s, api, "https://miro.com/app/board/uXjVK1abcd=/")
    frame = api.of("frames")[0]
    assert frame["data"]["title"] == "Event Storming: Workshop" and frame["position"]["x"] > 500  # right of existing frames
    stickies = api.of("sticky_notes")
    assert [b["style"]["fillColor"] for b in stickies] == ["light_blue", "orange", "light_blue"]
    assert all(b["parent"]["id"] == state["frame"] for b in stickies)
    lanes_y = {b["data"]["content"]: b["position"]["y"] for b in stickies}
    assert len({y for y in lanes_y.values()}) == 2  # two actors, two lanes
    assert "⚠ 1" in stickies[1]["data"]["content"]
    texts = " ".join(b["data"]["content"] for b in api.of("texts"))
    assert "R&amp;D Manager" in texts and "1. Wer ordnet Ressourcengruppen zu?" in texts
    # the time line inside each process only, not from one process into the next
    assert len(api.of("connectors")) == 1
    assert state["url"].endswith(f"moveToWidget={state['frame']}")


def test_second_sync_updates_in_place_and_redraws_deleted():
    s, api = session(), FakeMiro()
    s["miro"] = miro.sync(s, api, "uXjVK1abcd=")
    count = len(api.items)
    sticky = s["miro"]["items"][f"el:{s['prozesse'][0]['elemente'][1]['id']}"]["id"]
    api.remove(sticky)  # someone deleted it in Miro
    live.edit_process(s, {"op": "element_aendern", "id": s["prozesse"][1]["elemente"][0]["id"], "text": "Kapazität eintragen"})
    s["miro"] = miro.sync(s, api, "uXjVK1abcd=")
    assert len(api.items) == count
    assert any("Kapazität eintragen" in b["data"]["content"] for b in api.of("sticky_notes"))
    assert len(api.of("frames")) == 1 and len(api.of("connectors")) == 1


def test_removed_element_disappears_from_miro():
    s, api = session(), FakeMiro()
    s["miro"] = miro.sync(s, api, "uXjVK1abcd=")
    live.edit_process(s, {"op": "element_loeschen", "id": s["prozesse"][0]["elemente"][1]["id"]})
    s["miro"] = miro.sync(s, api, "uXjVK1abcd=")
    assert len(api.of("sticky_notes")) == 2 and not api.of("connectors")


def oauth(tmp_path, responses):
    calls = []

    def post(params):
        calls.append(params)
        return responses.pop(0)
    auth = miro.OAuth("cid", lambda: "geheim", "http://localhost:8765/miro/callback", tmp_path / "miro_token.json", post)
    return auth, calls


def test_oauth_login_callback_and_refresh(tmp_path, monkeypatch):
    auth, calls = oauth(tmp_path, [{"access_token": "a1", "refresh_token": "r1", "expires_in": 3600},
                                   {"access_token": "a2", "expires_in": 3600}])
    with pytest.raises(miro.LoginNeeded):
        auth.access_token()
    url = auth.login_url()
    state = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(url).query))
    assert state["redirect_uri"] == "http://localhost:8765/miro/callback" and state["client_id"] == "cid"
    with pytest.raises(miro.MiroError):
        auth.callback("code", "fremder-state")
    auth.callback("code", state["state"])
    assert calls[0] == {"grant_type": "authorization_code", "client_id": "cid", "client_secret": "geheim",
                        "code": "code", "redirect_uri": "http://localhost:8765/miro/callback"}
    assert auth.access_token() == "a1" and oct((tmp_path / "miro_token.json").stat().st_mode)[-3:] == "600"
    with pytest.raises(miro.MiroError):  # a state works only once
        auth.callback("code", state["state"])
    monkeypatch.setattr(miro.time, "time", lambda: 10**10)  # far in the future: the token has expired
    assert auth.access_token() == "a2" and calls[1]["refresh_token"] == "r1"
    assert auth.load()["refresh_token"] == "r1"  # Miro sent no new one, the old one stays


def test_oauth_failed_refresh_asks_for_login_again(tmp_path, monkeypatch):
    auth, _ = oauth(tmp_path, [])
    auth.save({"access_token": "a1", "refresh_token": "r1", "expires_in": 10})

    def fail(params):
        raise miro.MiroError("Miro login failed: invalid_grant")
    auth.post = fail
    monkeypatch.setattr(miro.time, "time", lambda: 10**10)
    with pytest.raises(miro.LoginNeeded):
        auth.access_token()
    assert not auth.connected()


def test_manager_uses_oauth_when_client_id_is_configured(tmp_path):
    lm = live.LiveManager({"kategorien": {}, "review": {"port": 9999},
                           "miro": {"client_id": "cid", "client_secret": "geheim"}}, directory=tmp_path / "live")
    lm.miro_token_file = tmp_path / "miro_token.json"
    status = lm.miro_status()
    assert status == {"miro": True, "miro_oauth": True, "miro_verbunden": False, "miro_board": ""}
    assert lm.miro_auth().redirect_uri == "http://localhost:9999/miro/callback"
    assert lm.miro_auth().secret() == "geheim"

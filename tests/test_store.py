import json, sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import pytest
from app.config import Store, Config, gemini_params


def test_legacy_flat_config_becomes_event_1(tmp_path):
    p = tmp_path / "c.json"
    p.write_text(json.dumps({"officer": "Budi", "spreadsheet_url": "u", "min_amount": 10000,
                             "gemini_model": "gemini-2.5-flash", "gemini_api_key": "K"}))
    s = Store.load(p); c = s.config_for("Event 1")
    assert (c.officer, c.spreadsheet_url, c.min_amount, c.gemini_api_key) == ("Budi", "u", 10000, "K")
    assert c.gemini_model == "gemini-3.5-flash-lite"            # model lama dipindah
    assert s.names() == ["Event 1"] and "officer" in s.globals and "min_amount" not in s.globals

def test_events_have_own_rules_but_share_global(tmp_path):
    s = Store.load(tmp_path / "c.json"); s.path = tmp_path / "c.json"
    s.globals["officer"] = "Budi"; s.globals["gemini_api_key"] = "K"
    a = s.config_for("Event 1"); a.min_amount = 10000; a.spreadsheet_url = "A"; s.update_event(a)
    b_name = s.add("Event B"); b = s.config_for(b_name); b.min_amount = 50000; b.spreadsheet_url = "B"; s.update_event(b)
    s.save(); s2 = Store.load(tmp_path / "c.json")
    assert s2.config_for("Event 1").min_amount == 10000 and s2.config_for("Event B").min_amount == 50000
    assert s2.config_for("Event 1").spreadsheet_url == "A" and s2.config_for("Event B").spreadsheet_url == "B"
    assert s2.config_for("Event 1").officer == s2.config_for("Event B").officer == "Budi"

def test_update_event_never_clobbers_global_edits(tmp_path):
    s = Store.load(tmp_path / "c.json")
    stale = s.config_for("Event 1")                    # salinan lama (belum tahu pengaturan umum baru)
    s.globals["gemini_paid"] = True                    # admin mengubah pengaturan umum
    stale.min_amount = 5000; s.update_event(stale)     # lalu event menyimpan dirinya
    assert s.globals["gemini_paid"] is True and s.config_for("Event 1").gemini_paid is True

def test_duplicate_rename_delete_rules(tmp_path):
    s = Store.load(tmp_path / "c.json")
    e = s.config_for("Event 1"); e.min_amount = 7000; e.spreadsheet_url = "X"; s.update_event(e)
    dup = s.add("Event 1 (salinan)", copy_from="Event 1")
    assert s.config_for(dup).min_amount == 7000 and s.config_for(dup).name == dup
    assert s.add("Event 1") == "Event 1 2"             # nama selalu unik
    s.rename(dup, "Rapat"); assert "Rapat" in s.names()
    with pytest.raises(ValueError): s.rename("Rapat", "Event 1")
    for n in list(s.names())[1:]: s.delete(n)
    with pytest.raises(ValueError): s.delete(s.names()[0])      # minimal satu event

def test_gemini_params_free_vs_paid():
    c = Config()
    assert gemini_params(c) == (13, 1)
    c.gemini_paid = True; assert gemini_params(c) == (120, 6)
    c.gemini_rpm, c.gemini_concurrency = 300, 12; assert gemini_params(c) == (300, 12)

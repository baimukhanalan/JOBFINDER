"""Offline tests for the assessment harvester bank + classifier (no network, no DB, no browser)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from backend.tools.assessment_harvester import bank, core  # noqa: E402


def _use_temp_bank(tmp_path):
    bank._BANK_PATH = str(tmp_path / "assessment_bank.json")
    bank.reload()


def test_dedup_key_order_independent_and_platform_scoped():
    k1 = bank.dedup_key("amcat", "What is 2+2?", ["4", "3", "5"])
    k2 = bank.dedup_key("amcat", "what is 2+2?", ["5", "4", "3"])  # reordered + case
    assert k1 == k2
    k3 = bank.dedup_key("shl_sutherland", "What is 2+2?", ["4", "3", "5"])
    assert k3 != k1  # platform-scoped


def test_media_sig_stable_and_empty():
    assert bank.media_sig([]) == ""
    assert bank.media_sig(None) == ""
    a = bank.media_sig(["https://cdn/x.png", "https://cdn/y.png"])
    b = bank.media_sig(["https://cdn/y.png", "https://cdn/x.png"])  # order independent
    assert a == b and len(a) == 16
    assert bank.media_sig(["https://cdn/z.png"]) != a


def test_media_sig_disambiguates_image_items():
    """Two picture items with identical (empty) text but different images must not collide."""
    k1 = bank.dedup_key("amcat", "", [], bank.media_sig(["a.png"]))
    k2 = bank.dedup_key("amcat", "", [], bank.media_sig(["b.png"]))
    assert k1 != k2


def test_record_upsert_bumps_hits(tmp_path):
    _use_temp_bank(tmp_path)
    opts = [{"text": "Yes", "image": None}, {"text": "No", "image": None}]
    k = bank.record(platform="amcat", item_type="personality", question="I enjoy helping people",
                    options=opts, chosen_answer={"text": "Yes", "index": 0, "source": "random"})
    assert bank.size() == 1
    k2 = bank.record(platform="amcat", item_type="personality", question="I enjoy helping people",
                     options=[{"text": "No"}, {"text": "Yes"}])  # same item, reordered
    assert k == k2
    assert bank.size() == 1  # no new entry
    assert bank._load()["items"][k]["hits"] == 2


def test_record_persists_media_and_source(tmp_path):
    _use_temp_bank(tmp_path)
    bank.record(platform="shl_sutherland", item_type="picture", question="Which figure comes next?",
                options=[{"text": "", "image": "x.png"}],
                media={"image": "shots/1.png", "audio": None, "prompt_text": None},
                source={"mailbox": "a@takhet.com", "invite_url": "http://x"},
                kind_meta={"is_scored": True, "is_ability": True},
                msig=bank.media_sig(["q.png"]))
    e = list(bank._load()["items"].values())[0]
    assert e["media"]["image"] == "shots/1.png"
    assert e["source"]["mailbox"] == "a@takhet.com"
    assert e["kind_meta"]["is_ability"] is True
    assert bank.counts_by("item_type") == {"picture": 1}


def test_classify_numerical():
    it = {"question": "What is the value of 15% of 240?", "options": [{"text": "36"}, {"text": "24"}, {"text": "40"}]}
    t, ab = core.classify(it)
    assert t == "numerical" and ab is True


def test_classify_verbal_truefalse():
    it = {"question": "Based on the passage above, the company grew.",
          "options": [{"text": "True"}, {"text": "False"}, {"text": "Cannot say"}]}
    t, ab = core.classify(it)
    assert t == "verbal" and ab is True


def test_classify_picture_from_image_options():
    it = {"question": "Which figure completes the pattern?", "qimgs": ["p.png"],
          "options": [{"text": "", "image": "a.png"}, {"text": "", "image": "b.png"}]}
    t, ab = core.classify(it)
    assert t == "picture" and ab is True


def test_classify_personality():
    it = {"question": "I remain calm under pressure.",
          "options": [{"text": "Strongly agree"}, {"text": "Agree"}, {"text": "Disagree"}]}
    t, ab = core.classify(it)
    assert t == "personality" and ab is False


def test_migrate_from_shl(tmp_path):
    import json
    _use_temp_bank(tmp_path)
    shl = {
        "k1": {"q": "Which statement describes you best?",
               "options": ["I stay calm", "I get anxious"], "answer": "i stay calm",
               "kind": "forced_choice", "n": 5},
        "k2": {"q": "What is 2+2?", "options": ["4", "3"], "answer": "4", "kind": "numerical", "n": 1},
    }
    p = tmp_path / "shl_answer_bank.json"
    p.write_text(json.dumps(shl))
    n = bank.migrate_from_shl(str(p))
    assert n == 2 and bank.size() == 2
    items = list(bank._load()["items"].values())
    fc = [e for e in items if e["item_type"] == "forced_choice"][0]
    assert fc["platform"] == "shl" and fc["chosen_answer"]["source"] == "bank_replay"
    assert fc["chosen_answer"]["index"] == 0
    num = [e for e in items if e["item_type"] == "numerical"][0]
    assert num["kind_meta"]["is_ability"] is True
    # idempotent
    assert bank.migrate_from_shl(str(p)) == 2 and bank.size() == 2


def test_classify_speaking_typing_listening():
    assert core.classify({"question": "Speak about yourself", "has_mic": True, "options": []})[0] == "speaking"
    assert core.classify({"question": "Type the paragraph", "has_textarea": True, "options": []})[0] == "typing"
    lis = {"question": "Listen and answer", "has_audio": True,
           "options": [{"text": "A"}, {"text": "B"}]}
    assert core.classify(lis)[0] == "listening"

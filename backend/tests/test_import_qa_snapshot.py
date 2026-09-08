"""Offline tests for the qa_bot snapshot importer (temp bank + tiny inline fixture, no network)."""
import csv
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from backend.tools.assessment_harvester import bank, import_qa_snapshot as imp  # noqa: E402


def _use_temp_bank(tmp_path):
    bank._BANK_PATH = str(tmp_path / "assessment_bank.json")
    bank.reload()


def _write_fixture(tmp_path):
    d = tmp_path / "qa_snapshot"
    d.mkdir()
    qs = [
        # analytical — will UPGRADE a pre-existing harvested item (banked below)
        {"id": "A1", "section": "Basic Analytical Ability", "prompt": "Choose the correct option.",
         "text": "A bricklayer lays 263, 137 and 140 bricks in three hours. What is the average?",
         "options": ["90 bricks", "263 bricks", "180 bricks", "100 bricks"], "answer_key": None},
        # analytical — NEW
        {"id": "A2", "section": "Basic Analytical Ability", "prompt": "Choose the correct option.",
         "text": "You arrived at 9:35 AM and left at 6:05 PM. How long were you in the office?",
         "options": ["8 hours and 45 minutes", "8 hours and 30 minutes", "8 hours and 20 minutes"],
         "answer_key": None},
        # analytical — image-only options, must be SKIPPED (no text to map)
        {"id": "A3", "section": "Basic Analytical Ability", "prompt": "Click the image.",
         "text": "Which figure comes next?", "options": [], "passage": {"type": "image_transcription"},
         "answer_key": None},
        # sales — best/worst
        {"id": "S1", "section": "Sales Competency Test",
         "prompt": "Choose the 'best' and the 'worst' action for the given situation.",
         "text": "A plant manager gives you a lead outside your sector.",
         "options": ["Pursue it yourself", "Refuse the lead rudely", "Pass it to the right colleague",
                     "Ignore the manager"], "answer_key": None},
        # writex — email
        {"id": "W1", "section": "WriteX - Email Writing",
         "prompt": "Compose an email response for the topic provided.",
         "text": ("Compose an email response for the topic provided. Please ensure that your response "
                  "is a minimum of 30 words. Write to sonya.smith@swillsys.com requesting leave."),
         "options": [], "answer_key": None},
    ]
    with open(d / "questions.jsonl", "w", encoding="utf-8") as f:
        for q in qs:
            f.write(json.dumps(q) + "\n")
    header = ["id", "section", "question_number", "question", "recommended_answer", "reasoning",
              "confidence", "official_answer_key", "exact_content_group", "exact_occurrence_count",
              "source_screenshots"]
    rows = [
        ["A1", "Basic Analytical Ability", "1", "avg bricks", "180 bricks", "average of three",
         "высокая по сохранённой формулировке", "нет", "", "1", ""],
        ["A2", "Basic Analytical Ability", "2", "office time", "8 hours and 30 minutes", "diff",
         "высокая по сохранённой формулировке", "нет", "", "1", ""],
        ["A3", "Basic Analytical Ability", "3", "next figure", "Option 2: the circle", "vision",
         "низкая: неоднозначные исходные данные", "нет", "", "1", ""],
        ["S1", "Sales Competency Test", "1", "lead", "BEST: Pass it to the right colleague WORST: "
         "Refuse the lead rudely", "helps the client", "средняя", "нет", "", "1", ""],
        ["W1", "WriteX - Email Writing", "1", "leave email",
         "To: sonya.smith@swillsys.com\nSubject: Request for leave\n\n" + ("word " * 35).strip(),
         "example", "пример ответа", "нет", "", "1", ""],
    ]
    with open(d / "SHL_answers_all.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)
    return str(d)


def test_import_new_and_upgrade_and_skip(tmp_path):
    _use_temp_bank(tmp_path)
    data_dir = _write_fixture(tmp_path)
    # pre-bank A1 as a harvested item with a weak local_llm key -> the import must UPGRADE it in place
    a1_opts = ["90 bricks", "263 bricks", "180 bricks", "100 bricks"]
    a1_q = "A bricklayer lays 263, 137 and 140 bricks in three hours. What is the average?"
    bank.record(platform="amcat", item_type="unknown", question=a1_q,
                options=[{"text": o} for o in a1_opts],
                chosen_answer={"text": "90 bricks", "index": 0, "source": "random"},
                kind_meta={"is_scored": True, "is_ability": True})
    bank.set_answer("amcat", a1_q, a1_opts,
                    {"text": "90 bricks", "index": 0, "source": "local_llm", "needs_vision": False})
    before = bank.size()

    stats = imp.run(data_dir=data_dir)

    # analytical: A1 upgraded, A2 new, A3 skipped (image-only options)
    an = stats["analytical"]
    assert an["total"] == 3 and an["upgraded"] == 1 and an["new"] == 1 and an["skipped_no_options"] == 1
    assert stats["sales"]["total"] == 1 and stats["sales"]["new"] == 1
    assert stats["writex"]["total"] == 1 and stats["writex"]["new"] == 1
    # A1's weak key was overwritten by the authoritative qa_bot_csv answer (180 bricks, index 2)
    ak = bank.answer_for("amcat", a1_q, a1_opts)
    assert ak["source"] == "qa_bot_csv" and ak["text"] == "180 bricks" and ak["index"] == 2
    # bank grew by A2 + S1 + W1 (A1 upgraded in place, A3 skipped)
    assert bank.size() == before + 3


def test_sales_answer_key_carries_best_and_worst(tmp_path):
    _use_temp_bank(tmp_path)
    data_dir = _write_fixture(tmp_path)
    imp.run(sections=("sales",), data_dir=data_dir)
    opts = ["Pursue it yourself", "Refuse the lead rudely", "Pass it to the right colleague",
            "Ignore the manager"]
    ak = bank.answer_for("amcat", "A plant manager gives you a lead outside your sector.", opts)
    assert ak["kind"] == "best_worst"
    assert ak["best"]["text"] == "Pass it to the right colleague" and ak["best"]["index"] == 2
    assert ak["worst"]["text"] == "Refuse the lead rudely" and ak["worst"]["index"] == 1
    assert ak["index"] == 2                       # single-index replay defaults to BEST


def test_writex_answer_key_is_a_replayable_email(tmp_path):
    _use_temp_bank(tmp_path)
    data_dir = _write_fixture(tmp_path)
    imp.run(sections=("writex",), data_dir=data_dir)
    topic = ("Compose an email response for the topic provided. Please ensure that your response is a "
             "minimum of 30 words. Write to sonya.smith@swillsys.com requesting leave.")
    ak = bank.answer_for("amcat", topic, [])
    assert ak["kind"] == "email" and ak["to"] == "sonya.smith@swillsys.com"
    assert len(ak["body"].split()) >= 30


def test_idempotent_rerun_does_not_duplicate(tmp_path):
    _use_temp_bank(tmp_path)
    data_dir = _write_fixture(tmp_path)
    imp.run(data_dir=data_dir)
    size1 = bank.size()
    imp.run(data_dir=data_dir)                    # re-run: upserts, no new rows
    assert bank.size() == size1


def test_dry_run_writes_nothing(tmp_path):
    _use_temp_bank(tmp_path)
    data_dir = _write_fixture(tmp_path)
    stats = imp.run(data_dir=data_dir, dry=True)
    # empty bank -> both mappable analytical items count as new (A3 skipped), sales/writex new
    assert stats["analytical"]["new"] == 2 and stats["analytical"]["skipped_no_options"] == 1
    assert stats["sales"]["new"] == 1 and stats["writex"]["new"] == 1
    assert bank.size() == 0                       # nothing persisted

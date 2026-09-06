"""Import user corpus as unapproved source data, never as an answer key."""
import argparse
import csv
import json
from pathlib import Path
from qa_bot.knowledge.bank import QuestionBank


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--answers", type=Path, required=True)
    parser.add_argument("--database", type=Path, required=True)
    args = parser.parse_args()
    with args.answers.open(encoding="utf-8-sig", newline="") as stream:
        recommendations = {r["id"]: r["recommended_answer"] for r in csv.DictReader(stream)}
    args.database.parent.mkdir(parents=True, exist_ok=True)
    with QuestionBank(args.database) as bank, args.source.open(encoding="utf-8") as stream:
        count = bank.import_corpus((json.loads(line) for line in stream if line.strip()), recommendations)
        print(json.dumps({"imported": count, "approved": 0, "source": "candidate_corpus"}))


if __name__ == "__main__":
    main()

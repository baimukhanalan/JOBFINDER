"""Build a local source+reference archive from explicit allowlists, not raw sessions."""
import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import subprocess
import zipfile


def safe_relative(value):
    path = PurePosixPath(value)
    if not value or path.is_absolute() or ".." in path.parts or ":" in value or "\\" in value:
        raise ValueError("unsafe archive path")
    return path.as_posix()


def scan_secret_markers(data):
    patterns = (rb"sk[_-][A-Za-z0-9_-]{32,}",
                rb"eyJ[A-Za-z0-9_-]{12,}\.[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}",
                rb"(?i)(?:token|api_key|authorization)=[A-Za-z0-9_.%-]{60,}",
                rb"-----BEGIN [A-Z ]*PRIVATE KEY-----")
    if any(re.search(pattern, data) for pattern in patterns):
        raise ValueError("credential marker found; package not created")


def sanitized_record(record):
    fields = ("id", "test_id", "section", "subsection", "question_number", "practice", "prompt",
              "text", "passage", "options", "ordered_items", "answer_key", "ambiguity",
              "initial_screen", "ui_note", "reasoning", "answer_reasoning")
    result = {key: record[key] for key in fields if key in record}
    result["approval_status"] = "candidate"
    result["screenshots"] = [{"path": safe_relative(s["path"]), "sha256": s["sha256"]}
                              for s in record.get("screenshots", [])]
    return result


def build_package(project, source, output):
    project, source, output = Path(project).resolve(), Path(source).resolve(), Path(output).resolve()
    if output.exists():
        raise ValueError("output exists; choose a new filename")
    entries = {}

    def add(name, data):
        name = safe_relative(name)
        if name in entries:
            raise ValueError("duplicate archive path")
        scan_secret_markers(data)
        entries[name] = data

    tracked = subprocess.check_output(["git", "ls-files", "-z"], cwd=project).decode().split("\0")
    allowed_roots = {"src", "tests", "configs", "docs", "scripts"}
    allowed_files = {"README.md", "pyproject.toml", "requirements.txt", ".gitignore"}
    for name in filter(None, tracked):
        if PurePosixPath(name).name == ".gitkeep":
            continue
        if name not in allowed_files and PurePosixPath(name).parts[0] not in allowed_roots:
            continue
        path = project / safe_relative(name)
        if path.is_symlink() or not path.resolve().is_relative_to(project):
            raise ValueError("linked source path rejected")
        if path.suffix.lower() not in {".py", ".toml", ".md", ".txt", ".json", ".html", ".svg", ".pbm", ".ps1"} and name != ".gitignore":
            raise ValueError("unexpected project file type")
        add("qa_bot/" + name, path.read_bytes())
    for name in ("SHL_answers_RU.md", "SHL_answers_all.csv", "SHL_QA_architecture_RU.md", "SHL_QA_task_type_catalog_RU.md"):
        add(name, (project.parent / name).read_bytes())
    records = [sanitized_record(json.loads(line)) for line in (source / "questions.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    add("data/questions.jsonl", ("\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n").encode())
    index = json.loads((source / "assets/index.json").read_text(encoding="utf-8"))
    images = {}
    for asset in index["assets"]:
        name = safe_relative(asset["path"])
        if not name.startswith("assets/") or not name.endswith(".jpg"):
            raise ValueError("unexpected reference image")
        path = source / name
        if path.is_symlink() or not path.resolve().is_relative_to(source):
            raise ValueError("linked image rejected")
        data = path.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        if digest != asset["sha256"]:
            raise ValueError("reference image hash mismatch")
        if name not in images:
            add("data/" + name, data)
        images[name] = {"path": name, "sha256": digest, "question_id": asset["question_id"]}
    for record in records:
        for image in record["screenshots"]:
            if image["path"] not in images or image["sha256"] != images[image["path"]]["sha256"]:
                raise ValueError("question image reference mismatch")
    add("data/asset_index.json", json.dumps(list(images.values()), indent=2).encode())
    add("START_HERE_RU.md", (project / "docs/share_guide.md").read_bytes())
    manifest = {"format_version": 1, "release_status": "development_preview_not_assessment_runner",
                "question_count": len(records), "screenshot_count": len(images), "approved_answers": 0,
                "excluded": ["credentials", "tokens", "cookies", "sessions", "raw capture logs", "personal databases", "dependencies"],
                "files": {name: hashlib.sha256(data).hexdigest() for name, data in sorted(entries.items())}}
    add("MANIFEST.json", json.dumps(manifest, indent=2).encode())
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "x", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in sorted(entries.items()):
            archive.writestr(name, data)
    with zipfile.ZipFile(output) as archive:
        if archive.testzip() is not None:
            raise ValueError("archive CRC failed")
        for name, digest in manifest["files"].items():
            if hashlib.sha256(archive.read(name)).hexdigest() != digest:
                raise ValueError("archive manifest failed")
    return {"files": len(entries), "questions": len(records), "screenshots": len(images),
            "size_bytes": output.stat().st_size, "sha256": hashlib.sha256(output.read_bytes()).hexdigest()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(build_package(args.project, args.source, args.output), indent=2))


if __name__ == "__main__":
    main()

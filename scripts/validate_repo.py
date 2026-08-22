"""Run dependency-free repository checks used by local development and CI."""

from __future__ import annotations

import ast
import json
import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

IGNORED_DIRECTORY_NAMES = {
    ".git",
    ".venv",
    "venv",
    "env",
    "__pycache__",
    ".pytest_cache",
    "content",
}

REQUIRED_FILES = (
    "README.md",
    ".gitignore",
    ".gitattributes",
    "backend/practice_api.py",
    "backend/gunicorn.conf.py",
    "backend/requirements.txt",
    "frontend/index.html",
    "frontend/app.js",
    "deploy/environment/gdsa-practice.env.example",
    "deploy/scripts/deploy-release.sh",
    "deploy/scripts/health-check.sh",
    "deploy/scripts/validate-release-archive.sh",
    "deploy/scripts/validate-study-content.py",
    "docs/EC2_DEPLOYMENT.md",
    "docs/GITHUB_ACTIONS.md",
    "docs/GOOGLE_OIDC_SETUP.md",
    ".github/workflows/ci.yml",
    ".github/workflows/deploy-ec2.yml",
)

PINNED_REQUIREMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*==[^#\s]+$")


def iter_repository_files() -> list[Path]:
    files: list[Path] = []
    for path in ROOT.rglob("*"):
        if not path.is_file():
            continue
        relative_parts = path.relative_to(ROOT).parts
        if any(part in IGNORED_DIRECTORY_NAMES for part in relative_parts):
            continue
        files.append(path)
    return files


def main() -> int:
    errors: list[str] = []
    files = iter_repository_files()

    for relative_name in REQUIRED_FILES:
        if not (ROOT / relative_name).is_file():
            errors.append(f"missing required file: {relative_name}")

    python_count = 0
    json_count = 0
    jsonl_count = 0
    for path in files:
        relative_name = path.relative_to(ROOT).as_posix()
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue

        if path.suffix == ".py":
            python_count += 1
            try:
                ast.parse(text, filename=relative_name)
            except SyntaxError as exc:
                errors.append(f"Python syntax error in {relative_name}: {exc}")
        elif path.suffix == ".json":
            json_count += 1
            try:
                json.loads(text)
            except json.JSONDecodeError as exc:
                errors.append(f"JSON error in {relative_name}: {exc}")
        elif path.suffix == ".jsonl":
            jsonl_count += 1
            for line_number, line in enumerate(text.splitlines(), start=1):
                if not line.strip():
                    continue
                try:
                    json.loads(line)
                except json.JSONDecodeError as exc:
                    errors.append(f"JSONL error in {relative_name}:{line_number}: {exc}")
                    break

    requirements_path = ROOT / "backend/requirements.txt"
    if requirements_path.is_file():
        for line_number, raw_line in enumerate(requirements_path.read_text(encoding="utf-8").splitlines(), start=1):
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if not PINNED_REQUIREMENT.fullmatch(line):
                errors.append(
                    f"backend/requirements.txt:{line_number} is not pinned with ==: {line}"
                )

    suspicious_files = []
    for path in files:
        if path.name in {".env", "credentials.json"} or path.suffix.lower() in {
            ".pem",
            ".key",
            ".p12",
            ".pfx",
            ".jks",
        }:
            suspicious_files.append(path.relative_to(ROOT).as_posix())
    if suspicious_files:
        errors.append("credential-like files must not be tracked: " + ", ".join(suspicious_files))

    if errors:
        print("Repository validation failed:", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1

    print(
        "Repository validation passed: "
        f"{python_count} Python files, {json_count} JSON files, {jsonl_count} JSONL files."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

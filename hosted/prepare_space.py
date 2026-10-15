"""Export the exact canonical commit without local state or credentials."""

import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess

ROOT = Path(__file__).resolve().parents[1]
FILES = {
    "Dockerfile": "hosted/Dockerfile",
    "README.md": "hosted/SPACE.md",
    "requirements.txt": "hosted/requirements.txt",
    "app.py": "hosted/app.py",
    "engine.py": "hosted/engine.py",
    "static/index.html": "hosted/static/index.html",
    "static/privacy.html": "hosted/static/privacy.html",
    "static/studio.js": "hosted/static/studio.js",
    "static/studio.css": "hosted/static/studio.css",
    "static/favicon.png": "hosted/static/favicon.png",
    "static/lucide.min.js": "hosted/static/lucide.min.js",
    "static/LICENSE-lucide.txt": "hosted/static/LICENSE-lucide.txt",
}


def git(*args):
    return subprocess.check_output(["git", "-C", str(ROOT), *args])


def prepare(revision, output):
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise ValueError("An exact commit SHA is required")
    remote = git("remote", "get-url", "origin").decode().strip()
    if remote not in {"https://github.com/szl-holdings/agents-studio.git",
                      "https://github.com/szl-holdings/agents-studio",
                      "git@github.com:szl-holdings/agents-studio.git"}:
        raise ValueError("Unexpected source repository")
    if git("rev-parse", "origin/main").decode().strip() != revision:
        raise ValueError("Refresh origin/main; only canonical main can be exported")
    output = Path(output).resolve()
    if output.exists():
        raise ValueError("Output must be a fresh directory; existing evidence is preserved")
    files = {target: git("show", f"{revision}:{source}") for target, source in FILES.items()}
    if any(len(data) > 2_000_000 for data in files.values()):
        raise ValueError("Unexpectedly large deployment file")
    manifest = {
        "schema": "szl.agents-studio-source/v1",
        "repository": "szl-holdings/agents-studio",
        "revision": revision,
        "evidence_class": "DECLARED",
        "controlled_files": {name: hashlib.sha256(data).hexdigest() for name, data in files.items()},
        "signature_status": "UNSIGNED",
        "deployment": "UNKNOWN",
        "inference_witness": "UNAVAILABLE",
    }
    output.mkdir(parents=True)
    for name, data in files.items():
        destination = output / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(data)
    (output / "source.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return manifest


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--revision", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    print(json.dumps(prepare(args.revision, args.output), indent=2))

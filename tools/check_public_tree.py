"""Guard against accidentally tracking account data and common secret formats.

This is a narrow publication check, not an exhaustive secret detector.
"""
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
PATTERNS = [
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,})\b"),
    re.compile(r"\b\d{8,}:[A-Za-z0-9_-]{25,}\b"),
    re.compile(r"0x[0-9a-fA-F]{40,64}\b"),
    re.compile(r"/(?:Users|home)/[A-Za-z0-9_.-]+/"),
    re.compile(r"\b(?:192\.168\.\d{1,3}\.\d{1,3}|10\.\d{1,3}\.\d{1,3}\.\d{1,3})\b"),
]


def main():
    result = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, check=True)
    files = [ROOT / p for p in result.stdout.decode().split("\0") if p]
    failures = []
    for path in files:
        rel = path.relative_to(ROOT)
        if path.name.startswith(".env") or any(
            path.name.endswith(ext) for ext in (".db", ".sqlite", ".sqlite3", ".log", ".pem", ".key")
        ) or any(x in rel.parts for x in (".venv", "data", "logs", "exports")) or re.search(r"\.(?:db|sqlite3?)-(?:wal|shm|journal)$", path.name):
            failures.append(f"{rel}: excluded file type")
        if path.suffix.lower() in {".png", ".jpg", ".jpeg"}:
            continue
        text = path.read_text(encoding="utf-8")
        for number, line in enumerate(text.splitlines(), 1):
            if any(pattern.search(line) for pattern in PATTERNS):
                failures.append(f"{rel}:{number}: possible private data (value omitted)")
    if failures:
        print("\n".join(failures), file=sys.stderr)
        return 1
    print(f"Publication check passed for {len(files)} tracked files.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Guard against accidentally tracking account data and common secret formats.

This is a narrow publication check, not an exhaustive secret detector.
"""
from pathlib import Path
import re
import subprocess
import struct
import sys

ROOT = Path(__file__).resolve().parents[1]
PATTERNS = [
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,})\b"),
    re.compile(r"\b\d{8,}:[A-Za-z0-9_-]{25,}\b"),
    re.compile(r"0x[0-9a-fA-F]{40,64}\b"),
    re.compile(r"/(?:Users|home)/[A-Za-z0-9_.-]+/"),
    re.compile(r"\b(?:192\.168\.\d{1,3}\.\d{1,3}|10\.\d{1,3}\.\d{1,3}\.\d{1,3})\b"),
    re.compile(r"\b172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}\b"),
]


def image_metadata(data, suffix):
    """Reject metadata that can hide filenames, GPS, author data or comments."""
    if suffix == ".png":
        if not data.startswith(b"\x89PNG\r\n\x1a\n"):
            return True
        pos = 8
        while pos + 12 <= len(data):
            size = struct.unpack(">I", data[pos:pos + 4])[0]
            kind = data[pos + 4:pos + 8]
            end = pos + 12 + size
            if end > len(data) or kind in {b"eXIf", b"tEXt", b"iTXt", b"zTXt"}:
                return True
            if kind == b"IEND":
                return end != len(data)
            pos = end
        return True
    if not data.startswith(b"\xff\xd8"):
        return True
    pos = 2
    while pos + 4 <= len(data):
        if data[pos] != 255:
            return True
        marker = data[pos + 1]
        if marker == 218:  # Start of compressed image data.
            return False
        size = struct.unpack(">H", data[pos + 2:pos + 4])[0]
        if size < 2 or pos + 2 + size > len(data) or marker in {225, 237, 254}:
            return True
        pos += 2 + size
    return True


def main():
    result = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, check=True)
    files = [ROOT / p for p in result.stdout.decode().split("\0") if p]
    failures = []
    for path in files:
        rel = path.relative_to(ROOT)
        if path.name.startswith(".env") or path.name in {".DS_Store", "id_rsa", "id_ed25519", ".netrc"} or any(
            path.name.lower().endswith(ext) for ext in (".db", ".sqlite", ".sqlite3", ".log", ".pem", ".key", ".p12", ".pfx", ".keystore", ".zip", ".tar", ".gz")
        ) or any(x in rel.parts for x in (".venv", "data", "logs", "exports", "backups")) or re.search(r"\.(?:db|sqlite3?)-(?:wal|shm|journal)$", path.name):
            failures.append(f"{rel}: excluded file type")
        data = path.read_bytes()
        if path.suffix.lower() in {".png", ".jpg", ".jpeg"}:
            if image_metadata(data, path.suffix.lower()):
                failures.append(f"{rel}: image metadata or invalid format")
            continue
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            failures.append(f"{rel}: unexpected binary file")
            continue
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

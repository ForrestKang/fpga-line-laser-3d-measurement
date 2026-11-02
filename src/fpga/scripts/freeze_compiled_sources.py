"""Bind the actual synthesis input list to file content before synthesis."""

import argparse
import hashlib
import json
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--list", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    entries = {}
    for line in args.list.read_text().splitlines():
        if not line.strip():
            continue
        path = Path(line.strip()).resolve(strict=True)
        entries[str(path)] = hashlib.sha256(path.read_bytes()).hexdigest()
    if not entries:
        raise ValueError("no compiled sources")
    with args.output.open("x", encoding="utf8") as stream:
        json.dump(entries, stream, indent=2)
    print(f"COMPILED_SOURCE_FREEZE {len(entries)} files")


if __name__ == "__main__":
    main()

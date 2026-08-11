from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path


def archive_bytes(data: bytes, directory: Path, label: str, suffix: str) -> tuple[Path, bool]:
    directory.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(data).hexdigest()
    existing = next(directory.glob(f"*--{digest[:16]}{suffix.lower()}"), None)
    if existing is not None:
        return existing, False
    safe_label = re.sub(r"[^A-Za-z0-9_.-]+", "-", label).strip("-") or "export"
    destination = directory / f"{safe_label}--{digest[:16]}{suffix.lower()}"
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    temporary.write_bytes(data)
    os.replace(temporary, destination)
    return destination, True


def archive_file(path: Path, directory: Path) -> tuple[Path, bool]:
    return archive_bytes(path.read_bytes(), directory, path.stem, path.suffix)

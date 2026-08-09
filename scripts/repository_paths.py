#!/usr/bin/env python3
"""Safe path serialization for public repository artifacts."""

from __future__ import annotations

from pathlib import Path
from typing import Union


PathValue = Union[str, Path]


class RepositoryPathError(ValueError):
    """Raised when a public artifact references a path outside its repository."""


def repository_relative_path(
    path: PathValue,
    *,
    repository_root: PathValue,
) -> str:
    """Return an in-repository path as a repository-relative POSIX string.

    Resolving both paths prevents a symlink or ``..`` segment from making an
    outside path appear repository-controlled. Unexpected outside paths are a
    generation error, not metadata that may be persisted in a public artifact.
    """
    root = Path(repository_root).resolve()
    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = root / candidate
    resolved = candidate.resolve()
    try:
        return resolved.relative_to(root).as_posix()
    except ValueError as error:
        raise RepositoryPathError(
            f"public artifact path is outside repository root: {resolved} "
            f"(repository root: {root})"
        ) from error

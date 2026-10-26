"""Pack loading: reads every YAML/config file in a pack folder."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass
class Pack:
    """Everything that defines one catalogue. Config lives in pack/, data in data/."""

    path: Path
    pack: dict[str, Any] = field(default_factory=dict)
    schema: dict[str, Any] = field(default_factory=dict)
    taxonomy: dict[str, Any] = field(default_factory=dict)
    facets: dict[str, Any] = field(default_factory=dict)
    sources: list[dict[str, Any]] = field(default_factory=list)
    rules: dict[str, Any] = field(default_factory=dict)
    prompt: str = ""
    copy: dict[str, Any] = field(default_factory=dict)
    theme_css: str | None = None
    theme_yml: dict[str, Any] = field(default_factory=dict)
    outputs: list[str] = field(default_factory=list)
    decide: dict[str, Any] = field(default_factory=dict)

    # -- convenience --------------------------------------------------------
    @property
    def id(self) -> str:
        return self.pack["id"]

    @property
    def data_dir(self) -> Path:
        """Published data: items, thumbnails, changelog, overrides."""
        return self.path.parent.parent / "data"

    @property
    def private_dir(self) -> Path:
        """Working data that never belongs in a public repo: the review queue, rejection
        log, model and HTTP caches, run report, eval set, hand-picked lists and the
        source list. Set by $CATALOGUE_PRIVATE_DIR or pack.yml `private_data` (relative
        to the project root); without either it is data_dir, as before."""
        configured = os.environ.get("CATALOGUE_PRIVATE_DIR") or self.pack.get("private_data")
        if not configured:
            return self.data_dir
        path = Path(configured)
        return path if path.is_absolute() else self.path.parent.parent / path

    @property
    def private_missing(self) -> bool:
        return self.private_dir != self.data_dir and not self.private_dir.is_dir()

    @property
    def categories(self) -> dict[str, Any]:
        return self.taxonomy.get("categories", {})

    def category_ids(self) -> list[str]:
        return list(self.categories.keys())

    def subcategory_ids(self) -> set[str]:
        out: set[str] = set()
        for cat in self.categories.values():
            for sub in cat.get("subcategories", []):
                out.add(sub["id"] if isinstance(sub, dict) else sub)
        return out

    @property
    def gate(self) -> dict[str, Any]:
        return self.pack.get("gate", {})

    @property
    def classifier(self) -> dict[str, Any]:
        return self.pack.get("classifier", {})


def _load_yaml(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    with path.open(encoding="utf-8") as f:
        return yaml.safe_load(f) or default


def load_pack(path: str | Path) -> Pack:
    """Load a pack folder. Every file except pack.yml is optional."""
    p = Path(path)
    if not p.is_dir():
        raise SystemExit(f"pack folder not found: {p}")
    pack = Pack(path=p)
    pack.pack = _load_yaml(p / "pack.yml", {})
    if not pack.pack.get("id"):
        raise SystemExit(f"{p}/pack.yml is missing an id")
    pack.schema = _load_yaml(p / "schema.yml", {"fields": []})
    pack.taxonomy = _load_yaml(p / "taxonomy.yml", {"categories": {}})
    pack.facets = _load_yaml(p / "facets.yml", {"facets": []})
    # The source list is working data too: a private sources.yml wins over the pack's.
    # Relative `file:` paths in it (manual lists) resolve against its own folder.
    private_sources = pack.private_dir / "sources.yml"
    src_file = private_sources if private_sources.is_file() else p / "sources.yml"
    src = _load_yaml(src_file, {"sources": []})
    pack.sources = src.get("sources", []) if isinstance(src, dict) else src
    if src_file == private_sources:
        for s in pack.sources:
            if s.get("file") and not Path(s["file"]).is_absolute():
                s["file"] = str(src_file.parent / s["file"])
    pack.rules = _load_yaml(p / "rules.yml", {"include": [], "exclude": []})
    prompt_file = p / pack.classifier.get("prompt", "classify.prompt.md")
    pack.prompt = prompt_file.read_text(encoding="utf-8") if prompt_file.exists() else ""
    pack.copy = _load_yaml(p / "copy.yml", {"strings": {}})
    theme_file = p / pack.pack.get("theme", "theme.css")
    if theme_file.exists():
        pack.theme_css = theme_file.read_text(encoding="utf-8")
    pack.theme_yml = _load_yaml(p / "theme.yml", {})
    pack.decide = _str_bool_keys(_load_yaml(p / pack.classifier.get("decide", "decide.yml"), {}))
    pack.outputs = pack.pack.get("outputs", ["rss", "jsonfeed", "changelog"])
    outputs_file = _load_yaml(p / "outputs.yml", {})
    if isinstance(outputs_file, dict) and outputs_file.get("outputs"):
        pack.outputs = outputs_file["outputs"]
    return pack


def _str_bool_keys(value: Any) -> Any:
    """YAML reads bare `true:` / `false:` keys as booleans; decide.yml means the words
    (noul criteria, map targets, confidence terms)."""
    if isinstance(value, dict):
        return {(str(k).lower() if isinstance(k, bool) else k): _str_bool_keys(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_str_bool_keys(v) for v in value]
    return value


def load_json(path: Path) -> Any:
    if not path.exists():
        return None
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def write_json(path: Path, data: Any, atomic: bool = True) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp") if atomic else path
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, sort_keys=False)
        f.write("\n")
    if atomic:
        tmp.replace(path)

#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import plistlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any


SCHEMA = "vscode://schemas/color-theme"
EXTENSION_ID_RE = re.compile(r"itemName=([A-Za-z0-9_.-]+)")
JSONC_RE = re.compile(r'"(?:\\.|[^"\\])*"|//.*?$|/\*.*?\*/', re.MULTILINE | re.DOTALL)


@dataclass
class ExportRecord:
    extension_id: str
    extension_dir: str
    theme_label: str
    source_theme_path: str
    output_path: str
    type: str
    color_count: int
    token_color_count: int
    semantic_highlighting: bool | None
    semantic_token_color_count: int


def cli_args() -> argparse.Namespace:
    here = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(
        description="Export installed VS Code themes listed in themes.md into normalized theme files."
    )
    parser.add_argument(
        "--themes-md",
        type=Path,
        default=here / "themes.md",
        help="Path to the markdown file that contains VS Code Marketplace theme links.",
    )
    parser.add_argument(
        "--extensions-dir",
        type=Path,
        default=Path.home() / ".vscode" / "extensions",
        help="VS Code extensions directory.",
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=here / "exported",
        help="Output directory for normalized theme files.",
    )
    parser.add_argument(
        "--tokens-only",
        action="store_true",
        help="Omit UI colors and only export tokenColors plus semantic token styling.",
    )
    return parser.parse_args()


def load_jsonc(path: Path) -> Any:
    text = path.read_text(encoding="utf-8", errors="replace")
    text = JSONC_RE.sub(_jsonc_replacer, text)
    text = re.sub(r",(\s*[}\]])", r"\1", text)
    return json.loads(text)


def _jsonc_replacer(match: re.Match[str]) -> str:
    token = match.group(0)
    return "" if token.startswith("/") else token


def extract_extension_ids(themes_md: Path) -> list[str]:
    text = themes_md.read_text(encoding="utf-8")
    seen: set[str] = set()
    result: list[str] = []
    for match in EXTENSION_ID_RE.finditer(text):
        ext_id = match.group(1)
        key = ext_id.lower()
        if key in seen:
            continue
        seen.add(key)
        result.append(ext_id)
    return result


def find_installed_extension(extensions_dir: Path, extension_id: str) -> Path | None:
    prefix = extension_id.lower() + "-"
    matches = [
        path
        for path in extensions_dir.iterdir()
        if path.is_dir() and path.name.lower().startswith(prefix)
    ]
    if not matches:
        return None
    matches.sort(key=lambda path: path.name.lower())
    return matches[-1]


def slugify(value: str) -> str:
    value = value.strip().lower()
    value = re.sub(r"[^a-z0-9]+", "-", value)
    value = re.sub(r"-{2,}", "-", value)
    return value.strip("-") or "theme"


def ui_theme_to_type(ui_theme: str | None) -> str | None:
    if ui_theme == "vs":
        return "light"
    if ui_theme == "vs-dark":
        return "dark"
    if ui_theme == "hc-black":
        return "hcDark"
    if ui_theme == "hc-light":
        return "hcLight"
    return None


def normalize_tmtheme_settings(data: dict[str, Any]) -> list[dict[str, Any]]:
    token_colors: list[dict[str, Any]] = []
    for item in data.get("settings", []):
        if not isinstance(item, dict):
            continue
        scope = item.get("scope")
        settings = item.get("settings")
        if not scope or not isinstance(settings, dict):
            continue
        normalized: dict[str, Any] = {"settings": settings}
        name = item.get("name")
        if isinstance(name, str) and name:
            normalized["name"] = name
        normalized["scope"] = scope
        token_colors.append(normalized)
    return token_colors


def ensure_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    return []


def ensure_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    return {}


def resolve_theme(path: Path, seen: set[Path] | None = None) -> dict[str, Any]:
    path = path.resolve()
    seen = set() if seen is None else seen
    if path in seen:
        raise ValueError(f"Theme include cycle detected at {path}")
    seen.add(path)
    try:
        if path.suffix.lower() == ".tmtheme":
            with path.open("rb") as handle:
                data = plistlib.load(handle)
            return {
                "colors": {},
                "tokenColors": normalize_tmtheme_settings(ensure_dict(data)),
                "semanticHighlighting": None,
                "semanticTokenColors": {},
                "type": None,
            }

        data = ensure_dict(load_jsonc(path))

        base: dict[str, Any] = {
            "colors": {},
            "tokenColors": [],
            "semanticHighlighting": None,
            "semanticTokenColors": {},
            "type": None,
        }
        include = data.get("include")
        if isinstance(include, str) and include.strip():
            base = resolve_theme((path.parent / include).resolve(), seen)

        colors = dict(ensure_dict(base.get("colors")))
        colors.update(ensure_dict(data.get("colors")))

        semantic_token_colors = dict(ensure_dict(base.get("semanticTokenColors")))
        semantic_token_colors.update(ensure_dict(data.get("semanticTokenColors")))

        token_colors = list(ensure_list(base.get("tokenColors")))
        token_source = data.get("tokenColors")
        if isinstance(token_source, str) and token_source.strip():
            referenced = resolve_theme((path.parent / token_source).resolve(), seen)
            token_colors.extend(ensure_list(referenced.get("tokenColors")))
        else:
            token_colors.extend(ensure_list(token_source))
            token_colors.extend(ensure_list(data.get("settings")))

        return {
            "name": data.get("name") or base.get("name"),
            "type": data.get("type") or base.get("type"),
            "colors": colors,
            "semanticHighlighting": (
                data["semanticHighlighting"]
                if "semanticHighlighting" in data
                else base.get("semanticHighlighting")
            ),
            "semanticTokenColors": semantic_token_colors,
            "tokenColors": [item for item in token_colors if isinstance(item, dict) and item.get("scope")],
        }
    finally:
        seen.remove(path)


def export_theme(
    extension_id: str,
    extension_dir: Path,
    descriptor: dict[str, Any],
    out_dir: Path,
    tokens_only: bool,
    duplicate_label: bool,
) -> ExportRecord:
    theme_path = (extension_dir / descriptor["path"]).resolve()
    resolved = resolve_theme(theme_path)
    theme_type = resolved.get("type") or ui_theme_to_type(descriptor.get("uiTheme")) or "dark"
    payload: dict[str, Any] = {"$schema": SCHEMA, "type": theme_type}
    if not tokens_only:
        payload["colors"] = resolved.get("colors", {})
    if resolved.get("semanticHighlighting") is not None:
        payload["semanticHighlighting"] = resolved["semanticHighlighting"]
    if resolved.get("semanticTokenColors"):
        payload["semanticTokenColors"] = resolved["semanticTokenColors"]
    payload["tokenColors"] = resolved.get("tokenColors", [])

    extension_slug = slugify(extension_id)
    label = descriptor.get("label") or resolved.get("name") or theme_path.stem
    output_stem = slugify(label)
    if duplicate_label:
        output_stem = f"{output_stem}--{slugify(theme_path.stem)}"
    output_name = f"{output_stem}.jsonc"
    output_path = out_dir / extension_slug / output_name
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    return ExportRecord(
        extension_id=extension_id,
        extension_dir=str(extension_dir),
        theme_label=label,
        source_theme_path=str(theme_path),
        output_path=str(output_path),
        type=theme_type,
        color_count=len(payload.get("colors", {})),
        token_color_count=len(payload["tokenColors"]),
        semantic_highlighting=payload.get("semanticHighlighting"),
        semantic_token_color_count=len(payload.get("semanticTokenColors", {})),
    )


def main() -> int:
    args = cli_args()
    extension_ids = extract_extension_ids(args.themes_md)
    args.out_dir.mkdir(parents=True, exist_ok=True)

    exported: list[ExportRecord] = []
    missing: list[str] = []
    skipped_without_themes: list[str] = []

    for extension_id in extension_ids:
        extension_dir = find_installed_extension(args.extensions_dir, extension_id)
        if extension_dir is None:
            missing.append(extension_id)
            continue

        package_json = extension_dir / "package.json"
        package = ensure_dict(load_jsonc(package_json))
        descriptors = ensure_list(ensure_dict(package.get("contributes")).get("themes"))
        if not descriptors:
            skipped_without_themes.append(extension_id)
            continue

        label_counts: dict[str, int] = {}
        for descriptor in descriptors:
            if not isinstance(descriptor, dict) or "path" not in descriptor:
                continue
            label = str(descriptor.get("label") or "")
            label_counts[label] = label_counts.get(label, 0) + 1

        for descriptor in descriptors:
            if not isinstance(descriptor, dict) or "path" not in descriptor:
                continue
            exported.append(
                export_theme(
                    extension_id=extension_id,
                    extension_dir=extension_dir,
                    descriptor=descriptor,
                    out_dir=args.out_dir,
                    tokens_only=args.tokens_only,
                    duplicate_label=label_counts.get(str(descriptor.get("label") or ""), 0) > 1,
                )
            )

    index = {
        "themesMd": str(args.themes_md.resolve()),
        "extensionsDir": str(args.extensions_dir.resolve()),
        "outDir": str(args.out_dir.resolve()),
        "tokensOnly": bool(args.tokens_only),
        "exportedThemeCount": len(exported),
        "missingExtensionCount": len(missing),
        "skippedWithoutThemesCount": len(skipped_without_themes),
        "exported": [record.__dict__ for record in exported],
        "missingExtensions": missing,
        "skippedWithoutThemes": skipped_without_themes,
    }
    (args.out_dir / "index.json").write_text(json.dumps(index, indent=2) + "\n", encoding="utf-8")

    print(f"Exported {len(exported)} theme variants to {args.out_dir}")
    if missing:
        print(f"Missing extensions: {len(missing)}")
    if skipped_without_themes:
        print(f"Linked entries without contributed themes: {len(skipped_without_themes)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

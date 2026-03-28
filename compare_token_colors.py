#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import statistics
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from extract_vscode_themes import load_jsonc


def cli_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Compare VS Code theme tokenColors sections."
    )
    parser.add_argument("old_theme", type=Path, help="Baseline theme JSONC file.")
    parser.add_argument("new_theme", type=Path, help="New theme JSONC file.")
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON instead of a text summary.",
    )
    parser.add_argument(
        "--top",
        type=int,
        default=8,
        help="How many regrouping examples to show in the text summary.",
    )
    return parser.parse_args()


def split_scopes(scope: Any) -> list[str]:
    items = scope if isinstance(scope, list) else [scope]
    selectors: list[str] = []
    for item in items:
        if not isinstance(item, str):
            continue
        for part in item.split(","):
            selector = part.strip()
            if selector:
                selectors.append(selector)
    return selectors


def normalize_settings(settings: Any) -> dict[str, Any]:
    if not isinstance(settings, dict):
        return {}
    normalized: dict[str, Any] = {}
    for key, value in settings.items():
        if isinstance(value, str) and key in {"foreground", "background"}:
            normalized[key] = value.upper()
        else:
            normalized[key] = value
    return normalized


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def selector_specificity(selector: str) -> tuple[int, int]:
    words = selector.split()
    atoms = sum(len([part for part in word.split(".") if part]) for word in words)
    return atoms, len(words)


def rule_selector_count(rule: dict[str, Any]) -> int:
    return len(split_scopes(rule.get("scope")))


def is_prefix_scope(prefix: str, selector: str) -> bool:
    return selector.startswith(prefix + ".") or selector.startswith(prefix + " ")


def load_theme(path: Path) -> dict[str, Any]:
    data = load_jsonc(path)
    token_colors = data.get("tokenColors", [])
    if not isinstance(token_colors, list):
        token_colors = []

    final_selector_settings: dict[str, dict[str, Any]] = {}
    selector_rule_indexes: dict[str, list[int]] = defaultdict(list)
    style_to_selectors: dict[str, set[str]] = defaultdict(set)
    foreground_to_selectors: dict[str | None, set[str]] = defaultdict(set)

    for idx, rule in enumerate(token_colors):
        if not isinstance(rule, dict):
            continue
        settings = normalize_settings(rule.get("settings"))
        style_sig = canonical_json(settings)
        for selector in split_scopes(rule.get("scope")):
            final_selector_settings[selector] = settings
            selector_rule_indexes[selector].append(idx)
            style_to_selectors[style_sig].add(selector)
            foreground_to_selectors[settings.get("foreground")].add(selector)

    selectors = sorted(final_selector_settings)
    atoms = [selector_specificity(selector)[0] for selector in selectors]
    words = [selector_specificity(selector)[1] for selector in selectors]
    per_rule = [rule_selector_count(rule) for rule in token_colors if isinstance(rule, dict)]

    return {
        "path": str(path),
        "token_colors": token_colors,
        "token_rule_count": len(token_colors),
        "selector_mentions": sum(per_rule),
        "unique_selectors": selectors,
        "unique_selector_count": len(selectors),
        "unique_final_styles": len({canonical_json(style) for style in final_selector_settings.values()}),
        "unique_foregrounds": len(
            {style.get("foreground") for style in final_selector_settings.values() if style.get("foreground")}
        ),
        "final_selector_settings": final_selector_settings,
        "selector_rule_indexes": selector_rule_indexes,
        "duplicate_selector_count": sum(1 for indexes in selector_rule_indexes.values() if len(indexes) > 1),
        "avg_atoms": statistics.mean(atoms) if atoms else 0.0,
        "avg_words": statistics.mean(words) if words else 0.0,
        "selectors_with_space": sum(1 for selector in selectors if " " in selector),
        "avg_selectors_per_rule": statistics.mean(per_rule) if per_rule else 0.0,
        "single_selector_rules": sum(1 for count in per_rule if count == 1),
        "style_to_selectors": style_to_selectors,
        "foreground_to_selectors": foreground_to_selectors,
    }


def compare(old_path: Path, new_path: Path) -> dict[str, Any]:
    old = load_theme(old_path)
    new = load_theme(new_path)

    old_rules = [canonical_json(rule) for rule in old["token_colors"]]
    new_rules = [canonical_json(rule) for rule in new["token_colors"]]
    old_rule_counter = Counter(old_rules)
    new_rule_counter = Counter(new_rules)

    common_prefix = 0
    for old_rule, new_rule in zip(old["token_colors"], new["token_colors"]):
        if old_rule != new_rule:
            break
        common_prefix += 1

    exact_rule_added = sum((new_rule_counter - old_rule_counter).values())
    exact_rule_removed = sum((old_rule_counter - new_rule_counter).values())

    old_selectors = set(old["unique_selectors"])
    new_selectors = set(new["unique_selectors"])
    common_selectors = sorted(old_selectors & new_selectors)
    added_selectors = sorted(new_selectors - old_selectors)
    removed_selectors = sorted(old_selectors - new_selectors)

    changed_selectors: list[dict[str, Any]] = []
    for selector in common_selectors:
        old_style = old["final_selector_settings"][selector]
        new_style = new["final_selector_settings"][selector]
        if canonical_json(old_style) == canonical_json(new_style):
            continue
        changed_selectors.append(
            {
                "selector": selector,
                "old_style": old_style,
                "new_style": new_style,
            }
        )

    regroupings: dict[tuple[str, str], list[str]] = defaultdict(list)
    old_to_new_foregrounds: dict[str | None, Counter[str | None]] = defaultdict(Counter)
    new_to_old_foregrounds: dict[str | None, Counter[str | None]] = defaultdict(Counter)
    for item in changed_selectors:
        old_sig = canonical_json(item["old_style"])
        new_sig = canonical_json(item["new_style"])
        regroupings[(old_sig, new_sig)].append(item["selector"])

    for selector in common_selectors:
        old_fg = old["final_selector_settings"][selector].get("foreground")
        new_fg = new["final_selector_settings"][selector].get("foreground")
        old_to_new_foregrounds[old_fg][new_fg] += 1
        new_to_old_foregrounds[new_fg][old_fg] += 1

    added_relation_counts = Counter()
    for selector in added_selectors:
        broader = any(is_prefix_scope(selector, other) for other in old_selectors)
        narrower = any(is_prefix_scope(other, selector) for other in old_selectors)
        if broader and narrower:
            added_relation_counts["both"] += 1
        elif broader:
            added_relation_counts["broader_than_old"] += 1
        elif narrower:
            added_relation_counts["narrower_than_old"] += 1
        else:
            added_relation_counts["unrelated_to_old"] += 1

    top_regroupings: list[dict[str, Any]] = []
    for (old_sig, new_sig), selectors in sorted(
        regroupings.items(), key=lambda item: (-len(item[1]), item[0][0], item[0][1])
    ):
        old_style = json.loads(old_sig)
        new_style = json.loads(new_sig)
        top_regroupings.append(
            {
                "count": len(selectors),
                "old_style": old_style,
                "new_style": new_style,
                "selectors": selectors,
                "old_cluster_size": len(old["style_to_selectors"][old_sig]),
                "new_cluster_size": len(new["style_to_selectors"][new_sig]),
            }
        )

    old_splits = []
    for foreground, mapping in old_to_new_foregrounds.items():
        if len(mapping) < 2:
            continue
        old_splits.append(
            {
                "old_foreground": foreground,
                "count": sum(mapping.values()),
                "new_foregrounds": dict(mapping),
            }
        )
    old_splits.sort(key=lambda item: (-item["count"], str(item["old_foreground"])))

    new_merges = []
    for foreground, mapping in new_to_old_foregrounds.items():
        if len(mapping) < 2:
            continue
        new_merges.append(
            {
                "new_foreground": foreground,
                "count": sum(mapping.values()),
                "old_foregrounds": dict(mapping),
            }
        )
    new_merges.sort(key=lambda item: (-item["count"], str(item["new_foreground"])))

    return {
        "old": old,
        "new": new,
        "comparison": {
            "common_prefix_rules": common_prefix,
            "exact_rule_added": exact_rule_added,
            "exact_rule_removed": exact_rule_removed,
            "common_selectors": len(common_selectors),
            "added_selectors": len(added_selectors),
            "removed_selectors": len(removed_selectors),
            "changed_common_selectors": len(changed_selectors),
            "unchanged_common_selectors": len(common_selectors) - len(changed_selectors),
            "changed_common_selector_pct": (
                round(len(changed_selectors) * 100 / len(common_selectors), 2)
                if common_selectors
                else 0.0
            ),
            "added_selector_relations": dict(added_relation_counts),
            "added_selector_examples": added_selectors[:15],
            "removed_selector_examples": removed_selectors[:15],
            "top_regroupings": top_regroupings,
            "old_color_splits": old_splits,
            "new_color_merges": new_merges,
        },
    }


def print_text_report(result: dict[str, Any], top: int) -> None:
    old = result["old"]
    new = result["new"]
    comparison = result["comparison"]

    print("Counts")
    print(
        f"- rules: {old['token_rule_count']} -> {new['token_rule_count']} "
        f"(+{new['token_rule_count'] - old['token_rule_count']})"
    )
    print(
        f"- selector mentions: {old['selector_mentions']} -> {new['selector_mentions']} "
        f"(+{new['selector_mentions'] - old['selector_mentions']})"
    )
    print(
        f"- unique selectors: {old['unique_selector_count']} -> {new['unique_selector_count']} "
        f"(+{new['unique_selector_count'] - old['unique_selector_count']})"
    )
    print(
        f"- unique final styles: {old['unique_final_styles']} -> {new['unique_final_styles']} "
        f"(+{new['unique_final_styles'] - old['unique_final_styles']})"
    )
    print(
        f"- unique foregrounds: {old['unique_foregrounds']} -> {new['unique_foregrounds']} "
        f"(+{new['unique_foregrounds'] - old['unique_foregrounds']})"
    )
    print()

    print("Structure")
    print(f"- identical rule prefix: {comparison['common_prefix_rules']} rules")
    print(f"- exact rules added: {comparison['exact_rule_added']}")
    print(f"- exact rules removed: {comparison['exact_rule_removed']}")
    print(
        f"- duplicate selectors: {old['duplicate_selector_count']} -> {new['duplicate_selector_count']}"
    )
    print(
        f"- avg selectors per rule: {old['avg_selectors_per_rule']:.2f} -> "
        f"{new['avg_selectors_per_rule']:.2f}"
    )
    print(
        f"- selector specificity (avg atoms): {old['avg_atoms']:.2f} -> {new['avg_atoms']:.2f}"
    )
    print()

    print("Selectors")
    print(f"- common selectors: {comparison['common_selectors']}")
    print(f"- added selectors: {comparison['added_selectors']}")
    print(f"- removed selectors: {comparison['removed_selectors']}")
    print(
        f"- changed shared selectors: {comparison['changed_common_selectors']} "
        f"({comparison['changed_common_selector_pct']}%)"
    )
    print(
        "- added selector relation mix: "
        + ", ".join(
            f"{key}={value}" for key, value in sorted(comparison["added_selector_relations"].items())
        )
    )
    print()

    print("Top Regroupings")
    for item in comparison["top_regroupings"][:top]:
        old_style = item["old_style"] or {}
        new_style = item["new_style"] or {}
        print(
            f"- {item['count']} selector(s): {old_style} -> {new_style} "
            f"(old cluster {item['old_cluster_size']}, new cluster {item['new_cluster_size']})"
        )
        print("  " + ", ".join(item["selectors"][:8]))

    print()
    print("Old Color Splits")
    for item in comparison["old_color_splits"][:top]:
        print(f"- {item['old_foreground']} -> {item['new_foregrounds']}")

    print()
    print("New Color Merges")
    for item in comparison["new_color_merges"][:top]:
        print(f"- {item['new_foreground']} <- {item['old_foregrounds']}")


def main() -> None:
    args = cli_args()
    result = compare(args.old_theme, args.new_theme)
    if args.json:
        print(json.dumps(result, indent=2))
        return
    print_text_report(result, args.top)


if __name__ == "__main__":
    main()

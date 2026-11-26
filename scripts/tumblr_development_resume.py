#!/usr/bin/env python3
"""Read existing module records against the catalog without importing app code.

No writes, provider calls, deployment, new tracker or execution permission.
Normal GPT can read the same records directly in NINE_MODULE_SOURCE_MAP.md.
"""
import argparse
import ast
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import subprocess

ROOT = Path(__file__).resolve().parents[1]
COUNTS = dict(account=6, discovery=7, library=8, media=7, engagement=7,
              publisher=12, campaign=12, referral=9, analytics=11)


def inspect(root=ROOT, module=None):
    root = Path(root).resolve()
    if module is not None and module not in COUNTS:
        raise ValueError("MODULE_UNKNOWN")
    raw = (root / "tumblrkit/action_catalog.py").read_bytes()
    values = {}
    for node in ast.parse(raw.decode("utf-8")).body:
        if (isinstance(node, ast.Assign) and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
                and node.targets[0].id in ("AREAS", "OWNER_UI_ROUTES", "BRIDGE_ROUTES")):
            name = node.targets[0].id
            if name in values:
                raise ValueError("CATALOG_ASSIGNMENT_DUPLICATE")
            values[name] = ast.literal_eval(node.value)
    if set(values) != {"AREAS", "OWNER_UI_ROUTES", "BRIDGE_ROUTES"}:
        raise ValueError("CATALOG_STATIC_RECORDS_MISSING")
    actions, areas = {}, {}
    for domain, _, names, _, _ in values["AREAS"]:
        if domain in areas or COUNTS.get(domain) != len(names):
            raise ValueError("ORIGINAL_MODULE_COUNT_CHANGED")
        areas[domain] = [f"{domain}.{n:02d}" for n in range(1, len(names) + 1)]
        actions.update(zip(areas[domain], names))
    if set(areas) != set(COUNTS) or len(actions) != 79:
        raise ValueError("ORIGINAL_CATALOG_CHANGED")
    ui, bridge = set(values["OWNER_UI_ROUTES"]), set(values["BRIDGE_ROUTES"])
    if ui & bridge or not (ui | bridge) <= actions.keys():
        raise ValueError("CATALOG_ROUTES_INVALID")

    coverage = (root / "docs/79_ACTION_COVERAGE.md").read_text(encoding="utf-8")
    rows = [line for line in coverage.splitlines() if line.startswith("| `")]
    if [line.split("`")[1] for line in rows] != list(actions):
        raise ValueError("COVERAGE_ACTIONS_STALE")
    missing = set(actions) - ui - bridge
    if {line.split("`")[1] for line in rows if "No owner route" in line} != missing:
        raise ValueError("COVERAGE_ROUTES_STALE")
    counts = re.search(r"Exactly (\d+) catalog IDs\. (\d+) link to an owner UI, "
                       r"(\d+) link to a Bridge contract, (\d+) have no route", coverage)
    if not counts or tuple(map(int, counts.groups())) != (79, len(ui), len(bridge), len(missing)):
        raise ValueError("COVERAGE_TOTALS_STALE")

    source_map = (root / "docs/NINE_MODULE_SOURCE_MAP.md").read_text(encoding="utf-8")
    digest = hashlib.sha256(raw).hexdigest()
    if f"<!-- module-resume-catalog-sha256: {digest} -->" not in source_map:
        raise ValueError("MODULE_RECORD_CATALOG_STALE")
    records = {}
    for line in source_map.splitlines():
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if not line.startswith("| ") or cells[0] not in COUNTS:
            continue
        if len(cells) != 6 or cells[0] in records:
            raise ValueError("MODULE_RECORD_INVALID")
        domain, pending, source, tests, next_work, acceptance = cells
        pending_ids = re.findall(r"`([a-z]+\.\d{2})`", pending)
        expected = [i for i in areas[domain] if i in missing]
        if pending_ids != expected:
            raise ValueError("MODULE_RECORD_ACTIONS_STALE")
        groups = []
        for cell in (source, tests):
            paths = re.findall(r"`([^`]+)`", cell)
            if not paths:
                raise ValueError("MODULE_SOURCE_OR_TEST_MISSING")
            for path in paths:
                if (PurePosixPath(path).is_absolute() or ".." in PurePosixPath(path).parts
                        or not (root / path).resolve().is_relative_to(root)
                        or not (root / path).is_file()):
                    raise ValueError("MODULE_PATH_UNVERIFIED")
            groups.append(paths)
        if not next_work or not acceptance:
            raise ValueError("MODULE_NEXT_WORK_MISSING")
        records[domain] = dict(module=domain, action_count=len(areas[domain]),
                               ui_route_count=len(ui & set(areas[domain])),
                               bridge_route_count=len(bridge & set(areas[domain])),
                               no_catalog_route_ids=pending_ids, source_files=groups[0],
                               test_files=groups[1], next_work=next_work,
                               remaining_acceptance=acceptance,
                               runtime_acceptance="NOT_VERIFIED")
    if set(records) != set(COUNTS):
        raise ValueError("MODULE_RECORDS_INCOMPLETE")
    selected = [records[module]] if module else [records[name] for name in COUNTS]
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root,
                          capture_output=True, text=True, timeout=5, check=False)
    status = subprocess.run(["git", "status", "--porcelain"], cwd=root,
                            capture_output=True, text=True, timeout=5, check=False)
    return dict(repository="seigo-gace/tumblr-integration", catalog_sha256=digest,
                source_sha=head.stdout.strip() if head.returncode == 0 else None,
                worktree_dirty=bool(status.stdout) if status.returncode == 0 else None,
                remote_readback="NOT_PERFORMED", product_completion="NOT_VERIFIED",
                routing_is_not_acceptance=True, total_actions=79,
                ui_route_count=len(ui), bridge_route_count=len(bridge),
                no_catalog_route_count=len(missing), modules=selected,
                record_source="docs/NINE_MODULE_SOURCE_MAP.md#module-resume-records")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--module", choices=tuple(COUNTS))
    args = parser.parse_args(argv)
    try:
        print(json.dumps(inspect(module=args.module), ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError, SyntaxError, TypeError, subprocess.TimeoutExpired) as exc:
        code = str(exc) if re.fullmatch(r"[A-Z_]+", str(exc)) else "RECORD_READ_FAILED"
        print(json.dumps({"error": code, "product_completion": "NOT_VERIFIED"}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

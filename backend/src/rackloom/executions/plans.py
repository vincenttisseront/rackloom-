"""Résumé filtré d'un plan Terraform. Le plan JSON brut n'est jamais stocké ni affiché."""

from __future__ import annotations

import json
from typing import Any

ALLOWED_ATTRIBUTES = {"name", "size", "cpu", "memory_mb", "template_ref", "network_ref", "stack_id"}


def _kind(actions: list[str]) -> str:
    if actions == ["no-op"] or actions == ["read"]:
        return "no-op"
    if actions == ["create"]:
        return "create"
    if actions == ["update"]:
        return "update"
    if actions == ["delete"]:
        return "delete"
    if sorted(actions) == ["create", "delete"]:
        return "replace"
    return "unknown"


def summarize(plan_json: dict[str, Any]) -> dict[str, Any]:
    counts = {"create": 0, "update": 0, "delete": 0, "replace": 0, "no-op": 0, "unknown": 0}
    resources = []
    vms: dict[str, dict[str, Any]] = {}
    for rc in plan_json.get("resource_changes", []):
        actions = rc.get("change", {}).get("actions", [])
        kind = _kind(actions)
        counts[kind] += 1
        entry: dict[str, Any] = {"address": rc.get("address"), "type": rc.get("type"), "action": kind}
        after = rc.get("change", {}).get("after") or {}
        sensitive = rc.get("change", {}).get("after_sensitive") or {}
        if rc.get("type") == "local_file" and isinstance(after.get("content"), str) and not sensitive.get("content"):
            try:
                content = json.loads(after["content"])
                entry["attributes"] = {k: v for k, v in content.items() if k in ALLOWED_ATTRIBUTES}
            except ValueError:
                entry["attributes"] = {}
        resources.append(entry)
        index = rc.get("index")
        if isinstance(index, str):
            vm = vms.setdefault(index, {"name": index, "actions": set()})
            vm["actions"].add(kind)
            if "attributes" in entry:
                vm["attributes"] = entry["attributes"]
    vm_list = []
    for name in sorted(vms):
        acts = vms[name]["actions"] - {"no-op"}
        action = "no-op" if not acts else ("replace" if "replace" in acts else
                                            "delete" if "delete" in acts else
                                            "create" if acts == {"create"} else "update")
        vm_list.append({"name": name, "action": action, "attributes": vms[name].get("attributes", {})})
    vm_counts = {k: sum(1 for v in vm_list if v["action"] == k) for k in ("create", "update", "delete",
                                                                         "replace", "no-op")}
    destructive = counts["delete"] > 0 or counts["replace"] > 0 or counts["unknown"] > 0
    return {
        "terraform_version": plan_json.get("terraform_version"),
        "resource_counts": counts,
        "vm_counts": vm_counts,
        "vms": vm_list,
        "resources": resources,
        "destructive": destructive,
        "no_changes": all(counts[k] == 0 for k in ("create", "update", "delete", "replace", "unknown")),
    }

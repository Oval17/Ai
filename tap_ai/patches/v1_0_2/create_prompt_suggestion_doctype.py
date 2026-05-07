"""Patch: create Prompt Suggestion DocType and seed initial templates."""

from __future__ import annotations

from typing import Any, Dict, List

import frappe


DOCTYPE = "Prompt Suggestion"


def _ensure_doctype() -> None:
    if frappe.db.exists("DocType", DOCTYPE):
        return

    # Minimal DocType definition created programmatically
    dt = frappe.new_doc("DocType")
    dt.name = DOCTYPE
    dt.module = "tap_ai"
    dt.istable = 0
    dt.custom = 1
    dt.document_type = "Document"
    dt.fields = [
        {"label": "Identifier", "fieldname": "identifier", "fieldtype": "Data", "reqd": 1},
        {"label": "Title", "fieldname": "title", "fieldtype": "Data", "reqd": 1},
        {"label": "System Prompt", "fieldname": "system_prompt", "fieldtype": "Text", "reqd": 1},
        {"label": "Description", "fieldname": "description", "fieldtype": "Small Text"},
    ]
    dt.permissions = [
        {"role": "System Manager", "read": 1, "write": 1, "create": 1, "delete": 1}
    ]

    try:
        dt.insert(ignore_permissions=True)
    except Exception:
        frappe.log_error(f"Failed to create DocType {DOCTYPE}")


SEED_TEMPLATES: List[Dict[str, Any]] = [
    {
        "identifier": "suggestion_1",
        "title": "TAP Buddy - Short Friendly Persona",
        "system_prompt": "You are TAP Buddy, a friendly and encouraging WhatsApp chatbot for school students in India...",
        "description": "Short persona for WhatsApp interactions",
    },
    {
        "identifier": "suggestion_2",
        "title": "TAP Buddy - Didactic Didi Persona",
        "system_prompt": "You are TAP Buddy, a warm and encouraging WhatsApp learning companion...",
        "description": "Longer mentor persona",
    },
]


def _seed() -> None:
    for entry in SEED_TEMPLATES:
        try:
            existing = frappe.get_all(DOCTYPE, filters={"identifier": entry["identifier"]}, limit=1)
            if existing:
                continue
            doc = frappe.new_doc({"doctype": DOCTYPE})
            for k, v in entry.items():
                doc.set(k, v)
            doc.insert(ignore_permissions=True)
        except Exception:
            frappe.log_error(f"Failed to seed prompt suggestion {entry.get('identifier')}")


def execute() -> None:
    _ensure_doctype()
    _seed()

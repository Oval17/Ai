"""Patch: ensure Prompt Suggestion DocType exists and seed initial templates."""

from __future__ import annotations

from typing import Any, Dict, List

import frappe


DOCTYPE = "Prompt Suggestion"


def _ensure_doctype() -> None:
    if frappe.db.exists("DocType", DOCTYPE):
        return

    # Load file-backed doctype definition from app files.
    frappe.reload_doc("tap_ai", "doctype", "prompt_suggestion")


SEED_TEMPLATES: List[Dict[str, Any]] = [
    {
        "identifier": "default",
        "is_default": 1,
        "is_active": 1,
        "title": "TAP Buddy - Default Persona",
        "system_prompt": "You are TAP Buddy, a friendly and encouraging WhatsApp chatbot for school students in India. Use simple language, keep responses short, and gently redirect students back to learning activities.",
        "description": "Default fallback persona",
    },
    {
        "identifier": "suggestion_1",
        "is_default": 0,
        "is_active": 1,
        "title": "TAP Buddy - Short Friendly Persona",
        "system_prompt": "You are TAP Buddy, a friendly and encouraging WhatsApp chatbot for school students in India...",
        "description": "Short persona for WhatsApp interactions",
    },
    {
        "identifier": "suggestion_2",
        "is_default": 0,
        "is_active": 1,
        "title": "TAP Buddy - Didactic Didi Persona",
        "system_prompt": "You are TAP Buddy, a warm and encouraging WhatsApp learning companion...",
        "description": "Longer mentor persona",
    },
]


def _seed() -> None:
    for entry in SEED_TEMPLATES:
        try:
            existing = frappe.get_all(DOCTYPE, filters={"identifier": entry["identifier"]}, fields=["name"], limit=1)
            if existing:
                doc = frappe.get_doc(DOCTYPE, existing[0].name)
            else:
                doc = frappe.new_doc(DOCTYPE)

            for k, v in entry.items():
                doc.set(k, v)
            doc.save(ignore_permissions=True)
        except Exception:
            frappe.log_error(f"Failed to seed prompt suggestion {entry.get('identifier')}")


def execute() -> None:
    _ensure_doctype()
    _seed()

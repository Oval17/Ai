"""Prompt bank service: load prompt suggestions and render system prompts."""

from __future__ import annotations

import json
import os
from typing import Any, Dict, Optional

import frappe

SCHEMA_PATH = os.path.join(os.path.dirname(__file__), "..", "schema", "prompt_suggestions.json")
CACHE_KEY = "tap_ai:prompt_suggestions:v1"


def _load_from_disk() -> Dict[str, Any]:
    try:
        with open(SCHEMA_PATH, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return {}


def _get_all() -> Dict[str, Any]:
    try:
        cached = frappe.cache().get(CACHE_KEY)
        if cached:
            if isinstance(cached, bytes):
                cached = cached.decode("utf-8")
            return json.loads(cached)
    except Exception:
        pass

    data = _load_from_disk()
    try:
        frappe.cache().set(CACHE_KEY, json.dumps(data), ex=3600)
    except Exception:
        pass
    return data


def invalidate_prompt_cache() -> None:
    try:
        frappe.cache().delete(CACHE_KEY)
    except Exception:
        pass


def get_prompt_by_id(prompt_id: str = "default") -> Optional[Dict[str, Any]]:
    all_prompts = _get_all()
    # Accept either top-level keys or id field
    if prompt_id in all_prompts:
        return all_prompts[prompt_id]

    for _, val in all_prompts.items():
        if isinstance(val, dict) and val.get("id") == prompt_id:
            return val

    return None


def render_system_prompt(
    prompt_id: str = "default",
    student_name: Optional[str] = None,
    current_step: Optional[str] = None,
    topic_name: Optional[str] = None,
    class_name: Optional[str] = None,
    state: Optional[str] = None,
    language: Optional[str] = None,
) -> str:
    prompt = get_prompt_by_id(prompt_id) or get_prompt_by_id("default") or {}
    text = prompt.get("system_prompt", "")
    replacements = {
        "[STUDENT_NAME]": student_name or "Student",
        "[CURRENT_STEP]": current_step or "the activity",
        "[TOPIC_NAME]": topic_name or "the topic",
        "[CLASS]": class_name or "",
        "[STATE]": state or "",
        "[LANGUAGE]": language or "",
    }

    for token, val in replacements.items():
        text = text.replace(token, val)

    return text.strip()


def get_system_message_for_context(
    prompt_id: str = "default",
    user_profile: Optional[Dict[str, Any]] = None,
    content_details: Optional[Dict[str, Any]] = None,
) -> str:
    student_name = None
    class_name = None
    state = None
    language = None
    current_step = None
    topic_name = None

    if user_profile:
        student_name = user_profile.get("name")
        class_name = str(user_profile.get("grade") or user_profile.get("class") or "")
        language = user_profile.get("language")

    if content_details:
        topic_name = content_details.get("title")
        current_step = content_details.get("current_step") or content_details.get("step")

    return render_system_prompt(
        prompt_id=prompt_id,
        student_name=student_name,
        current_step=current_step,
        topic_name=topic_name,
        class_name=class_name,
        state=state,
        language=language,
    )

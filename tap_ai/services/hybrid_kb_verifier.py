"""
LLM-first Knowledge Bank Verifier

Flow:
1. Classify the user's query category using a strict LLM classifier (returns JSON).
2. If a KB category is returned: fetch all entries for that category and ask a selection LLM
    to choose the best entry or synthesize a short KB-grounded reply.
3. If the classifier returns "uncertain" (or fails): generate a direct LLM reply (no fuzzy/probe fallback).
4. Cache LLM outputs to reduce repeated latency.

This verifier replaces the previous fuzzy/probe-based acceptance flow with an LLM-first
classification + selection pipeline. The function returns a dict with keys: question,
answer, response_type, user_context, and metadata.
"""

import json
import time
import hashlib
from typing import Any, Dict, List, Optional

import frappe
from tap_ai.infra.config import get_config
from tap_ai.infra.llm_client import LLMClient
from tap_ai.services.direct_response_bank import (
    # probe_direct_response_match,  # probe-based flow replaced by LLM-first
    get_direct_response_entries,
    get_entries_for_category,
    _render_response,
)
from tap_ai.services.prompt_bank import get_system_message_for_context


LLM_VERIFIER_CACHE_TTL = 900  # 15 minutes


def _llm(model: Optional[str] = None, temperature: float = 0.0, max_tokens: int = 800):
    return LLMClient.get_client(model=model or (get_config("primary_llm_model") or "gpt-4o-mini"),
                                temperature=temperature, max_tokens=max_tokens)


def _llm_invoke_cached(messages: List, model: str, temperature: float = 0.0, cache_ttl: int = LLM_VERIFIER_CACHE_TTL, max_tokens: int = 800) -> str:
    try:
        payload = {"messages": messages, "model": model, "temperature": temperature}
        cache_key = "llm_verifier:" + hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode("utf-8")).hexdigest()
        cached = frappe.cache().get(cache_key)
        if cached:
            if isinstance(cached, bytes):
                cached = cached.decode("utf-8", errors="ignore")
            print(f"> Verifier LLM cache hit {cache_key[:12]}...")
            return str(cached)
    except Exception:
        cache_key = None

    llm = _llm(model=model, temperature=temperature, max_tokens=max_tokens)
    start = time.time()
    resp = llm.invoke(messages)
    content = getattr(resp, "content", "") or ""
    content = str(content).strip()

    try:
        if cache_key and content:
            frappe.cache().set(cache_key, content, ex=cache_ttl)
    except Exception:
        pass

    print(f"> Verifier LLM invoke ({model}) took {int((time.time() - start) * 1000)}ms")
    return content


SYSTEM_PROMPT = '''You are an assistant that decides whether a curated Knowledge Bank response matches the user's intent.

Input includes:
- User query
- Candidate knowledge-bank entry metadata: title, matched_query, match_score, category
- The curated KB response text

Task:
1) If the user's intent is the same as the candidate's intent, return JSON: {"action": "use_kb", "final_answer": "<the KB response possibly lightly personalized>", "reason": "short explanation"}
2) If the intent is different, return JSON: {"action": "llm_answer", "final_answer": "<LLM-generated answer>", "reason": "short explanation"}

Intent matters more than fuzzy score.
If the query is a greeting, small talk, identity question, or program explanation and the KB candidate matches that same intent, use KB.
If the query asks something else, do not force KB just because the response is semantically related.

Be concise. Preserve any essential facts from the KB when using it. Personalize using student name/grade if provided.

Return ONLY JSON and nothing else.
'''


CLASSIFIER_SYSTEM_PROMPT = """You are a strict intent classifier for an educational learning companion. You will receive JSON input containing a user query and must return EXACT JSON with one key: {"category":"<one-of-allowed_categories-or-uncertain>"}. Do not return any text outside the JSON.

Allowed Categories: "Greetings", "Gibberish", "Trying to Talk", "Requests", "uncertain".

Use the following strict routing hints to classify the query:
- "Greetings": Use for general hellos (hi, sup), goodbyes (bye, tata), hi/bye emoji, time-based greetings (good morning, good night), and festival/holiday wishes (Happy Diwali, Merry Christmas).
- "Gibberish": Use for random character spam, emoji-only or emoji-dominant messages, AND very short/unclear acknowledgments like "Ok", "K", "Hmm", "Acha", or "thik hai".
- "Trying to Talk": Use for questions about the bot's identity or the program (who are you, what are points/videos), asking to chat/listen, expressing boredom, reporting problems/asking for help, stating an activity is complete ("done", "submit kar diya"), refusing to submit ("no", "boring"), asking school admin questions, being stuck on ideas ("kya likhun", "no ideas"), expressing excitement/pride, or sharing class promotion news.
- "Requests": Use for actionable account or task requests, including changing the language, correcting a wrong name, asking HOW to do a submission/what it is ("how to submit", "explain submission"), asking for more time/delaying ("busy hoon", "later"), or affirming readiness to continue ("Yes", "Ready", "chalo", "Continue").

Disambiguation Rules:
- If the user says they FINISHED the submission -> "Trying to Talk".
- If the user asks HOW to do the submission or WHAT it is -> "Requests".
- If the user says they will do it LATER ("baad mein karunga") -> "Requests".

If the query does not fit any category clearly, return "uncertain"."""

SELECTION_SYSTEM_PROMPT = '''You are a strict responder that MUST return EXACT JSON only. Input: user_query and an array `entries` where each entry has `id`, `student_query`, `alternate_queries`, `response`. Choose best matching entry or synthesize a short reply grounded in entries.

Return one of:
{"match":"<id>","source":"kb_exact"}
{"match":"<id>","source":"kb_synthesized","synthesized":"<short reply>"}
{"match":null,"source":"llm_generated","synthesized":"<short reply>"}

Do NOT invent ids. Keep synthesized replies concise (1-2 sentences).'''


def verify_and_respond(query: str, user_profile: Optional[Dict[str, Any]] = None, chat_history: Optional[List[Dict[str, str]]] = None) -> Dict[str, Any]:
    """LLM-first classifier + selection flow.

    1. Classify query category with a small LLM classifier.
    2. If category != "uncertain": fetch entries for that category and ask selection LLM to choose or synthesize.
    3. If category == "uncertain": generate a direct LLM reply (no fuzzy probe fallback).
    """
    start = time.perf_counter()
    chat_history = chat_history or []

    # 1) Determine allowed categories from KB
    all_entries = get_direct_response_entries()
    allowed_categories = sorted({(e.get("category") or "").strip() for e in all_entries if e and e.get("is_active", 1)})

    # Build classifier prompt payload
    classifier_input = {"query": query, "allowed_categories": allowed_categories}
    classifier_messages = []
    try:
        persona = get_system_message_for_context(user_profile=user_profile)
        classifier_messages.append(("system", persona))
    except Exception:
        pass
    classifier_messages.append(("system", CLASSIFIER_SYSTEM_PROMPT))
    classifier_messages.append(("user", json.dumps(classifier_input)))

    model = get_config("primary_llm_model") or "gpt-4o-mini"
    raw_classify = _llm_invoke_cached(classifier_messages, model=model, temperature=0.0, max_tokens=60)

    # Parse classifier output
    category = None
    try:
        cleaned = raw_classify.replace("```json", "").replace("```", "").strip()
        parsed = json.loads(cleaned)
        category = parsed.get("category")
    except Exception:
        category = None

    # If classifier failed or returned uncertain, generate LLM response directly
    if not category or (isinstance(category, str) and category.strip().lower() == "uncertain"):
        # Direct LLM generation (persona + system + user query)
        messages = []
        try:
            persona = get_system_message_for_context(user_profile=user_profile)
            messages.append(("system", persona))
        except Exception:
            pass
        messages.append(("system", "You are TAP Buddy. Answer concisely in a friendly student-facing tone."))
        if chat_history:
            messages.append(("system", "Recent chat: " + " | ".join([m.get('content','') for m in chat_history[-3:]])))
        messages.append(("user", query))
        raw = _llm_invoke_cached(messages, model=model, temperature=0.3, max_tokens=300)
        timing_ms = int((time.perf_counter() - start) * 1000)
        return {
            "question": query,
            "answer": str(raw).strip(),
            "response_type": "llm_generated",
            "user_context": "personalized" if user_profile else "general",
            "metadata": {"classifier_raw": raw_classify, "decision": "llm_generated", "timing_ms": timing_ms},
        }

    # 2) Fetch entries for chosen category and call selection LLM
    entries = get_entries_for_category(category)
    # Build entries payload (keep fields user requested)
    entries_payload = [
        {"id": e.get("id"), "student_query": e.get("student_query"), "alternate_queries": e.get("alternate_queries"), "response": e.get("response")}
        for e in entries
    ]

    selection_messages = []
    try:
        persona = get_system_message_for_context(user_profile=user_profile)
        selection_messages.append(("system", persona))
    except Exception:
        pass

    selection_messages.append(("system", SELECTION_SYSTEM_PROMPT))
    selection_messages.append(("user", json.dumps({"user_query": query, "entries": entries_payload}, ensure_ascii=False)))

    raw_selection = _llm_invoke_cached(selection_messages, model=model, temperature=0.2, max_tokens=600)

    # Parse selection LLM JSON
    try:
        cleaned = raw_selection.replace("```json", "").replace("```", "").strip()
        selection_decision = json.loads(cleaned)
    except Exception:
        # If malformed, fall back to generating an LLM reply (safe path)
        raw = _llm_invoke_cached([( "system", "You are TAP Buddy."), ("user", query)], model=model, temperature=0.3, max_tokens=300)
        timing_ms = int((time.perf_counter() - start) * 1000)
        return {"question": query, "answer": str(raw).strip(), "response_type": "llm_generated", "user_context": "personalized" if user_profile else "general", "metadata": {"classifier": category, "selection_raw": raw_selection, "decision": "llm_generated_malformed_selection", "timing_ms": timing_ms}}

    match = selection_decision.get("match")
    source = selection_decision.get("source")
    synthesized = selection_decision.get("synthesized")

    # If matched a KB id, find entry and render response
    answer_text = ""
    if match:
        entry_map = {e.get("id"): e for e in entries}
        chosen = entry_map.get(match)
        if chosen:
            base = chosen.get("response") or ""
            if source == "kb_exact":
                answer_text = _render_response(base, user_profile=user_profile)
                response_type = "knowledge_bank"
            else:
                # kb_synthesized
                if synthesized:
                    answer_text = synthesized
                else:
                    answer_text = _render_response(base, user_profile=user_profile)
                response_type = "knowledge_bank_synthesized"
        else:
            # LLM returned an id we don't have -> treat as llm_generated
            answer_text = synthesized or ""
            response_type = "llm_generated"
    else:
        # No KB match selected
        answer_text = synthesized or ""
        response_type = "llm_generated" if source == "llm_generated" else "knowledge_bank_synthesized"

    timing_ms = int((time.perf_counter() - start) * 1000)
    return {
        "question": query,
        "answer": answer_text,
        "response_type": response_type,
        "user_context": "personalized" if user_profile else "general",
        "metadata": {"classifier": category, "selection_raw": raw_selection, "decision": source, "timing_ms": timing_ms},
    }

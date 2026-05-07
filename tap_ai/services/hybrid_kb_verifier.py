"""
Hybrid Knowledge Bank Verifier

Flow:
1. Run fuzzy/probe match to get the best KB candidate (even if below threshold)
2. Provide the query + candidate metadata + KB response to the LLM
3. LLM decides: accept KB response (return it) OR generate an original answer
4. Cache LLM verification results to reduce repeated latency

Return structure mirrors other answerers: question, answer, response_type, user_context, metadata
"""

import json
import time
import hashlib
from typing import Any, Dict, List, Optional

import frappe
from tap_ai.infra.config import get_config
from tap_ai.infra.llm_client import LLMClient
from tap_ai.services.direct_response_bank import (
    probe_direct_response_match,
    get_direct_response_entries,
    _render_response,
)


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


SYSTEM_PROMPT = '''You are an assistant that decides whether a curated Knowledge Bank response fits a user's query.

Input includes:
- User query
- Candidate knowledge-bank entry metadata: title, matched_query, match_score, category
- The curated KB response text

Task:
1) If the candidate KB response directly answers the user's query and is appropriate, return JSON: {"action": "use_kb", "final_answer": "<the KB response possibly lightly personalized>", "reason": "short explanation"}
2) If the KB response is not appropriate, generate a helpful answer from your own knowledge and return JSON: {"action": "llm_answer", "final_answer": "<LLM-generated answer>", "reason": "short explanation"}

Be concise. Preserve any essential facts from the KB when using it. Personalize using student name/grade if provided.

Return ONLY JSON and nothing else.
'''


def verify_and_respond(query: str, user_profile: Optional[Dict[str, Any]] = None, chat_history: Optional[List[Dict[str, str]]] = None) -> Dict[str, Any]:
    start = time.perf_counter()
    chat_history = chat_history or []

    # Probe best KB candidate (even if below threshold)
    probe = probe_direct_response_match(query)

    kb_info = probe.get("knowledge_bank") or {}
    kb_name = kb_info.get("name")

    # Load entries to find full entry if available
    entries = get_direct_response_entries()
    matched_entry = None
    if kb_name:
        for e in entries:
            if e.get("name") == kb_name:
                matched_entry = e
                break

    kb_response_text = ""
    if matched_entry:
        kb_response_text = _render_response(matched_entry.get("response", ""), user_profile=user_profile)

    # Build LLM prompt with candidate metadata
    personalization = ""
    if user_profile and user_profile.get("name"):
        personalization = f"Student: {user_profile.get('name')}"
        if user_profile.get("grade"):
            personalization += f", Grade: {user_profile.get('grade')}"

    candidate_preview = {
        "title": kb_info.get("title"),
        "matched_query": kb_info.get("matched_query"),
        "match_score": probe.get("best_score"),
        "category": kb_info.get("category"),
    }

    user_context = personalization
    if chat_history:
        user_context += "\nRecent chat: " + " | ".join([m.get('content','') for m in chat_history[-3:]])

    messages = [
        ("system", SYSTEM_PROMPT),
        ("system", f"Candidate metadata: {json.dumps(candidate_preview, default=str)}"),
        ("system", f"Candidate KB response: {kb_response_text[:200]}")
    ]

    if user_context:
        messages.append(("system", user_context))

    messages.append(("user", f"User query: {query}\n\nDecide whether to use the candidate KB response or generate an answer."))

    model = get_config("primary_llm_model") or "gpt-4o-mini"
    raw = _llm_invoke_cached(messages, model=model, temperature=0.2)

    # Try to parse JSON from LLM
    decision = None
    try:
        cleaned = raw.replace("```json", "").replace("```", "").strip()
        decision = json.loads(cleaned)
    except Exception:
        # If LLM didn't return JSON, fall back: if probe accepted, use KB; else use LLM raw answer
        if probe.get("matched"):
            return {
                "question": query,
                "answer": kb_response_text,
                "response_type": "knowledge_bank_verified",
                "user_context": "personalized" if user_profile else "general",
                "metadata": {"knowledge_bank_probe": probe, "decision_reason": "llm_malformed_output_fallback_to_probe"},
            }
        else:
            return {
                "question": query,
                "answer": raw,
                "response_type": "llm_generated",
                "user_context": "personalized" if user_profile else "general",
                "metadata": {"knowledge_bank_probe": probe, "decision_reason": "llm_malformed_output_no_probe"},
            }

    action = (decision.get("action") or "").lower()
    final_answer = decision.get("final_answer") or ""
    reason = decision.get("reason") or ""

    timing_ms = int((time.perf_counter() - start) * 1000)

    if action == "use_kb":
        # Prefer the KB-rendered response, but allow LLM to slightly adjust wording (final_answer may include it)
        answer_text = final_answer or kb_response_text or ""
        return {
            "question": query,
            "answer": answer_text,
            "response_type": "knowledge_bank_verified",
            "user_context": "personalized" if user_profile else "general",
            "metadata": {
                "knowledge_bank_probe": probe,
                "decision": "use_kb",
                "decision_reason": reason,
                "timing_ms": timing_ms,
            },
        }

    # Default: LLM generated
    answer_text = final_answer or raw or ""
    return {
        "question": query,
        "answer": answer_text,
        "response_type": "llm_generated",
        "user_context": "personalized" if user_profile else "general",
        "metadata": {
            "knowledge_bank_probe": probe,
            "decision": "llm_answer",
            "decision_reason": reason,
            "timing_ms": timing_ms,
        },
    }

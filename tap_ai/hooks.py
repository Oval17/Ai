app_name = "tap_ai"
app_title = "Tap Ai"
app_publisher = "Anish Aman"
app_description = "LMS system for tap"
app_email = "tech4dev@gmail.com"
app_license = "MIT"


# ======================================================
# DOC_EVENTS - Cache Invalidation for Knowledge Bank
# ======================================================
doc_events = {
	"TAP Response Knowledge": {
		"after_insert": "tap_ai.services.direct_response_bank.invalidate_kb_cache",
		"after_update": "tap_ai.services.direct_response_bank.invalidate_kb_cache",
		"after_delete": "tap_ai.services.direct_response_bank.invalidate_kb_cache",
	}
}

# Invalidate prompt cache when prompt suggestions (if implemented as a doctype) change
doc_events["Prompt Suggestion"] = {
    "after_insert": "tap_ai.services.prompt_bank.invalidate_prompt_cache",
    "after_update": "tap_ai.services.prompt_bank.invalidate_prompt_cache",
    "after_delete": "tap_ai.services.prompt_bank.invalidate_prompt_cache",
}



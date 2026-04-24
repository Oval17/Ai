# TAP AI — AI Agent Context

Python conversational AI engine built on Frappe. Intelligently routes student queries
to either a Text-to-SQL engine or a Vector RAG engine, with async processing via RabbitMQ.

## Architecture at a Glance
```
Student query (text or voice)
        │
        ▼
tap_ai/api/query.py         ← single entry-point API (Frappe whitelisted)
        │
        ▼
LLM router (llm_worker.py)  ← decides: SQL or RAG?
        │
   ┌────┴─────┐
   │ SQL      │ RAG
   ▼          ▼
remote_db   Pinecone
(PostgreSQL) vector store
   │          │
   └────┬─────┘
        ▼
   result.py / voice_result.py  ← format + return response
```

## Key Files
| File | Purpose |
|---|---|
| `tap_ai/api/query.py` | Main entry point — receives the query, publishes to RabbitMQ |
| `tap_ai/api/result.py` | Polls for result after worker finishes |
| `tap_ai/api/voice_query.py` | Voice input variant — receives audio, calls STT first |
| `tap_ai/api/voice_result.py` | Voice output — calls TTS on the answer |
| `tap_ai/workers/llm_worker.py` | RabbitMQ consumer — routes query to SQL or RAG |
| `tap_ai/workers/stt_worker.py` | Speech-to-Text via Whisper |
| `tap_ai/workers/tts_worker.py` | Text-to-Speech for voice output |
| `tap_ai/infra/config.py` | `TAPConfig` — loads from Frappe `site_config.json` first |
| `tap_ai/infra/llm_client.py` | OpenAI client wrapper |
| `tap_ai/infra/sql_catalog.py` | DB schema catalog for Text-to-SQL |
| `tap_ai/utils/remote_db.py` | PostgreSQL connection to `data.evalix.xyz` |
| `tap_ai/utils/mq.py` | RabbitMQ publish/consume helpers |
| `tap_ai/utils/dynamic_config.py` | Runtime config updates from TAP LMS |
| `tap_ai/schema/generate_schema.py` | Generates SQL catalog from live DB schema |

## Configuration
All secrets live in Frappe's `site_config.json` (never hardcoded). `TAPConfig` reads them:
```python
from tap_ai.infra.config import TAPConfig
config = TAPConfig()
openai_key = config.get("openai_api_key")
pinecone_key = config.get("pinecone_api_key")
```
Keys expected in `site_config.json`:
- `openai_api_key`
- `pinecone_api_key`, `pinecone_index`
- `remote_db_host`, `remote_db_name`, `remote_db_user`, `remote_db_password`
- `rabbitmq_host`, `rabbitmq_port`
- `enable_voice` (boolean)

**Never add API keys to source code or commit them to git.**

## Tech Stack
- Python **3.10+**
- Frappe **v15** (framework + API layer)
- OpenAI GPT (LLM routing + SQL generation + answer synthesis)
- Pinecone (vector store for RAG)
- PostgreSQL on `data.evalix.xyz` (remote LMS data)
- RabbitMQ + Pika (async processing)
- Redis (caching, conversation history)
- Flask (Telegram webhook — `telegram_webhook.py`, runs separately from Frappe)

## Running Workers
Workers must be started separately from `bench start`:
```bash
# In a separate terminal in your bench directory:
python tap_ai/workers/llm_worker.py
python tap_ai/workers/stt_worker.py   # only if voice is enabled
python tap_ai/workers/tts_worker.py   # only if voice is enabled
```
Without workers running, API calls will enqueue but never return a result.

## Code Style
- **Ruff** for linting (`ruff check .`) and formatting (`ruff format .`)
- All new functions must have type annotations
- Config values always go through `TAPConfig.get()` — never `os.environ` directly
- RabbitMQ messages are JSON-serialised dicts; always document the expected shape in a comment

## Common Pitfalls
- **Workers not running** → API enqueues but hangs. Check RabbitMQ management UI.
- **Pinecone index mismatch** → ensure `pinecone_index` in `site_config.json` matches the deployed index name
- **Remote DB connection** → `data.evalix.xyz` requires VPN or IP allowlist in production
- **Schema cache stale** → re-run `generate_schema.py` after DB schema changes

## Testing
```bash
# Sanity-check remote DB connectivity
python test_remote_connection.py

# Frappe-context tests
bench run-tests --app tap_ai
```

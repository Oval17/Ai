# 🧪 Performance Optimization Testing Checklist

**Status**: Ready for testing  
**Time to complete**: 30-45 minutes  
**Risk level**: Low (backward compatible)

---

## Pre-Deployment Validation

### ✅ Code Quality Checks
- [ ] No import errors: `python -c "import tap_ai.services.router; import tap_ai.utils.remote_db"`
- [ ] Syntax valid: Run through Python parser
- [ ] All files have 🚀 markers for changes (easy to identify)
- [ ] No hardcoded values (all configurable)

### ✅ Dependency Verification
- [ ] psycopg2 installed with pool support
- [ ] frappe.cache() available and working
- [ ] ThreadPoolExecutor available (stdlib)
- [ ] hashlib available (stdlib)

---

## Phase-by-Phase Testing

### 🟢 **Phase 1: Immediate Tests (15 mins)**

#### Test 1.1: Worker Concurrency
```bash
# Verify prefetch_count changed
grep -n "basic_qos" tap_ai/workers/llm_worker.py
# Expected: prefetch_count=worker_concurrency (not hardcoded 1)

# Test with environment variable
export TAP_AI_WORKER_CONCURRENCY=4
# Start worker, should log: "concurrency=4"
```

#### Test 1.2: LLM Caching
```bash
cd /home/frappe/frappe-bench

# Test 1: Same routing query twice
bench execute tap_ai.services.router.cli \
  --kwargs "{'q':'list videos','user_id':'test_user_1'}"

# Test 2: Run again (should see cache hit)
bench execute tap_ai.services.router.cli \
  --kwargs "{'q':'list videos','user_id':'test_user_1'}"

# Expected: Second run shows "✓ LLM cache hit: llm_cache:gpt-4o-mini:..."
```

#### Test 1.3: Embedding Caching
```bash
# Test with Pinecone search
bench execute tap_ai.services.pinecone_store.cli_search_auto \
  --kwargs "{'q':'financial literacy'}"

# Run again (should see embedding cache hit)
bench execute tap_ai.services.pinecone_store.cli_search_auto \
  --kwargs "{'q':'financial literacy'}"

# Expected: "✓ Embedding cache hit: embedding:text-embedding-3-small:..."
```

---

### 🟡 **Phase 2: Database & Parallel Tests (15 mins)**

#### Test 2.1: Connection Pooling
```bash
# Check pool is initialized
python -c "
from tap_ai.utils.remote_db import _remote_db_pool
pool = _remote_db_pool.get_pool()
print(f'Pool size: min-max connected')
"

# Expected: "✓ Remote DB pool created: 5-20 connections"
```

#### Test 2.2: Batch DB Queries
```bash
# Mock a RAG context fetch to verify batching
python -c "
from tap_ai.services.rag_answerer import _build_context_from_hits

# Create mock hits
hits = [
    {'metadata': {'doctype': 'VideoClass', 'record_ids': ['video1', 'video2']}, 'score': 0.9},
    {'metadata': {'doctype': 'VideoClass', 'record_ids': ['video3']}, 'score': 0.85},
    {'metadata': {'doctype': 'Course', 'record_ids': ['course1']}, 'score': 0.8},
]

# This should group by doctype and batch query
# Monitor DB queries: should see 2 queries (not 3)
ctx = _build_context_from_hits(hits)
print(f'Built context with {len(ctx[\"sources\"])} sources')
"
```

#### Test 2.3: Parallel Pinecone Search
```bash
# Verify parallel execution (visual inspection)
bench execute tap_ai.services.pinecone_store.cli_search_auto \
  --kwargs "{'q':'summarize financial literacy', 'route_top_n': 4}"

# Expected: Should execute ~4 Pinecone queries in parallel (~200ms vs 800ms sequential)
```

---

### 🔴 **Phase 3: Upsert & Request Handling Tests (10 mins)**

#### Test 3.1: Large Embedding Batch
```bash
# Upsert a small doctype to verify batch_size=100
bench execute tap_ai.services.pinecone_store.upsert_doctype \
  --kwargs "{'doctype': 'Quiz', 'since': None}"

# Monitor OpenAI API calls: should see ~10% of previous (batch 100 vs 10)
```

#### Test 3.2: Request Deduplication
```bash
# Simulate rapid-fire duplicate requests
curl -X POST http://localhost:8000/api/method/tap_ai.api.query.query \
  -H "Content-Type: application/json" \
  -d '{"q":"list videos","user_id":"dup_test"}'

# Wait <100ms, repeat same request
# Expected: Should get same request_id with "deduplicated": true

# Wait >3 seconds, repeat request
# Expected: Should get new request_id
```

---

## Performance Baseline Tests

### Benchmark Setup
```bash
# Create test data (few queries to warm up cache)
bench execute tap_ai.services.router.cli \
  --kwargs "{'q':'what are learning objectives?','user_id':'bench_user'}"

bench execute tap_ai.services.router.cli \
  --kwargs "{'q':'summarize financial literacy','user_id':'bench_user'}"
```

### Measure Latency (Before & After)
```bash
# Use clickhouse_store.py benchmark as reference
bench execute tap_ai.services.clickhouse_store.cli_benchmark \
  --kwargs "{'q':'summarize the video on financial literacy', 'repeat': 5}"

# Compare Pinecone avg_ms with and without optimizations
# Expected:
#   Without: 8000-10000ms
#   With:    4000-5000ms
#   Improvement: -50%
```

---

## Load Testing (Optional)

### Simulate Concurrent Users
```bash
# Install wrk if not present
# wrk -t4 -c10 -d30s \
#   -s load_test.lua \
#   http://localhost:8000/api/method/tap_ai.api.query.query

# Where load_test.lua simulates 10 concurrent users
```

---

## Monitoring During Tests

### Watch Cache Performance
```bash
# Monitor cache hits in real-time
redis-cli
> MONITOR

# Run tests and watch for cache operations:
# - llm_cache:gpt-4o-mini:* (LLM caching)
# - embedding:text-embedding-3-small:* (Embedding caching)
# - dedup_*:* (Request deduplication)
```

### Check Database Connection Pool
```bash
# Monitor active connections
psql -h data.evalix.xyz -U tap_ai_user -d tap_lms_db -c \
  "SELECT count(*) FROM pg_stat_activity WHERE datname='tap_lms_db';"

# Expected: Should stay within 5-20 (configured pool range)
```

---

## Validation Results Checklist

- [ ] **Phase 1 Passed**: All LLM/embedding caches working
- [ ] **Phase 2 Passed**: Connection pool initialized, queries batched
- [ ] **Phase 3 Passed**: Large batches processing, dedup working
- [ ] **No Regressions**: Existing queries still work
- [ ] **Latency Improvement**: Measured 25-50% improvement
- [ ] **Throughput Improvement**: 4-5x increase in concurrent requests
- [ ] **Cache Hit Rate**: >20% within 10 queries

---

## Deployment Steps

### 1. Backup Current State
```bash
cd /home/frappe/frappe-bench/apps/tap_ai
git add -A && git commit -m "Pre-optimization backup"
```

### 2. Deploy Changes
```bash
bench execute tap_ai.api.query.query  # Test API
```

### 3. Restart Workers
```bash
# Kill old workers
pkill -f "llm_worker\|stt_worker\|tts_worker"

# Start new workers with optimization
export TAP_AI_WORKER_CONCURRENCY=8
bench worker start
```

### 4. Monitor for 24 Hours
```bash
# Watch logs
tail -f frappe-bench/logs/bench.log | grep -E "cache|pool|dedup|queries"
```

---

## Rollback Procedure (If Issues Found)

```bash
# 1. Identify issue
# 2. Disable specific optimization:

# Disable cache:
redis-cli FLUSHALL

# Disable pool:
export TAP_AI_WORKER_CONCURRENCY=1

# 3. Restart workers
pkill -f llm_worker
bench worker start
```

---

## Success Criteria

✅ All Phase 1-3 tests pass  
✅ No errors in logs  
✅ Cache hit rates > 20%  
✅ Latency reduction ≥ 25%  
✅ No increase in memory usage  
✅ No database connection exhaustion  

---

## Sign-Off

- [ ] Tested by: _________________
- [ ] Date: _________________
- [ ] Ready for production: [ ] YES [ ] NO
- [ ] Notes: _________________

---

## Quick Troubleshooting

| Issue | Solution |
|-------|----------|
| "Pool exhausted" | Increase `remote_db_pool_max` in site_config.json |
| Cache not working | Check Redis: `redis-cli ping` should return PONG |
| Slow queries still | Run `redis-cli FLUSHALL` to clear stale cache |
| Workers not starting | Check: `export TAP_AI_WORKER_CONCURRENCY=8` is set |
| Import errors | Run: `python -m pip install --upgrade psycopg2-binary` |

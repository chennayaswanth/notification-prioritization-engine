# 🔔 Notification Prioritization Engine

**Cyepro Solutions — Assignment Task | OC.36641.2026.57933**  
Built with Python + Flask | AI tools used: Claude (Anthropic)

---

## 📌 Problem Summary

Users receive too many notifications — some repetitive, some at bad times, some low-value while important ones are delayed. This engine decides for **every incoming notification event**: should it be sent **Now**, **Later**, or **Never**?

---

## 🏗️ System Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│                     Upstream Services                               │
│      (chat-service, marketing, calendar, monitoring, etc.)          │
└───────────────────────────┬─────────────────────────────────────────┘
                            │  POST /api/v1/notify/classify
                            ▼
┌─────────────────────────────────────────────────────────────────────┐
│                  API Gateway / Load Balancer                        │
│                 (rate limiting, auth, routing)                      │
└───────────────────────────┬─────────────────────────────────────────┘
                            │
                            ▼
┌─────────────────────────────────────────────────────────────────────┐
│              Notification Prioritization Engine                     │
│                                                                     │
│  ┌─────────────┐  ┌──────────────┐  ┌───────────────────────────┐  │
│  │  Classifier │→ │ Rule Engine  │→ │  Decision Output          │  │
│  │  (now/later/│  │ (configurable│  │  now / later / never      │  │
│  │   never)    │  │  JSON rules) │  │  + reason + defer_seconds │  │
│  └─────────────┘  └──────────────┘  └───────────────────────────┘  │
│           │                                                         │
│  ┌────────▼──────────────────────────────────────────────────────┐ │
│  │                 Decision Pipeline (in order)                  │ │
│  │   1. Expiry Check  →  2. Dedup Check  →  3. Cooldown Check   │ │
│  │   4. Fatigue Check  →  5. Quiet Hours  →  6. SEND NOW        │ │
│  └───────────────────────────────────────────────────────────────┘ │
└──────┬──────────────┬─────────────────────────────────────────────┘
       │              │
       ▼              ▼
┌──────────┐   ┌─────────────┐   ┌────────────────────────────────┐
│  Dedupe  │   │  Notification│   │         Audit Log              │
│  Store   │   │  History /   │   │  (every decision + reason)     │
│ (Redis/  │   │  Fatigue     │   │                                │
│  In-Mem) │   │  Counters    │   └────────────────────────────────┘
└──────────┘   └─────────────┘
```

### Components

| Component | Role |
|---|---|
| **API Layer** | Flask REST API — 5 endpoints for classify, batch, audit, rules, history |
| **Decision Engine** | Ordered pipeline of checks, returns Now/Later/Never + reason |
| **Rule Engine** | Human-configurable JSON rules — update without redeployment |
| **Dedup Store** | Exact key + content-hash based duplicate detection (Redis in prod) |
| **Notification History** | Per-user event log for fatigue tracking (Redis Sorted Sets in prod) |
| **Audit Log** | Immutable append-only log of every decision with reason |

---

## 🧠 Decision Logic — Now / Later / Never

Every notification passes through a **sequential pipeline** of checks. The first failing check determines the outcome.

```
Incoming Event
      │
      ▼
┌─────────────────────┐
│ 1. Is it expired?   │ ──YES──→ NEVER  "Notification expired"
└────────┬────────────┘
         │ NO
         ▼
┌─────────────────────┐
│ 2. Is it a          │ ──YES──→ NEVER  "Exact/near-duplicate seen Xs ago"
│    duplicate?       │
└────────┬────────────┘
         │ NO
         ▼
┌─────────────────────┐
│ 3. Is event_type in │ ──YES (and not time-sensitive)──→ LATER  "Cooldown Xs remaining"
│    cooldown?        │
└────────┬────────────┘
         │ NO
         ▼
┌─────────────────────┐
│ 4. User hit hourly/ │ ──YES (and not time-sensitive)──→ LATER/NEVER  "Alert fatigue"
│    daily cap?       │
└────────┬────────────┘
         │ NO
         ▼
┌─────────────────────┐
│ 5. Quiet hours?     │ ──YES (and not time-sensitive)──→ LATER  "Defer to 8am"
└────────┬────────────┘
         │ NO
         ▼
       SEND NOW ✅
```

**Time-sensitive override:** Events with `event_type: alert / system_event` or `priority_hint: critical / urgent` **bypass steps 3, 4, and 5** and always send immediately.

---

## 📊 Data Model

### NotificationEvent (Input)

```json
{
  "user_id": "u123",
  "event_type": "promotion",
  "message": "Get 20% off today!",
  "source": "marketing-service",
  "priority_hint": "low",
  "timestamp": "2026-02-27T10:00:00Z",
  "channel": "push",
  "metadata": {},
  "dedupe_key": "promo_feb27_u123",
  "expires_at": "2026-02-27T23:59:00Z"
}
```

### DecisionRecord (Output + Audit)

```json
{
  "notification_id": "uuid-v4",
  "user_id": "u123",
  "event_type": "promotion",
  "decision": "later",
  "reason": "Cooldown active for 'promotion': 3599s remaining",
  "defer_seconds": 3599,
  "channel": "push",
  "timestamp": "2026-02-27T10:00:01Z"
}
```

### NotificationHistory (per user)

```json
{
  "user_id": "u123",
  "events": [
    { "ts": 1735300000, "event_type": "message", "decision": "now", "notification_id": "..." }
  ]
}
```

### DedupeStore (key-value)

```
dedupe_key   →  { "timestamp": unix_ts }           // exact match
hash_<md5>   →  { "timestamp": unix_ts }           // near-duplicate content hash
```

### SuppressionRules (configurable)

```json
{
  "quiet_hours": { "start": 22, "end": 8 },
  "max_per_hour": 10,
  "max_per_day": 30,
  "cooldown_seconds": {
    "promotion": 3600,
    "reminder": 1800,
    "update": 600,
    "alert": 0,
    "message": 0,
    "system_event": 0
  }
}
```

---

## 🔌 API Endpoints

### 1. `POST /api/v1/notify/classify`
Classify a single notification event.

**Request:**
```json
{
  "user_id": "u123",
  "event_type": "reminder",
  "message": "Your meeting starts in 15 minutes",
  "channel": "push"
}
```

**Response:**
```json
{
  "notification_id": "550e8400-e29b-41d4-a716-446655440000",
  "decision": "now",
  "reason": "Passed all checks. Priority: normal.",
  "defer_seconds": null,
  "processed_at": "2026-02-27T10:00:01Z"
}
```

---

### 2. `POST /api/v1/notify/batch`
Classify up to 100 events in one request.

**Response:**
```json
{
  "total": 3,
  "summary": { "now": 2, "later": 1, "never": 0 },
  "results": [ ... ]
}
```

---

### 3. `GET /api/v1/audit/logs?user_id=u123&decision=never&limit=20`
Retrieve audit logs with filtering. Every decision is logged with full reason.

---

### 4. `GET | PUT /api/v1/rules`
View or update suppression rules **without redeployment**.

**PUT Request:**
```json
{ "max_per_hour": 15, "cooldown_seconds": { "promotion": 7200 } }
```

---

### 5. `GET /api/v1/users/{user_id}/history`
View a user's recent notification history and remaining daily/hourly quota.

---

## 🔁 Duplicate Prevention

Two complementary strategies:

| Strategy | Mechanism | Window |
|---|---|---|
| **Exact Duplicate** | Match on provided `dedupe_key` | 1 hour |
| **Near-Duplicate** | MD5 hash of `user_id + event_type + message[:100]` | 5 minutes |

**Why both?** `dedupe_key` may be missing or unreliable (upstream bug, retry storms). Content hashing catches cases where two services send semantically identical messages with no key, or where the same event is retried without a key.

---

## 😴 Alert Fatigue Strategy

| Mechanism | How it works |
|---|---|
| **Hourly cap** | After N notifications/hour → defer remaining to next hour |
| **Daily cap** | After M notifications/day → suppress low-priority ones |
| **Cooldown per event_type** | Promotions: 1hr gap. Reminders: 30min gap. Alerts: 0 (always send) |
| **Quiet hours** | 10pm–8am UTC → non-urgent deferred to 8am |
| **Time-sensitive override** | `alert`, `system_event`, `priority_hint: critical/urgent` bypass ALL fatigue limits |

The combination ensures **important notifications always get through** while **promotional/low-value ones are batched or suppressed**.

---

## 🛡️ Fallback Strategy

When the classification engine fails (exception, timeout, dependency down):

```python
# From app.py
except Exception as e:
    result = {
        "decision": "now" if is_time_sensitive(event) else "later",
        "reason": f"[FALLBACK] Engine error. Defaulting to safe mode.",
        "defer_seconds": 300,
    }
```

| Situation | Behavior |
|---|---|
| Engine throws exception | Critical events → send now. Others → defer 5 min |
| Dependency (Redis) unavailable | Falls back to in-memory store |
| Malformed input | Returns 400 with clear error message |
| Unknown event_type | Treated as low-priority, normal pipeline applies |

**Key principle:** Critical notifications are **never silently dropped**. The system fails *open* for alerts and *closed* for promotions.

---

## 📈 Metrics & Monitoring Plan

### Key Metrics to Track

| Metric | Why |
|---|---|
| `decisions_by_type` (now/later/never per event_type) | Understand suppression patterns |
| `p99 classification latency` | Ensure low-latency SLA |
| `fallback_trigger_count` | Alert when engine degrades |
| `duplicate_suppression_rate` | Validate dedup effectiveness |
| `fatigue_defer_rate per user` | Identify noisy upstream services |
| `quiet_hours_defer_volume` | Tune quiet hours windows |

### Alerting

- `fallback_trigger_count > 10/min` → PagerDuty alert (engine degraded)
- `p99 latency > 200ms` → Scale up instances
- `never_rate > 40%` → Investigate upstream service flooding

### Dashboards

- Per-user fatigue trends
- Decision distribution by channel and event_type
- Suppression rules change history (audit trail)

---

## 🚀 Running the Project

### Prerequisites
```bash
pip install flask
```

### Start the server
```bash
python app.py
```
Server runs at `http://localhost:5000`

### Run the demo (tests all endpoints)
```bash
python demo.py
```

### Example cURL
```bash
# Classify a single notification
curl -X POST http://localhost:5000/api/v1/notify/classify \
  -H "Content-Type: application/json" \
  -d '{
    "user_id": "u001",
    "event_type": "alert",
    "message": "Server is down!",
    "channel": "sms",
    "priority_hint": "critical"
  }'

# Update rules (no redeployment!)
curl -X PUT http://localhost:5000/api/v1/rules \
  -H "Content-Type: application/json" \
  -d '{"max_per_hour": 5}'
```

---

## 🏭 Production Considerations

| Current (Demo) | Production |
|---|---|
| In-memory dict store | Redis (TTL-based keys) |
| Single process | Horizontally scalable (stateless workers + shared Redis) |
| No auth | API key / JWT middleware |
| Synchronous | Async queue (Celery/RabbitMQ) for "later" events |
| JSON rules in memory | Rules stored in DB, hot-reloaded via config service |

---

## 🤖 AI Tools Used

- **Claude (Anthropic):** Used to structure the decision pipeline logic, generate boilerplate Flask code, and draft README sections.
- **Manual changes:** Decision pipeline order, fallback behavior, near-duplicate hashing strategy, and all business logic thresholds were designed and tuned manually based on the problem constraints.

---

## 📁 File Structure

```
notification-engine/
├── app.py          # Main Flask API + Decision Engine (5 endpoints)
├── demo.py         # Demo script — runs 12 test scenarios
└── README.md       # This file
```

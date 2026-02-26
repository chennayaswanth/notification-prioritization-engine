"""
Demo script - runs the engine and tests all endpoints.
Run: python demo.py
"""

import sys
import os
sys.path.insert(0, os.path.dirname(__file__))

from app import app, suppression_rules
import json

client = app.test_client()

def pretty(label, response):
    print(f"\n{'='*60}")
    print(f"▶ {label}")
    print(f"  Status: {response.status_code}")
    data = json.loads(response.data)
    print(json.dumps(data, indent=2))

print("\n🚀 NOTIFICATION PRIORITIZATION ENGINE - DEMO\n")

# ──────────────────────────────────────────────
# 1. Health Check
# ──────────────────────────────────────────────
r = client.get("/health")
pretty("1. Health Check", r)

# ──────────────────────────────────────────────
# 2. Normal notification → SEND NOW
# ──────────────────────────────────────────────
r = client.post("/api/v1/notify/classify", json={
    "user_id": "user_001",
    "event_type": "message",
    "message": "You have a new message from Alice",
    "source": "chat-service",
    "channel": "push",
    "timestamp": "2026-02-27T10:00:00Z"
})
pretty("2. Normal message → Expected: now", r)

# ──────────────────────────────────────────────
# 3. Duplicate notification → SUPPRESS
# ──────────────────────────────────────────────
r = client.post("/api/v1/notify/classify", json={
    "user_id": "user_001",
    "event_type": "message",
    "message": "You have a new message from Alice",
    "source": "chat-service",
    "channel": "push",
    "timestamp": "2026-02-27T10:00:05Z"
})
pretty("3. Same message again → Expected: never (near-duplicate)", r)

# ──────────────────────────────────────────────
# 4. Promotion during cooldown → DEFER
# ──────────────────────────────────────────────
r = client.post("/api/v1/notify/classify", json={
    "user_id": "user_002",
    "event_type": "promotion",
    "message": "Flash sale! 50% off all items",
    "source": "marketing",
    "channel": "email",
    "priority_hint": "low",
})
pretty("4. First promotion → Expected: now", r)

r = client.post("/api/v1/notify/classify", json={
    "user_id": "user_002",
    "event_type": "promotion",
    "message": "Don't miss out! Huge discounts today",
    "source": "marketing",
    "channel": "email",
    "priority_hint": "low",
})
pretty("5. Second promotion within cooldown → Expected: later (cooldown)", r)

# ──────────────────────────────────────────────
# 5. Critical alert bypasses all limits
# ──────────────────────────────────────────────
# Force fatigue state for user_003 by pushing history manually
from app import notification_history
import time
now = time.time()
for i in range(12):
    notification_history["user_003"].append({
        "ts": now - (i * 200),
        "event_type": "update",
        "notification_id": f"fake_{i}",
        "decision": "now"
    })

r = client.post("/api/v1/notify/classify", json={
    "user_id": "user_003",
    "event_type": "alert",
    "message": "CRITICAL: Server down! Immediate action required.",
    "source": "monitoring-service",
    "channel": "sms",
    "priority_hint": "critical",
})
pretty("6. Critical alert even when user is fatigued → Expected: now", r)

# ──────────────────────────────────────────────
# 6. Expired notification → SUPPRESS
# ──────────────────────────────────────────────
r = client.post("/api/v1/notify/classify", json={
    "user_id": "user_004",
    "event_type": "reminder",
    "message": "Your meeting starts in 5 minutes",
    "source": "calendar-service",
    "channel": "push",
    "expires_at": "2024-01-01T00:00:00Z",  # past date
})
pretty("7. Expired notification → Expected: never", r)

# ──────────────────────────────────────────────
# 7. Batch classification
# ──────────────────────────────────────────────
r = client.post("/api/v1/notify/batch", json={
    "events": [
        {"user_id": "user_010", "event_type": "message", "message": "Hello!", "channel": "push"},
        {"user_id": "user_010", "event_type": "promotion", "message": "Sale today!", "channel": "email"},
        {"user_id": "user_010", "event_type": "system_event", "message": "Account login detected", "channel": "email", "priority_hint": "urgent"},
    ]
})
pretty("8. Batch classify (3 events) → Mixed decisions", r)

# ──────────────────────────────────────────────
# 8. View audit log
# ──────────────────────────────────────────────
r = client.get("/api/v1/audit/logs?limit=5")
pretty("9. Audit log (last 5)", r)

# ──────────────────────────────────────────────
# 9. View & update rules
# ──────────────────────────────────────────────
r = client.get("/api/v1/rules")
pretty("10. Current suppression rules", r)

r = client.put("/api/v1/rules", json={"max_per_hour": 5, "cooldown_seconds": {"promotion": 7200}})
pretty("11. Update rules (no redeployment needed!)", r)

# ──────────────────────────────────────────────
# 10. User history
# ──────────────────────────────────────────────
r = client.get("/api/v1/users/user_001/history")
pretty("12. User notification history & fatigue stats", r)

print("\n✅ Demo complete!\n")

# ──────────────────────────────────────────────
# NEW: AI Scoring demonstration
# ──────────────────────────────────────────────
print("\n" + "="*60)
print("▶ BONUS: AI Scoring Layer Demo")
print("="*60)

from app import compute_importance_score

test_events = [
    {"event_type": "alert", "priority_hint": "critical", "message": "Server is down - critical error", "channel": "sms"},
    {"event_type": "message", "priority_hint": "normal", "message": "Hey, how are you?", "channel": "push"},
    {"event_type": "promotion", "priority_hint": "low", "message": "Big sale! 50% off all items today", "channel": "email"},
    {"event_type": "reminder", "priority_hint": "high", "message": "Your meeting starts in 5 minutes", "channel": "push"},
]

for e in test_events:
    score, reasons = compute_importance_score(e)
    print(f"\n  [{e['event_type'].upper()}] \"{e['message'][:40]}\"")
    print(f"  → importance_score: {score}")
    print(f"  → scoring: {' | '.join(reasons)}")

# ──────────────────────────────────────────────
# NEW: Metrics endpoint
# ──────────────────────────────────────────────
r = client.get("/metrics")
pretty("BONUS: GET /metrics — live system metrics", r)
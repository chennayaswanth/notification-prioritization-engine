"""
Notification Prioritization Engine - API Server
Cyepro Solutions Assignment | Built with Flask

AI-Native Design:
  - AI importance scoring layer ranks every notification (0.0 to 1.0)
  - Rule-based pipeline uses score to make final Now/Later/Never decision
  - Fully explainable: score + reason logged for every decision
"""

from flask import Flask, request, jsonify
from datetime import datetime, timezone
import uuid
import hashlib
import time
from collections import defaultdict

app = Flask(__name__)

# ─────────────────────────────────────────────
# In-Memory Storage (replace with Redis in prod)
# ─────────────────────────────────────────────

notification_history = defaultdict(list)   # user_id -> [recent events]
dedupe_store = {}                          # key/hash -> {"timestamp": ...}
audit_log = []

# Global metrics counters
metrics_store = {
    "now_count": 0,
    "later_count": 0,
    "never_count": 0,
    "duplicate_count": 0,
    "fallback_count": 0,
    "total_processed": 0,
}

# Suppression rules — human-configurable via PUT /api/v1/rules (no redeployment)
suppression_rules = {
    "quiet_hours": {"start": 22, "end": 8},
    "max_per_hour": 10,
    "max_per_day": 30,
    "cooldown_seconds": {
        "promotion": 3600,
        "reminder": 1800,
        "update": 600,
        "system_event": 0,
        "alert": 0,
        "message": 0,
    },
    # AI score thresholds
    "score_send_now_threshold": 0.5,    # score >= this → always send now
    "score_suppress_threshold": 0.1,   # score <= this → candidate for never
}


# ─────────────────────────────────────────────
# AI Importance Scoring Layer
# ─────────────────────────────────────────────

def compute_importance_score(event: dict) -> tuple[float, list[str]]:
    """
    AI-inspired importance scoring (0.0 → 1.0).

    In production this would call an ML model or LLM.
    Here we use a weighted rule-based scorer that mimics
    what a trained classifier would produce for these features.

    Returns (score, list of scoring reasons)
    """
    score = 0.0
    reasons = []

    event_type = event.get("event_type", "")
    priority_hint = event.get("priority_hint", "")
    message = (event.get("message") or event.get("title") or "").lower()
    channel = event.get("channel", "")

    # ── Event type weight (0.0 – 0.45) ──────────────────────────────────────
    type_scores = {
        "alert": 0.45,
        "system_event": 0.40,
        "message": 0.30,
        "reminder": 0.25,
        "update": 0.15,
        "promotion": 0.05,
    }
    type_score = type_scores.get(event_type, 0.10)
    score += type_score
    reasons.append(f"event_type='{event_type}' (+{type_score:.2f})")

    # ── Priority hint weight (0.0 – 0.35) ────────────────────────────────────
    priority_scores = {
        "critical": 0.35,
        "urgent": 0.30,
        "high": 0.20,
        "normal": 0.05,
        "low": 0.0,
    }
    p_score = priority_scores.get(priority_hint, 0.0)
    if p_score > 0:
        score += p_score
        reasons.append(f"priority_hint='{priority_hint}' (+{p_score:.2f})")

    # ── Message keyword signals (0.0 – 0.20) ─────────────────────────────────
    urgent_keywords = ["error", "fail", "critical", "urgent", "down", "breach", "emergency"]
    promo_keywords = ["sale", "discount", "offer", "deal", "% off", "promo"]

    keyword_hits = [kw for kw in urgent_keywords if kw in message]
    promo_hits = [kw for kw in promo_keywords if kw in message]

    if keyword_hits:
        kw_score = min(0.20, len(keyword_hits) * 0.07)
        score += kw_score
        reasons.append(f"urgent keywords {keyword_hits} (+{kw_score:.2f})")
    if promo_hits:
        score -= 0.05
        reasons.append(f"promo keywords {promo_hits} (-0.05)")

    # ── Channel weight ────────────────────────────────────────────────────────
    if channel == "sms":
        score += 0.05
        reasons.append("channel=sms (+0.05)")

    # Clamp to [0.0, 1.0]
    score = round(max(0.0, min(1.0, score)), 3)
    return score, reasons


# ─────────────────────────────────────────────
# Deduplication
# ─────────────────────────────────────────────

def compute_content_hash(event: dict) -> str:
    key = f"{event.get('user_id')}|{event.get('event_type')}|{(event.get('message') or '')[:100]}"
    return hashlib.md5(key.encode()).hexdigest()


def is_duplicate(event: dict) -> tuple[bool, str]:
    dedupe_key = event.get("dedupe_key")
    if dedupe_key and dedupe_key in dedupe_store:
        age = time.time() - dedupe_store[dedupe_key]["timestamp"]
        if age < 3600:
            metrics_store["duplicate_count"] += 1
            return True, f"Exact duplicate key '{dedupe_key}' seen {int(age)}s ago"

    content_hash = compute_content_hash(event)
    hash_key = f"hash_{content_hash}"
    if hash_key in dedupe_store:
        age = time.time() - dedupe_store[hash_key]["timestamp"]
        if age < 300:
            metrics_store["duplicate_count"] += 1
            return True, f"Near-duplicate content seen {int(age)}s ago"

    return False, ""


def register_dedupe(event: dict):
    ts = time.time()
    dedupe_key = event.get("dedupe_key")
    if dedupe_key:
        dedupe_store[dedupe_key] = {"timestamp": ts}
    content_hash = compute_content_hash(event)
    dedupe_store[f"hash_{content_hash}"] = {"timestamp": ts}


# ─────────────────────────────────────────────
# Alert Fatigue / Rate Limiting
# ─────────────────────────────────────────────

def get_user_recent_history(user_id: str) -> dict:
    now = time.time()
    history = notification_history[user_id]
    recent_hour = [h for h in history if h["ts"] > now - 3600]
    recent_day  = [h for h in history if h["ts"] > now - 86400]
    return {"last_hour": len(recent_hour), "last_day": len(recent_day)}


def is_quiet_hours() -> bool:
    hour = datetime.now(timezone.utc).hour
    start = suppression_rules["quiet_hours"]["start"]
    end   = suppression_rules["quiet_hours"]["end"]
    if start > end:
        return hour >= start or hour < end
    return start <= hour < end


def is_time_sensitive(event: dict) -> bool:
    return (
        event.get("event_type") in ("alert", "system_event") or
        event.get("priority_hint") in ("critical", "urgent")
    )


def is_expired(event: dict) -> bool:
    expires_at = event.get("expires_at")
    if not expires_at:
        return False
    try:
        exp = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
        return exp < datetime.now(timezone.utc)
    except Exception:
        return False


def get_cooldown_remaining(event: dict, user_id: str) -> int:
    event_type = event.get("event_type", "")
    cooldown = suppression_rules["cooldown_seconds"].get(event_type, 0)
    if cooldown == 0:
        return 0
    now = time.time()
    for h in reversed(notification_history[user_id]):
        if h.get("event_type") == event_type:
            return max(0, int(cooldown - (now - h["ts"])))
    return 0


def _seconds_until_end_of_quiet_hours() -> int:
    now = datetime.now(timezone.utc)
    end_hour = suppression_rules["quiet_hours"]["end"]
    target = now.replace(hour=end_hour, minute=0, second=0, microsecond=0)
    if target <= now:
        import datetime as dt
        target = target + dt.timedelta(days=1)
    return int((target - now).total_seconds())


# ─────────────────────────────────────────────
# Core Decision Engine
# ─────────────────────────────────────────────

def classify_notification(event: dict) -> dict:
    """
    5-step decision pipeline with AI importance scoring.

    Pipeline order (cheapest checks first):
      1. Expiry          → never
      2. Deduplication   → never
      3. AI Score        → high score forces now; very low score forces never
      4. Cooldown        → later
      5. Alert fatigue   → later / never
      6. Quiet hours     → later
      7. Default         → now
    """
    notification_id = str(uuid.uuid4())
    user_id = event.get("user_id", "unknown")
    time_sensitive = is_time_sensitive(event)

    # ── AI Scoring (always computed, used in steps 3+) ───────────────────────
    importance_score, score_reasons = compute_importance_score(event)

    # ── Step 1: Expiry ────────────────────────────────────────────────────────
    if is_expired(event):
        return {
            "notification_id": notification_id,
            "decision": "never",
            "reason": "Notification has expired (expires_at is in the past)",
            "importance_score": importance_score,
        }

    # ── Step 2: Duplicate ─────────────────────────────────────────────────────
    is_dup, dup_reason = is_duplicate(event)
    if is_dup:
        return {
            "notification_id": notification_id,
            "decision": "never",
            "reason": f"Suppressed: {dup_reason}",
            "importance_score": importance_score,
        }

    # ── Step 3: AI Score override ─────────────────────────────────────────────
    # Very high score → send now regardless of fatigue/quiet hours
    if importance_score >= suppression_rules["score_send_now_threshold"] and time_sensitive:
        return {
            "notification_id": notification_id,
            "decision": "now",
            "reason": f"AI score {importance_score} >= threshold {suppression_rules['score_send_now_threshold']} + time-sensitive. Scoring: {'; '.join(score_reasons)}",
            "importance_score": importance_score,
        }
    # Very low score + not time-sensitive → candidate for suppression
    if importance_score <= suppression_rules["score_suppress_threshold"] and not time_sensitive:
        return {
            "notification_id": notification_id,
            "decision": "never",
            "reason": f"AI score {importance_score} below suppress threshold {suppression_rules['score_suppress_threshold']}. Low-value notification suppressed. Scoring: {'; '.join(score_reasons)}",
            "importance_score": importance_score,
        }

    # ── Step 4: Cooldown ──────────────────────────────────────────────────────
    cooldown_remaining = get_cooldown_remaining(event, user_id)
    if cooldown_remaining > 0 and not time_sensitive:
        return {
            "notification_id": notification_id,
            "decision": "later",
            "reason": f"Cooldown active for '{event.get('event_type')}': {cooldown_remaining}s remaining",
            "defer_seconds": cooldown_remaining,
            "importance_score": importance_score,
        }

    # ── Step 5: Alert fatigue ─────────────────────────────────────────────────
    history_stats = get_user_recent_history(user_id)
    if not time_sensitive:
        if history_stats["last_hour"] >= suppression_rules["max_per_hour"]:
            return {
                "notification_id": notification_id,
                "decision": "later",
                "reason": f"Alert fatigue: {history_stats['last_hour']} notifications in last hour (limit: {suppression_rules['max_per_hour']}). Deferring.",
                "defer_seconds": 3600,
                "importance_score": importance_score,
            }
        if history_stats["last_day"] >= suppression_rules["max_per_day"]:
            return {
                "notification_id": notification_id,
                "decision": "never",
                "reason": f"Daily cap reached: {history_stats['last_day']} notifications today (limit: {suppression_rules['max_per_day']}). Suppressing.",
                "importance_score": importance_score,
            }

    # ── Step 6: Quiet hours ───────────────────────────────────────────────────
    if is_quiet_hours() and not time_sensitive:
        return {
            "notification_id": notification_id,
            "decision": "later",
            "reason": "Quiet hours active (10pm–8am UTC). Deferred to 8am.",
            "defer_seconds": _seconds_until_end_of_quiet_hours(),
            "importance_score": importance_score,
        }

    # ── Step 7: Send Now ──────────────────────────────────────────────────────
    return {
        "notification_id": notification_id,
        "decision": "now",
        "reason": f"Passed all checks. AI score: {importance_score}. {'Time-sensitive override active.' if time_sensitive else ''}",
        "importance_score": importance_score,
    }


def record_decision(event: dict, result: dict):
    """Update history, dedupe, metrics, and audit log."""
    user_id = event.get("user_id", "unknown")
    now = time.time()
    decision = result["decision"]

    # Notification history
    notification_history[user_id].append({
        "ts": now,
        "event_type": event.get("event_type"),
        "notification_id": result["notification_id"],
        "decision": decision,
    })
    # Trim to last 24h
    notification_history[user_id] = [
        h for h in notification_history[user_id] if h["ts"] > now - 86400
    ]

    # Dedupe registration (only for sent notifications)
    if decision == "now":
        register_dedupe(event)

    # Metrics
    metrics_store["total_processed"] += 1
    if decision == "now":
        metrics_store["now_count"] += 1
    elif decision == "later":
        metrics_store["later_count"] += 1
    elif decision == "never":
        metrics_store["never_count"] += 1

    # Audit log
    audit_log.append({
        "notification_id": result["notification_id"],
        "user_id": user_id,
        "event_type": event.get("event_type"),
        "decision": decision,
        "reason": result["reason"],
        "importance_score": result.get("importance_score"),
        "channel": event.get("channel"),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })


# ─────────────────────────────────────────────
# API Endpoints
# ─────────────────────────────────────────────

@app.route("/health", methods=["GET"])
def health():
    """Health check."""
    return jsonify({"status": "ok", "timestamp": datetime.now(timezone.utc).isoformat()})


@app.route("/api/v1/notify/classify", methods=["POST"])
def classify():
    """
    POST /api/v1/notify/classify
    Classify a single notification: now / later / never.
    Response includes importance_score from AI scoring layer.
    """
    event = request.get_json()
    if not event:
        return jsonify({"error": "Request body must be valid JSON"}), 400
    missing = [f for f in ["user_id", "event_type"] if not event.get(f)]
    if missing:
        return jsonify({"error": f"Missing required fields: {missing}"}), 400

    try:
        result = classify_notification(event)
    except Exception as e:
        metrics_store["fallback_count"] += 1
        result = {
            "notification_id": str(uuid.uuid4()),
            "decision": "now" if is_time_sensitive(event) else "later",
            "reason": f"[FALLBACK] Engine error: {str(e)}. Safe mode: critical→now, others→defer.",
            "defer_seconds": 300,
            "importance_score": None,
        }

    record_decision(event, result)
    return jsonify({
        "notification_id": result["notification_id"],
        "decision": result["decision"],
        "reason": result["reason"],
        "importance_score": result.get("importance_score"),
        "defer_seconds": result.get("defer_seconds"),
        "user_id": event["user_id"],
        "event_type": event["event_type"],
        "processed_at": datetime.now(timezone.utc).isoformat(),
    })


@app.route("/api/v1/notify/batch", methods=["POST"])
def classify_batch():
    """
    POST /api/v1/notify/batch
    Classify up to 100 events in one request.
    """
    body = request.get_json()
    if not body or "events" not in body:
        return jsonify({"error": "Body must contain 'events' array"}), 400
    events = body["events"]
    if not isinstance(events, list) or len(events) == 0:
        return jsonify({"error": "'events' must be a non-empty array"}), 400
    if len(events) > 100:
        return jsonify({"error": "Batch limit is 100 events per request"}), 400

    results = []
    for event in events:
        try:
            result = classify_notification(event)
        except Exception as e:
            metrics_store["fallback_count"] += 1
            result = {
                "notification_id": str(uuid.uuid4()),
                "decision": "now" if is_time_sensitive(event) else "later",
                "reason": f"[FALLBACK] {str(e)}",
                "importance_score": None,
            }
        record_decision(event, result)
        results.append({
            "notification_id": result["notification_id"],
            "user_id": event.get("user_id"),
            "event_type": event.get("event_type"),
            "decision": result["decision"],
            "importance_score": result.get("importance_score"),
            "reason": result["reason"],
        })

    summary = {
        "now":   sum(1 for r in results if r["decision"] == "now"),
        "later": sum(1 for r in results if r["decision"] == "later"),
        "never": sum(1 for r in results if r["decision"] == "never"),
    }
    return jsonify({
        "total": len(results),
        "summary": summary,
        "results": results,
        "processed_at": datetime.now(timezone.utc).isoformat(),
    })


@app.route("/api/v1/audit/logs", methods=["GET"])
def get_audit_logs():
    """
    GET /api/v1/audit/logs?user_id=u123&decision=never&limit=20
    Retrieve audit logs — every decision logged with reason + AI score.
    """
    user_id = request.args.get("user_id")
    limit = min(int(request.args.get("limit", 50)), 200)
    decision_filter = request.args.get("decision")

    logs = audit_log.copy()
    if user_id:
        logs = [l for l in logs if l["user_id"] == user_id]
    if decision_filter:
        logs = [l for l in logs if l["decision"] == decision_filter]

    return jsonify({
        "total": len(logs),
        "filters": {"user_id": user_id, "decision": decision_filter},
        "logs": logs[-limit:][::-1],
    })


@app.route("/api/v1/rules", methods=["GET", "PUT"])
def manage_rules():
    """
    GET  /api/v1/rules  → view current suppression + AI scoring rules
    PUT  /api/v1/rules  → update rules without redeployment
    """
    global suppression_rules
    if request.method == "GET":
        return jsonify({"rules": suppression_rules})

    updates = request.get_json()
    if not updates:
        return jsonify({"error": "Request body must be valid JSON"}), 400

    for key in ["max_per_hour", "max_per_day", "score_send_now_threshold", "score_suppress_threshold"]:
        if key in updates:
            suppression_rules[key] = float(updates[key])
    if "quiet_hours" in updates:
        suppression_rules["quiet_hours"].update(updates["quiet_hours"])
    if "cooldown_seconds" in updates:
        suppression_rules["cooldown_seconds"].update(updates["cooldown_seconds"])

    return jsonify({
        "message": "Rules updated successfully",
        "updated_rules": suppression_rules,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    })


@app.route("/api/v1/users/<user_id>/history", methods=["GET"])
def user_history(user_id: str):
    """
    GET /api/v1/users/{user_id}/history
    View user's recent notification history and fatigue stats.
    """
    history = notification_history.get(user_id, [])
    stats = get_user_recent_history(user_id)
    now = time.time()
    return jsonify({
        "user_id": user_id,
        "stats": {
            "notifications_last_hour": stats["last_hour"],
            "notifications_last_day": stats["last_day"],
            "hourly_cap": suppression_rules["max_per_hour"],
            "daily_cap": suppression_rules["max_per_day"],
            "hourly_remaining": max(0, suppression_rules["max_per_hour"] - stats["last_hour"]),
            "daily_remaining": max(0, suppression_rules["max_per_day"] - stats["last_day"]),
        },
        "recent_events": [
            {
                "notification_id": h["notification_id"],
                "event_type": h["event_type"],
                "decision": h["decision"],
                "seconds_ago": int(now - h["ts"]),
            }
            for h in reversed(history[-20:])
        ]
    })


@app.route("/metrics", methods=["GET"])
def get_metrics():
    """
    GET /metrics
    Live system metrics — decision counts, duplicate rate, fallbacks.
    """
    total = metrics_store["total_processed"] or 1  # avoid division by zero
    return jsonify({
        "total_processed": metrics_store["total_processed"],
        "decisions": {
            "now_count":   metrics_store["now_count"],
            "later_count": metrics_store["later_count"],
            "never_count": metrics_store["never_count"],
        },
        "rates": {
            "send_rate":      round(metrics_store["now_count"] / total * 100, 1),
            "defer_rate":     round(metrics_store["later_count"] / total * 100, 1),
            "suppress_rate":  round(metrics_store["never_count"] / total * 100, 1),
        },
        "duplicate_count":  metrics_store["duplicate_count"],
        "fallback_count":   metrics_store["fallback_count"],
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })


# ─────────────────────────────────────────────
# Error Handlers
# ─────────────────────────────────────────────

@app.errorhandler(404)
def not_found(e):
    return jsonify({"error": "Endpoint not found"}), 404

@app.errorhandler(405)
def method_not_allowed(e):
    return jsonify({"error": "Method not allowed"}), 405

@app.errorhandler(500)
def internal_error(e):
    return jsonify({"error": "Internal server error", "fallback": "System in safe mode"}), 500


if __name__ == "__main__":
    print("Notification Prioritization Engine")
    print("   http://localhost:5000")
    print("")
    print("Endpoints:")
    print("  POST /api/v1/notify/classify      → classify single event")
    print("  POST /api/v1/notify/batch         → classify batch (up to 100)")
    print("  GET  /api/v1/audit/logs           → full audit log")
    print("  GET|PUT /api/v1/rules             → view/update suppression rules")
    print("  GET  /api/v1/users/<id>/history   → user fatigue stats")
    print("  GET  /metrics                     → live system metrics")
    app.run(debug=True, port=5000)

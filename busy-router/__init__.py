"""busy-router: route a message that arrives while the agent is mid-turn.

While a chat's agent is busy, each new plain or voice message is classified by one structured
decision call to an OpenAI-compatible endpoint that serves a Jev-style decision model:
  A  correction, constraint or stop for the running task  -> unchanged (busy_input_mode decides)
  B  quick question about progress or context              -> /btw
  C  new independent task                                  -> /bg
  D  next step after the running task                      -> /queue
Idle chats, slash commands, internal events, unauthorized senders, non-audio media, one- or
two-word fragments, a missing endpoint and every error or timeout pass through unchanged.

Stock Hermes runs pre_gateway_dispatch only while a chat is idle; the patch in gateway-patch/
extends it to the busy path.
"""
import asyncio
import json
import logging
import os
import re
import time
import urllib.request

logger = logging.getLogger(__name__)

# plugins.entries.busy-router.settings.<key> in config.yaml. Hermes reserves the roots "model", "plugins",
# "security" and "settings" (ctx.get_config raises ValueError), hence decision_model.
DEFAULTS = {"endpoint": "", "decision_model": "", "api_key_env": "OPENAI_API_KEY", "timeout_s": 3.0,
            "history_messages": 3}
HISTORY_CHARS = 300   # per message sent to the classifier
HISTORY_SCAN_PAGES = 5  # 200 rows each; a long tool loop can bury the last chat message
ROUTE = {"A": None, "B": "btw", "C": "bg", "D": "queue"}

INSTRUCTIONS = (
    "An AI agent in this chat is in the middle of the running task. Classify the user's NEW message. "
    "Messages are often voice transcripts and may contain recognition errors."
)
OPTIONS = [
    {"name": "A", "description": "about the running task itself: a correction, a new constraint or detail, "
                                 "approval, a complaint about it, or a request to stop/pause/change it"},
    {"name": "B", "description": "a quick question about progress, status, what is happening, which "
                                 "model/setting is used, or something already said in this conversation"},
    {"name": "C", "description": "a new independent request unrelated to the running task "
                                 "(a different job, lookup or research)"},
    {"name": "D", "description": "a follow-up step to start only once the running task is done: the message "
                                 "says that after something is finished, migrated, configured or done, do "
                                 "something else (\"after that\", \"when done\", \"once X is finished, do Y\"), "
                                 "in any language"},
]
SCHEMA = json.dumps({"questions": [{"id": "route", "type": "choice", "instructions": INSTRUCTIONS,
                                    "options": OPTIONS}], "samples": 1})
HISTORY_NOTE = (" recent_chat lists the last messages of this conversation, oldest first (user = the person "
                "sending the new message, assistant = the AI agent); use it to understand what the new message refers to.")
SCHEMA_HISTORY = json.dumps({"questions": [{"id": "route", "type": "choice", "instructions": INSTRUCTIONS + HISTORY_NOTE,
                                            "options": OPTIONS}], "samples": 1})

# Rows that are gateway or agent bookkeeping, not something the user said.
NOISE = re.compile(r"^\s*(\[IMPORTANT:|\[INTERNAL NOTIFICATION|\[CONTEXT COMPACTION|\[Context from the interrupted"
                   r"|\[OUT-OF-BAND|\[SYSTEM|\[ASYNC)", re.S)
ORIGIN = re.compile(r"Gateway message origin \(JSON data.*?Do not guess a reply destination when these fields are "
                    r"insufficient\.\s*", re.S)

# Short notices replace the stock multi-line echoes (prompt preview, task id, explanation).
SHORT_NOTICES = {
    "gateway.background.started": "\U0001F504 Background task started",
    "gateway.background.complete_header": "\u2705 Background task done\n\n",
    "gateway.btw.started": "\U0001F4AC btw",
    "gateway.btw.answer": "\U0001F4AC {answer}",
}

QUESTION = re.compile("[?\uff1f\u5417\u5462]")  # ASCII and full-width "?", Chinese question particles
WORDS = re.compile("[A-Za-z0-9_'-]+|[\u4e00-\u9fff]")
CJK = re.compile("[\u4e00-\u9fff]")

_ctx = None
_warned = False


def _settings():
    get = _ctx.get_config if _ctx is not None else (lambda key, default=None: default)
    return {k: get(k, v) for k, v in DEFAULTS.items()}


def _api_key(env_name):
    if os.environ.get(env_name):
        return os.environ[env_name]
    try:
        from hermes_constants import get_hermes_home
        with open(os.path.join(get_hermes_home(), ".env"), encoding="utf-8") as fh:
            for line in fh:
                if line.startswith(env_name + "="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
    except OSError:
        pass
    return None


def _clean_user_text(text):
    text = ORIGIN.sub("", text or "").strip()
    text = re.sub(r"\n\nVoice message\s*$", "", text).strip()
    quoted = re.match(r'^"(.*)"$', text, re.S)
    return (quoted.group(1) if quoted else text).strip()


def chat_tail(rows, k):
    """Last *k* chat messages from chronological transcript rows: the user's own messages and the
    agent's final replies. Tool calls, tool results and gateway notices are skipped."""
    out = []
    for row in rows:
        role, content = row.get("role"), row.get("content")
        content = content if isinstance(content, str) else ""
        if role == "user":
            if content.strip() and not NOISE.match(content):
                text = _clean_user_text(content)
                if text:
                    out.append({"role": "user", "text": text[:HISTORY_CHARS]})
        elif role == "assistant":
            calls = row.get("tool_calls")
            if (not calls or calls in ("[]", "null")) and content.strip():
                out.append({"role": "assistant", "text": content.strip()[:HISTORY_CHARS]})
    return out[-k:] if k > 0 else []


def _read_history(gateway, session_key, k):
    """Recent chat for the session, read from the gateway's own SessionDB; [] when unavailable."""
    if k <= 0:
        return []
    store = getattr(gateway, "session_store", None)
    session_id = store.peek_session_id(session_key) if store is not None else None
    from gateway.run import _gateway_session_db_inner
    db = _gateway_session_db_inner(gateway)
    if not session_id or db is None:
        return []
    session_id = db.get_compression_tip(session_id) or session_id
    rows = []
    for page in range(HISTORY_SCAN_PAGES):
        batch = db.get_messages(session_id, limit=200, offset=page * 200, latest=True)
        rows = batch + rows
        if len(chat_tail(rows, k)) >= k or len(batch) < 200:
            break
    return chat_tail(rows, k)


def classify(task, msg, endpoint, model, api_key=None, timeout=3.0, history=None):
    """Return (label or None, {label: probability}) from one structured decision call."""
    state = {"running_task": task or "(unknown)", "new_message": msg}
    if history:
        state = {"recent_chat": history, **state}
    body = {"model": model, "messages": [
        {"role": "system", "content": SCHEMA_HISTORY if history else SCHEMA},
        {"role": "user", "content": json.dumps({"route": state}, ensure_ascii=False)},
    ]}
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    req = urllib.request.Request(endpoint, data=json.dumps(body).encode(), headers=headers)
    resp = json.loads(urllib.request.urlopen(req, timeout=timeout).read())
    ans = json.loads(resp["choices"][0]["message"]["content"])["answers"]["route"]
    probs = {k: float(v) for k, v in (ans.get("probabilities") or ans.get("probs") or {}).items() if k in ROUTE}
    label = ans.get("label") or ans.get("choice")
    return (label if label in ROUTE else None), probs


def _is_fragment(text):
    """One or two words that are not a question (e.g. a re-sent keyword) carry no routable intent."""
    if QUESTION.search(text):
        return False
    words = WORDS.findall(text)
    cjk = sum(1 for w in words if CJK.match(w))
    return len(words) - cjk <= 2 and cjk <= 4


async def _transcribe_all(paths):
    from tools.transcription_tools import transcribe_audio
    parts = []
    for p in paths:
        r = await asyncio.to_thread(transcribe_audio, p)
        if not r.get("success") or not (r.get("transcript") or "").strip():
            return None
        parts.append(r["transcript"].strip())
    return " ".join(parts)


async def route(event, gateway):
    """Return a pre_gateway_dispatch result, or None to leave the event alone."""
    global _warned
    if event is None or gateway is None or getattr(event, "internal", False):
        return None
    text = (event.text or "").strip()
    if text.startswith("/"):
        return None
    source = event.source
    key = gateway._session_key_for_source(source)
    if not key or not gateway._is_session_running(key):
        return None
    if not gateway._is_user_authorized(source):
        return None
    cfg = _settings()
    if not cfg["endpoint"] or not cfg["decision_model"]:
        if not _warned:
            logger.warning("busy-router: settings.endpoint/decision_model not configured; passing messages through")
            _warned = True
        return None
    state = gateway._peek_session_state(key)
    running = getattr(getattr(state, "turn", None), "event", None)
    task = (getattr(running, "text", "") or "").strip()[:600]

    media = list(event.media_urls or [])
    if media:
        from gateway.run import _event_media_is_stt_input
        if not all(_event_media_is_stt_input(event, i) for i in range(len(media))):
            return None  # images and files keep the normal path
        text = await _transcribe_all(media)
        if not text:
            return None
    if not text or _is_fragment(text):
        return None

    t0 = time.monotonic()

    def _decide():
        try:
            history = _read_history(gateway, key, int(cfg["history_messages"]))
        except Exception as exc:  # history is an aid; without it the decision still works
            logger.debug("busy-router: history unavailable: %r", exc)
            history = []
        return classify(task, text, cfg["endpoint"], cfg["decision_model"], _api_key(cfg["api_key_env"]),
                        float(cfg["timeout_s"]), history), len(history)

    (label, probs), n_hist = await asyncio.to_thread(_decide)
    verb = ROUTE.get(label)
    logger.info("busy-router: chat=%s label=%s -> %s p=%s hist=%d %.0fms",
                getattr(source, "chat_id", "?"), label, verb or "pass",
                {k: round(v, 3) for k, v in probs.items()}, n_hist, (time.monotonic() - t0) * 1000)
    if not verb:
        return None
    if media:  # the transcript now rides in the command text; drop the audio so it is not re-sent
        from gateway.platforms.event import MessageType
        event.media_urls, event.media_types = [], []
        if hasattr(event, "media_text_inlined"):
            event.media_text_inlined = []
        event.message_type = MessageType.TEXT
    return {"action": "rewrite", "text": f"/{verb} {text}"}


async def _on_dispatch(event=None, gateway=None, **_):
    try:
        return await route(event, gateway)
    except Exception as exc:  # never block a message because the router failed
        logger.warning("busy-router: pass-through after error: %r", exc)
        return None


def register(ctx):
    global _ctx, _warned
    _ctx, _warned = ctx, False
    ctx.register_hook("pre_gateway_dispatch", _on_dispatch)
    ctx.register_locale("en", SHORT_NOTICES)  # resets the i18n caches, so it applies without a restart
    logger.info("busy-router: registered")

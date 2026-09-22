import re
from datetime import datetime, timedelta, timezone

from chatbot.workspaces import workspace_meta

_GREETING_RE = re.compile(
    r"^(?:hi|hii+|hiya|hello|hello there|hey|heyy+|howdy|yo|sup|hola|greetings|"
    r"good (?:morning|afternoon|evening|day)|morning|afternoon|evening|"
    r"what'?sup|wassup|good mor?ning)[!.,]?\s*(?:[a-z]|$)",
    re.IGNORECASE,
)

_HOW_ARE_YOU_RE = re.compile(
    r"^(?:how are you(?: doing)?(?: today| this fine morning| this evening)?|"
    r"how'?s it going|how'?s everything|how do you do|what'?s up)", 
    re.IGNORECASE,
)

_THANKS_RE = re.compile(r"\b(thanks?|thank you|thx|cheers)\b", re.IGNORECASE)
_HELP_RE = re.compile(
    r"\b(what can you do|can you help|help|how do i use|how do you work|"
    r"who are you|what are you|what is this)\b",
    re.IGNORECASE,
)

_WEEKDAY_LINES = {
    0: "Happy Monday - a fresh week to make ESG progress!",
    1: "Happy Tuesday - let's dig into the details together.",
    2: "Happy Wednesday - hope you're having a productive week.",
    3: "Happy Thursday - almost there, the weekend is close!",
    4: "Happy Friday - great day to wrap up the week's ESG wins!",
    5: "Happy Saturday - hope you're enjoying the weekend!",
    6: "Happy Sunday - a nice day to plan the week ahead.",
}


def _salutation(hour):
    if hour < 12:
        return "Good morning"
    if hour < 18:
        return "Good afternoon"
    return "Good evening"


def _local_now(tz_offset_minutes):
    return datetime.now(timezone.utc) + timedelta(minutes=int(tz_offset_minutes or 0))


def detect(text):
    clean = " ".join((text or "").split())
    if _HOW_ARE_YOU_RE.match(clean):
        return "howareyou"
    if _GREETING_RE.match(clean):
        return "greeting"
    if _HELP_RE.search(clean):
        return "help"
    if _THANKS_RE.search(clean):
        return "thanks"
    return None


def _greeting_reply(user_name, category, tz_offset_minutes):
    meta = workspace_meta(category)
    label = meta.get("label", category.title())
    emoji = meta.get("emoji", "\U0001F916")
    first = (user_name or "").split()[0] or "there"
    now = _local_now(tz_offset_minutes)
    salutation = _salutation(now.hour)
    weekday_line = _WEEKDAY_LINES[now.weekday()]
    date_line = now.strftime("%A, %B %d, %Y")
    blurb = meta.get("blurb", f"{label} documents")
    return (
        f"{salutation}, {first}! {emoji} {weekday_line} "
        f"Today is {date_line}. I'm your {label} assistant - I can help "
        f"with {blurb}. What would you like to know?"
    )


def _help_reply(user_name, category):
    meta = workspace_meta(category)
    label = meta.get("label", category.title())
    emoji = meta.get("emoji", "\U0001F916")
    first = (user_name or "").split()[0] or "there"
    return (
        f"I'm your {label} assistant {emoji}. Ask me anything about "
        f"{meta.get('blurb', 'documents in this workspace')}. I answer strictly "
        f"from the documents in your {label} workspace. Say a friendly "
        f"'hello' anytime and I'll greet you back!"
    )


def _thanks_reply(user_name, category):
    meta = workspace_meta(category)
    emoji = meta.get("emoji", "\U0001F916")
    return f"You're welcome{(', ' + (user_name or '').split()[0]) if user_name else ''}! {emoji} Happy to help - ask me anything else."


def _howareyou_reply(user_name, category):
    meta = workspace_meta(category)
    label = meta.get("label", category.title())
    emoji = meta.get("emoji", "\U0001F916")
    first = (user_name or "").split()[0] or "there"
    return (
        f"I'm doing great, {first}! {emoji} Thanks for asking. I'm your "
        f"{label} assistant - ready to help with {meta.get('blurb')}. "
        f"What would you like to know?"
    )


def handle(text, user_name=None, category="finance", tz_offset_minutes=0):
    intent = detect(text)
    if intent == "greeting":
        return _greeting_reply(user_name, category, tz_offset_minutes)
    if intent == "howareyou":
        return _howareyou_reply(user_name, category)
    if intent == "help":
        return _help_reply(user_name, category)
    if intent == "thanks":
        return _thanks_reply(user_name, category)
    return None
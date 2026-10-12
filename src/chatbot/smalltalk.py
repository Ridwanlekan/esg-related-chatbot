import re
from datetime import datetime, timedelta, timezone

from chatbot.workspaces import workspace_meta

_GREETING_RE = re.compile(
    r"^(?:hi|hii+|hiya|hello|hello there|hey|heyy+|howdy|yo|sup|hola|greetings|"
    r"good (?:morning|afternoon|evening|day)|morning|afternoon|evening|"
    r"what'?sup|wassup|good mor?ning|你好|您好|早上好|下午好|晚上好|嗨|"
    r"नमस्ते|नमस्कार|प्रणाम|हाय|हैलो)[!.,，。！？]?\s*(?:[a-zA-Z一-鿿ऀ-ॿ]|$)",
    re.IGNORECASE,
)

_HOW_ARE_YOU_RE = re.compile(
    r"^(?:how are you(?: doing)?(?: today| this fine morning| this evening)?|"
    r"how'?s it going|how'?s everything|how do you do|what'?s up|"
    r"(?:今天|今日|最近|近来)?\s*(?:你|您)\s*(?:今天|最近|近来)?\s*"
    r"(?:怎么样|好吗|好不好|过得好吗|过得怎么样|如何)|近来如何|"
    r"(?:आज\s*)?आप\s*(?:आज\s*)?कैसे\s*(?:हैं|हो)|कैसे हो|"
    r"क्या हाल है|क्या हाल चाल)"
    r"[\s!?.,，。！？~～、]*$",
    re.IGNORECASE,
)

_THANKS_RE = re.compile(r"谢谢|谢谢你|感谢|多谢|धन्यवाद|शुक्रिया|thank you|thanks|thx|cheers", re.IGNORECASE)
_HELP_RE = re.compile(
    r"\b(what can you do|can you help|help|how do i use|how do you work|"
    r"who are you|what are you|what is this|帮助|帮忙|你能做什么|怎么用|如何使用|"
    r"मदद|क्या कर सकते हो|कैसे इस्तेमाल करें|आप क्या कर सकते हैं|क्या मदद कर सकते हैं)\b",
    re.IGNORECASE,
)

_ZH_RE = re.compile(r"[\u4e00-\u9fff]")
_HI_RE = re.compile(r"[\u0900-\u097f]")

_WEEKDAY_LINES = {
    0: "Happy Monday - a fresh week to make ESG progress!",
    1: "Happy Tuesday - let's dig into the details together.",
    2: "Happy Wednesday - hope you're having a productive week.",
    3: "Happy Thursday - almost there, the weekend is close!",
    4: "Happy Friday - cheers to a strong week end ahead!",
    5: "Happy Saturday - time to recharge or catch up!",
    6: "Happy Sunday - a peaceful start to wrap or plan ahead.",
}

_WEEKDAY_LINES_ZH = {
    0: "星期一，ESG之路新的开始！",
    1: "星期二，让我们一起深入细节。",
    2: "星期三，愿您一周顺利。",
    3: "星期四，快到周末了！",
    4: "星期五，恭喜又一个高效周末近了！",
    5: "星期六，休息充电或再接再厉！",
    6: "星期天，为下周做好准备！",
}

_WEEKDAY_LINES_HI = {
    0: "सोमवार - ESG प्रगति के लिए एक नया सप्ताह!",
    1: "मंगलवार - चलो विवरण में गहराई से जाएं।",
    2: "बुधवार - आशा है आपका सप्ताह फलदायी हो रहा है।",
    3: "गुरुवार - बस थोड़ा और, सप्ताहांत करीब है!",
    4: "शुक्रवार - इस सप्ताह की मेहनत के लिए शुभकामनाएं!",
    5: "शनिवार - आराम करें या काम पूरा करें!",
    6: "रविवार - नया सप्ताह शुरू करने का समय!",
}


def _detect_lang(text):
    """Detect zh/hi by script only.

    Language *tags* are handled separately by _lang_from_tag, so an English
    greeting like "hi" is never mistaken for the Hindi tag.
    """
    if not text:
        return "en"
    if _ZH_RE.search(text):
        return "zh-Hans"
    if _HI_RE.search(text):
        return "hi"
    return "en"


def _lang_from_tag(language):
    """Map a UI language tag (en/zh-Hans/zh/hi) onto a smalltalk language."""
    if not language:
        return "en"
    try:
        from chatbot.languages import normalize_language

        normalized = normalize_language(language)
    except Exception:
        return "en"
    return {"zh": "zh-Hans", "hi": "hi", "en": "en"}.get(normalized, "en")


def _salutation(hour, lang="en"):
    if lang == "zh-Hans":
        if hour < 12:
            return "早上好"
        if hour < 18:
            return "下午好"
        return "晚上好"
    if lang == "hi":
        if hour < 12:
            return "शुभ प्रभात"
        if hour < 18:
            return "शुभ दोपहर"
        return "शुभ संध्या"
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


def _greeting_reply(user_name, category, tz_offset_minutes, lang="en"):
    meta = workspace_meta(category)
    label = meta.get("label", category.title())
    emoji = meta.get("emoji", "\U0001F916")
    first = (user_name or "").split()[0] or "there"
    now = _local_now(tz_offset_minutes)
    salutation = _salutation(now.hour, lang)
    if lang == "zh-Hans":
        weekday_line = _WEEKDAY_LINES_ZH[now.weekday()]
        date_line = now.strftime("%Y年%m月%d日")
        blurb = meta.get("blurb", f"{label}文档")
        return f"{salutation}，{first}！{emoji} {weekday_line} 今天是{date_line}。我是您的{label}助手，可以帮助您了解{blurb}。您想了解什么？"
    if lang == "hi":
        weekday_line = _WEEKDAY_LINES_HI[now.weekday()]
        date_line = now.strftime("%d %B %Y")
        blurb = meta.get("blurb", f"{label} दस्तावेज़")
        return f"{salutation}, {first}! {emoji} {weekday_line} आज {date_line} है। मैं आपका {label} सहायक हूँ - {blurb} के बारे में आपकी मदद कर सकता हूँ। आप क्या जानना चाहते हैं?"
    weekday_line = _WEEKDAY_LINES[now.weekday()]
    date_line = now.strftime("%A, %B %d, %Y")
    blurb = meta.get("blurb", f"{label} documents")
    return (
        f"{salutation}, {first}! {emoji} {weekday_line} "
        f"Today is {date_line}. I'm your {label} assistant - I can help "
        f"with {blurb}. What would you like to know?"
    )


def _help_reply(user_name, category, lang="en"):
    meta = workspace_meta(category)
    label = meta.get("label", category.title())
    emoji = meta.get("emoji", "\U0001F916")
    if lang == "zh-Hans":
        blurb = meta.get("blurb", f"{label}文档")
        return f"我是您的{label}助手{emoji}。您可以询问有关{blurb}的任何问题。我只会根据您{label}工作区中的文档回答。随时说'你好'，我会回复您！"
    if lang == "hi":
        blurb = meta.get("blurb", f"{label} दस्तावेज़")
        return f"मैं आपका {label} सहायक {emoji} हूँ। आप {blurb} से संबंधित कुछ भी पूछ सकते हैं। मैं केवल आपके {label} कार्यक्षेत्र के दस्तावेज़ों से ही उत्तर देता हूँ। कृपया 'नमस्ते' कहें, मैं आपको अभिवादन करूंगा!"
    blurb = meta.get("blurb", "documents in this workspace")
    return (
        f"I'm your {label} assistant {emoji}. Ask me anything about "
        f"{blurb}. I answer strictly "
        f"from the documents in your {label} workspace. Say a friendly "
        f"'hello' anytime and I'll greet you back!"
    )


def _thanks_reply(user_name, category, lang="en"):
    meta = workspace_meta(category)
    emoji = meta.get("emoji", "\U0001F916")
    first = (user_name or "").split()[0] if user_name else ""
    if lang == "zh-Hans":
        return f"不客气{(', ' + first) if first else ''}！{emoji} 很高兴能帮到您 - 有什么问题随时问我。"
    if lang == "hi":
        return f"आपका स्वागत है{(', ' + first) if first else ''}! {emoji} खुशी हुई मदद करके - कुछ और पूछें।"
    return f"You're welcome{(', ' + first) if first else ''}! {emoji} Happy to help - ask me anything else."


def _howareyou_reply(user_name, category, lang="en"):
    meta = workspace_meta(category)
    label = meta.get("label", category.title())
    emoji = meta.get("emoji", "\U0001F916")
    first = (user_name or "").split()[0] or "there"
    if lang == "zh-Hans":
        blurb = meta.get("blurb", f"{label}文档")
        return f"我很好，{first}！{emoji} 谢谢您的问候。我是您的{label}助手，随时为您解答{blurb}相关问题。您想了解什么？"
    if lang == "hi":
        blurb = meta.get("blurb", f"{label} दस्तावेज़")
        return f"मैं बहुत बढ़िया हूँ, {first}! {emoji} पूछने के लिए धन्यवाद। मैं आपका {label} सहायक हूँ - {blurb} के बारे में आपकी मदद करने के लिए तैयार हूँ। आप क्या जानना चाहते हैं?"
    return (
        f"I'm doing great, {first}! {emoji} Thanks for asking. I'm your "
        f"{label} assistant - ready to help with {meta.get('blurb')}. "
        f"What would you like to know?"
    )


def handle(text, user_name=None, category="finance", tz_offset_minutes=0, language=None):
    intent = detect(text)
    detected = _detect_lang(text or "")
    if detected != "en":
        lang = detected
    else:
        lang = _lang_from_tag(language)
    if intent == "greeting":
        return _greeting_reply(user_name, category, tz_offset_minutes, lang=lang)
    if intent == "howareyou":
        return _howareyou_reply(user_name, category, lang=lang)
    if intent == "help":
        return _help_reply(user_name, category, lang=lang)
    if intent == "thanks":
        return _thanks_reply(user_name, category, lang=lang)
    return None
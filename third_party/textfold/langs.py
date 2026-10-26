"""The languages a paper can be read in. English is the base every translation is built from.

The interface speaks fewer of them (web/i18n.js): a text read in one it does not speak has the interface in English.
Budgets are per script: languages written without spaces count characters, the rest count words.
Arabic and Hebrew are left out for now: the reader lays text out left to right.
"""

# code: (name used in prompts, native name, script)
LANGS = {
    "en": ("English", "English", "latin"),
    "zh": ("Simplified Chinese", "简体中文", "cjk"),
    "zh-Hant": ("Traditional Chinese", "繁體中文", "cjk"),
    "ja": ("Japanese", "日本語", "cjk"),
    "ko": ("Korean", "한국어", "hangul"),
    "es": ("Spanish", "Español", "latin"),
    "fr": ("French", "Français", "latin"),
    "de": ("German", "Deutsch", "latin"),
    "pt": ("Portuguese", "Português", "latin"),
    "it": ("Italian", "Italiano", "latin"),
    "ru": ("Russian", "Русский", "cyrillic"),
    "hi": ("Hindi", "हिन्दी", "devanagari"),
    "sv": ("Swedish", "Svenska", "latin"),
    "da": ("Danish", "Dansk", "latin"),
    "nb": ("Norwegian Bokmål", "Norsk bokmål", "latin"),
    "fi": ("Finnish", "Suomi", "latin"),
    "nl": ("Dutch", "Nederlands", "latin"),
    "pl": ("Polish", "Polski", "latin"),
    "cs": ("Czech", "Čeština", "latin"),
    "uk": ("Ukrainian", "Українська", "cyrillic"),
    "tr": ("Turkish", "Türkçe", "latin"),
    "id": ("Indonesian", "Bahasa Indonesia", "latin"),
    "vi": ("Vietnamese", "Tiếng Việt", "latin"),
}

_CHARS = {"takeaway": 28, "reason": 50, "topic": 6, "section": 32, "unit": "characters"}
_WORDS = {"takeaway": 16, "reason": 26, "topic": 4, "section": 18, "unit": "words"}
_HANGUL = {"takeaway": 12, "reason": 22, "topic": 3, "section": 14, "unit": "words"}
_SYLLABLES = {"takeaway": 24, "reason": 40, "topic": 6, "section": 27, "unit": "words"}   # Vietnamese spaces its syllables


def name(code):
    return LANGS[code][0]


def natives():
    """Every language a text can be read in, by its own name, in the order of LANGS (for the pages' menus)."""
    return {code: v[1] for code, v in LANGS.items()}


def budget(code):
    script = LANGS[code][2]
    return _CHARS if script == "cjk" else _HANGUL if script == "hangul" else _SYLLABLES if code == "vi" else _WORDS


def cjk(code):
    return LANGS[code][2] == "cjk"


def stop(code):
    """The full stop that joins the takeaway and the reason in Brief."""
    return "。" if cjk(code) else "।" if LANGS[code][2] == "devanagari" else "."


def punctuation_rule(code):
    n = name(code)
    if code in ("zh", "zh-Hant"):
        return f"{n} full-width punctuation; no dashes (——)."
    if code == "ja":
        return "Japanese punctuation (、。); no dashes (——)."
    return f"Standard {n} punctuation; no em or en dashes as punctuation, use commas or colons."

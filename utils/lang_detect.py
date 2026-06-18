"""
Lang Detect -- Nhan dien ngon ngu tu noi dung TXT.

Chien luoc 3 lop kiem tra (defense-in-depth):

  Layer 1: HEURISTIC -- Ky tu dac trung (Unicode unique chars)
           --> Neu phat hien ky tu 100% duy nhat cua 1 ngon ngu
               thi return ngay, do chinh xac tuyet doi.
               VD: ky tu 'n~' (n with tilde) -> es
                   ky tu Cyrillic -> ru
                   Hiragana/Katakana -> ja

  Layer 2: LANGID -- Thu vien ml, nhanh, ho tro gioi han ngon ngu
           --> Chay voi danh sach ngon ngu duoc phep (restrict)
               De tranh bi nham ngon ngu khong lien quan.

  Layer 3: LANGDETECT -- Thu vien ml thu 2 (Google port)
           --> Chay khi langid khong chac chan (score thap)

  Ket hop: Neu 2/3 lop dong y -> return ket qua.
           Neu khong dong nhat -> return cai co score cao nhat.

  Fallback: Neu ca 3 that bai -> return "" (API tu detect)

Cach dung:
    from utils.lang_detect import detect_language, detect_language_from_files
    lang = detect_language("Hola, como estas?")   # -> "es"
    lang = detect_language_from_files([path1, path2]) # -> "es"
"""

import os
import re
import logging

log = logging.getLogger("ElevenLabsTTS")

# ============================================================
# CAC NGON NGU DUOC HO TRO (restrict set cho langid)
# ============================================================

# Chi detect trong tap nay -- tranh nham sang cac ngon ngu hiem
SUPPORTED_LANGS = {
    "en", "es", "pt", "fr", "de", "it",
    "vi", "ru", "ja", "ko", "zh", "ar",
    "tr", "pl", "nl", "hi", "id", "th",
}

LANGUAGE_NAMES = {
    "en": "English",
    "es": "Spanish",
    "pt": "Portuguese",
    "fr": "French",
    "de": "German",
    "it": "Italian",
    "vi": "Vietnamese",
    "ru": "Russian",
    "ja": "Japanese",
    "ko": "Korean",
    "zh": "Chinese",
    "ar": "Arabic",
    "tr": "Turkish",
    "pl": "Polish",
    "nl": "Dutch",
    "hi": "Hindi",
    "id": "Indonesian",
    "th": "Thai",
}

# ============================================================
# LAYER 1: HEURISTIC -- KY TU 100% DUY NHAT
# ============================================================

# Ky tu TUYET DOI chi co trong 1 ngon ngu cu the
# Neu phat hien bat ky ky tu nao trong nhom nay -> ket luan 100%
_ABSOLUTE_UNIQUE = [
    # (lang_code, set_of_chars, min_count)
    # Spanish: n with tilde + inverted punctuation
    ("es", set("ñÑ¿¡"), 1),
    # German: eszett (sharp s) -- chi co trong tieng Duc
    ("de", set("ß"), 1),
    # Vietnamese: tong hop dau dac trung
    # d with stroke, o with hook, u with hook, tone marks with hook
    ("vi", set("đĐơưƯƠắặẮẶếệẾỆốộỐỘứựỨỰ"), 1),
    # Russian/Cyrillic: bat ky ky tu Cyrillic
    ("ru", set("абвгдеёжзийклмнопрстуфхцчшщъыьэюяАБВГДЕЁЖЗИЙКЛМНОПРСТУФХЦЧШЩЪЫЬЭЮЯ"), 2),
    # Japanese: Hiragana
    ("ja", set("あいうえおかきくけこさしすせそたちつてとなにぬねのはひふへほまみむめもやゆよらりるれろわをんがぎぐげござじずぜぞだぢづでどばびぶべぼぱぴぷぺぽ"), 1),
    # Japanese: Katakana
    ("ja", set("アイウエオカキクケコサシスセソタチツテトナニヌネノハヒフヘホマミムメモヤユヨラリルレロワヲンガギグゲゴザジズゼゾダヂヅデドバビブベボパピプペポ"), 1),
    # Korean: Hangul
    ("ko", set("가나다라마바사아자차카타파하"), 1),
    # Arabic: Ky tu Arab
    ("ar", set("ابتثجحخدذرزسشصضطظعغفقكلمنهوي"), 2),
    # Thai
    ("th", set("กขฃคฅฆงจฉชซฌญฎฏฐฑฒณดตถทธนบปผฝพฟภมยรลวศษสหฬอฮ"), 1),
]

# Ky tu dac trung MANH (khong unique 100%, nhung rat dac trung)
# Dung de boost score
_STRONG_CHARS = {
    "pt": set("ãÃõÕ"),           # a~, o~ unique cho Portuguese
    "fr": set("œŒ«»ÿŸ"),         # oe, guillemets unique cho French
    "de": set("äöüÄÖÜ"),         # umlauts manh cho German
    "es": set("áéíóúüÁÉÍÓÚÜ"),  # accents pho bien trong Spanish
    "it": set("àèéìòùÀÈÉÌÒÙ"),  # accents pho bien trong Italian
}


def _heuristic_detect(text: str) -> tuple:
    """
    Layer 1: Kiem tra ky tu 100% duy nhat.
    
    Returns: (lang_code, confidence) hoac ("", 0.0)
    confidence: 1.0 = chac chan tuyet doi, 0.5 = kha chac
    """
    # Dem tong ky tu alpha
    total_alpha = max(sum(1 for c in text if c.isalpha()), 1)

    # Kiem tra CJK (Chinese/Japanese/Korean co the dung chung Han tu)
    cjk_count = sum(1 for c in text if 0x4E00 <= ord(c) <= 0x9FFF or
                    0x3400 <= ord(c) <= 0x4DBF)
    ja_count = sum(1 for c in text if any(c in chars for _, chars, _ in _ABSOLUTE_UNIQUE if _ == 1 and "a" not in str(chars)))

    # Absolute unique chars
    lang_counts = {}
    for lang_code, char_set, min_count in _ABSOLUTE_UNIQUE:
        count = sum(1 for c in text if c in char_set)
        if count >= min_count:
            lang_counts[lang_code] = lang_counts.get(lang_code, 0) + count

    if lang_counts:
        best = max(lang_counts, key=lambda k: lang_counts[k])
        ratio = lang_counts[best] / total_alpha
        # CJK: neu da co Hiragana/Katakana thi la Japanese, khong phai Chinese
        if best == "ja":
            return ("ja", 1.0)
        # Chinese: neu co nhieu CJK ma khong co Hiragana/Katakana
        if cjk_count > 5 and best not in ("ja", "ko"):
            return ("zh", 0.9)
        return (best, 1.0)

    # CJK standalone (Chinese)
    if cjk_count > 5:
        return ("zh", 0.9)

    # Strong chars check (khong unique 100% nhung rat dac trung)
    strong_scores = {}
    for lang, chars in _STRONG_CHARS.items():
        count = sum(1 for c in text if c in chars)
        if count > 0:
            strong_scores[lang] = count / total_alpha

    if strong_scores:
        best = max(strong_scores, key=lambda k: strong_scores[k])
        ratio = strong_scores[best]
        # Chi tin neu ty le du cao (> 1% so voi total alpha)
        if ratio > 0.01:
            return (best, 0.6)

    return ("", 0.0)


# ============================================================
# LAYER 2: LANGID
# ============================================================

_langid_classifier = None
_langid_ok = False


def _init_langid():
    """Khoi tao langid voi danh sach ngon ngu gioi han."""
    global _langid_classifier, _langid_ok
    if _langid_ok:
        return True
    try:
        import langid
        langid.set_languages(list(SUPPORTED_LANGS))
        _langid_classifier = langid
        _langid_ok = True
        return True
    except Exception as e:
        log.debug(f"langid not available: {e}")
        return False


def _langid_detect(text: str) -> tuple:
    """Layer 2: langid detect. Returns (lang, confidence 0-1)."""
    if not _init_langid():
        return ("", 0.0)
    try:
        lang, score = _langid_classifier.classify(text)
        # langid tra score la log-likelihood (am, gan 0 la chac nhat)
        # Chuyen ve confidence 0-1 (approximate)
        confidence = max(0.0, min(1.0, 1.0 + score / 10.0)) if score < 0 else 0.5
        if lang not in SUPPORTED_LANGS:
            return ("", 0.0)
        return (lang, confidence)
    except Exception:
        return ("", 0.0)


# ============================================================
# LAYER 3: LANGDETECT
# ============================================================

_langdetect_ok = None


def _init_langdetect():
    global _langdetect_ok
    if _langdetect_ok is not None:
        return _langdetect_ok
    try:
        import langdetect
        from langdetect import DetectorFactory
        DetectorFactory.seed = 42  # Dam bao ket qua on dinh
        _langdetect_ok = True
        return True
    except Exception as e:
        log.debug(f"langdetect not available: {e}")
        _langdetect_ok = False
        return False


def _langdetect_detect(text: str) -> tuple:
    """Layer 3: langdetect detect. Returns (lang, confidence 0-1)."""
    if not _init_langdetect():
        return ("", 0.0)
    try:
        from langdetect import detect_langs
        results = detect_langs(text)
        if results:
            best = results[0]
            lang = str(best.lang)
            # Map mot so code bi khac
            lang_map = {"zh-cn": "zh", "zh-tw": "zh", "pt": "pt", "pt-pt": "pt"}
            lang = lang_map.get(lang, lang)
            if lang not in SUPPORTED_LANGS:
                return ("", 0.0)
            return (lang, float(best.prob))
        return ("", 0.0)
    except Exception:
        return ("", 0.0)


# ============================================================
# TONG HOP: VOTE SYSTEM
# ============================================================

def detect_language(text: str, sample_size: int = 3000) -> str:
    """
    Nhan dien ngon ngu tu noi dung text bang 3 lop kiem tra.

    Args:
        text: Noi dung van ban
        sample_size: So ky tu dau dung de phan tich

    Returns:
        Ma ngon ngu ISO 639-1 ("es", "en", ...) hoac "" neu khong xac dinh
    """
    if not text or not text.strip():
        return ""

    sample = text[:sample_size].strip()

    # Yeu cau toi thieu: >= 20 ky tu va >= 5 ky tu alpha
    alpha_chars = [c for c in sample if c.isalpha()]
    if len(sample) < 20 or len(alpha_chars) < 5:
        return ""

    # Neu text chu yeu la so/ky hieu (< 30% alpha) -> khong detect
    if len(alpha_chars) / len(sample) < 0.30:
        return ""

    # --- Layer 1: Heuristic (absolute) ---
    h_lang, h_conf = _heuristic_detect(sample)
    if h_conf >= 1.0:
        # Phat hien ky tu 100% duy nhat -> tin tuong tuyet doi
        log.debug(f"[LangDetect] L1 Heuristic: {h_lang} (conf=1.0)")
        return h_lang

    # --- Layer 2: langid ---
    l2_lang, l2_conf = _langid_detect(sample)
    log.debug(f"[LangDetect] L2 langid: {l2_lang} (conf={l2_conf:.2f})")

    # --- Layer 3: langdetect ---
    l3_lang, l3_conf = _langdetect_detect(sample)
    log.debug(f"[LangDetect] L3 langdetect: {l3_lang} (conf={l3_conf:.2f})")

    # --- Tong hop ---
    votes = {}
    # Heuristic strong chars (conf < 1.0)
    if h_lang and h_conf >= 0.5:
        votes[h_lang] = votes.get(h_lang, 0) + h_conf * 1.5

    # langid
    if l2_lang and l2_conf > 0.0:
        votes[l2_lang] = votes.get(l2_lang, 0) + l2_conf * 2.0  # langid duoc tin nhieu hon

    # langdetect
    if l3_lang and l3_conf > 0.1:
        votes[l3_lang] = votes.get(l3_lang, 0) + l3_conf * 1.5

    if not votes:
        # Ca 3 deu that bai -> fallback ASCII check
        ascii_ratio = sum(1 for c in sample if ord(c) < 128 and c.isalpha())
        total = max(sum(1 for c in sample if c.isalpha()), 1)
        if ascii_ratio / total > 0.95:
            return "en"
        return ""

    best_lang = max(votes, key=lambda k: votes[k])
    best_score = votes[best_lang]

    # Nguong tin cay toi thieu
    if best_score < 0.3:
        return ""

    # Kiem tra thuat toan: neu langid va langdetect cung dong y -> chac chan
    if l2_lang == l3_lang and l2_lang:
        log.debug(f"[LangDetect] L2+L3 agree: {l2_lang}")
        return l2_lang

    log.debug(f"[LangDetect] Vote winner: {best_lang} (score={best_score:.2f})")
    return best_lang


def detect_language_from_file(filepath: str) -> str:
    """
    Detect ngon ngu tu 1 file TXT.

    Returns: language code hoac ""
    """
    try:
        with open(filepath, 'r', encoding='utf-8', errors='ignore') as f:
            text = f.read(5000)
        return detect_language(text)
    except Exception:
        return ""


def detect_language_from_files(filepaths: list, max_files: int = 3) -> str:
    """
    Detect ngon ngu tu danh sach file -- lay vote majority.

    Args:
        filepaths: Danh sach duong dan file TXT
        max_files: So file toi da dung de detect

    Returns: language code chiem da so, hoac "" neu khong xac dinh
    """
    if not filepaths:
        return ""

    sample_files = filepaths[:max_files]
    lang_votes = {}

    for fp in sample_files:
        lang = detect_language_from_file(fp)
        if lang:
            lang_votes[lang] = lang_votes.get(lang, 0) + 1

    if not lang_votes:
        return ""

    winner = max(lang_votes, key=lambda k: lang_votes[k])
    return winner


# ============================================================
# QUICK TEST
# ============================================================

if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

    test_cases = [
        # (expected, description, text)
        ("es", "Spanish WITH accent (n tilde)", "Hola, como estas? El nino juega en el parque. Muchas personas no saben controlar sus emociones."),
        ("es", "Spanish NO accent", "Bienvenidos a nuestro canal. Hoy hablaremos sobre psicologia. Muchas personas no saben que sus pensamientos pueden cambiar su realidad."),
        ("es", "Spanish YouTube script", "Hoy vamos a hablar sobre algo que muchos de ustedes han experimentado pero pocos se atreven a admitir. La soledad no siempre significa estar solo."),
        ("en", "English standard", "Hello and welcome to our channel. Today we will talk about psychology and human behavior. Many people do not realize that their thoughts can change their reality."),
        ("en", "English YouTube script", "Today I want to share something that changed my life completely. It all started when I was just 25 years old and working at a small company in New York."),
        ("pt", "Portuguese WITH accent", "Ola, como voce esta? As criancas brincam no parque. Tambem gosto de musica brasileira."),
        ("fr", "French standard", "Bonjour, comment allez-vous? Aujourd'hui c'est une belle journee. Les enfants jouent dans le parc."),
        ("de", "German standard", "Hallo, wie geht es Ihnen? Heute ist ein schoner Tag. Die Kinder spielen im Park. Ich mochte Wasser bitte."),
        ("it", "Italian standard", "Ciao, come stai? Oggi e una bella giornata. I bambini giocano nel parco. Mi piace la musica italiana."),
        ("ru", "Russian Cyrillic", "Privet, kak dela? Segodnya khoroshaIa pogoda. Deti igrayut v parke. Mne nravitsya muzyka."),
        ("ja", "Japanese", "Konnichi wa, ogenki desu ka? Kyoo wa ii otenki desu ne. Kodomotachi ga kooen de asonde imasu."),
        ("vi", "Vietnamese", "Xin chao, ban co khoe khong? Hom nay la mot ngay dep troi. Nhung dua tre dang choi trong cong vien."),
        ("en", "Very short", "Hello world"),
        ("", "Empty", ""),
        ("", "Numbers only", "123 456 789"),
    ]

    print("=" * 70)
    print("  LANG DETECT 3-LAYER TEST")
    print("=" * 70)

    # Check libs
    try:
        import langid
        langid.set_languages(list(SUPPORTED_LANGS))
        print(f"  [OK] langid installed")
    except Exception as e:
        print(f"  [WARN] langid not available: {e}")

    try:
        import langdetect
        print(f"  [OK] langdetect installed")
    except Exception as e:
        print(f"  [WARN] langdetect not available: {e}")

    print()

    correct = 0
    total = 0
    for expected, desc, text in test_cases:
        result = detect_language(text)
        if expected == "":
            ok = (result == "")
        else:
            ok = (result == expected)

        if ok:
            correct += 1
            status = "[OK]  "
        else:
            status = "[FAIL]"

        total += 1
        snippet = text[:45] + "..." if len(text) > 45 else text
        print(f"{status} {desc:<35s} expect={expected or 'N/A':4s} got={result or 'N/A':4s}")
        if not ok:
            print(f"       Text: {snippet}")

    print()
    print(f"  Ket qua: {correct}/{total} chinh xac ({correct*100//total}%)")
    print("=" * 70)

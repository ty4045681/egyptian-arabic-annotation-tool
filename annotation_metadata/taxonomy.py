"""Stable scene codes, crawler aliases, and review/confidence rules."""

from __future__ import annotations

from typing import Iterable

# Canonical catalog: ten stable codes, English labels, this display order.
SCENE_DEFS: tuple[dict[str, object], ...] = (
    {"code": "restaurant", "label_zh": "餐厅", "label_en": "Restaurant",
     "sort_order": 1},
    {"code": "hotel", "label_zh": "酒店", "label_en": "Hotel", "sort_order": 2},
    {"code": "taxi", "label_zh": "出租车", "label_en": "Taxi", "sort_order": 3},
    {"code": "airport", "label_zh": "机场", "label_en": "Airport", "sort_order": 4},
    {"code": "clinic", "label_zh": "诊所", "label_en": "Clinic", "sort_order": 5},
    {"code": "tourism_information", "label_zh": "旅游信息",
     "label_en": "Tourism information", "sort_order": 6},
    {"code": "emergencies", "label_zh": "紧急情况",
     "label_en": "Emergencies", "sort_order": 7},
    {"code": "spoken_languages", "label_zh": "口语",
     "label_en": "Spoken languages", "sort_order": 8},
    {"code": "business_negotiation", "label_zh": "商务谈判",
     "label_en": "Business negotiation", "sort_order": 9},
    {"code": "shopping", "label_zh": "购物", "label_en": "Shopping",
     "sort_order": 10},
)

SCENE_CODES: frozenset[str] = frozenset(item["code"] for item in SCENE_DEFS)
SCENE_BY_CODE: dict[str, dict[str, object]] = {item["code"]: item for item in SCENE_DEFS}
SCENE_ORDER: tuple[str, ...] = tuple(item["code"] for item in SCENE_DEFS)

# Missing/NULL/legacy-unknown SOURCE evidence maps to this catalog scene.
# Model predictions and human reviews do not use this fallback.
FALLBACK_SOURCE_SCENE = "spoken_languages"

CONFIDENCE_LEVELS: tuple[str, ...] = ("high", "medium", "low", "unknown")
CONFIDENCE_RANK: dict[str, int] = {
    "high": 1, "medium": 2, "low": 3, "unknown": 4,
}
REVIEW_STATUSES: tuple[str, ...] = (
    "pending", "confirmed", "mixed", "out_of_scope", "uncertain",
)
SCOPE_MODES: tuple[str, ...] = ("all", "restricted", "none")
CLAIM_POLICIES: tuple[str, ...] = ("source_confidence", "fifo")
MEDIA_VARIANT = "pcm16k_mono"

# Folder names and crawler labels map onto source scenes. Other historical
# classify.py labels stay legacy/model tags and are not coerced.
_SCENE_ALIASES: dict[str, str] = {
    "restaurant": "restaurant",
    "餐厅": "restaurant",
    "07_餐厅": "restaurant",
    "07-餐厅": "restaurant",
    "hotel": "hotel",
    "酒店": "hotel",
    "08_酒店": "hotel",
    "08-酒店": "hotel",
    "taxi": "taxi",
    "出租车": "taxi",
    "09_出租车": "taxi",
    "09-出租车": "taxi",
    "airport": "airport",
    "机场": "airport",
    "01_机场": "airport",
    "01-机场": "airport",
    "clinic": "clinic",
    "诊所": "clinic",
    "04_诊所": "clinic",
    "04-诊所": "clinic",
    "tourism_information": "tourism_information",
    "tourism information": "tourism_information",
    "旅游信息": "tourism_information",
    "02_旅游信息": "tourism_information",
    "02-旅游信息": "tourism_information",
    "emergencies": "emergencies",
    "紧急情况": "emergencies",
    "05_紧急情况": "emergencies",
    "05-紧急情况": "emergencies",
    "spoken_languages": "spoken_languages",
    "spoken languages": "spoken_languages",
    "口语": "spoken_languages",
    "08_口语": "spoken_languages",
    "08-口语": "spoken_languages",
    "10_口语": "spoken_languages",
    "10-口语": "spoken_languages",
    "business_negotiation": "business_negotiation",
    "business negotiation": "business_negotiation",
    "商务谈判": "business_negotiation",
    "06_商务谈判": "business_negotiation",
    "06-商务谈判": "business_negotiation",
    "shopping": "shopping",
    "购物": "shopping",
    "03_购物": "shopping",
    "03-购物": "shopping",
}

# classify.py labels that correspond to a source scene. Unlisted labels
# (Other-*, Rejected) stay model-only.
MODEL_LABEL_TO_SCENE: dict[str, str] = {
    "Restaurant": "restaurant",
    "Hotel": "hotel",
    "Taxi": "taxi",
    "Airport": "airport",
    "Clinic": "clinic",
    "Tourism information": "tourism_information",
    "Emergencies": "emergencies",
    "Spoken languages": "spoken_languages",
    "Business negotiation": "business_negotiation",
    "Shopping": "shopping",
}

CONFIDENCE_LABEL: dict[str, str] = {
    "high": "Source confidence High",
    "medium": "Source confidence Medium",
    "low": "Source confidence Low",
    "unknown": "Source confidence Unknown",
}

REVIEW_LABEL: dict[str, str] = {
    "pending": "Scene review pending",
    "confirmed": "Scene confirmed",
    "mixed": "Multiple scenes",
    "out_of_scope": "Outside the ten scenes",
    "uncertain": "Cannot determine",
}

# Kept for crawler/export compatibility; platform copy uses CONFIDENCE_LABEL.
CONFIDENCE_LABEL_ZH: dict[str, str] = {
    "high": "高",
    "medium": "中",
    "low": "低",
    "unknown": "未知",
}

REVIEW_LABEL_ZH: dict[str, str] = {
    "pending": "场景待核验",
    "confirmed": "场景已确认",
    "mixed": "多场景",
    "out_of_scope": "不属于十场景",
    "uncertain": "无法判断",
}

VOLATILE_RAW_KEYS: frozenset[str] = frozenset({
    "crawled_at", "scraped_at", "fetched_at", "imported_at", "updated_at",
    "checked_at", "timestamp", "ts", "last_seen", "refresh_time",
})


def normalize_key(value: str | None) -> str:
    return " ".join(str(value or "").strip().lower().replace("_", " ").split())


def resolve_scene_code(value: str | None) -> str | None:
    """Map a crawler/folder/label string to a stable scene code.

    Unrecognised labels return None. Callers must not invent a scene from
    a directory number or a completed-task category. ``unknown`` is not a
    scene code; source filters map it separately.
    """
    raw = str(value or "").strip()
    if not raw:
        return None
    if raw in SCENE_CODES:
        return raw
    alias = _SCENE_ALIASES.get(raw) or _SCENE_ALIASES.get(raw.lower())
    if alias:
        return alias
    folded = raw.replace("-", "_")
    alias = _SCENE_ALIASES.get(folded) or _SCENE_ALIASES.get(folded.lower())
    if alias:
        return alias
    return _SCENE_ALIASES.get(normalize_key(raw))


def is_source_fallback_code(code: str | None) -> bool:
    """True for stored/effective Spoken languages, including NULL and unknown."""
    if code in (None, ""):
        return True
    text = str(code).strip().lower()
    return text in {"unknown", FALLBACK_SOURCE_SCENE}


def is_source_fallback_filter(value: str | None) -> bool:
    """True when a source_scene filter explicitly selects the fallback bucket."""
    if value in (None, ""):
        return False
    return is_source_fallback_code(value)


def effective_source_scene(code: str | None) -> str:
    """Canonical SOURCE category. NULL / unknown / missing -> spoken_languages.

    Do not use for model predictions or human review labels.
    """
    if is_source_fallback_code(code):
        return FALLBACK_SOURCE_SCENE
    return str(code)


def normalize_source_scene_filter(value: str | None) -> str | None:
    """Source-scene filter/claim input. ``unknown`` means Spoken languages."""
    if value in (None, "", "all"):
        return None
    if str(value).strip().lower() == "unknown":
        return FALLBACK_SOURCE_SCENE
    code = resolve_scene_code(value)
    if code is None:
        raise ValueError("unrecognised scene filter")
    return code


def effective_source_scene_sql(expr: str) -> str:
    """SQL fragment: NULL source scene_code is Spoken languages."""
    return f"COALESCE({expr}, '{FALLBACK_SOURCE_SCENE}')"


def scene_label(code: str | None, *, lang: str = "en") -> str:
    """Catalog label. Missing codes are empty; source fallback uses source_scene_label."""
    if not code:
        return ""
    item = SCENE_BY_CODE.get(code)
    if not item:
        return code
    return str(item["label_en"] if lang != "zh" else item["label_zh"])


def source_scene_label(code: str | None, *, lang: str = "en") -> str:
    return scene_label(effective_source_scene(code), lang=lang)


def confidence_label(level: str | None, *, lang: str = "en") -> str:
    value = level if level in CONFIDENCE_LEVELS else "unknown"
    if lang == "zh":
        return f"来源置信度{CONFIDENCE_LABEL_ZH[value]}"
    return CONFIDENCE_LABEL[value]


def review_label(status: str | None, *, lang: str = "en") -> str:
    value = status if status in REVIEW_STATUSES else "pending"
    if lang == "zh":
        return REVIEW_LABEL_ZH[value]
    return REVIEW_LABEL[value]


def validate_review_labels(status: str, scene_codes: Iterable[str]) -> list[str]:
    codes = []
    seen: set[str] = set()
    for raw in scene_codes:
        code = resolve_scene_code(raw) or str(raw or "").strip()
        if code not in SCENE_CODES:
            raise ValueError(f"unknown scene code: {raw!r}")
        if code not in seen:
            seen.add(code)
            codes.append(code)
    if status == "confirmed" and len(codes) != 1:
        raise ValueError("confirmed reviews require exactly one scene")
    if status == "mixed" and len(codes) < 2:
        raise ValueError("mixed reviews require at least two scenes")
    if status == "out_of_scope" and codes:
        raise ValueError("out_of_scope reviews must not include the ten scenes")
    if status == "pending" and codes:
        raise ValueError("pending reviews must not include scene labels")
    return codes


def model_scene_code(label: str | None) -> str | None:
    raw = str(label or "").strip()
    if not raw:
        return None
    if raw in SCENE_CODES:
        return raw
    return MODEL_LABEL_TO_SCENE.get(raw)


def ordered_scene_codes(codes: Iterable[str]) -> list[str]:
    wanted = {code for code in codes if code in SCENE_CODES}
    return [code for code in SCENE_ORDER if code in wanted]

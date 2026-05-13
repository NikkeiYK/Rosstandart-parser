from __future__ import annotations

"""Фильтрация уведомлений по ключевым словам, связанным с полимерными изделиями."""

# Ключевые слова (в нижнем регистре, без окончаний — для поиска подстрокой)
POLYMER_KEYWORDS = [
    "полимер",
    "пластмасс",
    "пластик",
    "резин",
    "каучук",
    "полиэтилен",
    "полипропилен",
    "пвх",
    "полиуретан",
    "эпоксид",
    "композит",
    "стеклопластик",
    "полиамид",
    "силикон",
    "фторопласт",
    "термопласт",
    "эластомер",
    "латекс",
    "полистирол",
    "полиэфир",
    "поликарбонат",
]


def _get_search_text(notification: dict) -> str:
    """Собирает текст из всех релевантных полей уведомления для поиска."""
    fields = [
        notification.get("project_name", ""),
        notification.get("title", ""),
        notification.get("technical_committee", ""),
        notification.get("developer", ""),
        notification.get("doc_type", ""),
    ]
    return " ".join(fields).lower()


def is_polymer_related(notification: dict) -> bool:
    """Проверяет, связано ли уведомление с полимерными изделиями."""
    text = _get_search_text(notification)
    return any(keyword in text for keyword in POLYMER_KEYWORDS)


def get_matched_keywords(notification: dict) -> list[str]:
    """Возвращает список совпавших полимерных ключевых слов."""
    text = _get_search_text(notification)
    return [kw for kw in POLYMER_KEYWORDS if kw in text]

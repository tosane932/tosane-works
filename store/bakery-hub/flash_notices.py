"""Present Flash notices without losing their category or unrelated messages."""

from flask import get_flashed_messages, session
from flask_login import current_user


LOGIN_REQUIRED_MESSAGE = "このページを利用するにはログインが必要です。"
_LOGIN_REQUIRED_FLASHES = {
    ("warning", LOGIN_REQUIRED_MESSAGE),
    ("message", "Please log in to access this page."),  # Existing sessions before localization.
}


def visible_flash_messages():
    """Consume queued notices once and collapse only identical category/text pairs."""
    seen = set()
    notices = []
    for category, message in get_flashed_messages(with_categories=True):
        category, message = str(category), str(message)
        if (category, message) in _LOGIN_REQUIRED_FLASHES:
            if current_user.is_authenticated:
                continue  # An already-valid session may still carry a pre-deployment prompt.
            category, message = "warning", LOGIN_REQUIRED_MESSAGE
        pair = (category, message)
        if pair not in seen:
            seen.add(pair)
            notices.append(pair)
    return notices


def discard_login_required_flashes():
    """After a successful login, keep every Flash except obsolete login prompts."""
    pending = session.get("_flashes")
    if not pending:
        return
    remaining = [entry for entry in pending if tuple(entry) not in _LOGIN_REQUIRED_FLASHES]
    if len(remaining) != len(pending):
        if remaining:
            session["_flashes"] = remaining
        else:
            session.pop("_flashes")

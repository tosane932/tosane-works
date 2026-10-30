"""Guest体験の独立ゲート。外部入力を認可根拠にしない。"""
from flask import current_app
from flask_login import current_user


def guest_register_enabled(owner=None):
    if not (current_app.config.get('REGISTER_V32_GUEST_ENABLED')
            and current_user and current_user.is_authenticated and current_user.is_guest):
        return False
    return owner is None or (owner.kind == 'guest'
                             and owner.id == current_user.dataset_id
                             and owner.system_key is None)


def guest_staff_enabled(owner=None):
    from models import RegisterCutover, db
    if not (current_app.config.get('GUEST_STAFF_ENABLED', False)
            and guest_register_enabled(owner)):
        return False
    return db.session.get(RegisterCutover, current_user.dataset_id) is not None

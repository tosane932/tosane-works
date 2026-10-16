"""旧日次売上の書込前にDataset行をロックして切替日を検証する。"""

from models import RegisterCutover, db
from register_products import _lock_dataset


class LegacySalesClosed(ValueError):
    pass


def assert_legacy_sales_writable(dataset, business_date):
    """呼出側の同じDB transactionで旧売上を書き、commit/rollbackまでロックを保持する。

    切替日を書き込む将来の処理も同じDataset行を先にロックすること。
    """
    owner = _lock_dataset(dataset)
    cutover = db.session.get(RegisterCutover, owner.id)
    if cutover is not None and business_date >= cutover.first_register_date:
        raise LegacySalesClosed("この営業日はレジ会計へ切り替わっています。旧日次売上は保存できません。")


def legacy_sales_closed(dataset, business_date):
    """GETの案内用。最終的な可否は必ずPOSTのロック下で再確認する。"""
    cutover = db.session.get(RegisterCutover, dataset.id)
    return cutover is not None and business_date >= cutover.first_register_date

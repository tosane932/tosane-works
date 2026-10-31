from flask import g
from models import PrepRecipe, RegisterProduct, db
from tests.test_guest_register_v32 import start
from tests.test_production_prep import _create


def test_register_product_recipe_is_dataset_scoped(flask_app, monkeypatch):
    monkeypatch.setitem(flask_app.config, 'REGISTER_V32_GUEST_ENABLED', True)
    monkeypatch.setitem(flask_app.config, 'GUEST_PREP_ENABLED', True)
    a, ca = start(flask_app)
    b, cb = start(flask_app)
    product = RegisterProduct(dataset_id=a.id, name='架空パン', price_yen=200)
    db.session.add(product)
    db.session.commit()
    g.pop('csrf_token', None)
    assert _create(ca, source=f'register:{product.id}').status_code == 303
    recipe = PrepRecipe.query.filter_by(dataset_id=a.id).one()
    assert recipe.product_key == f'register:{product.id}'
    assert len(recipe.ingredients) == 2
    assert _create(ca, source=f'register:{product.id}').status_code == 303
    assert PrepRecipe.query.filter_by(dataset_id=a.id).count() == 1
    g.pop('_login_user', None)
    g.pop('csrf_token', None)
    assert cb.get(f'/prep/recipes/{recipe.id}').status_code == 404
    assert cb.get(f'/prep/recipes/new?source=register:{product.id}').status_code == 404
    assert cb.get('/prep/recipes/new?source=template:croissant').status_code == 404
    monkeypatch.setitem(flask_app.config, 'GUEST_PREP_ENABLED', False)
    assert cb.get('/prep').status_code == 403


def test_guest_edit_bulk_history_and_cleanup(flask_app, monkeypatch, admin_dataset):
    import datetime
    from decimal import Decimal
    from sqlalchemy import text
    import app as app_module
    from models import Dataset, PrepPlan, PrepIngredient, RegisterPrepLink
    from tests.test_production_prep import _token
    monkeypatch.setitem(flask_app.config, 'REGISTER_V32_GUEST_ENABLED', True)
    monkeypatch.setitem(flask_app.config, 'GUEST_PREP_ENABLED', True)
    a, ca = start(flask_app)
    b, cb = start(flask_app)
    for owner in (a, b, admin_dataset):
        recipe = PrepRecipe(dataset_id=owner.id, product_key='register:999999', display_name='架空保持', base_quantity=10)
        recipe.ingredients = [PrepIngredient(dataset_id=owner.id, name='粉', unit='g', amount=Decimal('500'))]
        db.session.add(recipe)
        db.session.flush()
        db.session.add(PrepPlan(dataset_id=owner.id, recipe_id=recipe.id, business_date=datetime.date(2026,10,31), quantity=10))
    db.session.commit()
    r = PrepRecipe.query.filter_by(dataset_id=a.id).one()
    g.pop('csrf_token', None)
    path = f'/prep/recipes/{r.id}/edit'
    assert ca.post(path, data={'csrf_token':_token(ca,path),'base_quantity':'5','note':'架空',
        'ingredient_name':['粉','水'], 'ingredient_amount':['12.5','2'], 'ingredient_unit':['g','ml']}).status_code == 303
    db.session.refresh(r)
    assert len(r.ingredients) == 2 and r.ingredients[0].amount == Decimal('12.5')
    assert ca.post('/prep/plans/bulk',data={'csrf_token':_token(ca,'/prep'), 'business_date':'2026-10-31',
        'recipe_id':[str(r.id)], f'quantity_{r.id}':'3'}).status_code == 303
    assert PrepPlan.query.filter_by(dataset_id=a.id).one().quantity == 3
    assert ca.post('/prep/plans/bulk', data={'recipe_id':[str(r.id)]}).status_code == 400
    other = PrepRecipe.query.filter_by(dataset_id=b.id).one()
    assert ca.post('/prep/plans/bulk',data={'csrf_token':_token(ca,'/prep'), 'business_date':'2026-10-31',
        'recipe_id':[str(r.id),str(other.id)],f'quantity_{r.id}':'9',f'quantity_{other.id}':'9'}).status_code == 404
    assert PrepPlan.query.filter_by(dataset_id=a.id).one().quantity == 3
    db.session.execute(text('PRAGMA foreign_keys = ON'))
    aid,bid=a.id,b.id
    now=datetime.datetime.now(datetime.timezone.utc)
    a.created_at=now-datetime.timedelta(hours=3)
    a.absolute_expires_at=now-datetime.timedelta(seconds=1)
    db.session.commit()
    assert app_module._cleanup_expired_guest_datasets(now=now) == 1
    db.session.commit()
    for model in (PrepPlan,PrepIngredient,PrepRecipe,RegisterPrepLink):
        assert model.query.filter_by(dataset_id=aid).count() == 0
    assert db.session.get(Dataset,aid) is None
    for owner_id in (bid,admin_dataset.id):
        assert PrepPlan.query.filter_by(dataset_id=owner_id).count() == 1
        assert PrepRecipe.query.filter_by(dataset_id=owner_id).count() == 1
    assert app_module._cleanup_expired_guest_datasets(now=now) == 0


def test_same_name_and_retirement_preserve_recipe_identity(flask_app, monkeypatch):
    monkeypatch.setitem(flask_app.config,'REGISTER_V32_GUEST_ENABLED',True)
    monkeypatch.setitem(flask_app.config,'GUEST_PREP_ENABLED',True)
    a,client=start(flask_app)
    products=[RegisterProduct(dataset_id=a.id,name='同名パン',price_yen=100) for _ in range(2)]
    db.session.add_all(products);db.session.commit()
    g.pop('csrf_token',None)
    for product in products:
        assert _create(client,source=f'register:{product.id}').status_code==303
    assert PrepRecipe.query.filter_by(dataset_id=a.id).count()==2
    keys={r.product_key for r in PrepRecipe.query.filter_by(dataset_id=a.id)}
    products[0].name='改名';products[0].is_active=False;db.session.commit()
    for recipe in PrepRecipe.query.filter_by(dataset_id=a.id):
        assert client.get(f'/prep/recipes/{recipe.id}').status_code==200
    assert {r.product_key for r in PrepRecipe.query.filter_by(dataset_id=a.id)}==keys

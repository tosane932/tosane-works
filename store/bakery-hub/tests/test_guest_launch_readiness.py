"""本番に触れず、Guest仕込みの失敗と入力境界を確認する。"""
import pytest
from flask import g
from sqlalchemy.exc import SQLAlchemyError
from models import PrepPlan, PrepRecipe, RegisterProduct, db
from tests.test_guest_register_v32 import start
from tests.test_production_prep import _create, _token


@pytest.fixture
def prep(flask_app, monkeypatch):
    monkeypatch.setitem(flask_app.config,'REGISTER_V32_GUEST_ENABLED',True)
    monkeypatch.setitem(flask_app.config,'GUEST_PREP_ENABLED',True)
    owner,client=start(flask_app)
    product=RegisterProduct.query.filter_by(dataset_id=owner.id).first()
    g.pop('csrf_token',None)
    assert _create(client,source=f'register:{product.id}').status_code==303
    return owner,client,product,PrepRecipe.query.filter_by(dataset_id=owner.id).one()


@pytest.mark.parametrize('amount',['-1','1.0001','NaN','1000001',''])
def test_guest_invalid_material_preserves_recipe(prep,amount):
    owner,client,product,recipe=prep
    path=f'/prep/recipes/{recipe.id}/edit'
    before=[(r.name,r.amount) for r in recipe.ingredients]
    assert client.post(path,data={'csrf_token':_token(client,path),'base_quantity':'10',
        'ingredient_name':['粉'],'ingredient_amount':[amount],'ingredient_unit':['g'],'note':''}).status_code==400
    db.session.refresh(recipe)
    assert [(r.name,r.amount) for r in recipe.ingredients]==before


def test_guest_bulk_db_failure_rolls_back(prep,monkeypatch):
    owner,client,product,recipe=prep
    token=_token(client,'/prep')
    original=db.session.commit
    def fail_plan():
        if any(isinstance(row,PrepPlan) for row in list(db.session.new)+list(db.session.dirty)):
            raise SQLAlchemyError('synthetic prep save failure')
        return original()
    monkeypatch.setattr(db.session,'commit',fail_plan)
    response=client.post('/prep/plans/bulk',data={'csrf_token':token,'business_date':'2026-11-01',
        'recipe_id':[str(recipe.id)],f'quantity_{recipe.id}':'20'})
    assert response.status_code==500
    assert PrepPlan.query.filter_by(dataset_id=owner.id).count()==0
    assert PrepRecipe.query.filter_by(dataset_id=owner.id).count()==1


def test_guest_copy_explains_register_identity(prep):
    _,client,_,_=prep
    page=client.get('/prep/recipes').text
    assert 'レジの商品IDごとに選びます' in page
    assert '月次の商品IDごとに選びます' not in page

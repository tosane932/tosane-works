"""設定だけの退避・復元。実データ・ネットワークを使わない。"""
import csv
import datetime
from decimal import Decimal
import hashlib
import io
import json
from pathlib import Path
import uuid

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session

from models import (Dataset, Product, RegisterProduct, PrepRecipe, PrepIngredient,
                    RegisterPrepLink, RegisterPriceHistory, db)
from data_vault import VaultError, export_bundle, import_bundle, load_bundle, main

STATIC = Path(__file__).resolve().parents[1] / "static"


def database(path):
    engine = create_engine("sqlite:///" + str(path))
    @event.listens_for(engine, "connect")
    def fk(conn, _):
        conn.execute("PRAGMA foreign_keys=ON")
    db.metadata.create_all(engine)
    with Session(engine) as s, s.begin():
        owner = Dataset(id=uuid.uuid4(), kind="admin", system_key="admin")
        s.add(owner)
        identifier = owner.id
    return engine, identifier


def sample(engine, owner):
    photo = (STATIC / "register_images/croissant.webp").read_bytes()
    with Session(engine) as s, s.begin():
        s.add(Product(id=70, dataset_id=owner, name="元の月次商品", year=2026, month=10, price=280))
        s.add_all([
            RegisterProduct(id=101, dataset_id=owner, name="=架空パン,\"引用\"\n二行", price_yen=0,
                            bread_kind="sweet", product_class="seasonal", is_active=False,
                            display_order=8, photo_webp=photo),
            RegisterProduct(id=102, dataset_id=owner, name="同名", price_yen=100000,
                            bread_kind="other", product_class="special", display_order=2),
            RegisterProduct(id=103, dataset_id=owner, name="同名", price_yen=280,
                            template_key="croissant", image_key="croissant", display_order=4),
            PrepRecipe(id=201, dataset_id=owner, product_key="product:70", display_name="架空レシピ",
                       base_quantity=12, note="@メモ\n'そのまま\",引用"),
            PrepRecipe(id=202, dataset_id=owner, product_key="template:croissant",
                       display_name="長" * 100, base_quantity=1, note="", image_key="croissant"),
        ])
        s.flush()
        s.add_all([
            PrepIngredient(dataset_id=owner, recipe_id=201, name="小麦粉", unit="g", amount=Decimal("123.456")),
            PrepIngredient(dataset_id=owner, recipe_id=201, name="水", unit="ml", amount=Decimal("1000000")),
            PrepIngredient(dataset_id=owner, recipe_id=202, name="卵", unit="個", amount=Decimal("0.001")),
            RegisterPrepLink(dataset_id=owner, recipe_id=201, register_product_id=101),
        ])
    return photo


def business_snapshot(engine, owner):
    with Session(engine) as s:
        products = s.scalars(select(RegisterProduct).where(RegisterProduct.dataset_id == owner)).all()
        recipes = s.scalars(select(PrepRecipe).where(PrepRecipe.dataset_id == owner)).all()
        product_names = {p.id: (p.name, p.price_yen, p.is_active) for p in products}
        recipe_names = {r.id: r.display_name for r in recipes}
        def photo(p):
            return p.photo_webp or ((STATIC / 'register_images' / (p.image_key + '.webp')).read_bytes()
                                    if p.image_key else b'')
        return {
            "products": sorted((p.name, p.price_yen, p.bread_kind, p.product_class,
                p.is_active, p.display_order, p.template_key or "", hashlib.sha256(photo(p)).hexdigest()) for p in products),
            "recipes": sorted((r.display_name, r.base_quantity, r.note, r.image_key or "",
                sorted((i.name, i.unit, str(i.amount)) for i in r.ingredients)) for r in recipes),
            "links": sorted((recipe_names[l.recipe_id], product_names[l.register_product_id])
                for l in s.scalars(select(RegisterPrepLink).where(RegisterPrepLink.dataset_id == owner))),
            "templates": sorted((r.product_key, p.template_key) for r in recipes for p in products
                if p.template_key and r.product_key == 'template:' + p.template_key),
        }


@pytest.fixture()
def pair(tmp_path):
    source, src = database(tmp_path / "source.db")
    target, dst = database(tmp_path / "target.db")
    yield source, src, target, dst
    source.dispose()
    target.dispose()


def test_roundtrip_is_atomic_remaps_ids_and_preserves_business_data(pair, tmp_path):
    source, src, target, dst = pair
    sample(source, src)
    folder = tmp_path / "backup"
    manifest = export_bundle(source, src, folder, static_folder=STATIC)
    assert manifest['source_dataset'] == {'id': str(src), 'kind': 'admin'}
    assert manifest['counts'] == {'products.csv': 3, 'recipes.csv': 2,
                                  'ingredients.csv': 3, 'product_recipe_links.csv': 1}
    assert import_bundle(target, dst, folder, static_folder=STATIC)['dry_run'] is True
    with Session(target) as s:
        assert not s.scalars(select(RegisterProduct)).all()
    with pytest.raises(VaultError):
        import_bundle(target, dst, folder, static_folder=STATIC, apply=True)
    result = import_bundle(target, dst, folder, static_folder=STATIC, apply=True, confirm_dataset=str(dst))
    assert result['dry_run'] is False
    assert business_snapshot(source, src) == business_snapshot(target, dst)
    with Session(target) as s:
        assert set(s.scalars(select(RegisterProduct.id))).isdisjoint({101, 102, 103})
        assert not s.scalars(select(RegisterPriceHistory)).all()
        assert not s.scalars(select(Product)).all()
        restored = s.scalar(select(PrepRecipe).where(PrepRecipe.display_name == '架空レシピ'))
        assert restored.product_key.startswith('vault:') and restored.product_key != 'product:70'
    with pytest.raises(VaultError):
        import_bundle(target, dst, folder, static_folder=STATIC, apply=True, confirm_dataset=str(dst))
    assert business_snapshot(source, src) == business_snapshot(target, dst)


def test_empty_and_export_never_overwrites(pair, tmp_path):
    source, src, target, dst = pair
    folder = tmp_path / 'empty'
    m = export_bundle(source, src, folder, static_folder=STATIC)
    assert not any(m['counts'].values())
    import_bundle(target, dst, folder, static_folder=STATIC, apply=True, confirm_dataset=str(dst))
    with pytest.raises(VaultError):
        export_bundle(source, src, folder, static_folder=STATIC)


def rewrite_csv(folder, filename, change):
    path = folder / filename
    rows = list(csv.reader(io.StringIO(path.read_text())))
    change(rows)
    out = io.StringIO(newline='')
    csv.writer(out, quoting=csv.QUOTE_ALL, lineterminator='\n').writerows(rows)
    path.write_text(out.getvalue())
    resign(folder, filename)


def resign(folder, filename):
    path = folder / filename
    m = json.loads((folder / 'backup-manifest.json').read_text())
    m['files'][filename] = {'sha256': hashlib.sha256(path.read_bytes()).hexdigest(), 'bytes': path.stat().st_size}
    (folder / 'backup-manifest.json').write_text(json.dumps(m))


@pytest.mark.parametrize('damage', ['missing', 'checksum', 'price', 'reference', 'classification',
    'formula', 'duplicate', 'image', 'traversal', 'symlink', 'guest_manifest', 'unit', 'amount'])
def test_corrupt_or_hostile_bundle_is_rejected_without_writes(pair, tmp_path, damage):
    source, src, target, dst = pair
    sample(source, src)
    folder = tmp_path / 'bad'
    export_bundle(source, src, folder, static_folder=STATIC)
    if damage == 'missing': (folder / 'recipes.csv').unlink()
    elif damage == 'checksum': (folder / 'products.csv').write_text('broken')
    elif damage in ('price', 'classification', 'formula', 'duplicate'):
        def change(rows):
            if damage == 'price': rows[1][rows[0].index('price_yen')] = "'100001"
            elif damage == 'classification': rows[1][rows[0].index('bread_kind')] = "'bad"
            elif damage == 'formula': rows[1][rows[0].index('name')] = '=unsafe'
            else: rows[2][0] = rows[1][0]
        rewrite_csv(folder, 'products.csv', change)
    elif damage in ('reference', 'unit', 'amount'):
        def change(rows):
            key, value = {'reference': ('recipe_ref', "'r999"), 'unit': ('unit', "'kg"),
                          'amount': ('amount', "'NaN")}[damage]
            rows[1][rows[0].index(key)] = value
        rewrite_csv(folder, 'ingredients.csv', change)
    elif damage in ('image', 'symlink'):
        path = next((folder / 'images').glob('*.webp'))
        path.unlink()
        if damage == 'symlink': path.symlink_to(STATIC / 'register_images/croissant.webp')
        else:
            path.write_bytes(b'not an image')
            resign(folder, path.relative_to(folder).as_posix())
    else:
        path = folder / 'backup-manifest.json'
        m = json.loads(path.read_text())
        if damage == 'guest_manifest': m['source_dataset']['kind'] = 'guest'
        else: m['files']['../escape.webp'] = {'sha256': '0'*64, 'bytes': 1}
        path.write_text(json.dumps(m))
    with pytest.raises(VaultError):
        import_bundle(target, dst, folder, static_folder=STATIC, apply=True, confirm_dataset=str(dst))
    with Session(target) as s:
        assert not s.scalars(select(RegisterProduct)).all()
        assert not s.scalars(select(PrepRecipe)).all()


def test_guest_and_wrong_dataset_and_existing_rows_are_rejected(pair, tmp_path):
    source, src, target, dst = pair
    now = datetime.datetime.now(datetime.timezone.utc)
    guest = uuid.uuid4()
    with Session(source) as s, s.begin():
        s.add(Dataset(id=guest, kind='guest', created_at=now, last_activity_at=now,
                      absolute_expires_at=now + datetime.timedelta(hours=2)))
        s.flush()
        s.add(RegisterProduct(dataset_id=guest, name='他Dataset', price_yen=1))
    with pytest.raises(VaultError): export_bundle(source, guest, tmp_path/'guest', static_folder=STATIC)
    folder = tmp_path / 'admin'
    export_bundle(source, src, folder, static_folder=STATIC)
    assert load_bundle(folder)[0]['counts']['products.csv'] == 0
    with pytest.raises(VaultError): import_bundle(target, uuid.uuid4(), folder, static_folder=STATIC)
    with Session(target) as s, s.begin():
        s.add(Product(dataset_id=dst, name='既存旧商品', year=2026, month=10, price=100))
    with pytest.raises(VaultError): import_bundle(target, dst, folder, static_folder=STATIC)


def test_restore_exception_rolls_back_every_table(pair, tmp_path):
    source, src, target, dst = pair
    sample(source, src)
    folder = tmp_path/'rollback'
    export_bundle(source, src, folder, static_folder=STATIC)
    def fail(conn, cursor, statement, parameters, context, many):
        if 'INSERT INTO register_prep_links' in statement:
            raise RuntimeError('synthetic failure')
    event.listen(target, 'before_cursor_execute', fail)
    try:
        with pytest.raises(RuntimeError, match='synthetic'):
            import_bundle(target, dst, folder, static_folder=STATIC, apply=True, confirm_dataset=str(dst))
    finally:
        event.remove(target, 'before_cursor_execute', fail)
    with Session(target) as s:
        for model in (RegisterProduct, PrepRecipe, PrepIngredient, RegisterPrepLink, RegisterPriceHistory):
            assert not s.scalars(select(model)).all()


@pytest.mark.parametrize('name', ['長'*100, "'先頭の引用符", '  =1+1', '\t@式', '\r-1', '改行\n\"引用\",パン'])
def test_formula_escape_roundtrips_exact_text(pair, tmp_path, name):
    source, src, target, dst = pair
    with Session(source) as s, s.begin():
        s.add(RegisterProduct(dataset_id=src, name=name, price_yen=0))
    folder = tmp_path/'text'
    export_bundle(source, src, folder, static_folder=STATIC)
    cells = list(csv.reader(io.StringIO((folder/'products.csv').read_text())))[1]
    assert all(value.startswith("'") for value in cells)
    import_bundle(target, dst, folder, static_folder=STATIC, apply=True, confirm_dataset=str(dst))
    with Session(target) as s:
        assert s.scalar(select(RegisterProduct.name)) == name


def test_cross_dataset_legacy_recipe_source_cannot_be_exported(pair, tmp_path):
    source, src, _, _ = pair
    now = datetime.datetime.now(datetime.timezone.utc)
    guest = uuid.uuid4()
    with Session(source) as s, s.begin():
        s.add(Dataset(id=guest, kind='guest', created_at=now, last_activity_at=now,
                      absolute_expires_at=now + datetime.timedelta(hours=2)))
        s.flush()
        s.add(Product(id=99, dataset_id=guest, name='Guestの元商品', year=2026, month=10, price=1))
        s.add(PrepRecipe(id=2, dataset_id=src, product_key='product:99', display_name='不正対応', base_quantity=1))
        s.flush()
        s.add(PrepIngredient(dataset_id=src, recipe_id=2, name='粉', unit='g', amount=1))
    with pytest.raises(VaultError):
        export_bundle(source, src, tmp_path/'cross', static_folder=STATIC)
    assert not (tmp_path/'cross').exists()


def test_recipe_asset_must_match_and_export_failure_removes_only_new_output(pair, tmp_path, monkeypatch):
    source, src, target, dst = pair
    sample(source, src)
    folder = tmp_path/'image'
    export_bundle(source, src, folder, static_folder=STATIC)
    with pytest.raises(VaultError):
        import_bundle(target, dst, folder, static_folder=tmp_path, apply=True, confirm_dataset=str(dst))
    monkeypatch.setattr('data_vault.load_bundle', lambda _: (_ for _ in ()).throw(VaultError('synthetic')))
    with pytest.raises(VaultError):
        export_bundle(source, src, tmp_path/'unfinished', static_folder=STATIC)
    assert not (tmp_path/'unfinished').exists()
    assert folder.is_dir()


def test_cli_requires_protected_connection_and_never_prints_driver_details(pair, tmp_path, capsys):
    source, src, _, _ = pair
    folder = tmp_path/'cli'
    export_bundle(source, src, folder, static_folder=STATIC)
    assert main(['verify', '--folder', str(folder)]) == 0
    secret = tmp_path/'connection'
    secret.write_text('postgresql://fictional:fake-password@invalid.invalid/no_db')
    secret.chmod(0o644)
    args = ['import', '--folder', str(folder), '--dataset-id', str(src), '--database-url-file', str(secret)]
    assert main(args) == 2
    secret.chmod(0o600)
    # Invalid driver syntax raises before opening any network connection.
    secret.write_text('not-a-url-fake-password')
    assert main(args) == 2
    out = capsys.readouterr()
    assert 'fake-password' not in out.err + out.out


def test_cli_import_does_not_load_app_or_config():
    import subprocess
    import sys
    subprocess.run([sys.executable, '-c', "import data_vault, sys; assert 'app' not in sys.modules; assert 'config' not in sys.modules"], check=True)


def test_cli_export_dry_run_and_explicit_apply(pair, tmp_path, capsys):
    source, src, target, dst = pair
    with Session(source) as s, s.begin():
        s.add(RegisterProduct(dataset_id=src, name='CLI架空商品', price_yen=300))
    a, b = tmp_path/'source-url', tmp_path/'target-url'
    for path, engine in ((a, source), (b, target)):
        path.write_text(str(engine.url))
        path.chmod(0o600)
    folder = tmp_path/'cli-export'
    assert main(['export','--folder',str(folder),'--database-url-file',str(a),'--dataset-id',str(src)]) == 0
    args = ['import','--folder',str(folder),'--database-url-file',str(b),'--dataset-id',str(dst)]
    assert main(args) == 0
    with Session(target) as s:
        assert not s.scalars(select(RegisterProduct)).all()
    assert main(args + ['--apply','--confirm-dataset',str(dst)]) == 0
    assert business_snapshot(source, src) == business_snapshot(target, dst)


def test_readback_catches_valid_but_changed_database_values_and_rolls_back(pair, tmp_path):
    source, src, target, dst = pair
    with Session(source) as s, s.begin():
        s.add(RegisterProduct(dataset_id=src, name='架空', price_yen=280))
    folder = tmp_path/'readback'
    export_bundle(source, src, folder, static_folder=STATIC)
    def mutate(conn, cursor, statement, parameters, context, many):
        if statement.startswith('INSERT INTO register_products'):
            conn.exec_driver_sql('UPDATE register_products SET price_yen=281')
    event.listen(target, 'after_cursor_execute', mutate)
    try:
        with pytest.raises(VaultError, match='一致'):
            import_bundle(target, dst, folder, static_folder=STATIC, apply=True, confirm_dataset=str(dst))
    finally:
        event.remove(target, 'after_cursor_execute', mutate)
    with Session(target) as s:
        assert not s.scalars(select(RegisterProduct)).all()


def test_published_examples_are_fictional_and_restore(pair):
    _, _, target, dst = pair
    folder = Path(__file__).resolve().parents[1]/'docs/data-vault-template/examples'
    manifest, _, _ = load_bundle(folder)
    assert manifest['source_dataset']['id'] == '11111111-1111-4111-8111-111111111111'
    import_bundle(target, dst, folder, static_folder=STATIC, apply=True, confirm_dataset=str(dst))
    with Session(target) as s:
        assert len(s.scalars(select(RegisterProduct)).all()) == 2

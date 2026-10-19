"""Adminの商品設定だけを退避する独立CLI。app/configをimportしない。"""
import argparse
from collections import Counter
from contextlib import contextmanager
import csv
import datetime
from decimal import Decimal, InvalidOperation
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import stat
import uuid

from PIL import Image
from sqlalchemy import create_engine, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from models import (Dataset, Product, PrepRecipe, PrepIngredient, RegisterProduct,
                    RegisterPrepLink, db)
from product_images import (PRODUCT_IMAGE_OPTIONS, _safe_image_path,
                            available_product_image_assets)
from product_rules import BREAD_KINDS, PRODUCT_CLASSES, MAX_PRODUCT_PRICE_YEN
from register_images import register_image_choices


SCHEMAS = {
    'products.csv': ('ref', 'name', 'price_yen', 'bread_kind', 'product_class',
                     'is_active', 'display_order', 'template_key', 'image_file'),
    'recipes.csv': ('ref', 'source_key', 'display_name', 'base_quantity', 'note',
                    'image_key', 'image_file'),
    'ingredients.csv': ('ref', 'recipe_ref', 'name', 'unit', 'amount'),
    'product_recipe_links.csv': ('product_ref', 'recipe_ref'),
}
TABLES = ('register_products', 'prep_recipes', 'prep_ingredients', 'register_prep_links')
MAX_ROWS = 10000
MAX_FILE = 16 * 1024 * 1024
MAX_BUNDLE = 256 * 1024 * 1024
IMAGE_PATH = re.compile(r'images/[0-9a-f]{64}\.(webp|png|jpg)\Z')
KEY = re.compile(r'[a-z0-9_]{1,100}\Z')
STATIC = Path(__file__).resolve().parent / 'static'


class VaultError(ValueError):
    """CLIに表示してよい固定メッセージだけを含む。"""


def _uuid(value):
    try:
        parsed = uuid.UUID(str(value))
        if str(parsed) != str(value):
            raise ValueError
        return parsed
    except (ValueError, TypeError, AttributeError):
        raise VaultError('Dataset／backup IDの形式が不正です。') from None


def _integer(value, low, high):
    if not re.fullmatch(r'0|[1-9][0-9]{0,9}', str(value)):
        raise VaultError('整数の形式が不正です。')
    result = int(value)
    if not low <= result <= high:
        raise VaultError('整数が許容範囲を超えています。')
    return result


def _string(value, maximum, *, blank=False):
    if not isinstance(value, str) or '\x00' in value or len(value) > maximum:
        raise VaultError('文字列の形式・長さが不正です。')
    if not blank and not value.strip():
        raise VaultError('必須文字列が空です。')


def _amount(value):
    if not re.fullmatch(r'(0|[1-9][0-9]{0,6})(\.[0-9]{1,3})?', value):
        raise VaultError('材料量は小数第3位までの正数が必要です。')
    try:
        amount = Decimal(value)
    except InvalidOperation:
        raise VaultError('材料量が不正です。') from None
    if not Decimal('0') < amount <= Decimal('1000000'):
        raise VaultError('材料量が許容範囲を超えています。')
    return amount


def _image(data, *, product=False):
    if not data or len(data) > (512000 if product else MAX_FILE):
        raise VaultError('写真のサイズが不正です。')
    try:
        with Image.open(io.BytesIO(data)) as picture:
            extension = {'WEBP': 'webp', 'PNG': 'png', 'JPEG': 'jpg'}.get(picture.format)
            if (not extension or (product and extension != 'webp') or
                    picture.width * picture.height > 16_000_000 or
                    getattr(picture, 'n_frames', 1) != 1):
                raise VaultError('写真の形式・画素数・フレーム数が不正です。')
            picture.load()
            return extension
    except (OSError, ValueError, Image.DecompressionBombError):
        raise VaultError('写真の内容を検証できません。') from None


def _asset(key, static_folder, *, product):
    root = Path(static_folder)
    assets = available_product_image_assets(root, is_guest=False)
    if product:
        if key not in register_image_choices(root, is_guest=False, include_urls=False):
            raise VaultError('商品の承認画像が見つかりません。')
        path = (_safe_image_path((root/'register_images').resolve(), key+'.webp')
                if key in PRODUCT_IMAGE_OPTIONS else
                _safe_image_path((root/'product_images').resolve(), assets[key]['filename']))
    else:
        option = assets.get(key)
        path = _safe_image_path((root/'product_images').resolve(), option['filename']) if option else None
    if path is None or path.stat().st_size > MAX_FILE:
        raise VaultError('レシピ／商品の承認画像が見つかりません。')
    data = path.read_bytes()
    _image(data, product=product)
    return data


@contextmanager
def _session(engine, *, writing=False):
    if engine.dialect.name not in ('sqlite', 'postgresql'):
        raise VaultError('対応DBはSQLiteとPostgreSQLです。')
    with Session(engine, autoflush=False) as session, session.begin():
        if engine.dialect.name == 'postgresql':
            if writing:
                # Import is a maintenance operation on an empty target. Block raw
                # writers too, not just application writers that lock Dataset.
                names = [t.name for t in db.metadata.sorted_tables if 'dataset_id' in t.c]
                names.append('datasets')
                quoted = ', '.join(engine.dialect.identifier_preparer.quote(n) for n in sorted(names))
                session.execute(text('LOCK TABLE ' + quoted + ' IN SHARE ROW EXCLUSIVE MODE'))
            else:
                session.execute(text('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY'))
        else:
            session.connection().exec_driver_sql('BEGIN IMMEDIATE' if writing else 'BEGIN')
        yield session


def _admin(session, dataset_id):
    identifier = _uuid(dataset_id)
    owner = session.get(Dataset, identifier)
    if owner is None or owner.kind != 'admin' or owner.system_key != 'admin':
        raise VaultError('指定したAdmin Datasetが見つかりません。')
    return identifier


def _empty(session, dataset_id):
    # Include out-of-scope business tables in the emptiness guard without
    # exporting them. An existing staff row, old Product, audit or sale rejects.
    for table in db.metadata.sorted_tables:
        if 'dataset_id' in table.c and session.execute(select(table.c.dataset_id).where(
                table.c.dataset_id == dataset_id).limit(1)).first() is not None:
            raise VaultError('復元先Admin Datasetに既存データがあります。上書きしません。')


def _csv_bytes(columns, rows):
    output = io.StringIO(newline='')
    writer = csv.writer(output, quoting=csv.QUOTE_ALL, lineterminator='\n')
    writer.writerow(columns)
    # Every data cell has an apostrophe, including numbers. This is reversible
    # and prevents formula-prefix tricks using whitespace, quotes or newlines.
    for row in rows:
        writer.writerow(["'" + str(row[key]) for key in columns])
    return output.getvalue().encode('utf-8')


def _read_csv(data, columns):
    try:
        reader = csv.reader(io.StringIO(data.decode('utf-8'), newline=''), strict=True)
        if next(reader) != list(columns):
            raise VaultError('CSVの列・順序が不正です。')
        rows = []
        for row in reader:
            if len(row) != len(columns) or any(not cell.startswith("'") for cell in row):
                raise VaultError('CSVのセル数・数式保護が不正です。')
            rows.append(dict(zip(columns, (cell[1:] for cell in row))))
            if len(rows) > MAX_ROWS:
                raise VaultError('CSVの件数上限を超えています。')
        return rows
    except (UnicodeError, csv.Error, StopIteration):
        raise VaultError('CSVが破損しています。') from None


def _unique(rows, field, prefix):
    result = {}
    for row in rows:
        ref = row[field]
        if not re.fullmatch(prefix + r'[1-9][0-9]{0,19}', ref) or ref in result:
            raise VaultError('参照IDが不正または重複しています。')
        result[ref] = row
    return result


def _validate(rows, images):
    products = _unique(rows['products.csv'], 'ref', 'p')
    recipes = _unique(rows['recipes.csv'], 'ref', 'r')
    _unique(rows['ingredients.csv'], 'ref', 'i')
    templates, sources, materials, linked_products, linked_recipes, used = set(), set(), set(), set(), set(), set()
    for p in products.values():
        _string(p['name'], 100)
        _integer(p['price_yen'], 0, MAX_PRODUCT_PRICE_YEN)
        _integer(p['display_order'], 0, 2147483647)
        if p['bread_kind'] not in BREAD_KINDS or p['product_class'] not in PRODUCT_CLASSES or p['is_active'] not in ('true', 'false'):
            raise VaultError('商品の分類・状態が不正です。')
        key = p['template_key']
        if key and (not KEY.fullmatch(key) or key in templates):
            raise VaultError('商品候補キーが不正または重複しています。')
        if key:
            templates.add(key)
        if p['image_file']:
            data = images.get(p['image_file'])
            if data is None:
                raise VaultError('商品写真がありません。')
            _image(data, product=True)
            used.add(p['image_file'])
    for r in recipes.values():
        _string(r['display_name'], 100)
        _string(r['note'], 2000, blank=True)
        _integer(r['base_quantity'], 1, 1000)
        source = r['source_key']
        if (source in sources or not re.fullmatch(
                r'(template:[a-z0-9_]{1,100}|product:[1-9][0-9]{0,19}|vault:[0-9a-f-]{36}:r[1-9][0-9]{0,19})', source)):
            raise VaultError('レシピ元キーが不正または重複しています。')
        sources.add(source)
        if bool(r['image_key']) != bool(r['image_file']) or (r['image_key'] and not KEY.fullmatch(r['image_key'])):
            raise VaultError('レシピ写真の対応が不正です。')
        if r['image_file']:
            if r['image_file'] not in images:
                raise VaultError('レシピ写真がありません。')
            used.add(r['image_file'])
    material_counts = Counter()
    for i in rows['ingredients.csv']:
        _string(i['name'], 100)
        _amount(i['amount'])
        key = (i['recipe_ref'], i['name'], i['unit'])
        if i['recipe_ref'] not in recipes or i['unit'] not in ('g', 'ml', '個') or key in materials:
            raise VaultError('材料の参照・単位・重複が不正です。')
        materials.add(key)
        material_counts[i['recipe_ref']] += 1
    if any(not 1 <= material_counts[ref] <= 30 for ref in recipes):
        raise VaultError('各レシピの材料は1〜30件必要です。')
    for link in rows['product_recipe_links.csv']:
        p, r = products.get(link['product_ref']), recipes.get(link['recipe_ref'])
        if (p is None or r is None or p['template_key'] or r['source_key'].startswith('template:') or
                link['product_ref'] in linked_products or link['recipe_ref'] in linked_recipes):
            raise VaultError('商品とレシピの対応が不正です。')
        linked_products.add(link['product_ref'])
        linked_recipes.add(link['recipe_ref'])
    if used != set(images):
        raise VaultError('未参照または不足している写真があります。')


def _json_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise VaultError('Manifestに重複キーがあります。')
        result[key] = value
    return result


def _read_file(root, relative):
    path = root / relative
    # Reject symlinks in both the image directory and final file. Never unpack
    # paths or write files supplied by a manifest.
    if any(part.is_symlink() for part in (path, *path.parents)):
        raise VaultError('シンボリックリンクは読み込めません。')
    descriptors = []
    try:
        parent = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        descriptors.append(parent)
        parts = relative.split('/')
        for directory in parts[:-1]:
            parent = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
            descriptors.append(parent)
        fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        with os.fdopen(fd, 'rb') as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise VaultError('通常ファイル以外は読み込めません。')
            data = stream.read(MAX_FILE + 1)
        if len(data) > MAX_FILE:
            raise VaultError('ファイルが大きすぎます。')
        return data
    except OSError:
        raise VaultError('バックアップファイルが見つからないか読み込めません。') from None
    finally:
        for fd in reversed(descriptors):
            os.close(fd)


def load_bundle(folder):
    """全ファイルを検証してメモリ上に固定。検証後に画像を再読込しない。"""
    root = Path(folder).absolute()
    try:
        manifest = json.loads(_read_file(root, 'backup-manifest.json').decode('utf-8'),
                              object_pairs_hook=_json_object)
        if (set(manifest) != {'format', 'version', 'backup_id', 'created_at', 'source_dataset',
                              'tables', 'counts', 'files'} or
                manifest['format'] != 'bakeryhub-settings' or type(manifest['version']) is not int or
                manifest['version'] != 1 or manifest['tables'] != list(TABLES)):
            raise VaultError('Manifestの形式・版が不正です。')
        _uuid(manifest['backup_id'])
        if set(manifest['source_dataset']) != {'id', 'kind'} or manifest['source_dataset']['kind'] != 'admin':
            raise VaultError('Admin以外のバックアップは復元できません。')
        _uuid(manifest['source_dataset']['id'])
        stamp = datetime.datetime.fromisoformat(manifest['created_at'])
        if stamp.utcoffset() is None:
            raise VaultError('保存日時にはタイムゾーンが必要です。')
        files = manifest['files']
        if not isinstance(files, dict) or not set(SCHEMAS).issubset(files) or len(files) > MAX_ROWS * 2 + 4:
            raise VaultError('Manifestのファイル一覧が不正です。')
        loaded, total = {}, 0
        for name, expected in files.items():
            if name not in SCHEMAS and not IMAGE_PATH.fullmatch(name):
                raise VaultError('許可されていないファイル名です。')
            if (set(expected) != {'sha256', 'bytes'} or not re.fullmatch(r'[0-9a-f]{64}', expected['sha256']) or
                    type(expected['bytes']) is not int or not 0 <= expected['bytes'] <= MAX_FILE):
                raise VaultError('ファイル検査情報が不正です。')
            total += expected['bytes']
            if total > MAX_BUNDLE:
                raise VaultError('バックアップ全体が大きすぎます。')
            data = _read_file(root, name)
            if len(data) != expected['bytes'] or hashlib.sha256(data).hexdigest() != expected['sha256']:
                raise VaultError('チェックサムが一致しません。')
            if name not in SCHEMAS:
                extension = _image(data)
                if name != 'images/' + expected['sha256'] + '.' + extension:
                    raise VaultError('写真名と内容が一致しません。')
            loaded[name] = data
        actual = set()
        for path in root.rglob('*'):
            if path.is_symlink():
                raise VaultError('シンボリックリンクは読み込めません。')
            if path.is_file():
                actual.add(path.relative_to(root).as_posix())
        if actual != set(files) | {'backup-manifest.json'}:
            raise VaultError('一覧にないファイルがあります。')
        rows = {name: _read_csv(loaded[name], columns) for name, columns in SCHEMAS.items()}
        if (set(manifest['counts']) != set(SCHEMAS) or any(type(manifest['counts'][n]) is not int or
                manifest['counts'][n] != len(rows[n]) for n in SCHEMAS)):
            raise VaultError('件数が一致しません。')
        images = {name: data for name, data in loaded.items() if name not in SCHEMAS}
        _validate(rows, images)
        return manifest, rows, images
    except (KeyError, TypeError, ValueError, AttributeError, RecursionError) as error:
        if isinstance(error, VaultError):
            raise
        raise VaultError('バックアップの形式が不正です。') from None


def export_bundle(engine, dataset_id, destination, *, static_folder=STATIC):
    rows = {name: [] for name in SCHEMAS}
    images = {}
    image_bytes = 0
    def save_image(data, *, product=False):
        nonlocal image_bytes
        extension = _image(data, product=product)
        name = 'images/' + hashlib.sha256(data).hexdigest() + '.' + extension
        if name not in images:
            image_bytes += len(data)
            if image_bytes > MAX_BUNDLE:
                raise VaultError('写真の合計サイズ上限を超えています。')
        images[name] = data
        return name
    with _session(engine) as session:
        owner = _admin(session, dataset_id)
        def all_rows(model):
            result = session.scalars(select(model).where(model.dataset_id == owner).order_by(model.id)
                                     .limit(MAX_ROWS + 1).execution_options(yield_per=50))
            for count, item in enumerate(result):
                if count >= MAX_ROWS:
                    raise VaultError('書出し件数上限を超えています。')
                yield item
        for p in all_rows(RegisterProduct):
            photo = p.photo_webp or (_asset(p.image_key, static_folder, product=True) if p.image_key else None)
            rows['products.csv'].append(dict(ref=f'p{p.id}', name=p.name, price_yen=p.price_yen,
                bread_kind=p.bread_kind, product_class=p.product_class, is_active=str(p.is_active).lower(),
                display_order=p.display_order, template_key=p.template_key or '',
                image_file=save_image(photo, product=True) if photo else ''))
        for r in all_rows(PrepRecipe):
            if r.product_key.startswith('product:'):
                old_id = r.product_key.removeprefix('product:')
                if not old_id.isdecimal() or session.scalar(select(Product.id).where(
                        Product.id == int(old_id), Product.dataset_id == owner)) is None:
                    raise VaultError('レシピ元の旧商品が同じDatasetにありません。')
            photo = _asset(r.image_key, static_folder, product=False) if r.image_key else None
            rows['recipes.csv'].append(dict(ref=f'r{r.id}', source_key=r.product_key,
                display_name=r.display_name, base_quantity=r.base_quantity, note=r.note,
                image_key=r.image_key or '', image_file=save_image(photo) if photo else ''))
        for i in all_rows(PrepIngredient):
            rows['ingredients.csv'].append(dict(ref=f'i{i.id}', recipe_ref=f'r{i.recipe_id}',
                name=i.name, unit=i.unit, amount=format(i.amount, 'f')))
        links = session.scalars(select(RegisterPrepLink).where(RegisterPrepLink.dataset_id == owner)
                                .order_by(RegisterPrepLink.recipe_id).limit(MAX_ROWS+1)).all()
        if len(links) > MAX_ROWS:
            raise VaultError('対応表の件数上限を超えています。')
        rows['product_recipe_links.csv'] = [dict(product_ref=f'p{l.register_product_id}',
            recipe_ref=f'r{l.recipe_id}') for l in links]
        _validate(rows, images)
    files = {name: _csv_bytes(SCHEMAS[name], data) for name, data in rows.items()} | images
    if sum(len(data) for data in files.values()) > MAX_BUNDLE or any(len(data)>MAX_FILE for data in files.values()):
        raise VaultError('書出しサイズ上限を超えています。')
    manifest = dict(format='bakeryhub-settings', version=1, backup_id=str(uuid.uuid4()),
        created_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),
        source_dataset={'id': str(owner), 'kind': 'admin'}, tables=list(TABLES),
        counts={name: len(data) for name, data in rows.items()},
        files={name: {'sha256': hashlib.sha256(data).hexdigest(), 'bytes': len(data)}
               for name, data in sorted(files.items())})
    target = Path(destination)
    try:
        target.mkdir(mode=0o700)  # exclusive reservation; never overwrite a backup
    except OSError:
        raise VaultError('保存先を新規作成できません。既存フォルダは上書きしません。') from None
    try:
        (target/'images').mkdir(mode=0o700)
        for name, data in files.items():
            with (target/name).open('xb') as stream:
                os.chmod(stream.name, 0o600)
                stream.write(data)
        path = target/'backup-manifest.json'
        with path.open('x', encoding='utf-8') as stream:
            os.chmod(path, 0o600)
            json.dump(manifest, stream, ensure_ascii=False, indent=2)
            stream.write('\n')
        load_bundle(target)
    except Exception:
        shutil.rmtree(target)  # only the private directory created by this call
        raise
    return manifest


def _verify_restored(session, owner, rows, images, product_ids, recipe_ids, backup_id):
    """DBから読み戻して照合し、不一致ならcommitする前に止める。"""
    session.expire_all()
    products = {p.id: p for p in session.scalars(select(RegisterProduct).where(RegisterProduct.dataset_id == owner))}
    recipes = {r.id: r for r in session.scalars(select(PrepRecipe).where(PrepRecipe.dataset_id == owner))}
    if len(products) != len(product_ids) or len(recipes) != len(recipe_ids):
        raise VaultError('復元後の商品・レシピ件数が一致しません。')
    for row in rows['products.csv']:
        p = products[product_ids[row['ref']]]
        actual = (p.name, p.price_yen, p.bread_kind, p.product_class, p.is_active,
                  p.display_order, p.template_key, p.photo_webp, p.image_key)
        expected = (row['name'], int(row['price_yen']), row['bread_kind'], row['product_class'],
                    row['is_active']=='true', int(row['display_order']), row['template_key'] or None,
                    images.get(row['image_file']), None)
        if actual != expected:
            raise VaultError('復元後の商品・写真の値が一致しません。')
    for row in rows['recipes.csv']:
        r = recipes[recipe_ids[row['ref']]]
        key = row['source_key'] if row['source_key'].startswith('template:') else 'vault:' + backup_id + ':' + row['ref']
        if (r.product_key, r.display_name, r.base_quantity, r.note, r.image_key) != (
                key, row['display_name'], int(row['base_quantity']), row['note'], row['image_key'] or None):
            raise VaultError('復元後のレシピの値が一致しません。')
    ingredients = sorted((i.recipe_id, i.name, i.unit, i.amount) for i in session.scalars(
        select(PrepIngredient).where(PrepIngredient.dataset_id == owner)))
    expected = sorted((recipe_ids[i['recipe_ref']], i['name'], i['unit'], _amount(i['amount']))
                      for i in rows['ingredients.csv'])
    links = sorted((l.register_product_id, l.recipe_id) for l in session.scalars(
        select(RegisterPrepLink).where(RegisterPrepLink.dataset_id == owner)))
    expected_links = sorted((product_ids[l['product_ref']], recipe_ids[l['recipe_ref']])
                            for l in rows['product_recipe_links.csv'])
    if ingredients != expected or links != expected_links:
        raise VaultError('復元後の材料・対応関係が一致しません。')


def import_bundle(engine, dataset_id, folder, *, static_folder=STATIC, apply=False, confirm_dataset=None):
    owner = _uuid(dataset_id)
    if apply and confirm_dataset != str(owner):
        raise VaultError('書込には復元先Datasetの明示確認が必要です。')
    manifest, rows, images = load_bundle(folder)
    # Recipes still use approved static image keys. Do not write imported files
    # into public static directories. Require exactly the original approved asset.
    for recipe in rows['recipes.csv']:
        if recipe['image_key'] and _asset(recipe['image_key'], static_folder, product=False) != images[recipe['image_file']]:
            raise VaultError('復元先の承認レシピ画像が異なります。')
    with _session(engine, writing=apply) as session:
        _admin(session, owner)
        _empty(session, owner)
        if apply:
            products, recipes = {}, {}
            for p in rows['products.csv']:
                product = RegisterProduct(dataset_id=owner, name=p['name'], price_yen=int(p['price_yen']),
                    bread_kind=p['bread_kind'], product_class=p['product_class'], is_active=p['is_active']=='true',
                    display_order=int(p['display_order']), template_key=p['template_key'] or None,
                    photo_webp=images.get(p['image_file']))
                session.add(product)
                products[p['ref']] = product
            for r in rows['recipes.csv']:
                source = r['source_key'] if r['source_key'].startswith('template:') else (
                    'vault:' + manifest['backup_id'] + ':' + r['ref'])
                recipe = PrepRecipe(dataset_id=owner, product_key=source, display_name=r['display_name'],
                    base_quantity=int(r['base_quantity']), note=r['note'], image_key=r['image_key'] or None)
                session.add(recipe)
                recipes[r['ref']] = recipe
            session.flush()
            for i in rows['ingredients.csv']:
                session.add(PrepIngredient(dataset_id=owner, recipe_id=recipes[i['recipe_ref']].id,
                    name=i['name'], unit=i['unit'], amount=_amount(i['amount'])))
            for link in rows['product_recipe_links.csv']:
                session.add(RegisterPrepLink(dataset_id=owner,
                    register_product_id=products[link['product_ref']].id, recipe_id=recipes[link['recipe_ref']].id))
            session.flush()
            _verify_restored(session, owner, rows, images, {ref: p.id for ref,p in products.items()},
                             {ref: r.id for ref,r in recipes.items()}, manifest['backup_id'])
    return {'dry_run': not apply, 'source_dataset': manifest['source_dataset']['id'],
            'target_dataset': str(owner), 'counts': manifest['counts']}


def main(argv=None):
    parser = argparse.ArgumentParser(description='商品・写真・レシピの退避／空Adminへの復元（会計は対象外）')
    parser.add_argument('command', choices=('export', 'import', 'verify'))
    parser.add_argument('--folder', required=True)
    parser.add_argument('--dataset-id')
    parser.add_argument('--database-url-file')
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--confirm-dataset')
    args = parser.parse_args(argv)
    engine = None
    try:
        if args.command == 'verify':
            if args.apply:
                raise VaultError('verifyは書き込みません。')
            manifest, _, _ = load_bundle(args.folder)
            print(json.dumps({'verified': True, 'source_dataset': manifest['source_dataset'],
                              'counts': manifest['counts']}, ensure_ascii=False))
            return 0
        if not args.dataset_id or not args.database_url_file:
            raise VaultError('接続設定ファイルとDataset IDを明示してください。')
        path = Path(args.database_url_file)
        info = path.stat()
        if path.is_symlink() or not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_size > 4096:
            raise VaultError('接続設定は権限0600の通常ファイルにしてください。')
        url = make_url(_read_file(path.absolute().parent, path.name).decode('utf-8').strip())
        if url.get_backend_name() not in ('sqlite', 'postgresql'):
            raise VaultError('対応DBはSQLiteとPostgreSQLです。')
        engine = create_engine(url, echo=False)
        if args.command == 'export':
            if args.apply:
                raise VaultError('exportにapplyは指定できません。')
            result = export_bundle(engine, args.dataset_id, args.folder)
            result = {'exported': True, 'counts': result['counts']}
        else:
            result = import_bundle(engine, args.dataset_id, args.folder, apply=args.apply,
                                   confirm_dataset=args.confirm_dataset)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except VaultError as error:
        print(str(error), file=__import__('sys').stderr)
        return 2
    except Exception:
        # SQLAlchemy/driver exceptions can include connection URLs and parameters.
        print('処理を停止しました。接続・スキーマ・対象・ファイルを確認してください。秘密値は表示しません。',
              file=__import__('sys').stderr)
        return 2
    finally:
        if engine is not None:
            engine.dispose()


if __name__ == '__main__':
    raise SystemExit(main())

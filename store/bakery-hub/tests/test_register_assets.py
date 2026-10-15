"""レジ画像を元PNGと分離し、全18品のサイズと表示品質の基本条件を確認。"""

from pathlib import Path

from PIL import Image

from product_images import PRODUCT_IMAGE_OPTIONS


def test_all_standard_images_have_small_register_variants():
    root = Path(__file__).resolve().parent.parent / "static"
    originals = root / "product_images"
    variants = root / "register_images"
    expected = {f"{key}.webp" for key in PRODUCT_IMAGE_OPTIONS}
    assert len(expected) == 18
    assert {path.name for path in variants.glob("*.webp")} == expected
    assert sum(path.stat().st_size for path in variants.glob("*.webp")) < 2_000_000
    for key, option in PRODUCT_IMAGE_OPTIONS.items():
        original = originals / option["filename"]
        variant = variants / f"{key}.webp"
        assert original.is_file() and variant.stat().st_size < original.stat().st_size
        with Image.open(original) as source, Image.open(variant) as optimized:
            assert optimized.format == "WEBP"
            assert max(optimized.size) <= 320
            assert abs(source.width / source.height - optimized.width / optimized.height) < 0.02

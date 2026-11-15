import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from PIL import Image
from test_ui import window as window

from planbak.codec import BackupError, Cancelled
from planbak.image_cache import ImageCache


def cache_fixture(request, tmp_path, monkeypatch):
    import planbak.image_cache as module

    w = request.getfixturevalue("window")
    paths = [tmp_path / f"cache-{i}.png" for i in range(2)]
    for path in paths:
        Image.new("RGB", (30, 20), "red").save(path)
    w.session.import_paths(paths, "HomeDomain", "Documents")
    tokens = [w.session.at("HomeDomain", f"Documents/{p.name}").token for p in paths]
    cache = ImageCache(w.session)
    extracted, decoded = [], []
    original_extract = w.session.extract_file
    original_decode = module.decode_image

    def extract(token, *args, **kwargs):
        extracted.append(token)
        return original_extract(token, *args, **kwargs)

    def decode(path, cancel):
        decoded.append(path)
        return original_decode(path, cancel)

    monkeypatch.setattr(w.session, "extract_file", extract)
    monkeypatch.setattr(module, "decode_image", decode)
    return w, cache, paths, tokens, extracted, decoded


def test_memory_hit_disk_fallback_source_edit_and_force_reload(request, tmp_path, monkeypatch):
    import planbak.image_cache as module

    w, cache, paths, tokens, extracted, decoded = cache_fixture(request, tmp_path, monkeypatch)
    monkeypatch.setattr(module, "MEMORY_ENTRIES", 1)
    first = cache.load(tokens[0])
    again = cache.load(tokens[0])
    assert first.image.cacheKey() == again.image.cacheKey()
    assert len(extracted) == len(decoded) == 1
    cache.load(tokens[1])
    assert tokens[0] not in cache.decoded
    cache.load(tokens[0])
    assert extracted.count(tokens[0]) == 1 and len(decoded) == 3
    cache.load(tokens[0], force=True)
    assert extracted.count(tokens[0]) == 2 and len(decoded) == 4
    Image.new("RGB", (30, 20), "blue").save(paths[0])
    w.session.stage_edit(tokens[0], paths[0])
    updated = cache.load(tokens[0])
    assert updated.image.pixelColor(2, 2).blue() == 255
    assert extracted.count(tokens[0]) == 3 and len(decoded) == 5
    w.session.delete([tokens[0]])
    with pytest.raises(BackupError):
        cache.load(tokens[0])
    directory = cache.directory
    cache.clear()
    assert not directory.exists() and not cache.decoded and not cache.files


def test_lru_disk_eviction_and_cache_clear(request, tmp_path, monkeypatch):
    import planbak.image_cache as module

    _, cache, _, tokens, extracted, _ = cache_fixture(request, tmp_path, monkeypatch)
    monkeypatch.setattr(module, "MEMORY_ENTRIES", 0)
    monkeypatch.setattr(module, "DISK_ENTRIES", 1)
    cache.load(tokens[0])
    original = cache.files[tokens[0]].path
    cache.load(tokens[1])
    assert not original.exists() and tokens[0] not in cache.files
    cache.load(tokens[0])
    assert extracted.count(tokens[0]) == 2
    assert len(cache.files) == 1 and not cache.decoded
    cache.clear()


def test_concurrent_same_picture_extracts_and_decodes_once(request, tmp_path, monkeypatch):
    import planbak.image_cache as module

    _, cache, _, tokens, extracted, decoded = cache_fixture(request, tmp_path, monkeypatch)
    original = module.decode_image
    started, release = threading.Event(), threading.Event()

    def delayed(path, cancel):
        started.set()
        release.wait(2)
        return original(path, cancel)

    monkeypatch.setattr(module, "decode_image", delayed)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(cache.load, tokens[0])
        assert started.wait(1)
        second = pool.submit(cache.load, tokens[0])
        release.set()
        a, b = first.result(2), second.result(2)
    assert len(extracted) == len(decoded) == 1
    assert a.image.cacheKey() == b.image.cacheKey()
    assert not cache._loads
    cache.clear()


def test_wait_for_session_lock_is_cancellable(request, tmp_path, monkeypatch):
    w, cache, _, tokens, _, _ = cache_fixture(request, tmp_path, monkeypatch)
    cancel = threading.Event()
    with ThreadPoolExecutor(max_workers=1) as pool:
        with w.session.lock:
            future = pool.submit(cache.load, tokens[0], cancel.is_set)
            cancel.set()
            with pytest.raises(Cancelled):
                future.result(1)
    cache.clear()


def test_cancel_after_tiled_decode_still_accounts_and_evicts_files(request, tmp_path, monkeypatch):
    from PySide6.QtGui import QImage

    import planbak.image_cache as module
    from planbak.image_data import DecodedImage
    from planbak.large_image import LargeImage

    _, cache, _, tokens, _, _ = cache_fixture(request, tmp_path, monkeypatch)
    monkeypatch.setattr(module, "DISK_BUDGET", 150)
    cancelled = threading.Event()

    def decode(path, cancel):
        provider = LargeImage(path, 1, 1)
        provider.tiles.write_bytes(b"x" * 512)
        image = QImage(1, 1, QImage.Format.Format_RGBA8888)
        image.fill(0)
        cancelled.set()
        return DecodedImage(image, "HEIF", large=provider)

    monkeypatch.setattr(module, "decode_image", decode)
    with pytest.raises(Cancelled):
        cache.load(tokens[0], cancelled.is_set)
    assert not cache.files and not cache.decoded
    assert not list(cache.directory.iterdir())
    cache.clear()


def test_adopt_waits_for_same_token_decode_and_preserves_existing_cache(request, tmp_path, monkeypatch):
    import planbak.image_cache as module
    from planbak.backup import copy_file

    w, cache, paths, tokens, _, _ = cache_fixture(request, tmp_path, monkeypatch)
    token = tokens[0]
    _, revision = cache.source(token)
    prepared = tmp_path / 'prepared-image'
    copy_file(paths[0], prepared)
    started, release = threading.Event(), threading.Event()
    original = module.decode_image
    def slow(path, cancel):
        started.set()
        assert release.wait(3)
        return original(path, cancel)
    monkeypatch.setattr(module, 'decode_image', slow)
    with ThreadPoolExecutor(2) as pool:
        loading = pool.submit(cache.load, token)
        assert started.wait(3)
        cancelled = threading.Event()
        cancelled.set()
        adopting = pool.submit(cache.adopt, token, prepared, revision, cancelled.is_set)
        with pytest.raises(Cancelled):
            adopting.result(timeout=3)
        release.set()
        loading.result(timeout=3)
    existing = cache.files[token].path
    cache.adopt(token, prepared, revision)
    assert cache.files[token].path == existing and prepared.exists()
    assert len(list(cache.directory.iterdir())) == 1
    cache.clear()

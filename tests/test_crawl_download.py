from pipeline.crawl_download import download_images


class _FakeResponse:
    def __init__(self, content: bytes, status: int = 200):
        self.content = content
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise Exception(f"HTTP {self.status_code}")


def test_download_images_saves_with_indexed_names(tmp_path, monkeypatch):
    calls = []

    def fake_get(url, headers=None, timeout=None):
        calls.append(url)
        return _FakeResponse(b"fake-image-bytes")

    monkeypatch.setattr("pipeline.crawl_download.requests.get", fake_get)

    urls = [
        "https://postfiles.pstatic.net/a.jpg?type=w1600",
        "https://postfiles.pstatic.net/b.png?type=w1600",
    ]
    saved = download_images(urls, tmp_path / "850833", "850833")

    assert len(saved) == 2
    assert saved[0].name == "850833_0.jpg"
    assert saved[1].name == "850833_1.png"
    assert saved[0].read_bytes() == b"fake-image-bytes"
    assert len(calls) == 2


def test_download_images_skips_failed_downloads(tmp_path, monkeypatch):
    import requests

    def fake_get(url, headers=None, timeout=None):
        if "bad" in url:
            raise requests.RequestException("network error")
        return _FakeResponse(b"ok")

    monkeypatch.setattr("pipeline.crawl_download.requests.get", fake_get)

    urls = [
        "https://postfiles.pstatic.net/good.jpg",
        "https://postfiles.pstatic.net/bad.jpg",
    ]
    saved = download_images(urls, tmp_path / "art", "art")

    assert len(saved) == 1
    assert saved[0].name == "art_0.jpg"


def test_download_images_skips_known_boilerplate_hash(tmp_path, monkeypatch):
    import hashlib

    logo_bytes = b"this-is-a-known-logo-image"
    logo_hash = hashlib.sha256(logo_bytes).hexdigest()
    monkeypatch.setattr("pipeline.crawl_download.KNOWN_BOILERPLATE_HASHES", frozenset({logo_hash}))

    def fake_get(url, headers=None, timeout=None):
        if "logo" in url:
            return _FakeResponse(logo_bytes)
        return _FakeResponse(b"real-estimate-image")

    monkeypatch.setattr("pipeline.crawl_download.requests.get", fake_get)

    urls = [
        "https://postfiles.pstatic.net/logo.png",
        "https://postfiles.pstatic.net/estimate.jpg",
    ]
    saved = download_images(urls, tmp_path / "art", "art")

    assert len(saved) == 1
    assert saved[0].name == "art_0.jpg"
    assert saved[0].read_bytes() == b"real-estimate-image"


def test_download_images_creates_directory(tmp_path, monkeypatch):
    def fake_get(url, headers=None, timeout=None):
        return _FakeResponse(b"x")

    monkeypatch.setattr("pipeline.crawl_download.requests.get", fake_get)

    out_dir = tmp_path / "nested" / "art"
    assert not out_dir.exists()
    download_images(["https://x.com/a.jpg"], out_dir, "art")
    assert out_dir.exists()

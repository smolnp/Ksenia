"""Тесты Content-Type audit."""
from unittest.mock import patch, MagicMock

import pytest

from utils import URLUtils
from constants import StatusText


@pytest.mark.parametrize("ct,expected_error", [
    # Валидные для IPTV — без ошибки
    ("video/mp4", None),
    ("video/mp2t", None),
    ("audio/mpeg", None),
    ("application/vnd.apple.mpegurl", None),
    ("application/x-mpegurl", None),
    ("application/dash+xml", None),
    ("application/octet-stream", None),
    ("VIDEO/MP4; charset=utf-8", None),   # регистр + параметры
    # Явные ошибки
    ("text/html", StatusText.HTML_INSTEAD_OF_STREAM),
    ("text/html; charset=utf-8",
     StatusText.HTML_INSTEAD_OF_STREAM),
    ("application/xhtml+xml",
     StatusText.HTML_INSTEAD_OF_STREAM),
    ("application/json",
     StatusText.JSON_INSTEAD_OF_STREAM),
    ("application/json; charset=utf-8",
     StatusText.JSON_INSTEAD_OF_STREAM),
    # Прочий не-видео
    ("image/png", StatusText.NON_VIDEO_CONTENT),
    ("application/xml", StatusText.NON_VIDEO_CONTENT),
    # Пустой — без ошибки
    ("", None),
    (None, None),
])
def test_classify_content_type(ct, expected_error):
    if ct is None:
        ct = ""
    assert URLUtils._classify_content_type(ct) == expected_error


def _mock_response(code, ct, body=b"data"):
    r = MagicMock()
    r.status_code = code
    r.headers = {"Content-Type": ct} if ct else {}
    r.iter_content.return_value = iter([body])
    return r


@patch("requests.Session.get")
def test_check_url_html_returns_false(mock_get):
    """Сервер вернул 200 + text/html — URL помечается как
    неработающий, но с отдельным статусом."""
    mock_get.return_value.__enter__.return_value = \
        _mock_response(200, "text/html; charset=utf-8",
                        b"<html>...</html>")
    ok, rt, msg, code, ttfb, _ = URLUtils.check_url(
        "http://example.com/broken.m3u8", try_repair=False)
    assert ok is False
    assert code == 200
    assert "text/html" in msg
    assert ttfb is not None


@patch("requests.Session.get")
def test_check_url_json_returns_false(mock_get):
    """Сервер вернул 200 + application/json."""
    mock_get.return_value.__enter__.return_value = \
        _mock_response(200, "application/json",
                        b'{"error": "not found"}')
    ok, _, msg, _, _, _ = URLUtils.check_url(
        "http://example.com/broken.m3u8", try_repair=False)
    assert ok is False
    assert "application/json" in msg


@patch("requests.Session.get")
def test_check_url_hls_ok(mock_get):
    """HLS — application/vnd.apple.mpegurl — валидный тип."""
    mock_get.return_value.__enter__.return_value = \
        _mock_response(200, "application/vnd.apple.mpegurl",
                        b"#EXTM3U\n#EXT-X-VERSION:3\n")
    ok, _, _, _, _, _ = URLUtils.check_url(
        "http://example.com/stream.m3u8", try_repair=False)
    assert ok is True


@patch("requests.Session.get")
def test_check_url_measures_ttfb(mock_get):
    """TTFB должен быть измерен и возвращён."""
    import time

    def delayed_iter(*args, **kwargs):
        time.sleep(0.05)
        yield b"fake_video"

    r = MagicMock()
    r.status_code = 200
    r.headers = {"Content-Type": "video/mp4"}
    r.iter_content.side_effect = delayed_iter
    mock_get.return_value.__enter__.return_value = r

    ok, _, _, _, ttfb, _ = URLUtils.check_url(
        "http://example.com/x.mp4", try_repair=False)
    assert ok is True
    assert ttfb is not None
    assert ttfb >= 40.0   # с запасом на сон 50мс
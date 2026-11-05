"""Тесты Auto-Repair в URLUtils."""
from unittest.mock import patch

from utils import URLUtils


@patch("utils.URLUtils._try_scheme_swap")
@patch("utils.URLUtils._try_follow_redirects")
@patch("utils.URLUtils._check_url_once")
def test_try_repair_scheme_swap_success(
        mock_once, mock_redirects, mock_swap):
    """http -> https через swap."""
    mock_swap.return_value = "https://example.com/s.m3u8"
    mock_once.return_value = (
        True, 0.1, "HTTP 200", 200, 100.0, None)
    repaired = URLUtils.try_repair(
        "http://example.com/s.m3u8")
    assert repaired == "https://example.com/s.m3u8"
    mock_redirects.assert_not_called()


@patch("utils.URLUtils._try_scheme_swap")
@patch("utils.URLUtils._try_follow_redirects")
@patch("utils.URLUtils._check_url_once")
def test_try_repair_redirect_success(
        mock_once, mock_redirects, mock_swap):
    """Swap не помог, но редирект дал новый URL."""
    mock_swap.return_value = None
    mock_redirects.return_value = "https://cdn.example.com/new.m3u8"
    mock_once.return_value = (
        True, 0.1, "HTTP 200", 200, 80.0, None)
    repaired = URLUtils.try_repair(
        "http://example.com/old.m3u8")
    assert repaired == "https://cdn.example.com/new.m3u8"


@patch("utils.URLUtils._try_scheme_swap")
@patch("utils.URLUtils._try_follow_redirects")
@patch("utils.URLUtils._check_url_once")
def test_try_repair_nothing_works(
        mock_once, mock_redirects, mock_swap):
    """Ни swap, ни редирект не помогают."""
    mock_swap.return_value = None
    mock_redirects.return_value = None
    repaired = URLUtils.try_repair("http://example.com/broken")
    assert repaired is None


@patch("utils.URLUtils._try_scheme_swap")
@patch("utils.URLUtils._try_follow_redirects")
@patch("utils.URLUtils._check_url_once")
def test_try_repair_repaired_url_dead(
        mock_once, mock_redirects, mock_swap):
    """Swap вернул URL, но он тоже мёртв — пробуем ещё раз,
    потом сдаёмся."""
    mock_swap.return_value = "https://example.com/x"
    mock_redirects.return_value = None
    mock_once.return_value = (
        False, 0.1, "HTTP 404", 404, 50.0, None)
    repaired = URLUtils.try_repair(
        "http://example.com/x", max_attempts=2)
    assert repaired is None


@patch("utils.URLUtils._try_scheme_swap")
def test_try_repair_empty_url(mock_swap):
    assert URLUtils.try_repair("") is None
    assert URLUtils.try_repair("   ") is None
    mock_swap.assert_not_called()
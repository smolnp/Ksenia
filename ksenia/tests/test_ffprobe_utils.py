"""Тесты ffprobe_utils."""
import json
import subprocess
from unittest.mock import patch, MagicMock

import pytest

from ffprobe_utils import (
    normalize_resolution, resolution_rank,
    get_stream_resolution,
)


@pytest.mark.parametrize("width,height,expected", [
    (1920, 1080, "1080p"),
    (1280, 720, "720p"),
    (3840, 2160, "4K"),
    (720, 576, "576p"),
    (640, 480, "480p"),
    (100, 100, "100p"),
    (0, 0, ""),
    (1920, 0, ""),
])
def test_normalize_resolution(width, height, expected):
    assert normalize_resolution(width, height) == expected


@pytest.mark.parametrize("res,rank", [
    ("4K", 4), ("2160p", 4),
    ("1080p", 3), ("720p", 2),
    ("576p", 1), ("480p", 1),
    ("", 0), (None, 0),
])
def test_resolution_rank(res, rank):
    assert resolution_rank(res) == rank


@patch("ffprobe_utils.find_ffprobe", return_value=None)
def test_get_stream_resolution_no_ffprobe(mock_find):
    res, err = get_stream_resolution(
        "http://example.com/stream.m3u8")
    assert res is None
    assert "not found" in err


@patch("ffprobe_utils.find_ffprobe",
       return_value="/usr/bin/ffprobe")
@patch("subprocess.run")
def test_get_stream_resolution_success(mock_run, mock_find):
    mock_run.return_value = MagicMock(
        returncode=0, stderr="",
        stdout=json.dumps({
            "streams": [{"width": 1920, "height": 1080}]}))
    res, err = get_stream_resolution(
        "http://example.com/stream.m3u8")
    assert res == "1080p"
    assert err == ""
    cmd = mock_run.call_args[0][0]
    assert cmd[0] == "/usr/bin/ffprobe"
    assert "stream=width,height" in cmd


@patch("ffprobe_utils.find_ffprobe",
       return_value="/usr/bin/ffprobe")
@patch("subprocess.run")
def test_get_stream_resolution_timeout(mock_run, mock_find):
    mock_run.side_effect = subprocess.TimeoutExpired(
        cmd="ffprobe", timeout=5)
    res, err = get_stream_resolution(
        "http://example.com/stream.m3u8")
    assert res is None
    assert "timeout" in err


@patch("ffprobe_utils.find_ffprobe",
       return_value="/usr/bin/ffprobe")
@patch("subprocess.run")
def test_get_stream_resolution_bad_returncode(mock_run, mock_find):
    mock_run.return_value = MagicMock(
        returncode=1, stderr="Connection refused", stdout="")
    res, err = get_stream_resolution(
        "http://example.com/stream.m3u8")
    assert res is None
    assert "Connection refused" in err


@patch("ffprobe_utils.find_ffprobe",
       return_value="/usr/bin/ffprobe")
@patch("subprocess.run")
def test_get_stream_resolution_invalid_json(mock_run, mock_find):
    mock_run.return_value = MagicMock(
        returncode=0, stderr="", stdout="not a json")
    res, err = get_stream_resolution(
        "http://example.com/stream.m3u8")
    assert res is None
    assert err == "invalid json"


@patch("ffprobe_utils.find_ffprobe",
       return_value="/usr/bin/ffprobe")
@patch("subprocess.run")
def test_get_stream_resolution_empty_url(mock_run, mock_find):
    res, err = get_stream_resolution("")
    assert res is None
    assert err == "empty url"
    mock_run.assert_not_called()


@patch("ffprobe_utils.find_ffprobe",
       return_value="/usr/bin/ffprobe")
@patch("subprocess.run")
def test_get_stream_resolution_with_user_agent(
        mock_run, mock_find):
    mock_run.return_value = MagicMock(
        returncode=0, stderr="",
        stdout=json.dumps({
            "streams": [{"width": 1280, "height": 720}]}))
    res, _ = get_stream_resolution(
        "http://example.com/x", user_agent="TestUA/1.0")
    cmd = mock_run.call_args[0][0]
    assert "-user_agent" in cmd
    assert "TestUA/1.0" in cmd
    assert res == "720p"
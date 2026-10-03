# -*- coding: utf-8 -*-
"""Построчный diff для плейлистов (аналог git diff).

Модуль не зависит от Qt и может использоваться в CLI/тестах.
"""

from __future__ import annotations
import difflib
from enum import Enum
from typing import Dict, List, Sequence


class DiffOp(Enum):
    EQUAL = "equal"
    INSERT = "insert"
    DELETE = "delete"
    REPLACE = "replace"


class DiffLine:
    __slots__ = ('op', 'left_no', 'right_no', 'left', 'right')

    def __init__(self, op: DiffOp, left_no: int, right_no: int,
                 left: str = "", right: str = ""):
        self.op = op
        self.left_no = left_no
        self.right_no = right_no
        self.left = left
        self.right = right


def diff_lines(left: Sequence[str], right: Sequence[str]) -> List[DiffLine]:
    """Построчный diff через difflib.SequenceMatcher.

    Возвращает плоский список DiffLine, пригодный для отображения
    в двухколоночном виде.
    """
    sm = difflib.SequenceMatcher(a=left, b=right, autojunk=False)
    result: List[DiffLine] = []

    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == 'equal':
            for k in range(i2 - i1):
                result.append(DiffLine(
                    DiffOp.EQUAL, i1 + k + 1, j1 + k + 1,
                    left[i1 + k], right[j1 + k]))
        elif tag == 'delete':
            for k in range(i1, i2):
                result.append(DiffLine(
                    DiffOp.DELETE, k + 1, 0, left[k], ""))
        elif tag == 'insert':
            for k in range(j1, j2):
                result.append(DiffLine(
                    DiffOp.INSERT, 0, k + 1, "", right[k]))
        elif tag == 'replace':
            n_left = i2 - i1
            n_right = j2 - j1
            n = max(n_left, n_right)
            for k in range(n):
                l_no = i1 + k + 1 if k < n_left else 0
                r_no = j1 + k + 1 if k < n_right else 0
                l_txt = left[i1 + k] if k < n_left else ""
                r_txt = right[j1 + k] if k < n_right else ""
                result.append(DiffLine(
                    DiffOp.REPLACE, l_no, r_no, l_txt, r_txt))

    return result


def diff_stats(lines: List[DiffLine]) -> Dict[str, int]:
    added = sum(1 for d in lines if d.op == DiffOp.INSERT)
    removed = sum(1 for d in lines if d.op == DiffOp.DELETE)
    changed = sum(1 for d in lines if d.op == DiffOp.REPLACE)
    equal = sum(1 for d in lines if d.op == DiffOp.EQUAL)
    return {
        'added': added,
        'removed': removed,
        'changed': changed,
        'equal': equal,
        'total': len(lines),
    }


def to_unified_diff(left_lines: Sequence[str], right_lines: Sequence[str],
                    left_label: str = "current",
                    right_label: str = "other",
                    context: int = 3) -> str:
    """Классический unified diff (как `diff -u` / `git diff`)."""
    return "".join(difflib.unified_diff(
        left_lines, right_lines,
        fromfile=left_label, tofile=right_label,
        lineterm="\n", n=context))


def normalize_lines(text: str, ignore_whitespace: bool = False) -> List[str]:
    """Разбить текст на строки, опционально обрезав хвостовые пробелы."""
    lines = text.splitlines()
    if ignore_whitespace:
        lines = [ln.rstrip() for ln in lines]
    return lines


def channels_only(lines: Sequence[str]) -> List[str]:
    """Оставить только значимые строки M3U: #EXTINF и URL."""
    result: List[str] = []
    for ln in lines:
        s = ln.strip()
        if not s:
            continue
        if s.startswith('#EXTINF:') or not s.startswith('#'):
            result.append(s)
    return result
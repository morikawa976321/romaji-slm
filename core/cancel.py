"""変換の打ち切り。常駐入力の先読みは、後から新しい要求が来たらやめる（docs/SPEEDUP.md §0 の C、docs/decisions.md D47）。

呼び出し側（ime/server.py）が scope で「もう要らないか」を渡す。生成と採点は forward の前に check を呼ぶ。
ONNX Runtime 版は走っている forward の途中でも止められる（running に止め方を渡し、新しい要求が来たら interrupt が呼ぶ）。
scope の外では何もしない。
"""

import threading
from contextlib import contextmanager

_local = threading.local()
_running: set = set()   # 走っている forward の (stale, 止め方)
_running_lock = threading.Lock()


class Cancelled(BaseException):
    """もう要らない変換をやめた。BaseException にしてあるのは、途中の `except Exception`（候補ウィンドウの別の書き方の失敗を
    握りつぶす所など）に拾われて、やめたはずの変換が欠けた結果として返らないようにするため。"""


@contextmanager
def scope(stale):
    """この中の変換は、stale() が真になったら次の forward の前に Cancelled を投げる（スレッドごと）。stale が None なら何もしない。"""
    prev = getattr(_local, "stale", None)
    _local.stale = stale
    try:
        yield
    finally:
        _local.stale = prev


def check() -> None:
    stale = getattr(_local, "stale", None)
    if stale is not None and stale():
        raise Cancelled


def stopper():
    """今の scope の stale（無ければ None）。running に渡す。"""
    return getattr(_local, "stale", None)


@contextmanager
def running(stale, stop):
    """forward を走らせている間、interrupt が stale() を見て真なら stop() を呼べるようにする。"""
    if stale is None:
        yield
        return
    item = (stale, stop)
    with _running_lock:
        _running.add(item)
    try:
        yield
    finally:
        with _running_lock:
            _running.discard(item)


def interrupt() -> None:
    """新しい要求が来た: もう要らなくなった変換の、走っている forward を止める。"""
    with _running_lock:
        items = list(_running)
    for stale, stop in items:
        if stale():
            stop()

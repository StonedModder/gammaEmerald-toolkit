"""Container reads have to survive being called from many threads at once.

The daemon answers every request on its own thread, and scrolling the sprite
grid fires a request per visible tile. Both readers keep ONE file handle and
did `seek()` then `read()` as two calls, so a second thread could seek between
them -- the first thread then read from the wrong offset and the error surfaced
far away as "Oodle decompress failed: 0".
"""
import threading

import pytest

from gamma.iostore import CasExtractor
from gamma.pak import PakReader


class FakeFile:
    """A handle that notices interleaving instead of quietly returning junk."""

    def __init__(self, data):
        self.data = data
        self.pos = 0
        self.owner = None          # thread that last seeked

    def seek(self, off, whence=0):
        self.pos = off if whence == 0 else len(self.data) + off
        self.owner = threading.current_thread().ident

    def read(self, size=-1):
        if self.owner is not None and self.owner != threading.current_thread().ident:
            raise AssertionError("another thread seeked between seek and read")
        end = len(self.data) if size is None or size < 0 else self.pos + size
        out = self.data[self.pos:end]
        self.pos = end
        return out

    def tell(self):
        return self.pos


def _hammer(read_one, n=16, rounds=40):
    """Run read_one(i) from n threads; re-raise whatever any of them hit."""
    errors = []
    barrier = threading.Barrier(n)

    def work(i):
        barrier.wait()
        try:
            for _ in range(rounds):
                read_one(i)
        except BaseException as e:      # noqa: BLE001 - reported below
            errors.append(e)

    threads = [threading.Thread(target=work, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return errors


def test_pak_reads_are_atomic():
    reader = PakReader.__new__(PakReader)          # no real pak needed
    reader._lock = threading.Lock()
    reader.f = FakeFile(bytes(range(256)) * 64)

    def read_one(i):
        off = (i * 97) % 4096
        assert len(reader._read(off, 128)) == 128

    assert _hammer(read_one) == []


def test_iostore_reads_are_atomic():
    """CasExtractor.read_range walks blocks with the same seek/read pair."""
    class Toc:
        pass

    ext = CasExtractor.__new__(CasExtractor)
    ext._lock = threading.Lock()
    ext.f = FakeFile(bytes(range(256)) * 256)
    ext.block_size = 1024
    ext.oodle = None
    ext.toc = Toc()
    # (offset, csize, usize, method=0 -> stored, so no Oodle call)
    ext.toc.blocks = [(i * 1024, 1024, 1024, 0) for i in range(64)]

    def read_one(i):
        off = (i * 1024) % 16384
        assert len(ext.read_range(off, 1024)) == 1024

    assert _hammer(ext and read_one) == []


def test_the_fake_file_would_catch_an_unlocked_reader():
    """Guard the guard: without a lock the harness must actually fail."""
    class Unlocked:
        def __init__(self):
            self.f = FakeFile(bytes(range(256)) * 64)

        def read(self, off, size):
            self.f.seek(off)
            import time
            time.sleep(0.0005)          # widen the window the lock closes
            return self.f.read(size)

    u = Unlocked()
    errors = _hammer(lambda i: u.read((i * 97) % 4096, 128), n=8, rounds=20)
    assert errors, "the interleaving detector never fired -- test is toothless"
    assert isinstance(errors[0], AssertionError)


@pytest.mark.parametrize("cls", [PakReader, CasExtractor])
def test_readers_take_a_lock_at_construction(cls):
    """A new reader must own a lock, or the atomic reads above are a lie."""
    import inspect
    src = inspect.getsource(cls.__init__)
    assert "_lock" in src, "%s.__init__ never creates its lock" % cls.__name__

from concurrent.futures import ThreadPoolExecutor
import os
import threading

import pytest
from sourceloom.store import Store,Conflict,digest


def test_simultaneous_blob_publish_never_exposes_partial_bytes(tmp_path,monkeypatch):
    store=Store(tmp_path);raw=b'Complete immutable original\n'*10000
    barrier=threading.Barrier(6);link=os.link
    def together(source,target):
        barrier.wait(timeout=5)
        # At publication time every temporary file is already complete.
        assert open(source,'rb').read()==raw
        return link(source,target)
    monkeypatch.setattr(os,'link',together)
    with ThreadPoolExecutor(max_workers=6) as pool:
        keys=list(pool.map(lambda _:store.blob(raw),range(6)))
    assert keys==[digest(raw)]*6 and store.read_blob(keys[0])==raw
    assert list((tmp_path/'blobs').iterdir())==[tmp_path/'blobs'/keys[0]]


def test_failed_publish_removes_only_its_temporary_file(tmp_path,monkeypatch):
    store=Store(tmp_path);original=store.blob(b'Preserve this original')
    def fail(*args):raise OSError('Synthetic publication failure')
    monkeypatch.setattr(os,'link',fail)
    with pytest.raises(OSError):store.blob(b'A different original')
    assert store.read_blob(original)==b'Preserve this original'
    assert list((tmp_path/'blobs').iterdir())==[tmp_path/'blobs'/original]


def test_existing_corrupt_blob_is_never_overwritten(tmp_path):
    store=Store(tmp_path);key=store.blob(b'Expected bytes')
    (tmp_path/'blobs'/key).write_bytes(b'Corrupt fixture')
    with pytest.raises(Conflict):store.blob(b'Expected bytes')
    assert (tmp_path/'blobs'/key).read_bytes()==b'Corrupt fixture'

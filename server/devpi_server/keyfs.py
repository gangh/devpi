"""
filesystem key/value storage with support for storing and retrieving
basic python types based on parametrizable keys.  Multiple
read Transactions can execute concurrently while at most one
write Transaction is ongoing.  Each Transaction will see a consistent
view of key/values refering to the point in time it was started,
independent from any future changes.
"""
from __future__ import unicode_literals
import re
import contextlib
import py
from . import mythread
from .log import threadlog, thread_push_log, thread_pop_log
from .readonly import get_mutable_deepcopy, ensure_deeply_readonly, \
                      ReadonlyView, is_deeply_readonly
import os
import sys
import time

from execnet.gateway_base import Unserializer, _Serializer
from devpi_common.types import cached_property


_nodefault = object()

def load(io):
    return Unserializer(io, strconfig=(False, False)).load(versioned=False)

def dump(obj, io):
    return _Serializer(io.write).save(obj)

def loads(data):
    return load(py.io.BytesIO(data))

def dumps(obj):
    io = py.io.BytesIO()
    dump(obj, io)
    return io.getvalue()

def read_int_from_file(path, default=0):
    try:
        with open(path, "rb") as f:
            return int(f.read())
    except IOError:
        return default

def write_int_to_file(val, path):
    tmp_path = path + "-tmp"
    with get_write_file_ensure_dir(tmp_path) as f:
        f.write(str(val).encode("utf-8"))
    rename(tmp_path, path)

def load_from_file(path, default=_nodefault):
    try:
        with open(path, "rb") as f:
            return load(f)
    except IOError:
        if default is _nodefault:
            raise
        return default

def dump_to_file(value, path):
    tmp_path = path + "-tmp"
    with get_write_file_ensure_dir(tmp_path) as f:
        dump(value, f)
    rename(tmp_path, path)

def get_write_file_ensure_dir(path):
    try:
        return open(path, "wb")
    except IOError:
        dirname = os.path.dirname(path)
        if os.path.exists(dirname):
            raise
        os.makedirs(dirname)
        return open(path, "wb")


class FSWriter:
    def __init__(self, storage, conn):
        self.conn = conn
        self.storage = storage
        self.pending_renames = []
        self.changes = {}

    def db_get_typedkey_value(self, typedkey):
        try:
            return self.conn.db_read_typedkey(typedkey.relpath)
        except KeyError:
            return (typedkey.name, -1)

    def db_set_typedkey_value(self, typedkey, next_serial):
        self.conn.db_write_typedkey(typedkey.relpath, typedkey.name, next_serial)

    def record_set(self, typedkey, value=None):
        """ record setting typedkey to value (None means it's deleted) """
        assert not isinstance(value, ReadonlyView), value
        name, back_serial = self.db_get_typedkey_value(typedkey)
        self.db_set_typedkey_value(typedkey, self.storage.next_serial)
        # at __exit__ time we write out changes to the _changelog_cache
        # so we protect here against the caller modifying the value later
        value = get_mutable_deepcopy(value)
        self.changes[typedkey.relpath] = (typedkey.name, back_serial, value)

    def record_rename_file(self, source, dest):
        assert dest
        self.pending_renames.append((source, dest))

    def __enter__(self):
        self.log = thread_push_log("fswriter%s:" % self.storage.next_serial)
        return self

    def __exit__(self, cls, val, tb):
        thread_pop_log("fswriter%s:" % self.storage.next_serial)
        for path, content in self.conn.dirty_files.items():
            if content is None:
                self.record_rename_file(None, path)
            else:
                tmppath = path + "-tmp"
                with get_write_file_ensure_dir(tmppath) as f:
                    f.write(content)
                self.record_rename_file(tmppath, path)
        if cls is None:
            changed_keys, files_commit, files_del = self.commit_to_filesystem()
            commit_serial = self.storage.next_serial - 1

            # write out a nice commit entry to logging
            message = "committed: keys: %s"
            args = [",".join(map(repr, changed_keys))]
            if files_commit:
                message += ", files_commit: %s"
                args.append(",".join(files_commit))
            if files_del:
                message += ", files_del: %s"
                args.append(",".join(files_del))
            self.log.info(message, *args)

            self.storage.cache_commit_changes(commit_serial,
                                         ensure_deeply_readonly(self.changes))
            self.storage._notify_on_commit(commit_serial)
        else:
            while self.pending_renames:
                source, dest = self.pending_renames.pop()
                if source is not None:
                    os.remove(source)
            self.log.info("roll back at %s" %(self.storage.next_serial))

    def commit_to_filesystem(self):
        basedir = str(self.storage.basedir)
        rel_renames = list(
            make_rel_renames(basedir, self.pending_renames)
        )
        entry = self.changes, rel_renames
        self.conn.write_changelog_entry(self.storage.next_serial, entry)

        # If we crash in the remainder, the next restart will
        # - call check_pending_renames which will replay any remaining
        #   renames from the changelog entry, and
        # - initialize next_serial from the max committed serial + 1
        files_commit, files_del = commit_renames(basedir, rel_renames)
        self.storage.next_serial += 1
        self.storage.last_commit_timestamp = time.time()
        return list(self.changes), files_commit, files_del


def check_pending_renames(basedir, pending_relnames):
    for relpath in pending_relnames:
        path = os.path.join(basedir, relpath)
        if relpath.endswith("-tmp"):
            if os.path.exists(path):
                rename(path, path[:-4])
                threadlog.warn("completed file-commit from crashed tx: %s",
                               path[:-4])
            else:
                assert os.path.exists(path[:-4])
        else:
            try:
                os.remove(path)  # was already removed
                threadlog.warn("completed file-del from crashed tx: %s", path)
            except OSError:
                pass

def commit_renames(basedir, pending_renames):
    files_del = []
    files_commit = []
    for relpath in pending_renames:
        path = os.path.join(basedir, relpath)
        if relpath.endswith("-tmp"):
            rename(path, path[:-4])
            files_commit.append(relpath[:-4])
        else:
            try:
                os.remove(path)
            except OSError:
                pass
            files_del.append(relpath)
    return files_commit, files_del

def make_rel_renames(basedir, pending_renames):
    # produce a list of strings which are
    # - paths relative to basedir
    # - if they have "-tmp" at the end it means they should be renamed
    #   to the path without the "-tmp" suffix
    # - if they don't have "-tmp" they should be removed
    for source, dest in pending_renames:
        if source is not None:
            assert source == dest + "-tmp"
            yield source[len(basedir)+1:]
        else:
            assert dest.startswith(basedir)
            yield dest[len(basedir)+1:]

def rename(source, dest):
    try:
        os.rename(source, dest)
    except OSError:
        destdir = os.path.dirname(dest)
        if not os.path.exists(destdir):
            os.makedirs(destdir)
        if sys.platform == "win32" and os.path.exists(dest):
            os.remove(dest)
        os.rename(source, dest)


class TxNotificationThread:
    def __init__(self, keyfs):
        self.keyfs = keyfs
        self.cv_new_transaction = mythread.threading.Condition()
        self.cv_new_event_serial = mythread.threading.Condition()
        self.event_serial_path = str(self.keyfs.basedir.join(".event_serial"))
        self.event_serial_in_sync_at = None
        self._on_key_change = {}

    def on_key_change(self, key, subscriber):
        assert not mythread.has_active_thread(self), (
               "cannot register handlers after thread has started")
        keyname = getattr(key, "name", key)
        assert py.builtin._istext(keyname) or py.builtin._isbytes(keyname)
        self._on_key_change.setdefault(keyname, []).append(subscriber)

    def wait_event_serial(self, serial):
        with threadlog.around("info", "waiting for event-serial %s", serial):
            with self.cv_new_event_serial:
                while serial > self.read_event_serial():
                    self.cv_new_event_serial.wait()

    def wait_tx_serial(self, serial):
        with threadlog.around("info", "waiting for tx-serial %s", serial):
            with self.cv_new_transaction:
                while serial > self.keyfs.get_current_serial():
                    self.cv_new_transaction.wait()

    def read_event_serial(self):
        # the disk serial is kept one higher because pre-2.1.2
        # "event_serial" pointed to the "next event serial to be
        # processed" instead of the now "last processed event serial"
        return read_int_from_file(self.event_serial_path, 0) - 1

    def get_event_serial_timestamp(self):
        f = py.path.local(self.event_serial_path)
        if not f.exists():
            return
        return f.stat().mtime

    def write_event_serial(self, event_serial):
        write_int_to_file(event_serial + 1, self.event_serial_path)

    def notify_on_commit(self, serial):
        with self.cv_new_transaction:
            self.cv_new_transaction.notify_all()

    def thread_shutdown(self):
        with self.cv_new_transaction:
            self.cv_new_transaction.notify_all()

    def thread_run(self):
        event_serial = self.read_event_serial()
        log = thread_push_log("[NOTI]")
        while 1:
            while event_serial < self.keyfs.get_current_serial():
                self.thread.exit_if_shutdown()
                event_serial += 1
                self._execute_hooks(event_serial, log)
                with self.cv_new_event_serial:
                    self.write_event_serial(event_serial)
                    self.cv_new_event_serial.notify_all()
            serial = self.keyfs.get_current_serial()
            if event_serial >= serial:
                if event_serial == serial:
                    self.event_serial_in_sync_at = time.time()
                with self.cv_new_transaction:
                    self.cv_new_transaction.wait()
                    self.thread.exit_if_shutdown()

    def _execute_hooks(self, event_serial, log, raising=False):
        log.debug("calling hooks for tx%s", event_serial)
        changes = self.keyfs._storage.get_changes(event_serial)
        for relpath, (keyname, back_serial, val) in changes.items():
            key = self.keyfs.derive_key(relpath, keyname)
            ev = KeyChangeEvent(key, val, event_serial, back_serial)
            subscribers = self._on_key_change.get(keyname, [])
            for sub in subscribers:
                subname = getattr(sub, "__name__", sub)
                log.debug("%s(key=%r, at_serial=%r, back_serial=%r",
                          subname, key, event_serial, back_serial)
                try:
                    sub(ev)
                except Exception:
                    if raising:
                        raise
                    log.exception("calling %s failed, serial=%s", sub, event_serial)

        log.debug("finished calling all hooks for tx%s", event_serial)


class KeyFS(object):
    """ singleton storage object. """
    class ReadOnly(Exception):
        """ attempt to open write transaction while in readonly mode. """

    def __init__(self, basedir, storage, readonly=False, cache_size=10000):
        self.basedir = py.path.local(basedir).ensure(dir=1)
        self._keys = {}
        self._mode = None
        # a non-recursive lock because we don't support nested transactions
        self._write_lock = mythread.threading.Lock()
        self._threadlocal = mythread.threading.local()
        self._import_subscriber = {}
        self.notifier = t = TxNotificationThread(self)
        self._storage = storage(
            self.basedir,
            notify_on_commit=t.notify_on_commit,
            cache_size=cache_size)
        if self._storage.next_serial > 0:
            # perform some crash recovery
            data = self._storage.get_raw_changelog_entry(self.get_current_serial())
            changes, rel_renames = loads(data)
            check_pending_renames(str(self.basedir), rel_renames)
        self._readonly = readonly

    def derive_key(self, relpath, keyname=None, conn=None):
        """ return direct key for a given path and keyname.
        If keyname is not specified, the relpath key must exist
        to extract its name. """
        if keyname is None:
            try:
                return self.tx.get_key_in_transaction(relpath)
            except (AttributeError, KeyError):
                if conn is None:
                    conn = self._storage.get_connection()
                with conn as c:
                    keyname, serial = c.db_read_typedkey(relpath)
        key = self.get_key(keyname)
        if isinstance(key, PTypedKey):
            key = key(**key.extract_params(relpath))
        return key

    def import_changes(self, serial, changes):
        with self._write_lock:
            with self._storage.get_connection() as conn:
                with self.write_transaction(conn) as fswriter:
                    next_serial = self.get_next_serial()
                    assert next_serial == serial, (next_serial, serial)
                    for relpath, tup in changes.items():
                        name, back_serial, val = tup
                        typedkey = self.derive_key(relpath, name, conn=conn)
                        fswriter.record_set(typedkey, get_mutable_deepcopy(val))
                        meth = self._import_subscriber.get(typedkey.name)
                        if meth is not None:
                            threadlog.debug("calling import subscriber %r", meth)
                            meth(fswriter, typedkey, val, back_serial)

    def subscribe_on_import(self, key, subscriber):
        assert key.name not in self._import_subscriber
        self._import_subscriber[key.name] = subscriber

    def get_next_serial(self):
        return self._storage.next_serial

    def get_current_serial(self):
        return self._storage.next_serial - 1

    def get_last_commit_timestamp(self):
        return self._storage.last_commit_timestamp

    @property
    def tx(self):
        return getattr(self._threadlocal, "tx")

    def get_value_at(self, typedkey, at_serial, conn=None):
        relpath = typedkey.relpath
        if conn is None:
            conn = self._storage.get_connection()
        with conn as c:
            keyname, last_serial = c.db_read_typedkey(relpath)
        while last_serial >= 0:
            tup = self._storage.get_changes(last_serial).get(relpath)
            assert tup, "no transaction entry at %s" %(last_serial)
            keyname, back_serial, val = tup
            if last_serial > at_serial:
                last_serial = back_serial
                continue
            if val is not None:
                return val
            raise KeyError(relpath)  # was deleted

        # we could not find any change below at_serial which means
        # the key didn't exist at that point in time
        raise KeyError(relpath)

    def add_key(self, name, path, type):
        assert isinstance(path, py.builtin._basestring)
        if "{" in path:
            key = PTypedKey(self, path, type, name)
        else:
            key = TypedKey(self, path, type, name)
        self._keys[name] = key
        setattr(self, name, key)
        return key

    def get_key(self, name):
        return self._keys.get(name)

    def begin_transaction_in_thread(self, write=False, at_serial=None):
        if write and self._readonly:
            raise self.ReadOnly()
        assert not hasattr(self._threadlocal, "tx")
        tx = Transaction(self, write=write, at_serial=at_serial)
        self._threadlocal.tx = tx
        thread_push_log("[%stx%s]" %("W" if write else "R", tx.at_serial))
        return tx

    def clear_transaction(self):
        thread_pop_log()
        del self._threadlocal.tx

    def restart_as_write_transaction(self):
        if self._readonly:
            raise self.ReadOnly()
        tx = self.tx
        thread_pop_log()
        tx.restart(write=True)
        thread_push_log("[Wtx%s]" %(tx.at_serial))

    def restart_read_transaction(self):
        tx = self.tx
        assert not tx.write, "can only restart from read transaction"
        thread_pop_log()
        tx.restart(write=False)
        thread_push_log("[Rtx%s]" %(tx.at_serial))

    def rollback_transaction_in_thread(self):
        self._threadlocal.tx.rollback()
        self.clear_transaction()

    def commit_transaction_in_thread(self):
        self._threadlocal.tx.commit()
        self.clear_transaction()

    @contextlib.contextmanager
    def transaction(self, write=False, at_serial=None):
        tx = self.begin_transaction_in_thread(write=write, at_serial=at_serial)
        try:
            yield tx
        except:
            self.rollback_transaction_in_thread()
            raise
        self.commit_transaction_in_thread()

    def write_transaction(self, conn):
        return FSWriter(self._storage, conn)


class PTypedKey:
    rex_braces = re.compile(r'\{(.+?)\}')
    def __init__(self, keyfs, key, type, name):
        self.keyfs = keyfs
        self.pattern = py.builtin._totext(key)
        self.type = type
        self.name = name
        def repl(match):
            name = match.group(1)
            return r'(?P<%s>[^\/]+)' % name
        rex_pattern = self.pattern.replace("+", r"\+")
        rex_pattern = self.rex_braces.sub(repl, rex_pattern)
        self.rex_reverse = re.compile("^" + rex_pattern + "$")

    def __call__(self, **kw):
        for val in kw.values():
            if "/" in val:
                raise ValueError(val)
        relpath = self.pattern.format(**kw)
        return TypedKey(self.keyfs, relpath, self.type, self.name,
                        params=kw)

    def extract_params(self, relpath):
        m = self.rex_reverse.match(relpath)
        return m.groupdict() if m is not None else {}

    def on_key_change(self, callback):
        self.keyfs.notifier.on_key_change(self.name, callback)

    def __repr__(self):
        return "<PTypedKey %r type %r>" %(self.pattern, self.type.__name__)


class KeyChangeEvent:
    def __init__(self, typedkey, value, at_serial, back_serial):
        self.typedkey = typedkey
        self.value = value
        self.at_serial = at_serial
        self.back_serial = back_serial


class TypedKey:
    def __init__(self, keyfs, relpath, type, name, params=None):
        self.keyfs = keyfs
        self.relpath = relpath
        self.type = type
        self.name = name
        self.params = params or {}

    @cached_property
    def params(self):
        key = self.keyfs.get_key(self.name)
        if isinstance(key, PTypedKey):
            return key.extract_params(self.relpath)
        return {}

    def __hash__(self):
        return hash(self.relpath)

    def __eq__(self, other):
        return self.relpath == other.relpath

    def __repr__(self):
        return "<TypedKey %r type %r>" %(self.relpath, self.type.__name__)

    def get(self, readonly=True):
        return self.keyfs.tx.get(self, readonly=readonly)

    def is_dirty(self):
        return self.keyfs.tx.is_dirty(self)

    @contextlib.contextmanager
    def update(self):
        val = self.keyfs.tx.get(self, readonly=False)
        yield val
        # no exception, so we can set and thus mark dirty the object
        self.set(val)

    def set(self, val):
        if not isinstance(val, self.type):
            raise TypeError("%r requires value of type %r, got %r" %(
                            self.relpath, self.type.__name__,
                            type(val).__name__))
        self.keyfs.tx.set(self, val)

    def exists(self):
        return self.keyfs.tx.exists(self)

    def delete(self):
        return self.keyfs.tx.delete(self)


class Transaction(object):
    commit_serial = None
    write = False

    def __init__(self, keyfs, at_serial=None, write=False):
        self.keyfs = keyfs
        if write:
            assert at_serial is None, "write trans cannot use at_serial"
            keyfs._write_lock.acquire()
            self.write = True
        if at_serial is None:
            at_serial = keyfs.get_current_serial()
        self.at_serial = at_serial
        self.cache = {}
        self.dirty = set()
        self.conn = keyfs._storage.get_connection(closing=False)

    def get_key_in_transaction(self, relpath):
        for key in self.cache:
            if key.relpath == relpath:
                return key
        raise KeyError(relpath)

    def is_dirty(self, typedkey):
        return typedkey in self.dirty

    def get(self, typedkey, readonly=True):
        """ Return value referenced by typedkey, either as a readonly-view
        or as a mutable deep copy. """
        try:
            val = self.cache[typedkey]
        except KeyError:
            absent = typedkey in self.dirty
            if not absent:
                try:
                    val = self.keyfs.get_value_at(typedkey, self.at_serial,
                                                  conn=self.conn)
                except KeyError:
                    absent = True
            if absent:
                # for convenience we return an empty instance
                # but below we still respect the readonly property
                val = typedkey.type()
            else:
                assert is_deeply_readonly(val)
                self.cache[typedkey] = val
        if readonly:
            return ensure_deeply_readonly(val)
        else:
            return get_mutable_deepcopy(val)

    def exists(self, typedkey):
        if typedkey in self.cache:
            return True
        if typedkey in self.dirty:
            return False
        try:
            self.keyfs.get_value_at(typedkey, self.at_serial,
                                    conn=self.conn)
        except KeyError:
            return False
        else:
            return True


    def delete(self, typedkey):
        if not self.write:
            raise self.keyfs.ReadOnly()
        self.cache.pop(typedkey, None)
        self.dirty.add(typedkey)

    def set(self, typedkey, val):
        if not self.write:
            raise self.keyfs.ReadOnly()
        # sanity check for dictionaries: we always want to have unicode
        # keys, not bytes
        if typedkey.type == dict:
            check_unicode_keys(val)
        self.cache[typedkey] = val
        self.dirty.add(typedkey)

    def commit(self):
        if not self.write:
            return self._close()
        if not self.dirty and not self.conn.dirty_files:
            threadlog.debug("nothing to commit, just closing tx")
            return self._close()
        try:
            with self.keyfs.write_transaction(self.conn) as fswriter:
                for typedkey in self.dirty:
                    val = self.cache.get(typedkey)
                    # None signals deletion
                    fswriter.record_set(typedkey, val)
                commit_serial = fswriter.storage.next_serial
        finally:
            self._close()
        self.commit_serial = commit_serial
        return commit_serial

    def _close(self):
        del self.cache
        del self.dirty
        if self.write:
            self.keyfs._write_lock.release()
        self.conn.close()
        return self.at_serial

    def rollback(self):
        threadlog.debug("transaction rollback at %s" % (self.at_serial))
        return self._close()

    def restart(self, write=False):
        self.commit()
        threadlog.debug("restarting afresh as write transaction")
        newtx = self.__class__(self.keyfs, write=write)
        self.__dict__ = newtx.__dict__


def check_unicode_keys(d):
    for key, val in d.items():
        assert not isinstance(key, py.builtin.bytes), repr(key)
        # not allowing bytes seems ok for now, we might need to relax that
        # it certainly helps to get unicode clean
        assert not isinstance(val, py.builtin.bytes), repr(key) + "=" + repr(val)
        if isinstance(val, dict):
            check_unicode_keys(val)

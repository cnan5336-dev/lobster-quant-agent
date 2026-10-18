"""Opt-in retrieval gate. No HTTP client, credentials, or implicit transport.

All explicit retrieval adapters must enter through execute(). Provider-internal
search is outside this boundary. Counts are conservative local estimates, not bills.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import uuid
from urllib.parse import urlsplit, urlunsplit


class RetrievalBlocked(RuntimeError):
    """Fixed diagnostic; never includes query, URL, response or adapter exception."""


@dataclass(frozen=True)
class Policy:
    task_calls: int = 8
    task_attempts: int = 12
    month_calls: int = 20000
    month_attempts: int = 24000
    search_ttl: int = 900
    fetch_ttl: int = 3600

    def __post_init__(self):
        for value in self.__dict__.values():
            if type(value) is not int or value < 0:
                raise ValueError('invalid_retrieval_policy')


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def _identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r'[a-zA-Z0-9_.:-]{1,128}', value):
        raise ValueError('invalid_retrieval_identifier')
    return value


def _request(kind, arguments):
    if not isinstance(arguments, dict):
        raise ValueError('invalid_retrieval_arguments')
    args = dict(arguments)
    if kind == 'search':
        if set(args) - {'query', 'count', 'need_content', 'ori_question', 'localization'}:
            raise ValueError('unknown_search_argument')
        query = args.get('query')
        if not isinstance(query, str) or not query.strip() or len(query) > 128:
            raise ValueError('invalid_search_query')
        args['query'] = query.strip()
        args.setdefault('count', 10)
        args.setdefault('need_content', False)
        args.setdefault('localization', '')
        args.setdefault('ori_question', None)
        if type(args['count']) is not int or not 1 <= args['count'] <= 50:
            raise ValueError('invalid_search_count')
        if type(args['need_content']) is not bool:
            raise ValueError('invalid_search_content_flag')
        if not isinstance(args['localization'], str):
            raise ValueError('invalid_search_localization')
        q = args['ori_question']
        if q is not None and (not isinstance(q, str) or not q.strip() or len(q) > 128):
            raise ValueError('invalid_original_question')
    elif kind == 'fetch':
        if set(args) != {'url'} or not isinstance(args['url'], str) or len(args['url']) > 8192:
            raise ValueError('invalid_fetch_url')
        u = urlsplit(args['url'])
        if u.scheme not in ('https', 'http') or not u.hostname or u.username or u.password:
            raise ValueError('invalid_fetch_url')
        # Fragments never reach HTTP. Keep query parameters and path semantics.
        args['url'] = urlunsplit((u.scheme.lower(), u.netloc.lower(), u.path or '/', u.query, ''))
    else:
        raise ValueError('invalid_retrieval_kind')
    return args


class RetrievalGate:
    """Shared private SQLite ledger; successful immutable evidence is cached.

    transport(kind, arguments) returns (HTTP status, decoded JSON). A transport
    exception leaves the reservation UNKNOWN; no automatic retry or refund.
    The caller supplies a stable task ID across agents, restarts and retries.
    """
    def __init__(self, directory, *, policy=None, clock=None):
        self.policy = policy or Policy()
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        directory = Path(directory).absolute()
        # Caller owns a dedicated directory. Never follow a symlink component.
        if any(p.is_symlink() for p in [directory, *directory.parents]):
            raise RetrievalBlocked('unsafe_retrieval_store')
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        mode = directory.stat()
        if mode.st_uid != os.getuid() or stat.S_IMODE(mode.st_mode) & 0o077:
            raise RetrievalBlocked('unsafe_retrieval_store')
        self.path = directory / 'retrieval.sqlite3'
        for p in directory.iterdir():
            if p.name.startswith('retrieval.sqlite3'):
                s = p.lstat()
                if not stat.S_ISREG(s.st_mode) or s.st_uid != os.getuid() or stat.S_IMODE(s.st_mode) & 0o077:
                    raise RetrievalBlocked('unsafe_retrieval_store')
        marker = directory / '.retrieval-initialized'
        if marker.exists() or marker.is_symlink():
            marker_stat = marker.lstat()
            if (not stat.S_ISREG(marker_stat.st_mode) or marker_stat.st_uid != os.getuid()
                    or stat.S_IMODE(marker_stat.st_mode) & 0o077):
                raise RetrievalBlocked('unsafe_retrieval_store')
        existed = self.path.exists()
        if marker.exists() != existed:
            raise RetrievalBlocked('retrieval_store_incomplete')
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        os.close(fd)
        with self._db() as db:
            if existed:
                version = db.execute('SELECT version FROM schema_version').fetchall()
                if version != [(1,)] or db.execute('PRAGMA quick_check').fetchall() != [('ok',)]:
                    raise RetrievalBlocked('retrieval_store_incomplete')
                db.execute('SELECT id,state FROM calls LIMIT 0')
                db.execute('SELECT task,call_id,at FROM cache_reads LIMIT 0')
            db.executescript('''
                CREATE TABLE IF NOT EXISTS schema_version(version INTEGER NOT NULL);
                INSERT INTO schema_version SELECT 1 WHERE NOT EXISTS (SELECT 1 FROM schema_version);
                CREATE TABLE IF NOT EXISTS calls (
                    id TEXT PRIMARY KEY, task TEXT NOT NULL, month TEXT NOT NULL,
                    cache_key TEXT NOT NULL, kind TEXT NOT NULL, provider TEXT NOT NULL,
                    reason TEXT NOT NULL, state TEXT NOT NULL,
                    created REAL NOT NULL, completed REAL, expires REAL,
                    status INTEGER, evidence TEXT, evidence_sha256 TEXT);
                CREATE TABLE IF NOT EXISTS cache_reads (task TEXT NOT NULL, call_id TEXT NOT NULL, at REAL NOT NULL);
                CREATE UNIQUE INDEX IF NOT EXISTS pending_key ON calls(cache_key)
                    WHERE state = 'pending';
            ''')
        if not existed:
            marker.touch(mode=0o600, exist_ok=False)

    @contextmanager
    def _db(self):
        db = None
        try:
            db = sqlite3.connect(self.path, timeout=5)
            with db:
                yield db
        except sqlite3.Error:
            raise RetrievalBlocked('retrieval_store_unavailable') from None
        finally:
            if db is not None:
                db.close()

    def _now(self):
        now = self.clock()
        if now.tzinfo is None:
            raise ValueError('retrieval_clock_must_be_aware')
        return now.timestamp(), now.astimezone(timezone(timedelta(hours=8))).strftime('%Y-%m')

    def execute(self, kind, arguments, *, task_id, provider, reason, transport=None):
        task_id, provider = _identifier(task_id), _identifier(provider)
        if reason not in {'freshness', 'citation', 'conflict', 'user_requested'}:
            raise RetrievalBlocked('retrieval_reason_required')
        if transport is None:
            raise RetrievalBlocked('retrieval_transport_disabled')
        args = _request(kind, arguments)
        key = _digest({'schema': 1, 'provider': provider, 'kind': kind, 'args': args})
        now, month = self._now()
        ttl = self.policy.search_ttl if kind == 'search' else self.policy.fetch_ttl
        call_id = uuid.uuid4().hex
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            cached = db.execute('''SELECT id,evidence,evidence_sha256,created,completed FROM calls
                WHERE cache_key=? AND state='success' AND expires>? AND completed<=?
                AND completed+?>? ORDER BY completed DESC LIMIT 1''',
                (key, now, now, ttl, now)).fetchone()
            if cached:
                if _digest(json.loads(cached[1])) != cached[2]:
                    raise RetrievalBlocked('retrieval_evidence_integrity')
                db.execute('INSERT INTO cache_reads VALUES(?,?,?)', (task_id, cached[0], now))
                return {'cache_hit': True, 'call_id': cached[0], 'evidence': json.loads(cached[1]),
                        'evidence_sha256': cached[2], 'requested_at': cached[3], 'retrieved_at': cached[4],
                        'provider': provider, 'estimated_cost_microyuan': 0}
            if db.execute("SELECT 1 FROM calls WHERE cache_key=? AND state='pending'", (key,)).fetchone():
                raise RetrievalBlocked('retrieval_pending_or_unknown')
            for column, value, call_limit, attempt_limit in (
                ('task', task_id, self.policy.task_calls, self.policy.task_attempts),
                ('month', month, self.policy.month_calls, self.policy.month_attempts),
            ):
                total, charged = db.execute(
                    f"SELECT count(*),coalesce(sum(state IN ('pending','success')),0) FROM calls WHERE {column}=?",
                    (value,)).fetchone()
                if charged >= call_limit or total >= attempt_limit:
                    raise RetrievalBlocked('retrieval_' + column + '_limit')
            db.execute('INSERT INTO calls(id,task,month,cache_key,kind,provider,reason,state,created) VALUES(?,?,?,?,?,?,?,?,?)',
                       (call_id, task_id, month, key, kind, provider, reason, 'pending', now))
        # Commit reservation before adapter entry. A crash cannot erase its budget.
        try:
            status, payload = transport(kind, dict(args))
            encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, allow_nan=False)
            if type(status) is not int or not 100 <= status <= 599 or len(encoded.encode()) > 2_000_000:
                raise ValueError('invalid_transport_response')
            if not isinstance(payload, dict):
                raise ValueError('invalid_transport_response')
            if status == 200:
                if kind == 'search':
                    if not isinstance(payload.get('results'), list):
                        raise ValueError('invalid_search_response')
                    success = bool(payload['results'])
                else:
                    if not isinstance(payload.get('content'), str):
                        raise ValueError('invalid_fetch_response')
                    success = bool(payload['content'].strip())
            else:
                success = False
        except Exception:
            # Includes timeout, ambiguous adapter failure and malformed HTTP 200.
            raise RetrievalBlocked('retrieval_outcome_unknown') from None
        completed, _ = self._now()
        digest = _digest(payload)
        with self._db() as db:
            db.execute('''UPDATE calls SET state=?,completed=?,expires=?,status=?,evidence=?,evidence_sha256=?
                          WHERE id=? AND state='pending' ''',
                       ('success' if success else 'failed', completed, completed + ttl, status,
                        encoded, digest, call_id))
        return {'cache_hit': False, 'call_id': call_id, 'evidence': json.loads(encoded),
                'evidence_sha256': digest, 'requested_at': now, 'retrieved_at': completed,
                'provider': provider, 'estimated_cost_microyuan': 5000 if success else 0}

    def read_evidence(self, call_id):
        """Read an immutable snapshot even after expiry, without network access."""
        _identifier(call_id)
        with self._db() as db:
            row = db.execute("""SELECT evidence,evidence_sha256,provider,kind,created,completed
                FROM calls WHERE id=? AND state='success'""", (call_id,)).fetchone()
        if not row:
            raise RetrievalBlocked('retrieval_evidence_unavailable')
        evidence = json.loads(row[0])
        if _digest(evidence) != row[1]:
            raise RetrievalBlocked('retrieval_evidence_integrity')
        return {'call_id': call_id, 'evidence': evidence, 'evidence_sha256': row[1],
                'provider': row[2], 'kind': row[3], 'requested_at': row[4], 'retrieved_at': row[5]}

    def status(self, *, task_id):
        """Local-only counters; never instantiates or calls an adapter."""
        _identifier(task_id)
        _, month = self._now()
        with self._db() as db:
            result = {'month_key_bjt': month, 'limits': dict(self.policy.__dict__)}
            for column, value in [('task', task_id), ('month', month)]:
                counts = dict(db.execute(f'SELECT state,count(*) FROM calls WHERE {column}=? GROUP BY state', (value,)))
                result[column] = {'attempts': sum(counts.values()), 'successful': counts.get('success', 0),
                                  'reserved_unknown': counts.get('pending', 0), 'failed': counts.get('failed', 0),
                                  'estimated_cost_microyuan': counts.get('success', 0) * 5000,
                                  'reserved_microyuan': counts.get('pending', 0) * 5000}
            result['cache_hits'] = db.execute('SELECT count(*) FROM cache_reads WHERE task=?', (task_id,)).fetchone()[0]
        return result

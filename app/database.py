"""数据库连接与会话管理（SQLite）。"""
import os

from sqlalchemy import create_engine, event
from sqlalchemy.orm import declarative_base, sessionmaker

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./street_archive.db")

if DATABASE_URL.startswith("sqlite"):
    # 确保 SQLite 文件所在目录存在（容器内 /data 由 Dockerfile 创建）
    _db_path = DATABASE_URL.split("///", 1)[-1]
    if _db_path not in (":memory:", ""):
        os.makedirs(os.path.dirname(os.path.abspath(_db_path)), exist_ok=True)

_connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
engine = create_engine(DATABASE_URL, connect_args=_connect_args, pool_pre_ping=True)

if DATABASE_URL.startswith("sqlite"):
    @event.listens_for(engine, "connect")
    def _set_sqlite_pragma(dbapi_conn, _):
        cur = dbapi_conn.cursor()
        # WAL 允许核查读快照与资料写入并发；busy_timeout 避免瞬时写锁报错
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA busy_timeout=5000")
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
Base = declarative_base()

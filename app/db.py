"""SQLite 数据层。

表结构：
    datasets      数据集（一个出图目录 = 一个数据集）
    images        图片记录 + 解析出的提示词/元数据
    tags          标签词条库（带屏蔽标记）
    image_tags    图片 <-> 标签 多对多，带来源（manual / auto）

线程安全：单个连接 + RLock。打标线程池会并发写，所以所有写操作都走
``_write()`` 统一加锁；读操作也加锁，避免 WAL 下偶发的串扰。

健壮性：SQLite 排序溢出时默认要在临时目录建临时文件。受限环境里
``%TEMP%`` 可能不可写（例如 Windows 受控文件夹访问拦未知的 exe），
这时任何需要落盘排序的查询都会报 ``unable to open database file``。
所以这里强制 ``temp_store = MEMORY``，让 SQLite 永远不碰临时文件。
另外连接一旦失效（目录被删、文件被占）会自动重连重试一次。
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Iterable, Sequence

from . import captions, paths

SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS datasets (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL UNIQUE,
    path        TEXT NOT NULL,
    note        TEXT DEFAULT '',
    created_at  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS images (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    dataset_id  INTEGER NOT NULL REFERENCES datasets(id) ON DELETE CASCADE,
    path        TEXT NOT NULL UNIQUE,
    filename    TEXT NOT NULL,
    rel_path    TEXT DEFAULT '',
    width       INTEGER DEFAULT 0,
    height      INTEGER DEFAULT 0,
    size        INTEGER DEFAULT 0,
    mtime       REAL DEFAULT 0,
    prompt      TEXT DEFAULT '',
    negative    TEXT DEFAULT '',
    meta        TEXT DEFAULT '{}',
    tags_text   TEXT DEFAULT '',
    status      TEXT DEFAULT 'pending',
    rating      INTEGER DEFAULT 0,
    has_meta    INTEGER DEFAULT 0,
    has_caption INTEGER DEFAULT 0,
    added_at    REAL NOT NULL,
    tagged_at   REAL
);

CREATE INDEX IF NOT EXISTS idx_images_dataset ON images(dataset_id);
CREATE INDEX IF NOT EXISTS idx_images_status  ON images(status);
CREATE INDEX IF NOT EXISTS idx_images_mtime   ON images(mtime DESC);

CREATE TABLE IF NOT EXISTS tags (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL UNIQUE,
    category    TEXT DEFAULT 'general',
    banned      INTEGER DEFAULT 0,
    is_auto     INTEGER DEFAULT 0,
    created_at  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS image_tags (
    image_id    INTEGER NOT NULL REFERENCES images(id) ON DELETE CASCADE,
    tag_id      INTEGER NOT NULL REFERENCES tags(id)    ON DELETE CASCADE,
    source      TEXT DEFAULT 'manual',
    PRIMARY KEY (image_id, tag_id)
);

CREATE INDEX IF NOT EXISTS idx_image_tags_tag ON image_tags(tag_id);

-- 配套 txt 的标注暂存区。
-- 打标台的编辑先落在这儿（current_json），点保存才写回 txt，
-- 保存成功后清掉这一行。original_json 留着做 diff 和「放弃改动」。
CREATE TABLE IF NOT EXISTS caption_edits (
    image_id    INTEGER PRIMARY KEY REFERENCES images(id) ON DELETE CASCADE,
    original_json TEXT NOT NULL DEFAULT '[]',
    current_json  TEXT NOT NULL DEFAULT '[]',
    touched_at  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS preprocess_ops (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    detail_json TEXT NOT NULL,
    created_at REAL NOT NULL,
    undone_at REAL
);
"""

CAPTION_INDEX = """
CREATE INDEX IF NOT EXISTS idx_caption_edits_touched ON caption_edits(touched_at);
"""

STATUS_PENDING = "pending"
STATUS_TAGGED = "tagged"
STATUS_SKIP = "skip"

IMAGE_EXTENSIONS = {
    ".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif", ".avif", ".tif", ".tiff",
}


class Database:
    """工作台的全部持久化操作。"""

    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path or paths.db_path())
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._reconnects = 0
        self._conn = self._connect()

    # ------------------------------------------------------------------ #
    # 连接
    # ------------------------------------------------------------------ #
    def _connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(str(self.path), check_same_thread=False, timeout=5.0)
        con.row_factory = sqlite3.Row
        # 这些必须每次连接都设：它们是连接级的，不是库级的
        con.execute("PRAGMA busy_timeout = 5000")
        con.execute("PRAGMA foreign_keys = ON")
        # 排序 / GROUP BY 溢出时不写临时文件。
        # 受控文件夹访问、只读 %TEMP%、沙箱进程都可能让临时目录不可写，
        # 那时候 SQLite 报的错是 "unable to open database file"，
        # 而且只在数据量大到需要落盘排序时才炸，特别难查。
        con.execute("PRAGMA temp_store = MEMORY")
        con.execute("PRAGMA synchronous = NORMAL")
        con.executescript(SCHEMA)
        con.execute("PRAGMA journal_mode=WAL")
        self._migrate(con)
        con.commit()
        return con

    @staticmethod
    def _migrate(con: sqlite3.Connection) -> None:
        """给老库补新列 / 新表。CREATE TABLE IF NOT EXISTS 管不到已存在的表。"""
        columns = {
            row[1] for row in con.execute("PRAGMA table_info(images)").fetchall()
        }
        if "has_caption" not in columns:
            con.execute("ALTER TABLE images ADD COLUMN has_caption INTEGER DEFAULT 0")
        con.executescript(CAPTION_INDEX)

    def _reconnect(self) -> None:
        """连接已经废掉时重开一次。目录被删/文件被挪走时会用到。"""
        with self._lock:
            try:
                self._conn.close()
            except sqlite3.Error:
                pass
            # 目录可能连同数据库一起没了，先补出来
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
            except OSError:
                pass
            self._conn = self._connect()
            self._reconnects += 1

    # ------------------------------------------------------------------ #
    # 底层
    # ------------------------------------------------------------------ #
    def close(self) -> None:
        with self._lock:
            try:
                self._conn.commit()
                self._conn.close()
            except sqlite3.Error:
                pass

    def _write(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            try:
                cur = self._conn.execute(sql, params)
                self._conn.commit()
                return cur
            except sqlite3.OperationalError as exc:
                if not self._looks_recoverable(exc):
                    raise
                self._reconnect()
                cur = self._conn.execute(sql, params)
                self._conn.commit()
                return cur

    def _query(self, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
        with self._lock:
            try:
                return list(self._conn.execute(sql, params))
            except sqlite3.OperationalError as exc:
                if not self._looks_recoverable(exc):
                    raise
                self._reconnect()
                return list(self._conn.execute(sql, params))

    def _query_one(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Row | None:
        with self._lock:
            try:
                return self._conn.execute(sql, params).fetchone()
            except sqlite3.OperationalError as exc:
                if not self._looks_recoverable(exc):
                    raise
                self._reconnect()
                return self._conn.execute(sql, params).fetchone()

    @staticmethod
    def _looks_recoverable(exc: sqlite3.OperationalError) -> bool:
        """这个错值不值得重连一次再试。

        「unable to open database file」多半是临时目录/数据库目录出问题，
        重连能救回来；语法错误之类重连也没用，直接往上抛。
        """
        text = str(exc).lower()
        return any(
            marker in text
            for marker in (
                "unable to open database file",
                "database is locked",
                "disk i/o error",
                "attempt to write a readonly database",
            )
        )

    def health(self) -> dict[str, Any]:
        """给「设置 / 运行状态」用的体检信息。"""
        info: dict[str, Any] = {
            "path": str(self.path),
            "exists": self.path.exists(),
            "size": self.path.stat().st_size if self.path.exists() else 0,
            "reconnects": self._reconnects,
            "temp_store": "?",
            "journal_mode": "?",
            "ok": False,
            "error": "",
        }
        try:
            info["temp_store"] = self._query_one("PRAGMA temp_store")[0]
            info["journal_mode"] = self._query_one("PRAGMA journal_mode")[0]
            info["ok"] = True
        except sqlite3.Error as exc:
            info["error"] = f"{type(exc).__name__}: {exc}"
        return info

    def reopen(self) -> None:
        """手动重开连接（界面上「重新连接数据库」用）。"""
        self._reconnect()

    # ------------------------------------------------------------------ #
    # 配套 txt 标注（打标台）
    # ------------------------------------------------------------------ #
    def caption_original(self, image_id: int, image_path: str) -> list[str]:
        """拿这张图的基准标签。

        优先级：暂存区里已记的 original -> 磁盘上的 txt -> 空。
        （没有配套 txt 的图不参与打标，调用方负责过滤。）
        """
        row = self._query_one(
            "SELECT original_json FROM caption_edits WHERE image_id = ?", (image_id,)
        )
        if row:
            try:
                saved = json.loads(row["original_json"])
                if isinstance(saved, list):
                    return [str(x) for x in saved]
            except (ValueError, TypeError):
                pass
        return captions.read_caption(image_path)

    def caption_current(self, image_id: int, image_path: str) -> list[str]:
        """拿这张图当前的（可能改过还没保存的）标签。"""
        row = self._query_one(
            "SELECT original_json, current_json FROM caption_edits WHERE image_id = ?",
            (image_id,),
        )
        if row:
            try:
                current = json.loads(row["current_json"])
                if isinstance(current, list):
                    return [str(x) for x in current]
            except (ValueError, TypeError):
                pass
        return captions.read_caption(image_path)

    def set_caption_tags(
        self, image_id: int, image_path: str, tags: list[str]
    ) -> None:
        """把这张图的（未保存）标签写进暂存区。"""
        original = self.caption_original(image_id, image_path)
        self._write(
            """
            INSERT INTO caption_edits (image_id, original_json, current_json, touched_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(image_id) DO UPDATE SET
                current_json = excluded.current_json,
                touched_at   = excluded.touched_at
            """,
            (
                image_id,
                json.dumps(original, ensure_ascii=False),
                json.dumps(tags, ensure_ascii=False),
                time.time(),
            ),
        )

    def caption_edit(self, image_id: int, image_path: str) -> captions.CaptionEdit:
        edit = captions.CaptionEdit(image_id=image_id, image_path=str(image_path))
        edit.original = self.caption_original(image_id, image_path)
        edit.current = self.caption_current(image_id, image_path)
        return edit

    def caption_edits(self) -> list[captions.CaptionEdit]:
        """所有改过还没保存的图。"""
        rows = self._query(
            """
            SELECT e.image_id, e.original_json, e.current_json, i.path
            FROM caption_edits e JOIN images i ON i.id = e.image_id
            ORDER BY e.touched_at
            """
        )
        out: list[captions.CaptionEdit] = []
        for row in rows:
            try:
                original = json.loads(row["original_json"])
                current = json.loads(row["current_json"])
            except (ValueError, TypeError):
                continue
            edit = captions.CaptionEdit(
                image_id=int(row["image_id"]), image_path=str(row["path"])
            )
            edit.original = [str(x) for x in original]
            edit.current = [str(x) for x in current]
            if edit.changed:
                out.append(edit)
        return out

    def discard_caption(self, image_id: int) -> None:
        self._write("DELETE FROM caption_edits WHERE image_id = ?", (image_id,))

    def discard_all_captions(self) -> int:
        cur = self._write("DELETE FROM caption_edits")
        return cur.rowcount or 0

    def pending_caption_count(self) -> int:
        row = self._query_one(
            """
            SELECT COUNT(*) AS n FROM caption_edits
            WHERE original_json <> current_json
            """
        )
        return int(row["n"]) if row else 0

    def save_captions(
        self, edits: list[captions.CaptionEdit], *, make_backup: bool = True
    ) -> tuple[int, list[str]]:
        """把暂存的改动写回 txt。返回 (成功数, 失败说明列表)。"""
        saved = 0
        errors: list[str] = []
        for edit in edits:
            if not edit.changed:
                self.discard_caption(edit.image_id)
                continue
            ok, note = captions.write_caption(
                edit.image_path, edit.current, make_backup=make_backup
            )
            if ok:
                saved += 1
                self.discard_caption(edit.image_id)
            else:
                errors.append(note)
        return saved, errors

    # ------------------------------------------------------------------ #
    # 打标台的标签统计 / 批量操作
    # ------------------------------------------------------------------ #
    def caption_tag_counts(
        self, image_ids: Sequence[int]
    ) -> list[tuple[str, int]]:
        """统计这批图里每个标签出现的次数（未保存的改动也算）。

        返回按出现次数降序排列的 ``[(标签, 次数), ...]``。
        """
        ids = [int(i) for i in image_ids]
        if not ids:
            return []

        counter: dict[str, int] = {}
        display: dict[str, str] = {}
        placed = 0

        # 分批查，避免 SQLite 变量上限
        for start in range(0, len(ids), 500):
            batch = ids[start : start + 500]
            holes = ",".join("?" * len(batch))
            rows = self._query(
                f"""
                SELECT i.id, i.path, e.current_json
                FROM images i
                LEFT JOIN caption_edits e ON e.image_id = i.id
                WHERE i.id IN ({holes})
                """,
                batch,
            )
            by_id = {int(r["id"]): r for r in rows}
            for image_id in batch:
                row = by_id.get(image_id)
                if row is None:
                    continue
                placed += 1
                current = row["current_json"]
                if current:
                    try:
                        tags = [str(x) for x in json.loads(current)]
                    except (ValueError, TypeError):
                        tags = captions.read_caption(str(row["path"]))
                else:
                    tags = captions.read_caption(str(row["path"]))
                for tag in tags:
                    key = tag.strip().lower()
                    if not key:
                        continue
                    counter[key] = counter.get(key, 0) + 1
                    display.setdefault(key, tag.strip())

        ordered = sorted(counter.items(), key=lambda kv: (-kv[1], kv[0]))
        return [(display[key], count) for key, count in ordered]

    def image_ids_with_tag(
        self, tag: str, dataset_id: int | None = None
    ) -> list[int]:
        """找出所有带这个标签的图片 id（含暂存改动，大小写不敏感）。

        打标台「查看所有该标签图片」用它筛图。
        """
        want = (tag or "").strip().lower()
        if not want:
            return []

        sql = """
            SELECT i.id, i.path, e.current_json
            FROM images i
            LEFT JOIN caption_edits e ON e.image_id = i.id
            WHERE i.has_caption = 1
        """
        params: list[Any] = []
        if dataset_id is not None:
            sql += " AND i.dataset_id = ?"
            params.append(dataset_id)
        sql += " ORDER BY i.mtime DESC, i.id DESC"

        found: list[int] = []
        for row in self._query(sql, params):
            current = row["current_json"]
            if current:
                try:
                    tags = [str(x) for x in json.loads(current)]
                except (ValueError, TypeError):
                    tags = captions.read_caption(str(row["path"]))
            else:
                tags = captions.read_caption(str(row["path"]))
            if any(t.strip().lower() == want for t in tags):
                found.append(int(row["id"]))
        return found

    def all_caption_tags(self, dataset_id: int | None = None) -> list[tuple[str, int]]:
        """整库标签清单 [(标签, 出现次数)]，给替换框做下拉提示用。"""
        ids = self.caption_image_ids(dataset_id=dataset_id)
        return self.caption_tag_counts(ids)

    def caption_applied_count(self, image_ids: Sequence[int]) -> int:
        """这批图里实际有标注（含暂存改动）的张数，算占比用。"""
        ids = [int(i) for i in image_ids]
        if not ids:
            return 0
        total = 0
        for start in range(0, len(ids), 500):
            batch = ids[start : start + 500]
            holes = ",".join("?" * len(batch))
            rows = self._query(
                f"""
                SELECT e.current_json, i.path
                FROM images i
                LEFT JOIN caption_edits e ON e.image_id = i.id
                WHERE i.id IN ({holes})
                """,
                batch,
            )
            for row in rows:
                current = row["current_json"]
                if current:
                    try:
                        if json.loads(current):
                            total += 1
                            continue
                    except (ValueError, TypeError):
                        pass
                if captions.read_caption(str(row["path"])):
                    total += 1
        return total

    def images_missing_caption(self, dataset_id: int | None = None) -> int:
        """有多少张图没有配套 txt（这些图在打标台不显示标签）。

        依赖扫描时记下的 has_caption 字段，不做实时磁盘 IO。
        """
        if dataset_id is None:
            row = self._query_one(
                "SELECT COUNT(*) AS n FROM images WHERE has_caption = 0"
            )
        else:
            row = self._query_one(
                "SELECT COUNT(*) AS n FROM images WHERE has_caption = 0 AND dataset_id = ?",
                (dataset_id,),
            )
        return int(row["n"]) if row else 0

    def refresh_caption_flags(self, dataset_id: int | None = None) -> int:
        """重新核对磁盘上配套 txt 的存在情况，返回变化条数。"""
        if dataset_id is None:
            rows = self._query("SELECT id, path, has_caption FROM images")
        else:
            rows = self._query(
                "SELECT id, path, has_caption FROM images WHERE dataset_id = ?",
                (dataset_id,),
            )
        changed = 0
        for row in rows:
            actual = 1 if captions.has_caption(str(row["path"])) else 0
            if actual != int(row["has_caption"] or 0):
                self._write(
                    "UPDATE images SET has_caption = ? WHERE id = ?",
                    (actual, int(row["id"])),
                )
                changed += 1
        return changed

    def caption_image_ids(
        self, dataset_id: int | None = None, limit: int = 0
    ) -> list[int]:
        """有配套 txt 的图片 id（打标台只认这些）。"""
        sql = "SELECT id FROM images WHERE has_caption = 1"
        params: list[Any] = []
        if dataset_id is not None:
            sql += " AND dataset_id = ?"
            params.append(dataset_id)
        sql += " ORDER BY mtime DESC, id DESC"
        if limit:
            sql += " LIMIT ?"
            params.append(limit)
        return [int(r["id"]) for r in self._query(sql, params)]

    # ------------------------------------------------------------------ #
    # 数据集
    # ------------------------------------------------------------------ #
    def upsert_dataset(self, name: str, folder: str, note: str = "") -> int:
        name = name.strip() or Path(folder).name or "dataset"
        # 用 paths.norm_path 而不是 Path().resolve()：它会先剥掉 `\\?\` 设备前缀。
        # 不剥的话 `\\?\D:\imgs` 会存成另一条数据集记录（0 张图），
        # 用户看到"数据集重复了、图不见了"。
        folder = str(paths.norm_path(folder))
        row = self._query_one("SELECT id FROM datasets WHERE name = ?", (name,))
        if row:
            self._write(
                "UPDATE datasets SET path = ?, note = ? WHERE id = ?",
                (folder, note, row["id"]),
            )
            return int(row["id"])
        cur = self._write(
            "INSERT INTO datasets (name, path, note, created_at) VALUES (?, ?, ?, ?)",
            (name, folder, note, time.time()),
        )
        return int(cur.lastrowid)

    def dataset_by_path(self, folder: str) -> sqlite3.Row | None:
        # 必须和 upsert_dataset() 用**同一套**规范化，否则"存的"和"查的"
        # 会不一致：`\\?\D:\imgs` 查不到已经存好的 `D:\imgs`，
        # 于是又新建一条重复数据集。
        return self._query_one(
            "SELECT * FROM datasets WHERE path = ?", (str(paths.norm_path(folder)),)
        )

    def rename_dataset(self, dataset_id: int, name: str) -> None:
        self._write("UPDATE datasets SET name = ? WHERE id = ?", (name, dataset_id))

    def delete_dataset(self, dataset_id: int, drop_images: bool = True) -> None:
        if drop_images:
            self._write("DELETE FROM images WHERE dataset_id = ?", (dataset_id,))
        self._write("DELETE FROM datasets WHERE id = ?", (dataset_id,))
        self.prune_orphan_tags()

    def list_datasets(self) -> list[dict]:
        """带统计信息的数据集列表：总数 / 已打标 / 待打标。"""
        rows = self._query(
            """
            SELECT d.id, d.name, d.path, d.note, d.created_at,
                   COUNT(i.id)                                        AS total,
                   SUM(CASE WHEN i.status = 'tagged' THEN 1 ELSE 0 END) AS tagged,
                   SUM(CASE WHEN i.status = 'pending' THEN 1 ELSE 0 END) AS pending,
                   MAX(i.mtime)                                       AS last_mtime
            FROM datasets d
            LEFT JOIN images i ON i.dataset_id = d.id
            GROUP BY d.id
            ORDER BY d.name COLLATE NOCASE
            """
        )
        out = []
        for r in rows:
            total = int(r["total"] or 0)
            tagged = int(r["tagged"] or 0)
            d = dict(r)
            d["total"] = total
            d["tagged"] = tagged
            d["pending"] = int(r["pending"] or 0)
            d["progress"] = (tagged / total) if total else 0.0
            out.append(d)
        return out

    def get_dataset(self, dataset_id: int) -> sqlite3.Row | None:
        return self._query_one("SELECT * FROM datasets WHERE id = ?", (dataset_id,))

    # ------------------------------------------------------------------ #
    # 图片
    # ------------------------------------------------------------------ #
    def image_exists(self, path: str) -> bool:
        return self._query_one("SELECT 1 FROM images WHERE path = ?", (path,)) is not None

    def find_by_path(self, path: str) -> dict | None:
        """按绝对路径精确查一张图（扫描时用来判重）。"""
        row = self._query_one("SELECT * FROM images WHERE path = ?", (path,))
        return self._image_row_to_dict(row) if row is not None else None

    def find_by_id(self, image_id: int) -> dict | None:
        """按 id 查一张图。"""
        row = self._query_one("SELECT * FROM images WHERE id = ?", (int(image_id),))
        return self._image_row_to_dict(row) if row is not None else None

    def add_image(
        self,
        dataset_id: int,
        path: str,
        filename: str,
        rel_path: str = "",
        width: int = 0,
        height: int = 0,
        size: int = 0,
        mtime: float = 0.0,
        prompt: str = "",
        negative: str = "",
        meta: dict | None = None,
        has_meta: bool = False,
        has_caption: bool = False,
    ) -> int | None:
        """插入图片；若已存在则返回 None（扫描时靠这个跳过）。"""
        try:
            cur = self._write(
                """
                INSERT INTO images
                    (dataset_id, path, filename, rel_path, width, height, size,
                     mtime, prompt, negative, meta, has_meta, has_caption, added_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    dataset_id, path, filename, rel_path, width, height, size,
                    mtime, prompt, negative,
                    json.dumps(meta or {}, ensure_ascii=False),
                    1 if has_meta else 0,
                    1 if has_caption else 0,
                    time.time(),
                ),
            )
            return int(cur.lastrowid)
        except sqlite3.IntegrityError:
            return None

    def update_image_meta(
        self,
        image_id: int,
        prompt: str,
        negative: str,
        meta: dict,
        width: int = 0,
        height: int = 0,
        has_caption: bool | None = None,
    ) -> None:
        self._write(
            """
            UPDATE images
               SET prompt = ?, negative = ?, meta = ?, has_meta = 1,
                   width = CASE WHEN ? > 0 THEN ? ELSE width END,
                   height = CASE WHEN ? > 0 THEN ? ELSE height END,
                   has_caption = CASE WHEN ? IS NULL THEN has_caption ELSE ? END
             WHERE id = ?
            """,
            (
                prompt, negative, json.dumps(meta, ensure_ascii=False),
                width, width, height, height,
                None if has_caption is None else (1 if has_caption else 0),
                None if has_caption is None else (1 if has_caption else 0),
                image_id,
            ),
        )
        if has_caption is not None:
            self._set_caption_only(image_id, has_caption)

    def _set_caption_only(self, image_id: int, has_caption: bool) -> None:
        self._write(
            "UPDATE images SET has_caption = ? WHERE id = ?",
            (1 if has_caption else 0, image_id),
        )

    def update_image_prompt(self, image_id: int, prompt: str, negative: str = "") -> None:
        self._write(
            "UPDATE images SET prompt = ?, negative = ? WHERE id = ?",
            (prompt, negative, image_id),
        )

    def refresh_overwritten_image(self, path: str) -> None:
        """A pixel edit strips embedded metadata, so discard the stale parsed copy."""
        target = paths.norm_path(path)
        row = self.find_by_path(str(target))
        if row is None:
            return
        stat = target.stat()
        self._write(
            """UPDATE images SET prompt='', negative='', meta='{}', has_meta=0,
               size=?, mtime=? WHERE id=?""",
            (stat.st_size, stat.st_mtime, int(row["id"])),
        )

    def set_image_status(self, image_id: int, status: str) -> None:
        self._write(
            "UPDATE images SET status = ?, tagged_at = ? WHERE id = ?",
            (status, time.time() if status == STATUS_TAGGED else None, image_id),
        )

    def refresh_status(self, image_id: int) -> None:
        """根据当前标签数量刷新状态。"""
        row = self._query_one(
            "SELECT COUNT(*) AS n FROM image_tags WHERE image_id = ?", (image_id,)
        )
        n = int(row["n"]) if row else 0
        self.set_image_status(image_id, STATUS_TAGGED if n else STATUS_PENDING)

    def delete_image(self, image_id: int) -> None:
        self._write("DELETE FROM images WHERE id = ?", (image_id,))

    def get_image(self, image_id: int) -> dict | None:
        row = self._query_one("SELECT * FROM images WHERE id = ?", (image_id,))
        if row is None:
            return None
        return self._image_row_to_dict(row)

    def record_preprocess_op(self, kind: str, detail: dict) -> int:
        cur = self._write(
            "INSERT INTO preprocess_ops(kind, detail_json, created_at) VALUES (?, ?, ?)",
            (kind, json.dumps(detail, ensure_ascii=False), time.time()),
        )
        return int(cur.lastrowid)

    def recent_preprocess_ops(self, limit: int = 30) -> list[dict]:
        return [
            {**dict(row), "detail": json.loads(row["detail_json"])}
            for row in self._query(
                "SELECT * FROM preprocess_ops ORDER BY id DESC LIMIT ?", (limit,)
            )
        ]

    def mark_preprocess_undone(self, op_id: int) -> None:
        self._write("UPDATE preprocess_ops SET undone_at = ? WHERE id = ?", (time.time(), op_id))

    def relocate_images(self, changes: list[tuple[int, str, bool]],
                        dataset_ids: dict[int, int] | None = None) -> None:
        """Keep image ids, tags and staged captions while paths change.

        The temporary SQL paths allow a.png -> b.png -> a.png cycles without
        violating the unique path index. A moved image gets a destination
        dataset; an in-place rename keeps its existing dataset.
        """
        if not changes:
            return
        import uuid
        with self._lock:
            con = self._conn
            try:
                con.execute("BEGIN IMMEDIATE")
                temporary = uuid.uuid4().hex
                for image_id, _target, _moved in changes:
                    con.execute(
                        "UPDATE images SET path = ? WHERE id = ?",
                        (f"__preprocess_{temporary}_{image_id}", image_id),
                    )
                for image_id, target, moved in changes:
                    target_path = paths.norm_path(target)
                    stat = target_path.stat()
                    row = con.execute(
                        "SELECT dataset_id FROM images WHERE id = ?", (image_id,)
                    ).fetchone()
                    if row is None:
                        raise ValueError(f"图库记录不存在：{image_id}")
                    dataset_id = int(row["dataset_id"])
                    if dataset_ids and image_id in dataset_ids:
                        dataset_id = dataset_ids[image_id]
                    elif moved:
                        folder = str(target_path.parent)
                        found = con.execute(
                            "SELECT id FROM datasets WHERE path = ?", (folder,)
                        ).fetchone()
                        if found:
                            dataset_id = int(found["id"])
                        else:
                            base = target_path.parent.name or "移动结果"
                            name = base
                            suffix = 2
                            while con.execute("SELECT 1 FROM datasets WHERE name = ?", (name,)).fetchone():
                                name = f"{base} ({suffix})"
                                suffix += 1
                            cur = con.execute(
                                "INSERT INTO datasets(name, path, note, created_at) VALUES (?, ?, ?, ?)",
                                (name, folder, "预处理移动分流", time.time()),
                            )
                            dataset_id = int(cur.lastrowid)
                    ds = con.execute(
                        "SELECT path FROM datasets WHERE id = ?", (dataset_id,)
                    ).fetchone()
                    root = paths.norm_path(ds["path"])
                    try:
                        rel_path = str(target_path.relative_to(root))
                    except ValueError:
                        rel_path = target_path.name
                    con.execute(
                        """UPDATE images SET dataset_id=?, path=?, filename=?, rel_path=?,
                           size=?, mtime=?, has_caption=? WHERE id=?""",
                        (dataset_id, str(target_path), target_path.name, rel_path,
                         stat.st_size, stat.st_mtime,
                         int(target_path.with_suffix(".txt").exists()), image_id),
                    )
                con.commit()
            except BaseException:
                con.rollback()
                raise

    def list_images(
        self,
        dataset_id: int | None = None,
        search: str = "",
        status: str = "",
        tag: str = "",
        order: str = "mtime_desc",
        limit: int = 5000,
        offset: int = 0,
    ) -> list[dict]:
        where: list[str] = []
        params: list[Any] = []

        if dataset_id:
            where.append("i.dataset_id = ?")
            params.append(dataset_id)
        if search:
            where.append("(i.filename LIKE ? OR i.prompt LIKE ? OR i.tags_text LIKE ?)")
            like = f"%{search}%"
            params.extend([like, like, like])
        if status in (STATUS_PENDING, STATUS_TAGGED, STATUS_SKIP):
            where.append("i.status = ?")
            params.append(status)
        if tag:
            where.append(
                "i.id IN (SELECT it.image_id FROM image_tags it "
                "JOIN tags t ON t.id = it.tag_id WHERE t.name = ?)"
            )
            params.append(tag)

        order_sql = {
            "mtime_desc": "i.mtime DESC",
            "mtime_asc": "i.mtime ASC",
            "name": "i.filename COLLATE NOCASE ASC",
            "added": "i.added_at DESC",
            "tags_desc": "tag_count DESC, i.mtime DESC",
            "tags_asc": "tag_count ASC, i.mtime DESC",
        }.get(order, "i.mtime DESC")

        sql = f"""
            SELECT i.*, d.name AS dataset_name,
                   (SELECT COUNT(*) FROM image_tags it WHERE it.image_id = i.id) AS tag_count
              FROM images i
              LEFT JOIN datasets d ON d.id = i.dataset_id
              {'WHERE ' + ' AND '.join(where) if where else ''}
             ORDER BY {order_sql}
             LIMIT ? OFFSET ?
        """
        params.extend([limit, offset])
        return [self._image_row_to_dict(r) for r in self._query(sql, params)]

    def count_images(self, dataset_id: int | None = None, status: str = "") -> int:
        where, params = [], []
        if dataset_id:
            where.append("dataset_id = ?")
            params.append(dataset_id)
        if status:
            where.append("status = ?")
            params.append(status)
        sql = "SELECT COUNT(*) AS n FROM images"
        if where:
            sql += " WHERE " + " AND ".join(where)
        row = self._query_one(sql, params)
        return int(row["n"]) if row else 0

    def recent_images(self, limit: int = 12) -> list[dict]:
        return self.list_images(order="added", limit=limit)

    @staticmethod
    def _image_row_to_dict(row: sqlite3.Row) -> dict:
        d = dict(row)
        try:
            d["meta"] = json.loads(d.get("meta") or "{}")
        except (json.JSONDecodeError, TypeError):
            d["meta"] = {}
        d.setdefault("tag_count", 0)
        return d

    # ------------------------------------------------------------------ #
    # 标签
    # ------------------------------------------------------------------ #
    def ensure_tag(self, name: str, category: str = "general", is_auto: bool = False) -> int:
        name = (name or "").strip()
        if not name:
            raise ValueError("empty tag")
        row = self._query_one("SELECT id FROM tags WHERE name = ?", (name,))
        if row:
            return int(row["id"])
        try:
            cur = self._write(
                "INSERT INTO tags (name, category, is_auto, created_at) "
                "VALUES (?, ?, ?, ?)",
                (name, category, 1 if is_auto else 0, time.time()),
            )
            return int(cur.lastrowid)
        except sqlite3.IntegrityError:
            row = self._query_one("SELECT id FROM tags WHERE name = ?", (name,))
            return int(row["id"]) if row else 0

    def tags_for_image(self, image_id: int) -> list[dict]:
        rows = self._query(
            """
            SELECT t.id, t.name, t.banned, t.is_auto, it.source
              FROM image_tags it
              JOIN tags t ON t.id = it.tag_id
             WHERE it.image_id = ?
             ORDER BY t.name COLLATE NOCASE
            """,
            (image_id,),
        )
        return [dict(r) for r in rows]

    def tag_names_for_image(self, image_id: int) -> list[str]:
        return [t["name"] for t in self.tags_for_image(image_id)]

    def set_image_tags(
        self, image_id: int, names: Iterable[str], source: str = "manual"
    ) -> None:
        """整体替换一张图的标签集合。"""
        clean: list[str] = []
        seen: set[str] = set()
        for raw in names:
            n = (raw or "").strip()
            if not n:
                continue
            key = n.lower()
            if key in seen:
                continue
            seen.add(key)
            clean.append(n)

        with self._lock:
            self._conn.execute("DELETE FROM image_tags WHERE image_id = ?", (image_id,))
            for name in clean:
                tag_id = self.ensure_tag(name, is_auto=(source == "auto"))
                self._conn.execute(
                    "INSERT OR IGNORE INTO image_tags (image_id, tag_id, source) "
                    "VALUES (?, ?, ?)",
                    (image_id, tag_id, source),
                )
            self._conn.commit()
        self._rebuild_tags_text(image_id)
        self.refresh_status(image_id)

    def add_tags(
        self, image_id: int, names: Iterable[str], source: str = "manual"
    ) -> list[str]:
        """追加标签，返回真正新增的名字。"""
        added: list[str] = []
        existing = {n.lower() for n in self.tag_names_for_image(image_id)}
        with self._lock:
            for raw in names:
                name = (raw or "").strip()
                if not name or name.lower() in existing:
                    continue
                tag_id = self.ensure_tag(name, is_auto=(source == "auto"))
                self._conn.execute(
                    "INSERT OR IGNORE INTO image_tags (image_id, tag_id, source) "
                    "VALUES (?, ?, ?)",
                    (image_id, tag_id, source),
                )
                existing.add(name.lower())
                added.append(name)
            self._conn.commit()
        if added:
            self._rebuild_tags_text(image_id)
            self.refresh_status(image_id)
        return added

    def remove_tag(self, image_id: int, name: str) -> None:
        self._write(
            """
            DELETE FROM image_tags
             WHERE image_id = ?
               AND tag_id IN (SELECT id FROM tags WHERE name = ?)
            """,
            (image_id, name),
        )
        self._rebuild_tags_text(image_id)
        self.refresh_status(image_id)

    def _rebuild_tags_text(self, image_id: int) -> None:
        names = self.tag_names_for_image(image_id)
        self._write(
            "UPDATE images SET tags_text = ? WHERE id = ?", (" ".join(names), image_id)
        )

    def rebuild_all_tags_text(self) -> int:
        rows = self._query("SELECT id FROM images")
        for r in rows:
            self._rebuild_tags_text(int(r["id"]))
        return len(rows)

    def list_tags(
        self,
        search: str = "",
        only_banned: bool = False,
        order: str = "count_desc",
        limit: int = 2000,
    ) -> list[dict]:
        where, params = [], []
        if search:
            where.append("t.name LIKE ?")
            params.append(f"%{search}%")
        if only_banned:
            where.append("t.banned = 1")

        order_sql = {
            "count_desc": "cnt DESC, t.name COLLATE NOCASE ASC",
            "count_asc": "cnt ASC, t.name COLLATE NOCASE ASC",
            "name": "t.name COLLATE NOCASE ASC",
            "recent": "t.created_at DESC",
        }.get(order, "cnt DESC, t.name COLLATE NOCASE ASC")

        sql = f"""
            SELECT t.id, t.name, t.category, t.banned, t.is_auto,
                   COUNT(it.image_id) AS cnt
              FROM tags t
              LEFT JOIN image_tags it ON it.tag_id = t.id
              {'WHERE ' + ' AND '.join(where) if where else ''}
             GROUP BY t.id
             ORDER BY {order_sql}
             LIMIT ?
        """
        params.append(limit)
        return [dict(r) for r in self._query(sql, params)]

    def banned_tag_names(self) -> list[str]:
        return [r["name"] for r in self._query("SELECT name FROM tags WHERE banned = 1")]

    def get_tag(self, tag_id: int) -> dict | None:
        """单个词条 + 使用次数。"""
        row = self._query_one(
            """
            SELECT t.id, t.name, t.category, t.banned, t.is_auto,
                   COUNT(it.image_id) AS cnt
              FROM tags t
              LEFT JOIN image_tags it ON it.tag_id = t.id
             WHERE t.id = ?
             GROUP BY t.id
            """,
            (tag_id,),
        )
        return dict(row) if row is not None else None

    def set_tag_banned(self, name: str, banned: bool) -> None:
        tag_id = self.ensure_tag(name)
        self._write("UPDATE tags SET banned = ? WHERE id = ?", (1 if banned else 0, tag_id))

    def set_tag_banned_by_id(self, tag_id: int, banned: bool) -> None:
        self._write("UPDATE tags SET banned = ? WHERE id = ?", (1 if banned else 0, tag_id))

    def rename_tag(self, tag_id: int, new_name: str) -> bool:
        new_name = new_name.strip()
        if not new_name:
            return False
        dup = self._query_one("SELECT id FROM tags WHERE name = ?", (new_name,))
        if dup and int(dup["id"]) != tag_id:
            # 目标已存在 -> 合并
            with self._lock:
                self._conn.execute(
                    "INSERT OR IGNORE INTO image_tags (image_id, tag_id, source) "
                    "SELECT image_id, ?, source FROM image_tags WHERE tag_id = ?",
                    (int(dup["id"]), tag_id),
                )
                self._conn.execute("DELETE FROM tags WHERE id = ?", (tag_id,))
                self._conn.commit()
            self.rebuild_all_tags_text()
            return True
        self._write("UPDATE tags SET name = ? WHERE id = ?", (new_name, tag_id))
        self.rebuild_all_tags_text()
        return True

    def delete_tag(self, tag_id: int) -> None:
        self._write("DELETE FROM tags WHERE id = ?", (tag_id,))
        self.rebuild_all_tags_text()

    def prune_orphan_tags(self) -> int:
        cur = self._write(
            """
            DELETE FROM tags
             WHERE id NOT IN (SELECT DISTINCT tag_id FROM image_tags)
               AND banned = 0
            """
        )
        return cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0

    def tag_frequency(self) -> list[tuple[str, int]]:
        rows = self._query(
            """
            SELECT t.name, COUNT(it.image_id) AS cnt
              FROM tags t
              JOIN image_tags it ON it.tag_id = t.id
             GROUP BY t.id ORDER BY cnt DESC
            """
        )
        return [(str(r["name"]), int(r["cnt"])) for r in rows]

    # ------------------------------------------------------------------ #
    # 统计
    # ------------------------------------------------------------------ #
    def stats(self, dataset_id: int | None = None) -> dict:
        total = self.count_images(dataset_id=dataset_id)
        tagged = self.count_images(status=STATUS_TAGGED, dataset_id=dataset_id)
        pending = self.count_images(status=STATUS_PENDING, dataset_id=dataset_id)

        def count_where(column: str) -> int:
            sql = f"SELECT COUNT(*) AS n FROM images WHERE {column} = 1"
            params: tuple[Any, ...] = ()
            if dataset_id is not None:
                sql += " AND dataset_id = ?"
                params = (dataset_id,)
            row = self._query_one(sql, params)
            return int(row["n"]) if row else 0

        datasets = int(
            (self._query_one("SELECT COUNT(*) AS n FROM datasets") or {"n": 0})["n"]
        )
        tag_terms = int(
            (self._query_one("SELECT COUNT(*) AS n FROM tags") or {"n": 0})["n"]
        )
        banned = int(
            (
                self._query_one("SELECT COUNT(*) AS n FROM tags WHERE banned = 1")
                or {"n": 0}
            )["n"]
        )
        return {
            "images": total,
            "tagged": tagged,
            "pending": pending,
            "datasets": datasets,
            "tag_terms": tag_terms,
            "banned": banned,
            "with_meta": count_where("has_meta"),
            "with_caption": count_where("has_caption"),
            "tagged_ratio": (tagged / total) if total else 0.0,
        }

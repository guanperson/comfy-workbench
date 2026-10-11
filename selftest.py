"""核心逻辑自检：不依赖 PySide6，可以直接跑。

    python selftest.py

它会造一个假的 ComfyUI 出图目录（含真实格式的 PNG 元数据），
然后把 扫描 → 解析 → 打标过滤 → 导出 全流程走一遍。
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import struct
import sys
import tempfile
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app import comfy_meta, tagger  # noqa: E402
from app.config import Settings  # noqa: E402
from app.db import Database  # noqa: E402
from app.exporter import export_dataset  # noqa: E402
from app.scanner import scan_folder  # noqa: E402

ROOT = Path(__file__).resolve().parent

PASSED = 0
FAILED = 0


@contextlib.contextmanager
def scratch_dir(name: str):
    """临时目录：先试系统 temp，不能写就退回项目里的 .selftest_tmp。"""
    candidates = []
    try:
        candidates.append(Path(tempfile.mkdtemp(prefix=f"cwb_{name}_")))
    except (OSError, PermissionError):
        pass
    candidates.append(ROOT / ".selftest_tmp" / f"{name}_{os.getpid()}")

    path: Path | None = None
    for candidate in candidates:
        try:
            candidate.mkdir(parents=True, exist_ok=True)
            probe = candidate / ".write_probe"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
            path = candidate
            break
        except OSError:
            continue
    if path is None:
        raise RuntimeError("找不到可写的临时目录")

    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


_ENV_BACKUP: dict[str, str | None] = {}


def _swap_env(mapping: dict[str, str | None]) -> None:
    """临时改环境变量；传 None 表示删除。第一次改动时记下原值。"""
    for key, value in mapping.items():
        if key not in _ENV_BACKUP:
            _ENV_BACKUP[key] = os.environ.get(key)
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


def check(name: str, condition: bool, detail: str = "") -> None:
    global PASSED, FAILED
    if condition:
        PASSED += 1
        print(f"  [OK] {name}")
    else:
        FAILED += 1
        print(f"  [XX] {name}" + (f"  -- {detail}" if detail else ""))


# --------------------------------------------------------------------------- #
# 造测试用的 PNG
# --------------------------------------------------------------------------- #

def _chunk(ctype: bytes, data: bytes) -> bytes:
    return (
        struct.pack(">I", len(data))
        + ctype
        + data
        + struct.pack(">I", zlib.crc32(ctype + data) & 0xFFFFFFFF)
    )


def make_png(
    path: Path,
    width: int = 8,
    height: int = 8,
    texts: dict | None = None,
    use_itxt: bool = False,
) -> None:
    """手写一个最小合法 PNG，并塞进 tEXt / iTXt 文本块。"""
    scanline = b"\x00" + bytes([120, 150, 200] * width)
    raw = scanline * height
    out = [b"\x89PNG\r\n\x1a\n"]
    out.append(_chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)))
    for key, value in (texts or {}).items():
        if use_itxt:
            payload = (
                key.encode("latin-1") + b"\x00"
                + b"\x00\x00"           # 未压缩，方法 0
                + b"\x00"               # language tag
                + b"\x00"               # translated keyword
                + value.encode("utf-8")
            )
            out.append(_chunk(b"iTXt", payload))
        else:
            out.append(
                _chunk(b"tEXt", key.encode("latin-1") + b"\x00" + value.encode("latin-1"))
            )
    out.append(_chunk(b"IDAT", zlib.compress(raw)))
    out.append(_chunk(b"IEND", b""))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"".join(out))


# 一个典型的 ComfyUI API 格式工作流：CheckPoint → LoRA → CLIPText → KSampler
API_PROMPT = {
    "1": {
        "class_type": "CheckpointLoaderSimple",
        "inputs": {"ckpt_name": "dreamshaper_8_v1.safetensors"},
    },
    "2": {
        "class_type": "LoraLoader",
        "inputs": {
            "lora_name": "detail_slider_v2.safetensors",
            "strength_model": 0.7,
            "strength_clip": 0.6,
            "model": ["1", 0],
            "clip": ["1", 1],
        },
    },
    "3": {
        "class_type": "CLIPTextEncode",
        "inputs": {
            "text": "1girl, solo, long_hair, silver_eyes, school_uniform, smile",
            "clip": ["2", 1],
        },
    },
    "4": {
        "class_type": "CLIPTextEncode",
        "inputs": {"text": "lowres, worst_quality, watermark", "clip": ["2", 1]},
    },
    "5": {
        "class_type": "EmptyLatentImage",
        "inputs": {"width": 832, "height": 1216, "batch_size": 1},
    },
    "6": {
        "class_type": "KSampler",
        "inputs": {
            "seed": 123456789,
            "steps": 28,
            "cfg": 7.0,
            "sampler_name": "dpmpp_2m_sde",
            "scheduler": "karras",
            "denoise": 1.0,
            "model": ["2", 0],
            "positive": ["3", 0],
            "negative": ["4", 0],
            "latent_image": ["5", 0],
        },
    },
    "7": {
        "class_type": "SaveImage",
        "inputs": {"filename_prefix": "SD15/portrait", "images": ["6", 0]},
    },
}

# conditioning 中间夹了一层 ConditioningCombine（很常见的写法）
COND_CHAINED = {
    "1": {
        "class_type": "CheckpointLoaderSimple",
        "inputs": {"ckpt_name": "anything_v5.ckpt"},
    },
    "10": {
        "class_type": "LoraLoader",
        "inputs": {
            "lora_name": "char_xl.safetensors",
            "strength_model": 1.0,
            "model": ["1", 0],
            "clip": ["1", 1],
        },
    },
    "20": {
        "class_type": "CLIPTextEncode",
        "inputs": {
            "text": "masterpiece, best quality, cherry_blossom, hanami",
            "clip": ["10", 1],
        },
    },
    "21": {
        "class_type": "ConditioningCombine",
        "inputs": {"conditioning_1": ["20", 0], "conditioning_2": ["20", 0]},
    },
    "30": {
        "class_type": "KSampler",
        "inputs": {
            "seed": 42,
            "steps": 20,
            "cfg": 5.5,
            "sampler_name": "euler",
            "scheduler": "normal",
            "model": ["10", 0],
            "positive": ["21", 0],
            "negative": ["20", 0],
            "latent_image": ["5", 0],
        },
    },
}


# --------------------------------------------------------------------------- #
def test_metadata_parsing() -> None:
    print("\n[1] ComfyUI metadata parsing")
    with scratch_dir("meta") as root:
        a = root / "with_meta.png"
        make_png(a, texts={"prompt": json.dumps(API_PROMPT)})
        info = comfy_meta.extract(a)
        check("positive prompt", "1girl" in info["prompt"], info["prompt"])
        check("negative prompt", "worst_quality" in info["negative"], info["negative"])
        check("has_meta true", info["has_meta"] is True)
        check("source png:prompt", info["source"] == "png:prompt", info["source"])

        meta = info["meta"]
        check(
            "checkpoint parsed",
            meta.get("checkpoint_short") == "dreamshaper_8_v1",
            str(meta.get("checkpoint")),
        )
        check("lora parsed", len(meta.get("loras") or []) == 1, str(meta.get("loras")))
        check(
            "lora strength",
            (meta.get("loras") or [{}])[0].get("strength_model") == 0.7,
            str(meta.get("loras")),
        )
        check(
            "lora short name (trigger word)",
            (meta.get("loras") or [{}])[0].get("short") == "detail_slider_v2",
        )
        check("steps", meta.get("steps") == 28, str(meta.get("steps")))
        check("sampler", meta.get("sampler") == "dpmpp_2m_sde", str(meta.get("sampler")))
        check("scheduler", meta.get("scheduler") == "karras", str(meta.get("scheduler")))
        check("seed", meta.get("seed") == 123456789, str(meta.get("seed")))
        check(
            "resolution",
            (meta.get("width"), meta.get("height")) == (832, 1216),
            str(meta.get("width")) + "x" + str(meta.get("height")),
        )

        # 穿过 ConditioningCombine 回溯
        b = root / "chained.png"
        make_png(b, texts={"prompt": json.dumps(COND_CHAINED)})
        info2 = comfy_meta.extract(b)
        check(
            "trace through ConditioningCombine",
            "cherry_blossom" in info2["prompt"],
            info2["prompt"],
        )
        check(
            "chained checkpoint",
            info2["meta"].get("checkpoint_short") == "anything_v5",
        )

        # iTXt + UTF-8（中文提示词在 ComfyUI 里就是这个格式）
        c = root / "chinese.png"
        chinese_graph = {
            "1": {
                "class_type": "CLIPTextEncode",
                "inputs": {"text": "一位银发少女, 樱花, 微笑"},
            },
            "2": {
                "class_type": "KSampler",
                "inputs": {"positive": ["1", 0], "negative": ["1", 0]},
            },
        }
        make_png(
            c,
            texts={"prompt": json.dumps(chinese_graph, ensure_ascii=False)},
            use_itxt=True,
        )
        info3 = comfy_meta.extract(c)
        check("iTXt UTF-8 chinese prompt", "银发少女" in info3["prompt"], info3["prompt"])

        # 无元数据
        d = root / "plain.png"
        make_png(d)
        info4 = comfy_meta.extract(d)
        check("no metadata is safe", info4["has_meta"] is False and info4["prompt"] == "")

        # 损坏的 JSON
        e = root / "broken.png"
        make_png(e, texts={"prompt": "{not valid json"})
        info5 = comfy_meta.extract(e)
        check("broken json does not raise", isinstance(info5, dict))

        # 不存在的文件
        info6 = comfy_meta.extract(root / "nope.png")
        check("missing file is safe", isinstance(info6, dict) and info6["prompt"] == "")


def test_prompt_to_tags() -> None:
    print("\n[2] prompt -> tags")
    parts = comfy_meta.split_prompt_to_tags("1girl, solo,   long hair , ,smile; 1girl")
    check(
        "split and dedupe",
        parts == ["1girl", "solo", "long hair", "smile"],
        str(parts),
    )
    # 1girl / 2boys / 4k 不能被当成序号吃掉
    numeric = comfy_meta.split_prompt_to_tags("1girl, 2boys, 4k, 8k, 1other")
    check(
        "numeric tags preserved",
        numeric == ["1girl", "2boys", "4k", "8k", "1other"],
        str(numeric),
    )
    check(
        "list numbering stripped",
        comfy_meta.split_prompt_to_tags("1. smile\n2. 1girl")
        == ["smile", "1girl"],
        str(comfy_meta.split_prompt_to_tags("1. smile\n2. 1girl")),
    )
    check("empty prompt", comfy_meta.split_prompt_to_tags("") == [])


def test_response_parsing() -> None:
    print("\n[3] model response parsing")
    cases = [
        ('["1girl", "smile", "long_hair"]', ["1girl", "smile", "long_hair"]),
        ('```json\n["a", "b"]\n```', ["a", "b"]),
        ('{"tags": ["x", "y"]}', ["x", "y"]),
        ('{"labels": ["p", "q"]}', ["p", "q"]),
        ("a, b, c", ["a", "b", "c"]),
        ('[{"tag": "solo"}, {"tag": "smile"}]', ["solo", "smile"]),
    ]
    for raw, expect in cases:
        got = tagger.parse_tag_payload(raw)
        check(f"parse {raw[:26]!r}", got == expect, f"got {got}")

    banned = ["watermark", "low_quality"]
    filtered = tagger.filter_banned(["1girl", "watermark", "low_quality", "smile"], banned)
    check("ban filter", filtered == ["1girl", "smile"], str(filtered))
    check(
        "ban substring fallback",
        "watermark_text" not in tagger.filter_banned(["watermark_text"], banned),
    )
    check("max_tags cap", len(tagger.parse_tag_payload('["a","b","c","d"]', 2)) == 2)


def test_end_to_end() -> None:
    print("\n[4] scan -> tag -> export")
    with scratch_dir("e2e") as tmp_path:
        output = tmp_path / "ComfyUI_output"
        output.mkdir()
        make_png(output / "img_00001.png", texts={"prompt": json.dumps(API_PROMPT)})
        make_png(output / "img_00002.png", texts={"prompt": json.dumps(COND_CHAINED)})
        make_png(output / "img_00003.png", texts={"prompt": json.dumps(API_PROMPT)})
        make_png(output / "img_00004.png")  # 无元数据
        make_png(output / "nested" / "img_00005.png",
                 texts={"prompt": json.dumps(API_PROMPT)})  # 子目录
        (output / "notes.txt").write_text("not an image", encoding="utf-8")

        db = Database(tmp_path / "test.db")
        dataset_id = db.upsert_dataset("测试集", str(output))

        result = scan_folder(db, dataset_id, output, parse_meta=True)
        check("found 5 images", result.scanned == 5, f"scanned={result.scanned}")
        check("all inserted", result.added == 5, f"added={result.added}")
        check("4 with metadata", result.with_meta == 4, f"with_meta={result.with_meta}")
        check("ignored non-image", db.count_images(dataset_id) == 5)

        images = db.list_images(dataset_id=dataset_id, order="mtime_asc")
        first = images[0]
        check("prompt stored", "1girl" in str(first["prompt"]), str(first["prompt"]))
        check(
            "checkpoint stored",
            (first["meta"].get("checkpoint_short") == "dreamshaper_8_v1"),
            str(first["meta"].get("checkpoint_short")),
        )

        # 手动打标
        tags_for_first = [
            "1girl", "solo", "long_hair", "silver_eyes", "school_uniform", "smile",
        ]
        added = db.add_tags(int(first["id"]), tags_for_first, source="manual")
        check("added 6 tags", len(added) == 6, str(added))
        db.add_tags(int(images[1]["id"]), ["masterpiece", "cherry_blossom"], source="auto")
        db.add_tags(int(images[2]["id"]), ["plain_image"], source="manual")

        check("status -> tagged", db.get_image(int(first["id"]))["status"] == "tagged")
        check(
            "status stays pending",
            db.get_image(int(images[3]["id"]))["status"] == "pending",
        )

        again = db.add_tags(int(first["id"]), ["1girl", "1GIRL"], source="manual")
        check("case-insensitive dedupe", again == [], str(again))

        db.set_tag_banned("solo", True)
        check("banned tag recorded", "solo" in db.banned_tag_names())

        stats = db.stats()
        check("stats images", stats["images"] == 5, str(stats["images"]))
        check("stats tagged", stats["tagged"] == 3, str(stats["tagged"]))
        check("stats banned", stats["banned"] == 1, str(stats["banned"]))

        # 标签搜索
        found = db.list_images(dataset_id=dataset_id, search="plain_image")
        check("search by tag text", len(found) == 1, str(len(found)))
        found_prompt = db.list_images(dataset_id=dataset_id, search="cherry_blossom")
        check("search by prompt", len(found_prompt) >= 1, str(len(found_prompt)))
        by_name = db.list_images(dataset_id=dataset_id, search="img_00002")
        check("search by filename", len(by_name) == 1, str(len(by_name)))
        tag_filtered = db.list_images(dataset_id=dataset_id, tag="cherry_blossom")
        check("filter by exact tag", len(tag_filtered) == 1, str(len(tag_filtered)))

        # 重命名 + 合并
        tag_id = db.ensure_tag("oldname")
        db.rename_tag(tag_id, "newname")
        check("rename tag", db.get_tag(tag_id)["name"] == "newname", str(db.get_tag(tag_id)))

        merge_src = db.ensure_tag("merge_me")
        db.add_tags(int(images[0]["id"]), ["merge_me"])
        db.rename_tag(merge_src, "1girl")
        names = db.tag_names_for_image(int(images[0]["id"]))
        check("merge into existing tag", "merge_me" not in names and "1girl" in names, str(names))

        # 导出
        settings = Settings()
        settings.export_use_ckpt = True
        settings.export_trigger = "my_trigger"
        export_result = export_dataset(
            db, dataset_id, settings, root=tmp_path / "export_out"
        )
        check("export dir created", export_result.target.is_dir(), str(export_result.target))
        check(
            "exported 3 images",
            export_result.images == 3,
            f"images={export_result.images} errors={export_result.errors}",
        )
        check("wrote 3 txt", export_result.txt_files == 3, str(export_result.txt_files))
        check("jsonl exists", (export_result.target / "dataset.jsonl").is_file())
        check("tags.txt exists", (export_result.target / "tags.txt").is_file())
        check("captions.csv exists", (export_result.target / "captions.csv").is_file())

        jsonl_lines = [
            json.loads(line)
            for line in (export_result.target / "dataset.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
            if line.strip()
        ]
        check("jsonl has 3 rows", len(jsonl_lines) == 3, str(len(jsonl_lines)))
        with_ckpt = next(
            (r for r in jsonl_lines if r["checkpoint"] == "dreamshaper_8_v1"), None
        )
        check("row with checkpoint found", with_ckpt is not None)
        if with_ckpt:
            check(
                "trigger word first",
                with_ckpt["caption"].startswith("my_trigger"),
                with_ckpt["caption"],
            )
            check(
                "lora name in caption",
                "detail_slider_v2" in with_ckpt["caption"],
                with_ckpt["caption"],
            )
            check("original prompt kept", "1girl" in str(with_ckpt["prompt"]))
            check("loras in jsonl", len(with_ckpt["loras"]) == 1, str(with_ckpt["loras"]))

        freq = dict(_read_freq(export_result.target / "tags.txt"))
        check("freq table", freq.get("1girl") == 1, str(freq))
        check(
            "txt content matches jsonl",
            any(
                "my_trigger" in p.read_text(encoding="utf-8")
                for p in export_result.target.glob("*.txt")
                if p.name != "tags.txt"
            ),
        )

        # 重复扫描不应重复入库
        again_scan = scan_folder(db, dataset_id, output, parse_meta=True)
        check("rescan adds nothing", again_scan.added == 0, f"added={again_scan.added}")
        check("rescan skips unchanged", again_scan.skipped >= 4, f"skipped={again_scan.skipped}")

        # 删文件后重扫应清理
        (output / "img_00004.png").unlink()
        third = scan_folder(db, dataset_id, output, parse_meta=True)
        check("missing file pruned", third.removed == 1, f"removed={third.removed}")

        # 数据集统计
        rows = db.list_datasets()
        check("dataset progress", rows[0]["progress"] == 3 / 4, str(rows[0]["progress"]))

        db.close()


def _read_freq(path: Path) -> list[tuple[str, int]]:
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        name, _, count = line.partition("\t")
        rows.append((name, int(count)))
    return rows


def test_data_dir() -> None:
    """数据目录解析：环境变量优先，兜底固定，绝不用临时目录。

    背景：Windows 上 %TEMP% 不可写时 ``tempfile.gettempdir()`` 会退化成
    「当前工作目录」，于是数据跟着启动位置乱跑，看着像数据丢了。
    所以持久数据不能落在临时目录里。
    """
    from app import paths  # noqa: PLC0415

    def _is_within(child: Path, parent: Path) -> bool:
        return parent == child or parent in child.parents

    original = os.environ.get("COMFY_WORKBENCH_HOME")

    with scratch_dir("paths") as tmp:
        # 1) 环境变量优先级最高
        forced = Path(tmp) / "forced-home"
        os.environ["COMFY_WORKBENCH_HOME"] = str(forced)
        paths.LAST_RESOLUTION.clear()
        chosen = paths.data_dir()
        check("COMFY_WORKBENCH_HOME 优先", chosen == forced, str(chosen))
        check("环境变量目录被真的建出来", forced.is_dir(), str(forced))

        # 2) 不设环境变量时的默认候选：必须包含 <程序目录>/data，
        #    且不能落在临时目录里（临时目录会被系统清理，数据就没了）
        os.environ.pop("COMFY_WORKBENCH_HOME", None)
        candidates = paths.data_dir_candidates()
        check(
            "兜底包含 <程序目录>/data",
            any(c.name == "data" and c.parent == paths.app_root() for c in candidates),
            str(candidates),
        )
        temp_dir = Path(tempfile.gettempdir()).resolve()
        leaked = [
            str(c) for c in candidates
            if c.exists() and _is_within(c.resolve(), temp_dir)
        ]
        check("默认候选里不含临时目录", not leaked, str(leaked))

        # 3) 候选是真的会被试写，而不是只看存不存在
        os.environ["COMFY_WORKBENCH_HOME"] = str(forced)
        check("候选是可写目录", paths.data_dir().is_dir(), str(forced))

    # 还原环境变量
    if original is None:
        os.environ.pop("COMFY_WORKBENCH_HOME", None)
    else:
        os.environ["COMFY_WORKBENCH_HOME"] = original


def test_db_temp_independence() -> None:
    """SQLite 排序不能依赖 %TEMP%。

    这条是真机上踩出来的：受控文件夹访问会拦住新打包的 exe 写
    %TEMP% / %APPDATA% / 用户目录。SQLite 默认把排序溢出写到临时文件，
    临时目录不可写时报的就是 ``OperationalError: unable to open database file``，
    而且只在数据量大到需要落盘时才炸 —— 小数据集完全正常，特别难查。
    所以数据层强制 ``temp_store = MEMORY``。
    """
    from app.db import Database  # noqa: PLC0415

    saved = {k: os.environ.get(k) for k in ("TEMP", "TMP", "TMPDIR")}
    with scratch_dir("dbtmp") as tmp:
        # 把临时目录指到一个不存在、也建不出来的地方
        bogus = str(Path(tmp) / "no" / "such" / "temp")
        os.environ["TEMP"] = os.environ["TMP"] = os.environ["TMPDIR"] = bogus
        try:
            db = Database(Path(tmp) / "probe.db")
            try:
                health = db.health()
                check(
                    "temp_store = MEMORY",
                    int(health["temp_store"]) == 2,
                    f"temp_store={health['temp_store']}",
                )
                check(
                    "journal_mode = wal",
                    str(health["journal_mode"]).lower() == "wal",
                    str(health["journal_mode"]),
                )

                # 大排序 + TEMP 表：默认配置下这两个都要落盘临时文件
                with db._lock:
                    db._conn.execute("CREATE TEMP TABLE probe_big(v TEXT)")
                    db._conn.executemany(
                        "INSERT INTO probe_big(v) VALUES(?)",
                        [(f"{i:06d}-" + "x" * 40,) for i in range(50_000)],
                    )
                    db._conn.commit()
                rows = db._query("SELECT v FROM probe_big ORDER BY v")
                check("大排序不依赖 %TEMP%", len(rows) == 50_000, f"rows={len(rows)}")
                check("健康检查正常", bool(health["ok"]), str(health.get("error")))

                # 连接被外部搞坏之后要能自愈
                db.reopen()
                after = db.health()
                check("重开连接可用", bool(after["ok"]), str(after.get("error")))
                check("重开次数被记下", after["reconnects"] >= 1, str(after["reconnects"]))
            finally:
                db.close()
        finally:
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value


def make_png_utf8(path: Path, texts: dict, width: int = 8, height: int = 8) -> None:
    """和 make_png 一样，但 tEXt 的值按 UTF-8 编码。

    NAI 写的就是 tEXt + Comment，值是 UTF-8 的 JSON。
    （make_png 用 latin-1，遇到非 ASCII 会炸，所以单独留一个。）
    """
    scanline = b"\x00" + bytes([120, 150, 200] * width)
    raw = scanline * height
    out = [b"\x89PNG\r\n\x1a\n"]
    out.append(_chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)))
    for key, value in texts.items():
        out.append(
            _chunk(b"tEXt", key.encode("latin-1") + b"\x00" + value.encode("utf-8"))
        )
    out.append(_chunk(b"IDAT", zlib.compress(raw)))
    out.append(_chunk(b"IEND", b""))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"".join(out))


def test_nai_metadata() -> None:
    """NAI 元数据解析：权重语法、转义、下划线、粘连词、多角色分组。"""
    print("\n[NAI 元数据解析]")
    from app import comfy_meta, nai_meta

    nai_obj = {
        "prompt": "1girl, solo",
        "negative_prompt": "bad anatomy",
        "sampler": "k_euler_ancestral",
        "steps": 28,
        "source": "NovelAI",
        "v4_prompt": {
            "caption": {
                "base_caption": "1girl, solo, outdoors",
                "char_captions": [
                    {
                        "char_caption": (
                            "girl, solo, gigantic breasts::cheerleader, "
                            "lemuen\\(arknights\\), blue_hair, {{smile}}, "
                            "0::bad_hand::, 1.3::long hair::"
                        )
                    },
                    {"char_caption": "boy, solo, penis, faceless male"},
                ],
            }
        },
    }
    with scratch_dir("nai") as tmp:
        png = tmp / "nai.png"
        make_png_utf8(png, {"Comment": json.dumps(nai_obj)})

        check("识别为 NAI", nai_meta.has_nai_metadata(png))
        parsed = comfy_meta.extract(png, parser="auto")
        check("auto 解析出 NAI", parsed.get("source") == "nai", str(parsed.get("source")))
        check("解析出元数据", bool(parsed.get("has_meta")))

        tags = comfy_meta.quick_tags(png, max_tags=0)
        check("0 权重标签被丢弃", not any("bad hand" in t or "bad_hand" in t for t in tags),
              str(tags))
        check("权重语法已剥离", "long hair" in tags and "smile" in tags, str(tags))
        check("转义反斜杠已去掉", "lemuen(arknights)" in tags, str(tags))
        check("下划线转空格", "blue hair" in tags, str(tags))
        check("粘连尺寸词被切开",
              "gigantic breasts" in tags and "cheerleader" in tags, str(tags))
        check("男性角色组的词也在", "penis" in tags and "faceless male" in tags, str(tags))
        check("没有残留花括号", not any("{" in t or "}" in t for t in tags), str(tags))

        # 强制 ComfyUI 时不该认出 NAI
        forced = comfy_meta.extract(png, parser="comfyui")
        check("强制 comfyui 时不认 NAI", not forced.get("has_meta"),
              str(forced.get("source")))


def test_captions_io() -> None:
    """配套 txt 读写：分隔符、去重、原子写、备份、diff。"""
    print("\n[配套 txt 读写]")
    from app import captions

    with scratch_dir("caps") as tmp:
        img = tmp / "a.png"
        make_png(img, texts={"Comment": "{}"})
        txt = captions.caption_path_for(img)
        check("txt 路径同名同目录", txt == tmp / "a.txt", str(txt))
        check("一开始没有 txt", not captions.has_caption(img))

        ok, note = captions.write_caption(img, ["1girl", "solo", "long hair"],
                                          make_backup=True)
        check("写入成功", ok, note)
        check("落盘内容正确",
              captions.read_caption(img) == ["1girl", "solo", "long hair"],
              str(captions.read_caption(img)))
        check("has_caption 为真", captions.has_caption(img))
        check("没有留下临时文件", not list(tmp.glob("*.tmp*")), str(list(tmp.glob("*.tmp*"))))

        # 去重 + 空白清理 + 全角逗号
        caps = captions.split_caption("1girl，solo,  solo ,, long hair 、 blue eyes")
        check("全角逗号和顿号都能分", "1girl" in caps and "blue eyes" in caps, str(caps))
        check("空项被丢掉", "" not in caps, str(caps))
        deduped = captions.dedupe(["1girl", "1girl", " solo ", "SOLO"])
        check("去重忽略大小写和空白", len(deduped) == 2, str(deduped))

        # 备份
        captions.write_caption(img, ["changed"], make_backup=True)
        backups = list(tmp.glob("*.bak"))
        check("覆盖时生成了备份", len(backups) >= 1, f"{len(backups)} 个")
        # 注意：read_caption_text 收的是「图片路径」，会自己推出 txt 名。
        # 备份文件要直接读内容。
        backup_texts = [b.read_text(encoding="utf-8").strip() for b in backups]
        check("备份保留了旧内容",
              "1girl, solo, long hair" in backup_texts, str(backup_texts))

        # diff
        edits = captions.CaptionEdit(
            image_id=1, image_path=str(img),
            original=["1girl", "solo"], current=["1girl", "solo", "new tag"],
        )
        check("diff 认出新增", edits.added == ["new tag"], str(edits.added))
        check("diff 认出删除",
              captions.CaptionEdit(image_id=1, image_path=str(img),
                                   original=["a", "b"], current=["a"]).removed == ["b"])


def test_tag_isolation() -> None:
    """打标台「查看所有该标签图片」：按标签筛图 + 删除/替换。"""
    print("\n[标签抽离与批量删除/替换]")
    from app import captions
    from app.db import Database
    from app.scanner import scan_folder

    with scratch_dir("isolate") as tmp:
        root = tmp / "imgs"
        root.mkdir()
        plans = {
            "a.png": ["1girl", "solo", "oiled body"],
            "b.png": ["1girl", "solo", "oiled body", "blue eyes"],
            "c.png": ["1girl", "oiled body"],
            "d.png": ["1girl", "long hair"],
            "e.png": ["boy", "solo"],
        }
        for name, tags in plans.items():
            png = root / name
            make_png(png, texts={"Comment": "{}"})
            captions.write_caption(png, tags, make_backup=False)

        db = Database(tmp / "iso.db")
        ds_id = db.upsert_dataset("iso", str(root))
        scan_folder(db, ds_id, str(root), parse_meta=True)
        check("扫描到 5 张", db.stats(dataset_id=ds_id)["images"] == 5,
              str(db.stats(dataset_id=ds_id)["images"]))
        check("5 张都有 txt", len(db.caption_image_ids(ds_id)) == 5,
              str(len(db.caption_image_ids(ds_id))))

        # ---- 按标签找图 ----
        oiled = db.image_ids_with_tag("oiled body", dataset_id=ds_id)
        check("oiled body 命中 3 张", len(oiled) == 3, str(len(oiled)))
        check("solo 命中 3 张",
              len(db.image_ids_with_tag("solo", dataset_id=ds_id)) == 3,
              str(len(db.image_ids_with_tag("solo", dataset_id=ds_id))))
        check("不存在的标签返回空",
              db.image_ids_with_tag("nope nope", dataset_id=ds_id) == [])
        check("空标签返回空", db.image_ids_with_tag("", dataset_id=ds_id) == [])
        # 大小写不敏感
        check("大小写不敏感",
              len(db.image_ids_with_tag("OILED BODY", dataset_id=ds_id)) == 3)
        # 部分匹配不算命中
        check("不做部分匹配",
              db.image_ids_with_tag("oiled", dataset_id=ds_id) == [])

        # ---- 已有标签清单（替换框的下拉用）----
        alltags = dict(db.all_caption_tags(dataset_id=ds_id))
        check("清单里有 oiled body", "oiled body" in alltags, str(sorted(alltags)[:6]))
        check("oiled body 计数为 3", alltags.get("oiled body") == 3,
              str(alltags.get("oiled body")))

        # ---- 只删选中的那部分 ----
        ids = sorted(oiled)[:2]
        affected = 0
        for image_id in ids:
            edit = db.caption_edit(image_id, db.get_image(image_id)["path"])
            remaining = [t for t in edit.current if t != "oiled body"]
            db.set_caption_tags(image_id, edit.image_path, remaining)
            affected += 1
        check("删了 2 张", affected == 2, str(affected))
        left = db.image_ids_with_tag("oiled body", dataset_id=ds_id)
        check("只剩 1 张带 oiled body", len(left) == 1, str(len(left)))
        # 没被选中的那张不受影响
        check("未选中的图没被动",
              len(db.image_ids_with_tag("1girl", dataset_id=ds_id)) == 4,
              str(len(db.image_ids_with_tag("1girl", dataset_id=ds_id))))

        # ---- 替换 ----
        target_id = left[0]
        edit = db.caption_edit(target_id, db.get_image(target_id)["path"])
        updated = ["oiled_body" if t == "oiled body" else t for t in edit.current]
        db.set_caption_tags(target_id, edit.image_path, updated)
        check("替换后原标签没了",
              db.image_ids_with_tag("oiled body", dataset_id=ds_id) == [])
        check("替换后新标签有了",
              len(db.image_ids_with_tag("oiled_body", dataset_id=ds_id)) == 1)

        # ---- 暂存没保存前，磁盘 txt 不该变 ----
        path_c = root / "c.png"
        check("暂存期间磁盘 txt 仍是旧的",
              "oiled body" in captions.read_caption(path_c),
              str(captions.read_caption(path_c)))
        check("暂存改动计数", db.pending_caption_count() >= 3,
              str(db.pending_caption_count()))

        # ---- 保存后落盘 ----
        db.save_captions(db.caption_edits(), make_backup=True)
        check("保存后磁盘 txt 更新了",
              "oiled body" not in captions.read_caption(path_c),
              str(captions.read_caption(path_c)))

        # ---- 替换要顺带去重 ----
        p_d = root / "d.png"
        captions.write_caption(p_d, ["1girl", "long hair"], make_backup=False)
        db2 = Database(tmp / "iso2.db")
        ds2 = db2.upsert_dataset("iso2", str(root))
        scan_folder(db2, ds2, str(root), parse_meta=True)
        rows = db2.list_images(dataset_id=ds2)
        d_id = next(int(r["id"]) for r in rows if r["filename"] == "d.png")
        edit = db2.caption_edit(d_id, str(p_d))
        # 把 long hair 换成已有的 1girl → 不该出现两个 1girl
        merged = []
        for t in edit.current:
            new = "1girl" if t == "long hair" else t
            if new not in merged:
                merged.append(new)
        db2.set_caption_tags(d_id, str(p_d), merged)
        after = db2.caption_edit(d_id, str(p_d)).current
        check("替换不会产生重复标签", after.count("1girl") == 1, str(after))
        db2.close()

        db.close()


def test_caption_generation() -> None:
    """生成标注：元数据优先、已有 txt 不覆盖、按策略写入。"""
    print("\n[生成标注]")
    from app import caption_gen, captions, comfy_meta, paths
    from app.db import Database
    from app.scanner import scan_folder

    with scratch_dir("gen") as tmp:
        imgs = tmp / "imgs"
        imgs.mkdir(parents=True)
        nai_obj = {
            "prompt": "1girl, solo",
            "v4_prompt": {"caption": {"base_caption": "1girl, solo, cherry blossoms",
                                      "char_captions": []}},
        }
        make_png_utf8(imgs / "with_meta.png", {"Comment": json.dumps(nai_obj)})
        make_png(imgs / "no_meta.png", texts={})
        make_png(imgs / "has.txt.png", texts={"Comment": json.dumps(nai_obj)})
        (imgs / "has.txt.txt").write_text("keep me", encoding="utf-8")

        _swap_env({"COMFY_WORKBENCH_HOME": str(tmp / "data")})
        paths.reset_data_dir_cache()
        db = Database(paths.db_path())
        dataset_id = db.upsert_dataset("gen", str(imgs))
        scan_folder(db, dataset_id, str(imgs), parse_meta=True)

        check("扫描记下了 has_caption",
              db.stats()["with_caption"] == 1, str(db.stats()["with_caption"]))

        missing = list(caption_gen.iter_missing_caption_images(db))
        check("列出 2 张缺 txt", len(missing) == 2, str(len(missing)))

        # 这轮不带 WD14：有元数据的写，没元数据的跳过（这是说好的行为）
        report = caption_gen.generate_for_images(
            db, missing, parser="auto", use_wd14=False,
            policy=caption_gen.POLICY_SKIP,
        )
        check("写入了 1 个 txt（只有带元数据的那张）", report.written == 1, report.summary())
        check("1 个来自元数据", report.from_meta == 1, report.summary())
        check("无元数据的被跳过", report.empty == 1, report.summary())

        check("已有 txt 没被覆盖",
              captions.read_caption_text(imgs / "has.txt.png").strip() == "keep me",
              captions.read_caption_text(imgs / "has.txt.png"))
        gen = captions.read_caption(imgs / "with_meta.png")
        check("生成内容来自元数据",
              "cherry blossoms" in gen and "1girl" in gen, str(gen))
        check("无元数据的图没有 txt",
              not captions.has_caption(imgs / "no_meta.png"))

        # 统计
        ids = [int(r["id"]) for r in db._query("SELECT id FROM images")]
        counts = dict(db.caption_tag_counts(ids))
        # has.txt.png 的内容是 "keep me"，所以 1girl 只来自新生成的那张
        check("统计里 1girl 出现在 1 张", counts.get("1girl") == 1, str(counts))
        check("已有 txt 的标签也进统计", counts.get("keep me") == 1, str(counts))
        check("分母用的有标注张数", db.caption_applied_count(ids) == 2,
              str(db.caption_applied_count(ids)))
        db.close()
        _swap_env({"COMFY_WORKBENCH_HOME": None})


def test_tag_conversion() -> None:
    """标签写法转换：角色词互转、下划线转空格、不误伤普通标签。"""
    print("\n[标签写法转换]")
    from app import captions

    # ---- 哪些算角色词（anima / Danbooru 带命名空间括号的，物件也算）----
    role_like = [
        "swire \\(arknights\\)",
        "goldenglow \\(arknights\\)",
        "skadi the corrupting heart \\(arknights\\)",
        "male doctor \\(arknights\\)",
        "formidable \\(azur lane\\)",
        "slime \\(substance\\)",
        "blue_eyes_(series)",
        "swire (arknights)",
    ]
    for tag in role_like:
        check(f"认作角色词：{tag}", captions.looks_like_role_tag(tag))

    # ---- 普通标签绝不能被当成角色词 ----
    plain_tags = [
        "oil448style", "oiled", "oiled body", "1girl", "long hair",
        "huge breasts", "solo focus", "clothed female nude male",
        "no humans", "wuthering_waves", "male_rover_", "heart", "bow",
    ]
    for tag in plain_tags:
        check(f"不认作角色词：{tag}", not captions.looks_like_role_tag(tag))

    # ---- 转成「空格 + 裸括号」----
    # 注意：这个转换只管括号写法，不动下划线 —— 下划线是另一个按钮的活。
    expected_plain = {
        "swire \\(arknights\\)": "swire (arknights)",
        "skadi the corrupting heart \\(arknights\\)": "skadi the corrupting heart (arknights)",
        "slime \\(substance\\)": "slime (substance)",
        "swire_(arknights)": "swire (arknights)",
    }
    for src, want in expected_plain.items():
        got = captions.to_plain_role_tag(src)
        check(f"转裸括号：{src[:34]}", got == want, f"{got!r} != {want!r}")

    # ---- 普通标签必须原样返回 ----
    for tag in plain_tags:
        check(f"普通标签不动：{tag}",
              captions.to_plain_role_tag(tag) == tag,
              captions.to_plain_role_tag(tag))

    # ---- 往返要稳定 ----
    for tag in ("swire \\(arknights\\)", "slime \\(substance\\)",
                "skadi the corrupting heart \\(arknights\\)"):
        round_trip = captions.to_escaped_role_tag(captions.to_plain_role_tag(tag))
        check(f"往返稳定：{tag[:30]}", round_trip == tag, f"{round_trip!r} != {tag!r}")

    # ---- 下划线转空格 ----
    under_cases = {
        "wuthering_waves": "wuthering waves",
        "blue_eyes": "blue eyes",
        "male_rover_": "male rover",
        "a_b_c": "a b c",
        "double__under": "double under",
    }
    for src, want in under_cases.items():
        got = captions.letter_underscores_to_spaces(src)
        check(f"下划线转空格：{src}", got == want, f"{got!r} != {want!r}")

    # 角色词的命名空间也一起转（用户确认的行为）
    got = captions.letter_underscores_to_spaces("male_rover_\\(wuthering_waves\\)")
    check("角色词的下划线也转", got == "male rover (wuthering waves)", repr(got))

    # 没有下划线的标签不动
    for tag in ("oiled body", "1girl", "long hair"):
        check(f"无下划线不动：{tag}",
              captions.letter_underscores_to_spaces(tag) == tag)

    # ---- 转换后撞车要去重 ----
    mixed = ["swire \\(arknights\\)", "swire (arknights)", "swire_(arknights)", "oiled"]
    out, changed = captions.convert_tags(
        mixed, captions.to_plain_role_tag, filename="x.png"
    )
    check("三种写法归并成一个", out == ["swire (arknights)", "oiled"], str(out))
    # 只有 2 个真的变了：裸括号那个本来就是目标写法，oiled 不是角色词
    check("改动数只算真变了的", changed == 2, str(changed))

    # 去重时保留第一次出现的写法
    out2, _ = captions.convert_tags(
        ["a_b", "a b"], captions.letter_underscores_to_spaces
    )
    check("转换后重复只留一个", out2 == ["a b"], str(out2))

    # ---- 畸形输入不能崩 ----
    for weird in ("", "   ", "(worst quality)", "\\(weird\\)",
                  "name \\(ns\\) extra", "_leading", "trailing_"):
        try:
            captions.to_plain_role_tag(weird)
            captions.letter_underscores_to_spaces(weird)
            ok = True
        except Exception as exc:  # noqa: BLE001
            ok = False
            print(f"     {weird!r} 抛了 {type(exc).__name__}: {exc}")
        check(f"畸形输入不崩：{weird!r}", ok)

    # 没有角色名的畸形标签要原样保留，不能被拼坏
    check("畸形转义标签原样保留",
          captions.to_plain_role_tag("\\(weird\\)") == "\\(weird\\)",
          captions.to_plain_role_tag("\\(weird\\)"))

    # ---- 双命名空间（变体名 + 系列），真实数据里出现过 ----
    double = "zara \\(poolside coincidence\\) \\(azur lane\\)"
    check("双命名空间认作角色词", captions.looks_like_role_tag(double))
    check("双命名空间拆出两个",
          captions.role_namespaces(double) ==
          ("zara", ["poolside coincidence", "azur lane"]),
          str(captions.role_namespaces(double)))
    check("双命名空间转裸括号",
          captions.to_plain_role_tag(double) ==
          "zara (poolside coincidence) (azur lane)",
          captions.to_plain_role_tag(double))
    check("双命名空间往返一致",
          captions.to_escaped_role_tag(captions.to_plain_role_tag(double)) == double,
          captions.to_escaped_role_tag(captions.to_plain_role_tag(double)))

    # ---- 主推方向：统一成下划线写法（anima / Danbooru 规范）----
    underscore_cases = {
        "swire \\(arknights\\)": "swire_(arknights)",
        "swire (arknights)": "swire_(arknights)",
        "swire_(arknights)": "swire_(arknights)",
        "slime \\(substance\\)": "slime_(substance)",
        "blue_eyes_(series)": "blue_eyes_(series)",
        "skadi the corrupting heart \\(arknights\\)":
            "skadi_the_corrupting_heart_(arknights)",
        "zara \\(poolside coincidence\\) \\(azur lane\\)":
            "zara_(poolside_coincidence)_(azur_lane)",
    }
    for src, want in underscore_cases.items():
        got = captions.to_underscore_role_tag(src)
        check(f"转下划线：{src[:36]}", got == want, f"{got!r} != {want!r}")

    # 三种写法互相转换要收敛到同一个结果
    variants = ["swire \\(arknights\\)", "swire (arknights)", "swire_(arknights)"]
    results = {captions.to_underscore_role_tag(v) for v in variants}
    check("三种写法收敛成一个", results == {"swire_(arknights)"}, str(results))

    # 普通标签不能被动下划线
    for tag in ("oiled body", "long hair", "1girl", "no humans",
                "(worst quality)", "a_b_c"):
        check(f"转下划线不动普通标签：{tag}",
              captions.to_underscore_role_tag(tag) == tag,
              captions.to_underscore_role_tag(tag))

    # 单命名空间的往返（回归）
    for tag in ("swire \\(arknights\\)", "slime \\(substance\\)",
                "skadi the corrupting heart \\(arknights\\)"):
        check(f"往返稳定：{tag[:30]}",
              captions.to_escaped_role_tag(captions.to_plain_role_tag(tag)) == tag)

def test_escape_convention() -> None:
    r"""448style 转义写法：文档《448style_角色名格式与括号转义》的约定。

    目标形态是 `musashi \(皮肤名\) \(作品名\)`（转义），不是裸括号。
    判定规则：标签里有括号，且括号内容含作品名，才算角色名。
    """
    print("\n[448style 转义写法]")
    from app import captions

    # ---- 文档里的目标形态 ----
    doc_cases = {
        "musashi (the purple rabbit's marksmanship) (azur lane)":
            "musashi \\(the purple rabbit's marksmanship\\) \\(azur lane\\)",
        "musashi \\(the purple rabbit's marksmanship\\) \\(azur lane\\)":
            "musashi \\(the purple rabbit's marksmanship\\) \\(azur lane\\)",
        "sirius (azur lane)": "sirius \\(azur lane\\)",
        "sirius \\(azur lane\\)": "sirius \\(azur lane\\)",
        "lemuen (arknights)": "lemuen \\(arknights\\)",
        "lemuen \\(arknights\\)": "lemuen \\(arknights\\)",
        "firefly (honkai: star rail)": "firefly \\(honkai: star rail\\)",
        "swire_(arknights)": "swire \\(arknights\\)",
        "musashi_(azur_lane)": "musashi \\(azur lane\\)",
        "zara_(poolside_coincidence)_(azur_lane)":
            "zara \\(poolside coincidence\\) \\(azur lane\\)",
    }
    for src, want in doc_cases.items():
        got = captions.to_escaped_role_tag(src)
        check(f"转义：{src[:44]}", got == want, f"{got!r} != {want!r}")

    # ---- 文档里「不转义」的那几类必须原样不动 ----
    untouched = [
        "arknights", "azur lane", "breasts", "oiled body", "1girl",
        "(worst quality)", "(词:1.3)", "score_9",
    ]
    for tag in untouched:
        got = captions.to_escaped_role_tag(tag)
        check(f"不该动：{tag}", got == tag, repr(got))

    # ---- 转义后不该再有裸括号（裸括号会被 ComfyUI 当权重剥掉）----
    for tag in ("musashi (the purple rabbit's marksmanship) (azur lane)",
                "sirius (azur lane)"):
        escaped = captions.to_escaped_role_tag(tag)
        check(f"转义后没有裸括号：{escaped[:34]}",
              "(" not in escaped.replace("\\(", ""), escaped)
        check(f"转义后括号都带反斜杠：{escaped[:34]}",
              escaped.count("\\(") == escaped.count(")"), escaped)

    # ---- 三种写法都收敛到转义形态 ----
    variants = ["swire \\(arknights\\)", "swire (arknights)", "swire_(arknights)"]
    results = {captions.to_escaped_role_tag(v) for v in variants}
    check("三种写法收敛到转义形态",
          results == {"swire \\(arknights\\)"}, str(results))

    # ---- 往返稳定 ----
    for tag in ("swire \\(arknights\\)", "sirius \\(azur lane\\)",
                "musashi \\(the purple rabbit's marksmanship\\) \\(azur lane\\)",
                "slime \\(substance\\)"):
        back = captions.to_escaped_role_tag(captions.to_plain_role_tag(tag))
        check(f"转义往返稳定：{tag[:36]}", back == tag, f"{back!r} != {tag!r}")

    # ---- 下划线转换也归一命名空间里的下划线 ----
    check("转下划线归一命名空间",
          captions.to_underscore_role_tag("musashi (azur lane)")
          == "musashi_(azur_lane)",
          captions.to_underscore_role_tag("musashi (azur lane)"))

    # ---- 修饰语括号绝不能被当成角色名（真实数据里的误判）----
    # 判据：括号内容得像作品名。这些括号里是 larger / cosplay / sexual…
    modifier_tags = [
        "alternate breast size (larger)",
        "kaname madoka (cosplay)",
        "miki sayaka (magical girl)",
        "prisma illya (cosplay)",
        "teamwork (sexual)",
        "tomoe mami (magical girl)",
    ]
    for tag in modifier_tags:
        got = captions.to_escaped_role_tag(tag)
        check(f"修饰语不转义：{tag}", got == tag, repr(got))
        check(f"  不算作品名：{tag}",
              not captions.looks_like_work_namespace(
                  captions.role_namespaces(tag)[1][0]),
              str(captions.role_namespaces(tag)))

    # ---- 作品名 / 物件命名空间要认出来 ----
    for ns in ("azur lane", "arknights", "honkai: star rail",
               "wuthering waves", "elden ring", "substance"):
        check(f"认出命名空间：{ns}", captions.looks_like_work_namespace(ns))
    for ns in ("larger", "cosplay", "magical girl", "sexual", ""):
        check(f"不认作命名空间：{ns or '(空)'}",
              not captions.looks_like_work_namespace(ns))

    # ---- 物件命名空间也转义（用户要求：物件按角色处理）----
    check("物件命名空间转义",
          captions.to_escaped_role_tag("slime (substance)")
          == "slime \\(substance\\)",
          captions.to_escaped_role_tag("slime (substance)"))

    # ---- 真实数据里的漏空格情形 ----
    missing_space = {
        "sirius\\(azur lane\\)": "sirius \\(azur lane\\)",
        "ganj-i-sawai\\(azur lane\\)": "ganj-i-sawai \\(azur lane\\)",
        "typhon\\(arknights\\)": "typhon \\(arknights\\)",
    }
    for src, want in missing_space.items():
        got = captions.to_escaped_role_tag(src)
        check(f"补上缺失的空格：{src[:34]}", got == want, f"{got!r} != {want!r}")


def test_nai_tag_cleaning() -> None:
    """NAI 提示词清洗：剥权重、丢画师词、丢负权重、切开粘连。"""
    print("\n[NAI 提示词清洗]")
    import re as _re

    from app import nai_meta

    # ---- 画师词整条丢掉（真实数据里的写法）----
    artist_cases = [
        "1.3::Artists:meion::",
        "1.7::artist:binggong asylum::",
        "2.1::artitst:yunsang::",       # 真实数据里就有这个拼错的
        "1::artists:yushe_quetzalli::",
        "by2.5::oving::",                # by 和权重粘在一起
        "by 3::obui ::",
        "by 1.34::masaki (ekakiningen)::",
        "by swd3e2",
        "by shexyo",
    ]
    for case in artist_cases:
        check(f"画师词丢掉：{case[:34]}", nai_meta.norm_tag(case) == "",
              repr(nai_meta.norm_tag(case)))
        check(f"  认出是画师词：{case[:28]}", nai_meta.is_artist_tag(case))

    # ---- 负权重 / 零权重整条丢掉 ----
    for case in ("-5.1::artist collaboration::", "-5::flat_color::", "-2::2coma::",
                 "0::word::", "0.0::word::"):
        check(f"负/零权重丢掉：{case}", nai_meta.norm_tag(case) == "",
              repr(nai_meta.norm_tag(case)))

    # ---- 正权重剥掉数字留词 ----
    weight_cases = {
        "1.34::kuroi suna::": "kuroi suna",
        "2::big breasts::": "big breasts",
        "{{word}}": "word",
        "(word:1.2)": "word",
    }
    for src, want in weight_cases.items():
        got = nai_meta.norm_tag(src)
        check(f"剥权重：{src}", got == want, f"{got!r} != {want!r}")

    # ---- 普通词原样保留 ----
    for tag in ("very aesthetic", "masterpiece", "no text", "1girl",
                "girl", "solo", "kuroi suna"):
        check(f"普通词保留：{tag}", nai_meta.norm_tag(tag) == tag,
              repr(nai_meta.norm_tag(tag)))

    # ---- 粘连要切开（真实数据里的坑）----
    stuck_cases = [
        ("2::gigantic breasts,breasts apart::autumn leaves",
         ["gigantic breasts", "breasts apart", "autumn leaves"]),
        ("no text,skindentation", ["no text", "skindentation"]),
        ("2::oiled body,oiled,huge nipples::",
         ["oiled body", "oiled", "huge nipples"]),
    ]
    for src, want in stuck_cases:
        got = nai_meta.split_tags(src)
        check(f"粘连切开：{src[:36]}", got == want, f"{got} != {want}")

    # ---- 一段里挤了两坨带权重的 ----
    got = nai_meta.split_tags("1girl 2::big breasts:: 1.5::long hair::")
    check("两坨权重都切出来", got == ["1girl", "big breasts", "long hair"], str(got))

    # ---- 真实的一整条 char_caption ----
    real = (
        "girl, solo, ruanmei \\(honkai: star rail\\),naughty face, half-closed eyes,,, "
        "upper body, 2::oiled body,oiled,huge nipples,puffy nipples:: "
        "2::gigantic breasts,hanging breasts,breasts apart::autumn leaves, outdoors,,nude,nipples,"
        "2::bent over, hands on knees,::from side,"
    )
    tags = nai_meta.split_tags(real)
    for want in ("girl", "solo", "ruanmei (honkai: star rail)", "naughty face",
                 "oiled body", "puffy nipples", "gigantic breasts",
                 "breasts apart", "autumn leaves", "bent over", "from side"):
        check(f"真实 caption 切出：{want}", want in tags, str(tags))
    glued = [t for t in tags if _re.search(r"[a-z][A-Z]", t)]
    check("真实 caption 没有粘连", not glued, str(glued))
    check("真实 caption 没有权重残留", not any("::" in t for t in tags), str(tags))

    # ---- 反斜杠转义清洗后应去掉 ----
    check("转义去掉",
          nai_meta.norm_tag("swire \\(arknights\\)") == "swire (arknights)",
          repr(nai_meta.norm_tag("swire \\(arknights\\)")))


def test_trash() -> None:
    """垃圾桶：删图片连 txt 一起移走、不真删、能还原、桶内结构不丢。"""
    print("\n[垃圾桶]")
    from app import paths, trash

    with scratch_dir("trash") as tmp:
        home = tmp / "home"
        home.mkdir()
        _swap_env({"COMFY_WORKBENCH_HOME": str(home)})
        paths.reset_data_dir_cache()
        try:
            # ---- 造几张真图（带配套 txt）----
            src = tmp / "2_dsh"
            src.mkdir()
            imgs = []
            for i in (1, 2, 3):
                p = src / f"gen_{i:03d}.png"
                make_png(p, texts={"Comment": "{}"})
                p.with_suffix(".txt").write_text(f"tag{i}, solo", encoding="utf-8")
                imgs.append(p)
            # 一张没有 txt 的
            bare = src / "gen_004.png"
            make_png(bare, texts={"Comment": "{}"})

            items = [(str(p), "dsh 448style") for p in imgs]
            items.append((str(bare), "dsh 448style"))
            # keep_structure 现在由设置项 trash_keep_structure 控制（默认平铺）。
            # 这段自检专门验「分层」那条能力，所以显式传 True。
            moved, errors = trash.trash_images(
                items, note="自检", keep_structure=True
            )
            check("移动不报错", not errors, str(errors))
            check("图片 + 配套 txt 都移走了", moved == 7, f"moved={moved}（期望 7）")

            check("原图片不在了", not any(p.exists() for p in imgs))
            check("原 txt 不在了",
                  not any(p.with_suffix(".txt").exists() for p in imgs))
            check("没 txt 的那张也移走了", not bare.exists())

            stats = trash.stats()
            check("垃圾桶里 4 张图", stats["images"] == 4, str(stats))
            check("桶里 7 个文件", stats["files"] == 7, str(stats))
            check("桶大小 > 0", stats["bytes"] > 0, str(stats["bytes"]))

            # ---- 桶内保留目录结构 ----
            root = trash.trash_root()
            landed = sorted(
                p.relative_to(root).as_posix()
                for p in root.rglob("*")
                if p.is_file() and p.name not in ("说明.txt", "index.jsonl")
            )
            check("桶内按数据集/目录分层",
                  all("/" in name for name in landed), str(landed))
            check("桶内保留了数据集名",
                  any("dsh 448style" in name for name in landed), str(landed))
            check("桶内有说明文件", (root / "说明.txt").exists())
            check("说明文件有内容", (root / "说明.txt").read_text(
                encoding="utf-8").strip() != "")

            # ---- 自定义目录里绝不能塞说明文件/索引 ----
            mine = tmp / "我的废图"
            mine.mkdir()
            trash.set_trash_dir(str(mine))
            extra = tmp / "extra.png"
            make_png(extra, texts={"Comment": "{}"})
            trash.trash_images([(str(extra), "ds")], note="进自定义目录")
            check("自定义目录里没有说明文件",
                  not (mine / "说明.txt").exists(),
                  str(sorted(p.name for p in mine.iterdir())))
            check("自定义目录里没有索引文件",
                  not (mine / "index.jsonl").exists(),
                  str(sorted(p.name for p in mine.iterdir())))
            check("自定义目录里只有那张图",
                  [p.name for p in mine.iterdir()] == ["extra.png"],
                  str(sorted(p.name for p in mine.iterdir())))
            trash.set_trash_dir("")

            # ---- 全是移动，没有真删 ----
            check("源目录已经空了",
                  not any(src.glob("*.png")) and not any(src.glob("*.txt")),
                  str(list(src.iterdir())))

            # ---- 还原：图片连 txt 一起回来 ----
            entries = trash.list_trashed()
            check("索引里有 4 条图片记录", len(entries) == 4, str(len(entries)))
            one = next(e for e in entries if "gen_002" in e.name)
            done, errs = trash.restore([one])
            check("还原不报错", not errs, str(errs))
            check("图片和它的 txt 一起回来", done == 2, f"done={done}（期望 2）")
            check("gen_002 回来了", (src / "gen_002.png").exists())
            check("gen_002 的 txt 也回来了",
                  (src / "gen_002.txt").exists())
            check("没碰到别的图", not (src / "gen_001.png").exists())
            check("索引减到 3 条", len(trash.list_trashed()) == 3,
                  str(len(trash.list_trashed())))

            # ---- 还原时不覆盖原位置已有的同名文件 ----
            clash = src / "gen_003.png"
            clash.write_bytes(b"NEW FILE")
            rest = trash.list_trashed()
            done2, errs2 = trash.restore(rest)
            check("批量还原不报错", not errs2, str(errs2))
            names = sorted(p.name for p in src.iterdir())
            check("不覆盖已有的同名文件",
                  "gen_003_restored2.png" in names, str(names))
            check("原来的 gen_003 内容没被动", clash.read_bytes() == b"NEW FILE")
            check("全部还原后桶空了", trash.stats()["files"] == 0,
                  str(trash.stats()))

            # ---- 清空垃圾桶是真删，且只删桶里的 ----
            keep = tmp / "外面.png"
            make_png(keep, texts={"Comment": "{}"})
            trash.trash_images([(str(keep), "ds")], note="待清空")
            check("清空前桶里有东西", trash.stats()["files"] == 1)
            removed, errs3 = trash.empty_trash()
            check("清空不报错", not errs3, str(errs3))
            check("清空删掉了文件", removed == 1, str(removed))
            check("清空后桶空了", trash.stats()["files"] == 0)
            check("说明文件留着", (root / "说明.txt").exists())

            # ---- 文件不在了也不崩 ----
            gone = tmp / "不存在.png"
            moved2, errs4 = trash.trash_images([(str(gone), "ds")])
            check("删不存在的文件报错不崩", moved2 == 0 and bool(errs4), str(errs4))

        finally:
            for key in list(_ENV_BACKUP):
                value = _ENV_BACKUP.pop(key)
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
            paths.reset_data_dir_cache()


def test_sampler_picking() -> None:
    """正向/反向该顺着连线找，不能被 KSamplerSelect / FaceDetailer 带偏。

    实盘老图（E:\\output原）里踩过的坑：SAMPLER_NODES 收了 KSamplerSelect
    （只有 sampler_name，没有正负输入）和 FaceDetailer（脸部细化，连的是
    自己那条支线），取 samplers[-1] 正好挑中它们，于是把负面读成了正向。
    """
    print("\n[采样器挑选 / 提示词回溯]")
    from app import comfy_meta

    POS = "1girl, solo, masterpiece, long_hair, looking at viewer, smile"
    NEG = "worst quality, lowres, bad anatomy, jpeg artifacts, watermark, signature"

    def graph(nodes: dict) -> dict:
        return nodes

    # ---- 1. KSamplerSelect 排在 SamplerCustom 后面，不能赢 ----
    g = graph({
        "4": {"class_type": "CheckpointLoaderSimple",
              "inputs": {"ckpt_name": "a.safetensors"}},
        "7": {"class_type": "CLIPTextEncode", "inputs": {"text": NEG}},
        "62": {"class_type": "BNK_CLIPTextEncodeAdvanced", "inputs": {"text": POS}},
        "75": {"class_type": "SamplerCustom",
               "inputs": {"positive": ["62", 0], "negative": ["7", 0]}},
        "81": {"class_type": "KSamplerSelect",
               "inputs": {"sampler_name": "euler_ancestral"}},
    })
    r = comfy_meta.parse_api_prompt(g)
    check("KSamplerSelect 不抢正向", r["prompt"] == POS, repr(r["prompt"][:60]))
    check("KSamplerSelect 不抢反向", r["negative"] == NEG, repr(r["negative"][:60]))

    # ---- 2. 脸部细化器排在后面，也不能赢 ----
    g2 = graph({
        "7": {"class_type": "CLIPTextEncode", "inputs": {"text": NEG}},
        "62": {"class_type": "BNK_CLIPTextEncodeAdvanced", "inputs": {"text": POS}},
        "75": {"class_type": "SamplerCustom",
               "inputs": {"positive": ["62", 0], "negative": ["7", 0]}},
        "120": {"class_type": "FaceDetailer",
                "inputs": {"positive": ["128", 0], "negative": ["7", 0]}},
        "128": {"class_type": "CLIPTextEncode", "inputs": {"text": ""}},
    })
    r2 = comfy_meta.parse_api_prompt(g2)
    check("细化器空链不抢正向", r2["prompt"] == POS, repr(r2["prompt"][:60]))

    # ---- 3. text 是连线（BNK 接 WeiLin 系节点）也要能穿透 ----
    WEILIN_POS = "utage \\(arknights\\), 1girl, solo, breasts, open mouth"
    g3 = graph({
        "7": {"class_type": "CLIPTextEncode", "inputs": {"text": NEG}},
        "60": {"class_type": "Power Lora Loader (rgthree)", "inputs": {}},
        "62": {"class_type": "BNK_CLIPTextEncodeAdvanced",
               "inputs": {"text": ["127", 0], "clip": ["60", 1]}},
        "75": {"class_type": "SamplerCustom",
               "inputs": {"positive": ["62", 0], "negative": ["7", 0]}},
        "127": {"class_type": "WeiLinComfyUIPromptAllInOneGreat",
                "inputs": {"positive": WEILIN_POS, "neg": None}},
    })
    r3 = comfy_meta.parse_api_prompt(g3)
    check("穿透到 WeiLin 的 positive 字段", r3["prompt"] == WEILIN_POS,
          repr(r3["prompt"][:60]))
    check("WeiLin 图反向仍对", r3["negative"] == NEG, repr(r3["negative"][:60]))

    # ---- 4. 完全没有可用采样器时，兜底也不能把负面当正向 ----
    g4 = graph({
        "7": {"class_type": "CLIPTextEncode", "inputs": {"text": NEG}},
        "62": {"class_type": "CLIPTextEncode", "inputs": {"text": POS}},
    })
    r4 = comfy_meta.parse_api_prompt(g4)
    check("兜底挑正向（避开负面特征）", r4["prompt"] == POS, repr(r4["prompt"][:60]))

    # ---- 5. 负面打分本身 ----
    check("负面打分：质量贬义词堆", comfy_meta._negative_score(NEG) >= 4,
          str(comfy_meta._negative_score(NEG)))
    check("负面打分：safe_neg 开头加权", comfy_meta._negative_score("safe_neg, ai-generated") >= 3,
          str(comfy_meta._negative_score("safe_neg, ai-generated")))
    check("负面打分：正常正向接近 0", comfy_meta._negative_score(POS) <= 1,
          str(comfy_meta._negative_score(POS)))

    # ---- 6. 真实老图（有就跑，没有跳过）----
    real_dir = Path("E:/output原/测试读取")
    real_cases = {
        "ComfyUI_00095_.png": ("alens,", "miaomiao harem"),
        "ComfyUI_06893_.png": ("utage \\(arknights\\)", "safe_neg"),
    }
    for name, (pos_head, neg_head) in real_cases.items():
        path = real_dir / name
        if not path.exists():
            print(f"  跳过真实文件（不存在）: {name}")
            continue
        got = comfy_meta.extract(str(path))
        check(f"真实老图 {name} 正向读对",
              str(got["prompt"]).startswith(pos_head),
              repr(str(got["prompt"])[:60]))
        check(f"真实老图 {name} 反向读对",
              str(got["negative"]).startswith(neg_head),
              repr(str(got["negative"])[:60]))


def test_wd14_config() -> None:
    """WD14 的配置开关、屏蔽词解析、模型发现。

    不加载 1.2GB 模型（加载要 2-3 秒，还会占 GPU），只测纯逻辑部分。
    """
    print("\n[WD14 本地打标 / 配置]")
    from app import wd14

    # ---- 1. TaggerConfig 默认值 ----
    cfg = wd14.TaggerConfig()
    check("默认阈值 0.35", abs(cfg.threshold - 0.35) < 1e-9, str(cfg.threshold))
    check("默认角色阈值 0.6", abs(cfg.character_threshold - 0.6) < 1e-9, str(cfg.character_threshold))
    check("默认不限标签数", cfg.max_tags == 0, str(cfg.max_tags))
    check("默认优先 GPU", cfg.use_gpu is True, str(cfg.use_gpu))
    check("默认批量 8", cfg.batch_size == 8, str(cfg.batch_size))
    check("默认下划线转空格", cfg.replace_underscore is True, str(cfg.replace_underscore))

    # ---- 2. default_config(**overrides) ----
    cfg2 = wd14.default_config(threshold=0.5, use_gpu=False, max_tags=20)
    check("default_config 覆盖阈值", abs(cfg2.threshold - 0.5) < 1e-9, str(cfg2.threshold))
    check("default_config 覆盖 GPU", cfg2.use_gpu is False, str(cfg2.use_gpu))
    check("default_config 覆盖标签数", cfg2.max_tags == 20, str(cfg2.max_tags))
    check("default_config 不改原默认", abs(wd14.TaggerConfig().threshold - 0.35) < 1e-9)

    # ---- 3. 屏蔽词：下划线和空格两种写法都要认 ----
    # 逻辑内联在 Wd14Tagger._postprocess 里，所以直接调真实后处理，
    # 用假 scores 驱动，不需要加载模型。
    tagger = wd14.Wd14Tagger(wd14.default_config(
        threshold=0.1, character_threshold=0.1, replace_underscore=True,
    ))
    tagger.tags = ["general", "1girl", "watermark", "artist_name", "style", "big_breasts"]
    tagger.config.undesired = {"watermark", "artist name"}

    def post(scores):
        import numpy as np
        return tagger._postprocess(np.array(scores, dtype="float32"))  # noqa: SLF001

    all_high = [0.9] * 6
    out = post(all_high)
    check("屏蔽词去掉下划线写法", "artist name" not in out,
          f"用户输入 'artist name'，模型输出 'artist_name'，必须能过滤: {out}")
    check("屏蔽词去掉原样写法", "watermark" not in out, str(out))
    check("屏蔽词不误伤 style", "style" in out, str(out))
    check("正常词保留", "1girl" in out and "big breasts" in out, str(out))
    check("下划线转成空格", "big breasts" in out and "big_breasts" not in out, str(out))

    # 阈值过滤：1girl（index 1）给 0.05，低于 threshold=0.1，应该被滤掉
    out2 = post([0.9, 0.05, 0.9, 0.9, 0.9, 0.9])
    check("低于阈值的词被滤掉", "1girl" not in out2, str(out2))
    check("高于阈值的词留着", "general" in out2, str(out2))

    # max_tags 截断
    tagger.config.undesired = set()
    tagger.config.max_tags = 2
    out3 = post(all_high)
    check("max_tags 生效", len(out3) == 2, str(out3))

    # replace_underscore 关掉时保留下划线
    tagger.config.replace_underscore = False
    tagger.config.max_tags = 0
    out4 = post(all_high)
    check("关掉转换后保留下划线", "big_breasts" in out4, str(out4))

    # ---- 4. 真实模型发现（有就算，没有就跳过）----
    root = wd14.models_root()
    models = wd14.list_models()
    print(f"  模型目录: {root}")
    print(f"  找到模型: {len(models)} 个")
    picked = wd14.pick_default_model()
    if picked is None:
        print("  跳过模型检查（没找到模型，不影响其它功能）")
    else:
        check("默认模型能选中", Path(picked).is_file(), str(picked))
        check("默认模型有配套 csv", Path(picked).with_suffix(".csv").is_file(),
              str(Path(picked).with_suffix(".csv")))
        check("默认模型名是 wd-eva02-large-tagger-v3",
              Path(picked).stem == wd14.DEFAULT_MODEL, Path(picked).stem)
        # 词表：行数 = 模型输出列数，对不上会整体错位
        tags = wd14._load_tags(Path(picked).with_suffix(".csv"))  # noqa: SLF001
        check("词表读得到", len(tags) > 8000, f"{len(tags)} 个词")
        check("词表首词是 general", tags and tags[0].strip() == "general",
              tags[0] if tags else "(空)")

    # ---- 5. 配置能塞进 Settings 并存下来 ----
    with scratch_dir("wd14cfg") as tmp:
        _swap_env({"COMFY_WORKBENCH_HOME": str(tmp)})
        try:
            from app import paths
            from app.config import load_settings, save_settings
            paths.reset_data_dir_cache()
            s = Settings()
            s.wd14_enabled = True
            s.wd14_threshold = 0.42
            s.wd14_max_tags = 33
            s.wd14_undesired = "watermark, signature"
            s.wd14_use_gpu = False
            check("WD14 设置存得进文件", save_settings(s) is True)
            s2 = load_settings()
            check("WD14 开关存得住", s2.wd14_enabled is True)
            check("WD14 阈值存得住", abs(s2.wd14_threshold - 0.42) < 1e-9, str(s2.wd14_threshold))
            check("WD14 标签数存得住", s2.wd14_max_tags == 33, str(s2.wd14_max_tags))
            check("WD14 屏蔽词存得住", s2.wd14_undesired == "watermark, signature",
                  repr(s2.wd14_undesired))
            check("WD14 GPU 开关存得住", s2.wd14_use_gpu is False, str(s2.wd14_use_gpu))
        finally:
            for key, old in _ENV_BACKUP.items():
                if old is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = old
            _ENV_BACKUP.clear()
            from app import paths
            paths.reset_data_dir_cache()


def test_wd14_fallback() -> None:
    """生成标注时：有元数据用元数据，没元数据的才用 WD14 兜底。

    WD14 关掉时，没元数据的图应该老实报「无内容」，而不是被跳过或写空文件。
    """
    print("\n[WD14 本地打标 / 生成兜底]")
    from app import caption_gen, captions, paths, wd14

    real = Path("E:/output原/测试读取/ComfyUI_06893_.png")
    if not real.exists():
        print("  跳过（真实素材不存在，需要 E:\\output原\\测试读取 里的图）")
        return

    with scratch_dir("wd14fb") as tmp:
        _swap_env({"COMFY_WORKBENCH_HOME": str(tmp)})
        try:
            paths.reset_data_dir_cache()
            from app.db import Database
            from app.scanner import scan_folder

            src = tmp / "mixed"
            src.mkdir()

            # 有元数据的：直接拷，PNG tEXt 块保留
            shutil.copy2(real, src / "有元数据.png")

            # 没元数据的：用 PIL 重存一次，元数据就丢了
            from PIL import Image
            with Image.open(real) as im:
                im = im.convert("RGB")
                w, h = im.size
                im.crop((0, 0, w // 2, h)).resize((384, 384)).save(src / "没元数据.png", "PNG")

            meta_check = caption_gen.tags_from_metadata(str(src / "有元数据.png"), parser="auto")
            none_check = caption_gen.tags_from_metadata(str(src / "没元数据.png"), parser="auto")
            check("有元数据的图能读出标签", len(meta_check[0]) > 0, str(meta_check[0])[:50])
            check("重存过的图读不出元数据", len(none_check[0]) == 0, str(none_check[0])[:50])

            db = Database(tmp / "t.db")
            ds = db.upsert_dataset("mixed", str(src))
            scan_folder(db, ds, str(src), parse_meta=True)

            def clean():
                for row in db.list_images(ds):
                    p = Path(row["path"]).with_suffix(".txt")
                    if p.is_file():
                        p.unlink()
                db.refresh_caption_flags()

            def missing():
                return list(caption_gen.iter_missing_caption_images(db, None))

            # ---- WD14 关闭：没元数据的算「无内容」----
            clean()
            rep_off = caption_gen.generate_for_images(
                db, missing(), parser="auto", use_wd14=False,
                policy="skip", make_backup=False,
            )
            check("WD14 关闭：有元数据的写成 1 张", rep_off.written == 1, str(rep_off.written))
            check("WD14 关闭：没元数据的算无内容", rep_off.empty == 1, str(rep_off.empty))
            check("WD14 关闭：没有 WD14 来源", rep_off.from_wd14 == 0, str(rep_off.from_wd14))
            off_txt = (src / "没元数据.txt").is_file()
            check("WD14 关闭：没元数据的没生成 txt", not off_txt)

            # ---- WD14 打开：没元数据的用 WD14 ----
            if wd14.pick_default_model() is None:
                print("  跳过 WD14 打开那半（没找到模型）")
            else:
                clean()
                rep_on = caption_gen.generate_for_images(
                    db, missing(), parser="auto", use_wd14=True,
                    wd14_config=wd14.default_config(),
                    policy="skip", make_backup=False,
                )
                check("WD14 打开：两张都写了", rep_on.written == 2, str(rep_on.written))
                check("WD14 打开：有 1 张来自 WD14", rep_on.from_wd14 == 1, str(rep_on.from_wd14))
                check("WD14 打开：没有无内容", rep_on.empty == 0, str(rep_on.empty))

                # 逐个 item 验来源：有元数据那条必须是 meta，没被降级
                by_name = {i.filename: i for i in rep_on.items}
                m = by_name.get("有元数据.png")
                n = by_name.get("没元数据.png")
                check("有元数据的来源仍是 meta（元数据优先）",
                      m is not None and m.source == caption_gen.SOURCE_META,
                      m.source if m else "(缺失)")
                check("没元数据的来源是 wd14",
                      n is not None and n.source == caption_gen.SOURCE_WD14,
                      n.source if n else "(缺失)")

                tags = captions.read_caption(str(src / "没元数据.png"))
                check("WD14 写的 txt 真有标签", len(tags) >= 5, f"{len(tags)} 个")
                check("WD14 写的 txt 没有下划线残留",
                      not any("_" in t for t in tags), str(tags[:4]))
                check("WD14 写的 txt 没有屏蔽词",
                      not any(t.strip().lower() in {"watermark", "signature"} for t in tags),
                      str(tags[:4]))
                print(f"  没元数据.png -> {', '.join(tags[:8])} …（共 {len(tags)} 个）")
        finally:
            for key, old in _ENV_BACKUP.items():
                if old is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = old
            _ENV_BACKUP.clear()
            paths.reset_data_dir_cache()


def main() -> int:
    print("=" * 62)
    print("ComfyUI Workbench - core self test")
    print("=" * 62)
    test_metadata_parsing()
    test_prompt_to_tags()
    test_response_parsing()
    test_end_to_end()
    test_data_dir()
    test_db_temp_independence()
    test_nai_metadata()
    test_captions_io()
    test_caption_generation()
    test_tag_isolation()
    test_tag_conversion()
    test_escape_convention()
    test_nai_tag_cleaning()
    test_sampler_picking()
    test_wd14_config()
    test_wd14_fallback()
    test_trash()
    print()
    print("=" * 62)
    if FAILED == 0:
        print(f"ALL PASSED: {PASSED} checks")
    else:
        print(f"PASSED {PASSED}, FAILED {FAILED}")
    print("=" * 62)
    return 0 if FAILED == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())

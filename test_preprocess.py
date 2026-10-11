"""Non-destructive regression tests for image preparation."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image, PngImagePlugin

from app.db import Database
from app.preprocess import (
    RenameEntry, RenameOptions, build_rename_plan, centered_crop_box, crop_image,
    execute_rename, ratio_drag_box, save_processed, undo_rename, validate_rename_plan,
)


class PreprocessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db = Database(self.root / "db.sqlite")

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def _image(self, name, folder=None, caption=True):
        path = (folder or self.root) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (40, 30), "red").save(path)
        if caption:
            path.with_suffix(".txt").write_text("1girl, red_hair", encoding="utf-8")
        return path

    def _index(self, path, folder=None):
        dataset = self.db.dataset_by_path(str(folder or path.parent))
        dataset_id = int(dataset["id"]) if dataset else self.db.upsert_dataset("source", str(folder or path.parent))
        return self.db.add_image(dataset_id, str(path), path.name, has_caption=True)

    def test_cycle_rename_pairs_and_restart_undo(self):
        a, b = self._image("a.png"), self._image("b.png")
        a_id, b_id = self._index(a), self._index(b)
        op = execute_rename(self.db, [RenameEntry(a,b,a_id), RenameEntry(b,a,b_id)], "rename")
        self.assertEqual(self.db.get_image(a_id)["path"], str(b))
        self.assertEqual(b.with_suffix(".txt").read_text(encoding="utf-8"), "1girl, red_hair")
        self.db.close()
        self.db = Database(self.root / "db.sqlite")
        undo_rename(self.db, op)
        self.assertEqual(self.db.get_image(a_id)["path"], str(a))
        self.assertEqual(self.db.get_image(b_id)["path"], str(b))

    def test_move_gets_new_dataset_and_undo_restores_old(self):
        source = self._image("x.png", self.root / "original")
        image_id = self._index(source)
        original_dataset = self.db.get_image(image_id)["dataset_id"]
        target = self.root / "export" / "x.png"
        op = execute_rename(self.db, [RenameEntry(source,target,image_id)], "move")
        self.assertNotEqual(self.db.get_image(image_id)["dataset_id"], original_dataset)
        self.assertTrue(target.with_suffix(".txt").is_file())
        undo_rename(self.db, op)
        self.assertEqual(self.db.get_image(image_id)["dataset_id"], original_dataset)

    def test_copy_undo_and_collision(self):
        source = self._image("x.png")
        target = self.root / "out" / "y.png"
        entry = RenameEntry(source,target)
        op = execute_rename(self.db, [entry], "copy")
        self.assertTrue(source.is_file())
        self.assertTrue(target.with_suffix(".txt").is_file())
        self.assertTrue(validate_rename_plan([entry], "copy"))
        undo_rename(self.db, op)
        self.assertFalse(target.exists())
        self.assertTrue(source.exists())

    def test_numbering_groups_and_tokens(self):
        a = self._image("a.png", self.root / "one")
        b = self._image("b.png", self.root / "two")
        plan = build_rename_plan([a,b], RenameOptions(prefix="{组名}", group="folder", digits=2),
                                 mode="copy", output=self.root / "out")
        self.assertEqual([e.target.name for e in plan], ["one_01.png", "two_01.png"])

    def test_drag_crop_locks_ratio(self):
        self.assertEqual(ratio_drag_box((5,5), (35,25), (40,30), (1,1)), (5,5,25,25))
        self.assertEqual(ratio_drag_box((35,25), (5,5), (40,30), (1,1)), (15,5,35,25))

    def test_crop_strips_metadata_and_copies_caption(self):
        source = self.root / "meta.png"
        meta = PngImagePlugin.PngInfo(); meta.add_text("prompt", "secret")
        Image.new("RGB", (40,30), "red").save(source, pnginfo=meta)
        source.with_suffix(".txt").write_text("caption", encoding="utf-8")
        target = self.root / "crop.png"
        crop_image(source, target, centered_crop_box(40,30,1,1))
        with Image.open(target) as image:
            self.assertEqual(image.size, (30,30))
            self.assertNotIn("prompt", image.info)
        self.assertEqual(target.with_suffix(".txt").read_text(encoding="utf-8"), "caption")

    def test_jpeg_overwrite_preserves_format_and_backup(self):
        source = self._image("photo.jpg", caption=False)
        old = source.read_bytes()
        backup = save_processed(source, Image.new("RGB", (40,30), "blue"), source,
                                overwrite=True, backup_root=self.root / "backups")
        self.assertEqual(backup.read_bytes(), old)
        with Image.open(source) as image:
            self.assertEqual(image.format, "JPEG")

    def test_webp_overwrite_preserves_format(self):
        source = self._image("photo.webp", caption=False)
        backup = save_processed(source, Image.new("RGB", (40,30), "blue"), source,
                                overwrite=True, backup_root=self.root / "backups")
        self.assertTrue(backup.is_file())
        with Image.open(source) as image:
            self.assertEqual(image.format, "WEBP")

    def test_overwrite_failure_keeps_original(self):
        source = self._image("photo.jpg", caption=False)
        before = source.read_bytes()
        with patch.object(Image.Image, "save", side_effect=OSError("encoder failed")):
            with self.assertRaises(OSError):
                save_processed(source, Image.new("RGB", (40,30), "blue"), source,
                               overwrite=True, backup_root=self.root / "backups")
        self.assertEqual(source.read_bytes(), before)

    def test_cancelled_rename_restores_source(self):
        a, b = self._image("a.png"), self._image("b.png")
        calls = 0
        def stop():
            nonlocal calls
            calls += 1
            return calls > 1
        with self.assertRaises(InterruptedError):
            execute_rename(self.db, [RenameEntry(a,self.root/"1.png"),
                                     RenameEntry(b,self.root/"2.png")], "rename", should_stop=stop)
        self.assertTrue(a.is_file()); self.assertTrue(b.is_file())
        self.assertTrue(a.with_suffix(".txt").is_file())

    def test_undo_refuses_changed_target(self):
        source = self._image("a.png")
        target = self.root / "renamed.png"
        op = execute_rename(self.db, [RenameEntry(source,target)], "rename")
        target.write_bytes(target.read_bytes() + b"change")
        with self.assertRaises(ValueError):
            undo_rename(self.db, op)
        self.assertTrue(target.is_file())


if __name__ == "__main__":
    unittest.main()

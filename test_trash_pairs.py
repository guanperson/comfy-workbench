import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app import trash


class TrashPairsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.data = self.root / "data"
        self.bin = self.root / "bin"
        self.data.mkdir()
        self.bin.mkdir()
        self.data_patch = mock.patch.object(trash.paths, "data_dir", return_value=self.data)
        self.data_patch.start()
        self.addCleanup(self.data_patch.stop)
        trash.set_trash_dir(str(self.bin))
        self.addCleanup(lambda: trash.set_trash_dir(""))

    def _image(self, name="same.png", caption=True):
        image = self.root / "source" / name
        image.parent.mkdir(exist_ok=True)
        image.write_text("image", encoding="utf-8")
        txt = image.with_suffix(".txt")
        if caption:
            txt.write_text("caption", encoding="utf-8")
        return image, txt

    def _trash(self, image):
        return trash.trash_images([(str(image), "set")])

    def test_same_image_name_keeps_caption_pair(self):
        a, atxt = self._image("same.png")
        bdir = self.root / "other"
        bdir.mkdir()
        b = bdir / "same.png"
        btxt = b.with_suffix(".txt")
        b.write_text("image-b", encoding="utf-8")
        btxt.write_text("caption-b", encoding="utf-8")
        self.assertEqual(self._trash(a)[0], 2)
        self.assertEqual(self._trash(b)[0], 2)
        a_target = self.bin / "same.png"
        b_target = self.bin / "same__2.png"
        self.assertTrue(a_target.exists())
        self.assertTrue(a_target.with_suffix(".txt").exists())
        self.assertTrue(b_target.exists())
        self.assertTrue(b_target.with_suffix(".txt").exists())
        self.assertEqual(b_target.with_suffix(".txt").read_text(encoding="utf-8"), "caption-b")

    def test_caption_only_stem_collision_gets_consistent_new_stem(self):
        (self.bin / "same.txt").write_text("unrelated", encoding="utf-8")
        image, _ = self._image("same.png")
        self.assertEqual(self._trash(image)[0], 2)
        self.assertTrue((self.bin / "same__2.png").exists())
        self.assertEqual((self.bin / "same__2.txt").read_text(encoding="utf-8"), "caption")
        self.assertEqual((self.bin / "same.txt").read_text(encoding="utf-8"), "unrelated")

    def test_cross_extension_same_stem_collision(self):
        (self.bin / "same.jpg").write_text("other extension", encoding="utf-8")
        image, _ = self._image("same.png")
        self.assertEqual(self._trash(image)[0], 2)
        self.assertTrue((self.bin / "same__2.png").exists())
        self.assertTrue((self.bin / "same__2.txt").exists())

    def test_caption_move_failure_rolls_image_back(self):
        image, caption = self._image("fail.png")
        real_move = trash.shutil.move

        def move(src, dest, *args, **kwargs):
            if Path(src) == caption:
                raise OSError("injected caption failure")
            return real_move(src, dest, *args, **kwargs)

        with mock.patch.object(trash.shutil, "move", side_effect=move):
            moved, errors = self._trash(image)
        self.assertEqual(moved, 0)
        self.assertTrue(any("配套 txt 移动失败" in error for error in errors))
        self.assertTrue(image.exists())
        self.assertTrue(caption.exists())
        self.assertFalse((self.bin / image.name).exists())

    def test_image_without_caption(self):
        image, _ = self._image("solo.png", caption=False)
        self.assertEqual(self._trash(image), (1, []))
        self.assertTrue((self.bin / image.name).exists())

    def test_restore_pair_uses_shared_restored_stem(self):
        image, _ = self._image("restore.png")
        self.assertEqual(self._trash(image)[0], 2)
        Path(image).write_text("new image", encoding="utf-8")
        image.with_suffix(".txt").write_text("new caption", encoding="utf-8")
        entries = trash.list_trashed()
        self.assertEqual(len(entries), 1)
        self.assertEqual(trash.restore(entries), (2, []))
        self.assertTrue(image.with_name("restore_restored2.png").exists())
        self.assertTrue(image.with_name("restore_restored2.txt").exists())
        self.assertEqual(image.with_suffix(".txt").read_text(encoding="utf-8"), "new caption")


if __name__ == "__main__":
    unittest.main()

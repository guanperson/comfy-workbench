"""Offscreen regression checks for the native preparation canvas and themes."""

import os
import tempfile
import time
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PIL import Image
from PySide6.QtWidgets import QApplication

from app.config import Settings
from app.db import Database
from app.ui import theme
from app.ui.main_window import MainWindow


class PreprocessUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        os.environ["COMFY_WORKBENCH_HOME"] = str(self.root / "home")
        from app import paths
        paths.reset_data_dir_cache()
        self.db = Database(self.root / "test.db")
        self.settings = Settings()
        theme.apply_theme(self.app, self.settings)
        self.window = MainWindow(self.db, self.settings)
        self.window.show()
        self.window.goto("preprocess")
        self.page = self.window.pages.currentWidget()
        self.app.processEvents()

    def tearDown(self):
        self.window.close()
        self.db.close()
        self.temp.cleanup()

    def test_load_mosaic_undo_and_crop(self):
        image = self.root / "input.png"
        Image.new("RGB", (80, 60), "red").save(image)
        self.page._add_rows([{"path": str(image)}])
        self.page.tabs.setCurrentIndex(1)
        canvas = self.page.canvas
        self.assertEqual(canvas.work.size, (80,60))
        canvas.effect = "black"
        canvas.points = [(20,20), (40,40)]
        canvas.shape = "rect"
        canvas._apply_effect()
        self.assertEqual(canvas.work.getpixel((30,30))[:3], (0,0,0))
        self.assertTrue(canvas.undo())
        self.assertEqual(canvas.work.getpixel((30,30))[:3], (255,0,0))
        self.page.tabs.setCurrentIndex(2)
        canvas.set_ratio((1,1))
        self.assertEqual(canvas.crop_box, (10,0,70,60))

    def test_light_dark_and_background(self):
        background = self.root / "background.jpg"
        Image.new("RGB", (200,100), "blue").save(background)
        self.settings.ui_background = str(background)
        theme.apply_theme(self.app, self.settings)
        self.assertIsNotNone(theme.background_pixmap())
        self.settings.ui_theme = "dark"
        theme.apply_theme(self.app, self.settings)
        self.assertEqual(theme.BG, "#0a0f14")
        self.assertIsNotNone(theme.background_pixmap())

    def test_auto_export_on_switch_keeps_original(self):
        a, b = self.root / "a.png", self.root / "b.png"
        Image.new("RGB", (80,60), "red").save(a)
        Image.new("RGB", (80,60), "blue").save(b)
        self.page._add_rows([{"path": str(a)}, {"path": str(b)}])
        self.page.items.setCurrentRow(0)
        self.page.tabs.setCurrentIndex(1)
        canvas = self.page.canvas
        canvas.effect, canvas.shape = "black", "rect"
        canvas.points = [(20,20), (40,40)]
        canvas._apply_effect()
        output = self.root / "output"
        self.page.output.setText(str(output))
        self.page.auto_export.setChecked(True)
        self.page.items.setCurrentRow(1)
        until = time.monotonic() + 5
        while time.monotonic() < until and not self.db.recent_preprocess_ops():
            self.app.processEvents()
            time.sleep(0.02)
        self.assertTrue((output / "a_masked.png").is_file())
        self.assertTrue(self.db.recent_preprocess_ops())
        with Image.open(a) as image:
            self.assertEqual(image.getpixel((30,30)), (255,0,0))


if __name__ == "__main__":
    unittest.main()

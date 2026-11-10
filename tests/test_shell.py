"""Tests for the global hotkey parser, the settings file and the shell agent."""

from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path

from peverything.settings import Settings
from peverything.shell_agent import (
    MOD_ALT,
    MOD_CONTROL,
    MOD_SHIFT,
    MOD_WIN,
    ShellAgent,
    SingleInstance,
    parse_hotkey,
)

ICON = Path(__file__).resolve().parent.parent / "peverything" / "assets" / "peverything.ico"


class ParseHotkeyTestCase(unittest.TestCase):
    def test_simple_combo(self) -> None:
        modifiers, vk, canonical = parse_hotkey("Ctrl+Alt+E")
        self.assertEqual(modifiers, MOD_CONTROL | MOD_ALT)
        self.assertEqual(vk, ord("E"))
        self.assertEqual(canonical, "Ctrl+Alt+E")

    def test_lower_case_and_function_key(self) -> None:
        modifiers, vk, canonical = parse_hotkey("ctrl+shift+f12")
        self.assertEqual(modifiers, MOD_CONTROL | MOD_SHIFT)
        self.assertEqual(vk, 0x7B)
        self.assertEqual(canonical, "Ctrl+Shift+F12")

    def test_win_and_named_key(self) -> None:
        modifiers, vk, canonical = parse_hotkey("Win+Space")
        self.assertEqual(modifiers, MOD_WIN)
        self.assertEqual(vk, 0x20)
        self.assertEqual(canonical, "Win+Space")

    def test_modifier_order_is_canonical(self) -> None:
        self.assertEqual(parse_hotkey("Alt+Ctrl+Shift+K")[2], "Ctrl+Alt+Shift+K")

    def test_single_key_without_modifier(self) -> None:
        modifiers, vk, canonical = parse_hotkey("F9")
        self.assertEqual(modifiers, 0)
        self.assertEqual(vk, 0x78)
        self.assertEqual(canonical, "F9")

    def test_errors(self) -> None:
        for bad in ("", "   ", "Ctrl+", "Ctrl+Alt", "Ctrl+Bogus", "Ctrl+A+B", "Ctrl+F25"):
            with self.subTest(spec=bad):
                with self.assertRaises(ValueError):
                    parse_hotkey(bad)


class SettingsTestCase(unittest.TestCase):
    def test_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "settings.json"
            original = Settings(hotkey="Win+Shift+P", hotkey_enabled=True, tray_icon=False, close_action="taskbar")
            original.save(path)
            loaded = Settings.load(path)
            self.assertEqual(loaded.hotkey, "Win+Shift+P")
            self.assertFalse(loaded.tray_icon)
            self.assertEqual(loaded.close_action, "taskbar")

    def test_missing_file_gives_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            loaded = Settings.load(Path(folder) / "nope.json")
            self.assertEqual(loaded.close_action, "tray")
            self.assertTrue(loaded.hotkey_enabled)

    def test_invalid_values_fall_back(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "settings.json"
            path.write_text('{"close_action": "explode", "hotkey": 42}', encoding="utf-8")
            loaded = Settings.load(path)
            self.assertEqual(loaded.close_action, "tray")
            self.assertEqual(loaded.hotkey, "42")

    def test_corrupt_file_gives_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "settings.json"
            path.write_text("{not json", encoding="utf-8")
            self.assertEqual(Settings.load(path).close_action, "tray")


@unittest.skipUnless(os.name == "nt", "仅 Windows")
class ShellAgentTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.hotkeys: list[int] = []
        self.clicks: list[int] = []
        self.menu: list[int] = []
        self.agent = ShellAgent(
            icon_path=ICON,
            tooltip="Peverything 测试",
            on_hotkey=lambda: self.hotkeys.append(1),
            on_tray_activate=lambda: self.clicks.append(1),
            on_tray_menu=self.menu.append,
        )
        self.assertTrue(self.agent.start(), self.agent.error)

    def tearDown(self) -> None:
        self.agent.stop()

    def wait_for(self, predicate, timeout: float = 3.0) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if predicate():
                return True
            time.sleep(0.02)
        return False

    def test_hidden_window_created(self) -> None:
        self.assertIsNotNone(self.agent.hwnd)
        self.assertIsNotNone(self.agent.thread_id)

    def test_hotkey_message_reaches_callback(self) -> None:
        # Anything can be occupied on a real desktop; try a few unusual combos.
        for spec in ("Ctrl+Alt+Shift+F11", "Ctrl+Alt+Shift+F9", "Ctrl+Shift+Alt+F7", "Ctrl+Alt+F10"):
            if self.agent.register_hotkey(spec):
                break
        else:
            self.skipTest("找不到可用的测试热键")
        self.agent.simulate_hotkey()
        self.assertTrue(self.wait_for(lambda: bool(self.hotkeys)), "WM_HOTKEY 没有回调")

    def test_tray_icon_lifecycle(self) -> None:
        self.assertTrue(self.agent.set_tray_visible(True))
        self.assertTrue(self.agent.tray_visible)
        # S_OK (visible) or S_FALSE (parked in the overflow flyout): both mean
        # the shell really owns our icon.
        self.assertIsNotNone(self.agent.tray_rect(), "托盘图标没有注册到系统")
        self.agent.simulate_tray_click()
        self.assertTrue(self.wait_for(lambda: bool(self.clicks)), "托盘点击没有回调")
        self.agent.set_tray_visible(False)
        self.assertFalse(self.agent.tray_visible)
        self.assertIsNone(self.agent.tray_rect())

    def test_stop_is_idempotent(self) -> None:
        self.agent.stop()
        self.agent.stop()
        self.assertIsNone(self.agent.hwnd)


@unittest.skipUnless(os.name == "nt", "仅 Windows")
class SingleInstanceTestCase(unittest.TestCase):
    def test_second_guard_detects_the_first(self) -> None:
        name = "Local\\Peverything.Test.SingleInstance"
        first = SingleInstance(name)
        self.assertFalse(first.already_running)
        second = SingleInstance(name)
        self.assertTrue(second.already_running)
        self.assertIsNotNone(first._handle)
        del first, second


if __name__ == "__main__":
    unittest.main()

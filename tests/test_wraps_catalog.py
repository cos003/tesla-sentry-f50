import importlib.util
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def module(name, file):
    spec = importlib.util.spec_from_file_location(name, ROOT / file)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


viewer = module("wraps_viewer", "sentry-viewer.py")

SAMPLE = '''
<h2>共 440 款</h2><span>第 1 / 10 页</span>
<a class="card glass block" href="/zh/wrap/JjyYQjnPceX9"><div class="ph "><img src="https://img.wraps.cc/thumb/JjyYQjnPceX9.webp" alt="x"/></div>
<div><p class="truncate text-[15px] font-semibold">中国警察巡逻涂装（Chinese Police Patrol Livery）</p>
<p><span class="truncate">Model Y (2020–2024)</span><span>↓ <!-- -->13</span></p></div></a>
<a class="card glass block" href="/wrap/Abcdef12"><img src="https://img.wraps.cc/thumb/Abcdef12.webp"><p class="truncate">A &amp; B</p><span class="truncate">Cybertruck</span></a>
<a class="card glass block" href="/zh/wrap/JjyYQjnPceX9"><p class="truncate">dup</p></a>
'''


class WrapsCatalogTest(unittest.TestCase):
    def test_parse_cards_page_and_entities(self):
        items, page, pages, total = viewer.parse_wraps_catalog(SAMPLE)
        self.assertEqual(page, 1)
        self.assertEqual(pages, 10)
        self.assertEqual(total, 440)
        self.assertEqual([i["id"] for i in items], ["JjyYQjnPceX9", "Abcdef12"])
        self.assertEqual(items[0]["model"], "Model Y (2020–2024)")
        self.assertEqual(items[1]["title"], "A & B")
        self.assertTrue(items[0]["thumb"].endswith("JjyYQjnPceX9.webp"))

    def test_filename_from_disposition(self):
        class H(dict):
            def get(self, k, default=None):
                return super().get(k, default)
        fn = viewer._wraps_filename({"Content-Disposition": 'attachment; filename="LV_Matte_Black.png"'}, "Tx6GWUBHRWBZ")
        self.assertEqual(fn, "LV_Matte_Black.png")
        fn = viewer._wraps_filename({"Content-Disposition": "attachment; filename*=UTF-8''Sakura%20Wrap.png"}, "Abcd1234")
        self.assertEqual(fn, "Sakura Wrap.png")

    def test_login_rejects_bad_email_and_code_without_network(self):
        ok, msg = viewer.wraps_login_request("not-an-email")
        self.assertFalse(ok)
        self.assertIn("邮箱", msg)
        ok, msg = viewer.wraps_login_verify("owner@example.com", "12")
        self.assertFalse(ok)
        self.assertIn("验证码", msg)

    def test_install_rejects_bad_id_without_network(self):
        ok, msg = viewer.wraps_install("../etc/passwd")
        self.assertFalse(ok)
        self.assertIn("无效", msg)


if __name__ == "__main__":
    unittest.main()

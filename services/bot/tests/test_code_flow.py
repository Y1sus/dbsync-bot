# =============================================================================
# services/bot/tests/test_code_flow.py — Pruebas del asistente /codigo: navegación, búsqueda, confirmación y callbacks
# Autor: Jesús Montalvo <jmontalvo_m@outlook.com>
# =============================================================================

import re
import unittest

from bot import flow
from bot.packages import PackageRequest

HOSTILE = "<script>alert(1)</script>&\"'"
DB = {"server": "alpha", "container": "mysql8", "db": "appdb"}
PROJECTS = [
    {"name": "alpha-app", "path": "g/alpha-app", "has_db": True, "db": DB},
    {"name": HOSTILE, "path": "g/x", "has_db": True, "db": {"server": HOSTILE, "container": "c<1>", "db": "d&2"}},
    {"name": "solo", "path": "g/solo", "has_db": False},
]


def refs(n, default="main"):
    out = [{"name": f"feat/b{i:02d}", "default": False, "protected": False, "sha": f"{i:040x}"} for i in range(n)]
    out.append({"name": default, "default": True, "protected": True, "sha": "ab12cd34" + "0" * 32})
    return {"refs": out, "truncated": False}


class Clock:
    t = 0.0

    def __call__(self):
        return self.t


def buttons(view):
    return [b for row in view.keyboard for b in row]


def by_text(view, text):
    return next(b for b in buttons(view) if b.text == text)


def press(w, button):
    parsed = flow.parse_callback(button.data)
    assert parsed and parsed[0] == "k", button.data
    return flow.advance_code(w, parsed[2], parsed[3])


def strip_code(text):
    return re.sub(r"<code>.*?</code>", "", text, flags=re.S)


class CodeFlowTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.store = flow.CodeStore(clock=self.clock)
        self.w = self.store.create(42, 3600, 1)
        flow.set_catalog(self.w, PROJECTS)

    def to_ref(self, idx=0, n=20):
        step = press(self.w, buttons(flow.render_code(self.w))[idx])
        self.assertEqual(step.kind, "fetch_branches")
        flow.set_branches(self.w, refs(n))

    def test_full_path_both_with_exact_db_target_and_sha8(self):
        self.to_ref()
        view = flow.render_code(self.w)
        self.assertEqual(by_text(view, "main ★").data.split("|")[3], "0")  # la rama por defecto va primero
        self.assertEqual(press(self.w, by_text(view, "main ★")).kind, "view")
        view = flow.render_code(self.w)
        self.assertIn("Formato", view.text)
        press(self.w, by_text(view, "• zip"))
        view = flow.render_code(self.w)
        self.assertEqual([b.text for b in buttons(view)][:3], ["📦 Código", "🗄 Base de datos", "📦🗄 Ambos"])
        press(self.w, by_text(view, "📦🗄 Ambos"))
        view = flow.render_code(self.w)
        self.assertIn("<b>alpha-app@main</b> (<code>ab12cd34</code>)", view.text)
        self.assertIn("<code>alpha/mysql8/appdb</code>", view.text)
        self.assertIn("código + BD", view.text)
        self.assertEqual(press(self.w, by_text(view, "Generar entrega")).kind, "run")
        req = flow.build_package_request(self.w)
        self.assertEqual(req, PackageRequest(42, "alpha-app", "main", "ab12cd34" + "0" * 32, "zip", "both", 3600, 1, ("alpha", "mysql8", "appdb")))

    def test_db_only_confirmation_also_shows_exact_target_and_sha(self):
        self.to_ref()
        press(self.w, by_text(flow.render_code(self.w), "main ★"))
        press(self.w, by_text(flow.render_code(self.w), "tar.gz"))
        self.assertEqual(self.w.fmt, "tar.gz")
        press(self.w, by_text(flow.render_code(self.w), "🗄 Base de datos"))
        text = flow.render_code(self.w).text
        self.assertIn("<code>alpha/mysql8/appdb</code>", text)
        self.assertIn("ab12cd34", text)
        self.assertNotIn("Formato", text)

    def test_project_without_db_only_offers_code(self):
        step = press(self.w, buttons(flow.render_code(self.w))[2])
        self.assertEqual(step.kind, "fetch_branches")
        flow.set_branches(self.w, refs(2))
        press(self.w, by_text(flow.render_code(self.w), "main ★"))
        press(self.w, by_text(flow.render_code(self.w), "• zip"))
        view = flow.render_code(self.w)
        self.assertEqual([b.text for b in buttons(view)][:1], ["📦 Código"])
        self.assertNotIn("Base de datos", [b.text for b in buttons(view)])
        for arg in (1, 2):
            self.assertEqual(flow.advance_code(self.w, "wh", arg).kind, "invalid")
        self.assertEqual(flow.advance_code(self.w, "wh", 0).kind, "view")
        self.assertNotIn("Base de datos", flow.render_code(self.w).text)
        self.assertIsNone(flow.build_package_request(self.w).db)

    def test_branch_pagination_default_first_and_arrows(self):
        self.to_ref(n=20)  # 21 ramas = 3 páginas
        view = flow.render_code(self.w)
        refs_btns = [b for b in buttons(view) if flow.parse_callback(b.data)[2] == "r"]
        self.assertEqual(len(refs_btns), 8)
        self.assertEqual(refs_btns[0].text, "main ★")
        self.assertEqual([b.text for b in buttons(view) if b.text in ("◀", "▶")], ["▶"])
        press(self.w, by_text(view, "▶"))
        press(self.w, by_text(flow.render_code(self.w), "▶"))
        view = flow.render_code(self.w)
        self.assertEqual(len([b for b in buttons(view) if flow.parse_callback(b.data)[2] == "r"]), 5)
        self.assertEqual([b.text for b in buttons(view) if b.text in ("◀", "▶")], ["◀"])
        step = press(self.w, [b for b in buttons(view) if flow.parse_callback(b.data)[2] == "r"][0])
        self.assertEqual(step.kind, "view")
        self.assertEqual(self.w.ref, "feat/b15")
        self.assertEqual(flow.advance_code(self.w, "pg", 1).kind, "invalid")

    def test_page_out_of_range_and_out_of_order_actions_are_invalid(self):
        self.to_ref(n=3)
        for action, arg in (("pg", 5), ("r", 99), ("f", 0), ("wh", 0), ("go", None), ("t", 14400), ("p", 0)):
            self.assertEqual(flow.advance_code(self.w, action, arg).kind, "invalid", action)

    def test_tags_are_fetched_once_and_toggle_back(self):
        self.to_ref(n=3)
        view = flow.render_code(self.w)
        step = press(self.w, by_text(view, "🏷 Tags"))
        self.assertEqual(step.kind, "fetch_tags")
        self.assertEqual(flow.advance_code(self.w, "r", 0).kind, "invalid")  # ocupado
        flow.set_tags(self.w, {"refs": [{"name": "v1.0", "sha": "f" * 40}], "truncated": False})
        view = flow.render_code(self.w)
        self.assertIn("Tags:", view.text)
        self.assertEqual(press(self.w, by_text(view, "v1.0")).kind, "view")
        self.assertEqual((self.w.ref, self.w.sha), ("v1.0", "f" * 40))
        flow.advance_code(self.w, "b", None)  # atrás a la lista de tags
        press(self.w, by_text(flow.render_code(self.w), "🌿 Ramas"))
        self.assertIn("Ramas:", flow.render_code(self.w).text)
        self.assertEqual(press(self.w, by_text(flow.render_code(self.w), "🏷 Tags")).kind, "view")  # ya en memoria

    def test_search_flow_sets_results_and_takes_sha_from_results(self):
        self.to_ref(n=3)
        self.assertEqual(press(self.w, by_text(flow.render_code(self.w), "🔎 Buscar")).kind, "search_prompt")
        self.assertIn("siguiente mensaje", flow.render_code(self.w).text)
        self.assertIs(self.store.find_search(42), self.w)
        step = flow.apply_search(self.w, "  feat/b01 <b>; rm -rf  ")
        self.assertEqual((step.kind, self.w.query), ("fetch_search", "feat/b01brm-rf"))
        self.assertIsNone(self.store.find_search(42))  # ocupado: no admite otro texto
        flow.set_results(self.w, {"refs": [{"name": "feat/b01", "sha": "e" * 40}], "truncated": False})
        view = flow.render_code(self.w)
        self.assertIn("«feat/b01brm-rf»", view.text)
        press(self.w, by_text(view, "feat/b01"))
        self.assertEqual(self.w.sha, "e" * 40)

    def test_search_hostile_text_is_sanitized_and_empty_rejected(self):
        self.to_ref(n=1)
        press(self.w, by_text(flow.render_code(self.w), "🔎 Buscar"))
        for text in ("<>;&", "   ", "\x00\n", ""):
            step = flow.apply_search(self.w, text)
            self.assertEqual(step.kind, "invalid", repr(text))
            self.assertIn("Texto no válido", step.notice)
        self.assertEqual(self.w.step, "search")
        flow.apply_search(self.w, "--" + "x" * 500)
        self.assertEqual(self.w.query, "x" * 60)
        flow.set_results(self.w, {"refs": [], "truncated": False})
        text = flow.render_code(self.w).text
        self.assertIn("Sin resultados", text)

    def test_search_expires_after_ten_minutes_and_text_outside_search_is_ignored(self):
        self.to_ref(n=1)
        self.assertIsNone(self.store.find_search(42))
        press(self.w, by_text(flow.render_code(self.w), "🔎 Buscar"))
        self.clock.t = 599
        self.assertIs(self.store.find_search(42), self.w)
        self.clock.t = 601
        self.assertIsNone(self.store.find_search(42))
        self.assertIsNone(self.store.get(self.w.id, 42))
        self.assertEqual(flow.apply_search(flow.CodeWizard("x", 1, 1, 1), "main").kind, "invalid")

    def test_search_is_per_user(self):
        self.to_ref(n=1)
        press(self.w, by_text(flow.render_code(self.w), "🔎 Buscar"))
        self.assertIsNone(self.store.find_search(43))
        self.assertIsNone(self.store.get(self.w.id, 43))

    def test_back_and_cancel(self):
        self.to_ref(n=2)
        press(self.w, by_text(flow.render_code(self.w), "main ★"))
        for expected in ("ref", "project"):
            self.assertEqual(flow.advance_code(self.w, "b", None).kind, "view")
            self.assertEqual(self.w.step, expected)
        self.assertIsNone(self.w.project)
        self.assertEqual(flow.advance_code(self.w, "b", None).kind, "cancel")
        self.assertEqual(flow.advance_code(self.w, "x", None).kind, "cancel")

    def test_back_from_search_returns_to_refs(self):
        self.to_ref(n=2)
        press(self.w, by_text(flow.render_code(self.w), "🔎 Buscar"))
        flow.advance_code(self.w, "b", None)
        self.assertEqual(self.w.step, "ref")
        self.assertIn("Elige la referencia", flow.render_code(self.w).text)

    def test_ttl_and_max_buttons_on_confirm(self):
        self.to_ref(n=1)
        for text in ("main ★", "• zip", "📦 Código"):
            press(self.w, by_text(flow.render_code(self.w), text))
        press(self.w, by_text(flow.render_code(self.w), "4 h"))
        press(self.w, by_text(flow.render_code(self.w), "3"))
        view = flow.render_code(self.w)
        self.assertIn("<b>4 h</b>", view.text)
        self.assertIn("Descargas máximas por archivo: <b>3</b>", view.text)
        self.assertEqual((flow.build_package_request(self.w).ttl, flow.build_package_request(self.w).max_downloads), (14400, 3))
        self.assertEqual(flow.advance_code(self.w, "t", 5).kind, "invalid")

    def test_hostile_names_are_escaped_everywhere(self):
        press(self.w, buttons(flow.render_code(self.w))[1])
        flow.set_branches(self.w, {"refs": [{"name": "main", "default": True, "sha": "1" * 40}], "truncated": False})
        press(self.w, by_text(flow.render_code(self.w), "main ★"))
        press(self.w, by_text(flow.render_code(self.w), "• zip"))
        press(self.w, by_text(flow.render_code(self.w), "📦🗄 Ambos"))
        text = flow.render_code(self.w).text
        self.assertNotIn("<script>", text)
        self.assertNotIn("c<1>", text)
        self.assertIn("&lt;script&gt;", text)
        self.assertIn("d&amp;2", text)
        self.assertNotIn("<script>", flow.render_code_error(self.w, "x").text)

    def test_every_callback_fits_64_bytes_and_parses(self):
        self.to_ref(n=40)
        stages = [lambda: None]
        views = [flow.render_code(self.w)]
        for text in ("main ★", "• zip", "📦🗄 Ambos"):
            press(self.w, by_text(flow.render_code(self.w), text))
            views.append(flow.render_code(self.w))
        for view in views:
            for b in buttons(view):
                self.assertLessEqual(len(b.data.encode()), 64)
                self.assertIsNotNone(flow.parse_callback(b.data), b.data)

    def test_tampered_callbacks(self):
        for bad in ("k|zzzzzzzz|p|1", "k|00000000|zz|1", "k|00000000|p|-1", "k|00000000|p|1" + "9" * 70, "k|0000000|p|1", "K|00000000|p|1"):
            self.assertIsNone(flow.parse_callback(bad), bad)
        self.assertEqual(flow.parse_callback("k|0a0a0a0a|pg|2"), ("k", "0a0a0a0a", "pg", 2))

    def test_catalog_pagination_and_project_labels(self):
        w = self.store.create(1, 60, 1)
        flow.set_catalog(w, [{"name": f"p{i}", "path": "g/p", "has_db": i % 2 == 0, **({"db": DB} if i % 2 == 0 else {})} for i in range(10)])
        view = flow.render_code(w)
        self.assertEqual([b.text for b in buttons(view)][:2], ["p0 · BD", "p1"])
        self.assertEqual(len([b for b in buttons(view) if flow.parse_callback(b.data)[2] == "p"]), 8)
        press(w, by_text(view, "▶"))
        self.assertIn("p9", [b.text for b in buttons(flow.render_code(w))])
        flow.set_catalog(w, [])
        self.assertIn("No hay proyectos", flow.render_code(w).text)

    def test_busy_blocks_double_tap_and_release_allows_retry(self):
        step = press(self.w, buttons(flow.render_code(self.w))[0])
        self.assertEqual(step.kind, "fetch_branches")
        self.assertEqual(press(self.w, buttons(flow.render_code(self.w))[0]).kind, "invalid")
        flow.release(self.w)
        self.w.step = "project"
        self.assertEqual(press(self.w, buttons(flow.render_code(self.w))[0]).kind, "fetch_branches")

    def test_truncated_refs_hint_search(self):
        self.to_ref(n=2)
        self.w.branches_truncated = True
        self.assertIn("🔎 Buscar", flow.render_code(self.w).text)

    def test_help_mentions_codigo_and_status_label(self):
        self.assertIn("/codigo", flow.fmt_help().text)
        from bot.jobs import RunningJob
        job = RunningJob("j", 1, HOSTILE, "r", "both", started=0.0, label=f"{HOSTILE}@r (both)")
        text = flow.fmt_status([job], 0, now=3.0).text
        self.assertNotIn("<script>", text)
        self.assertIn("3 s", text)


if __name__ == "__main__":
    unittest.main()

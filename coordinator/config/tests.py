from django.test import SimpleTestCase


class PageTests(SimpleTestCase):
    def test_demo_page_loads_its_script_from_a_file(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'src="/static/demo/app.js"')
        self.assertNotContains(response, "<script>")

    def test_strict_content_security_policy(self):
        policy = self.client.get("/")["Content-Security-Policy"]
        script_src = next(d for d in policy.split("; ") if d.startswith("script-src"))
        self.assertEqual(script_src, "script-src 'self'")
        self.assertIn("frame-ancestors 'none'", policy)

    def test_health(self):
        self.assertEqual(self.client.get("/health").json(), {"status": "ok"})

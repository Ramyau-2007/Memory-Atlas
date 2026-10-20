import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class FrontendRenderingTests(unittest.TestCase):
    def test_local_pinned_markdown_libraries_load_before_application(self):
        html=(ROOT/'static'/'index.html').read_text(encoding='utf-8')
        marked='/static/vendor/marked-18.0.14.min.js'
        purifier='/static/vendor/dompurify-3.4.17.min.js'
        application='/static/app.js'
        self.assertIn(marked,html); self.assertIn(purifier,html)
        self.assertLess(html.index(marked),html.index(application))
        self.assertLess(html.index(purifier),html.index(application))
        self.assertTrue((ROOT/'static'/'vendor'/'LICENSE.marked.txt').is_file())
        self.assertTrue((ROOT/'static'/'vendor'/'LICENSE.dompurify.txt').is_file())

    def test_assistant_markdown_is_sanitized_and_user_messages_remain_text(self):
        source=(ROOT/'static'/'app.js').read_text(encoding='utf-8')
        self.assertIn('window.marked.parse(source',source)
        self.assertIn('window.DOMPurify.sanitize(parsed',source)
        self.assertIn("element.innerHTML = safe",source)
        self.assertIn("else content.textContent = text",source)
        self.assertIn("role === 'assistant'",source)
        self.assertIn("ALLOWED_TAGS:",source)
        self.assertIn("FORBID_TAGS:",source)
        self.assertIn("FORBID_ATTR: ['style', 'target']",source)

    def test_hosted_cookie_auth_bootstrap_and_privacy_disclosure_are_present(self):
        source=(ROOT/'static'/'app.js').read_text(encoding='utf-8')
        html=(ROOT/'static'/'index.html').read_text(encoding='utf-8')
        self.assertIn("credentials: 'same-origin'",source)
        self.assertIn("api('/session-mode')",source)
        self.assertIn("api('/session')",source)
        self.assertIn('localStorage.removeItem(\'sessionToken\')',source)
        self.assertIn('an extractive summary of recent user messages in longer chats',html)
        self.assertIn('Some messages may also be sent alone for memory analysis',html)
        self.assertIn('up to three matching active memories may be sent to Groq',html)

    def test_deployment_defaults_use_a_separate_persistent_sqlite_path(self):
        config=(ROOT/'render.yaml').read_text(encoding='utf-8')
        env=(ROOT/'.env.example').read_text(encoding='utf-8')
        ignore=(ROOT/'.gitignore').read_text(encoding='utf-8')
        self.assertIn('DATABASE_PATH=data/memory.sqlite3',env)
        self.assertIn('DATABASE_PATH',config)
        self.assertIn('/var/data/memory-atlas-demo.sqlite3',config)
        self.assertIn('GROQ_API_KEY',config)
        self.assertIn('sync: false',config)
        self.assertIn('.env',ignore)
        self.assertIn('*.sqlite3',ignore)
        self.assertIn('*.db',ignore)

    def test_welcome_page_has_distinct_account_choices_and_switching(self):
        html=(ROOT/'static'/'index.html').read_text(encoding='utf-8')
        script=(ROOT/'static'/'app.js').read_text(encoding='utf-8')
        self.assertIn('Create New Account',html)
        self.assertIn('Already have an account? Sign In',html)
        self.assertIn("byId('chooseRegister').addEventListener",script)
        self.assertIn("byId('chooseLogin').addEventListener",script)
        self.assertIn("authMode || 'login'",script)
        self.assertIn("mode === 'register' ? 'new-password' : 'current-password'",script)

    def test_auth_layout_has_desktop_and_mobile_breakpoints(self):
        styles=(ROOT/'static'/'style.css').read_text(encoding='utf-8')
        self.assertIn('.auth-choice .button { width: 100%;',styles)
        self.assertIn('@media (max-width: 820px)',styles)
        self.assertIn('@media (max-width: 560px)',styles)
        self.assertIn('min-height: calc(100dvh - 56px)',styles)


if __name__ == '__main__':
    unittest.main()

import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/fetch_wechat_article.py'
SPEC = importlib.util.spec_from_file_location('reader', SCRIPT)
reader = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(reader)
URL = 'https://mp.weixin.qq.com/s/example'
HTML = '''<html><h1 id="activity-name">标题 &amp; "引号"</h1>
<span id="js_name">示例公众号</span><span id="js_author_name">作者甲</span>
<em id="publish_time">2025-01-02 10:30</em>
<div id="js_content"><div><p>开始</p><div><p>嵌套正文</p></div></div>
<h2>小标题</h2><ul><li>项目一</li><li>项目二</li></ul><blockquote>引用</blockquote>
<pre><code>print("一")</code><code>print("二")</code></pre>
<img data-src="https://mmbiz.qpic.cn/a?x=1&amp;y=2" src="placeholder" alt="图甲">
<table><tr><th>列甲</th><th>列乙</th></tr><tr><td>值甲</td><td>值乙</td></tr></table>
<a href="/s/next">下篇</a><p>最后一段不可截断</p><script>BAD_SCRIPT</script></div></html>'''


class ReaderTests(unittest.TestCase):
    def test_nested_body_format_images_and_dates(self):
        article = reader.extract_article(HTML, URL)
        md = article['markdown']
        for expected in ['最后一段不可截断', '嵌套正文', '## 小标题', '- 项目一', '> 引用',
                         '```', 'print("一")\nprint("二")', '![图甲]', '| 列甲',
                         'https://mp.weixin.qq.com/s/next']:
            self.assertIn(expected, md)
        self.assertNotIn('BAD_SCRIPT', md)
        self.assertNotIn('placeholder', md)
        self.assertEqual(article['author'], '作者甲')
        self.assertEqual(article['account'], '示例公众号')
        self.assertEqual(article['published_at'], '2025-01-02 10:30')
        self.assertNotEqual(article['published_at'], article['fetched_at'])
        self.assertEqual(article['images'], ['https://mmbiz.qpic.cn/a?x=1&y=2'])
        fm = reader.render(article, 'markdown').split('---', 2)[1]
        import yaml
        self.assertEqual(yaml.safe_load(fm)['title'], '标题 & "引号"')
        self.assertEqual(json.loads(reader.render(article, 'json'))['title'], article['title'])

    def test_short_and_image_only_articles(self):
        for body in ['<p>短文。</p>', '<img data-src="https://mmbiz.qpic.cn/img">']:
            article = reader.extract_article(f'<h1 id="activity-name">短文</h1><div id="js_content">{body}</div>')
            self.assertTrue(article['markdown'])
            self.assertIsNone(article['published_at'])
            self.assertIsNone(article['author'])

    def test_reject_invalid_hosts_and_schemes(self):
        for url in ['http://mp.weixin.qq.com/s/a', 'https://mp.weixin.qq.com.evil.org/s/a',
                    'https://evil.org/?mp.weixin.qq.com', 'https://user@mp.weixin.qq.com/s/a',
                    'https://mp.weixin.qq.com:8080/s/a', 'file:///article', 'https://mp.weixin.qq.com/ a']:
            with self.subTest(url=url), self.assertRaises(reader.ArticleError):
                reader.validate_url(url)

    def test_reject_restricted_missing_and_empty_body(self):
        for html in ['<h1>已删除</h1>', '<h1 id="activity-name">空文</h1><div id="js_content"></div>',
                     HTML + '<div id="js_pay_content">付费</div>',
                     HTML + '<div class="weui-msg__title">环境异常</div>']:
            with self.subTest(html=html[:30]), self.assertRaises(reader.ArticleError):
                reader.extract_article(html)

    def test_redirect_cannot_leave_wechat(self):
        response = MagicMock(status_code=302, headers={'Location': 'https://evil.org'})
        response.__enter__.return_value = response
        session = MagicMock()
        session.__enter__.return_value = session
        session.get.return_value = response
        with patch.object(reader.requests, 'Session', return_value=session):
            with self.assertRaises(reader.ArticleError):
                reader.fetch_http(URL, 1)
        self.assertEqual(session.get.call_count, 1)

    def test_http_size_and_error_pages(self):
        for status, kind, chunks in [(403, 'text/html', []), (200, 'application/pdf', []),
                                     (200, 'text/html', [b'x' * (reader.MAX_BYTES + 1)])]:
            response = MagicMock(status_code=status, headers={'Content-Type': kind})
            response.__enter__.return_value = response
            response.iter_content.return_value = chunks
            session = MagicMock()
            session.__enter__.return_value = session
            session.get.return_value = response
            with patch.object(reader.requests, 'Session', return_value=session):
                with self.assertRaises(reader.ArticleError):
                    reader.fetch_http(URL, 1)

    def test_http_success_preserves_utf8(self):
        response = MagicMock(status_code=200, headers={'Content-Type': 'text/html'})
        response.__enter__.return_value = response
        response.iter_content.return_value = [HTML.encode('utf-8')]
        session = MagicMock()
        session.__enter__.return_value = session
        session.get.return_value = response
        with patch.object(reader.requests, 'Session', return_value=session):
            html, final_url = reader.fetch_http(URL, 1)
        self.assertEqual(html, HTML)
        self.assertEqual(final_url, URL)

    def test_unsafe_links_and_images_are_removed(self):
        html = '<h1 id="activity-name">测试</h1><div id="js_content"><p>正文</p><a href="javascript:alert(1)">文本</a><img src="data:text/html,unsafe"></div>'
        article = reader.extract_article(html, URL)
        self.assertNotIn('javascript:', article['markdown'])
        self.assertNotIn('data:text', article['markdown'])
        self.assertIn('文本', article['markdown'])

    def test_independent_copy_cli(self):
        import shutil
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            copied = root / 'fetch_wechat_article.py'
            shutil.copyfile(SCRIPT, copied)
            html = root / 'input.html'
            html.write_text(HTML, encoding='utf-8')
            result = subprocess.run([sys.executable, str(copied), '--html', str(html), '--format', 'json'],
                                    cwd=root, capture_output=True, encoding='utf-8')
            self.assertEqual(result.returncode, 0, result.stderr)
            article = json.loads(result.stdout)
            self.assertIsNone(article['source'])
            self.assertEqual(article['retrieved_via'], 'offline-html')
            self.assertIn('最后一段不可截断', article['markdown'])

    def test_js_metadata_and_timestamp(self):
        article = reader.extract_article('''<script>var msg_title="备用标题";var nickname="公众号";var ct="1700000000";</script>
        <div id="js_content"><p>正文</p></div>''')
        self.assertEqual(article['title'], '备用标题')
        self.assertEqual(article['account'], '公众号')
        self.assertTrue(article['published_at'].startswith('2023-11-14'))

    def test_offline_cli_no_network_and_no_overwrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            html = root / 'input.html'
            out = root / 'article.md'
            html.write_text(HTML, encoding='utf-8')
            with patch.object(reader, 'fetch_http', side_effect=AssertionError('offline must not fetch')):
                self.assertEqual(reader.main(['--html', str(html), '--output', str(out)]), 0)
            original = out.read_bytes()
            self.assertEqual(reader.main(['--html', str(html), '--output', str(out)]), 2)
            self.assertEqual(out.read_bytes(), original)
            html.write_text('<h1>error</h1>', encoding='utf-8')
            missing = root / 'failure.md'
            self.assertEqual(reader.main(['--html', str(html), '--output', str(missing)]), 2)
            self.assertFalse(missing.exists())

    def test_status_codes_and_diagnostics(self):
        for html, code in [('<div class="weui-msg__title">环境异常，请完成验证</div>', 'verification_required'),
                           ('<div class="weui-msg__title">该内容已被发布者删除</div>', 'article_deleted'),
                           ('<div class="weui-msg__title">请登录后阅读</div>', 'login_required'),
                           ('<div id="js_pay_content">付费</div>', 'paid_or_restricted'),
                           ('<html></html>', 'body_missing')]:
            with self.subTest(code=code), self.assertRaises(reader.ArticleError) as caught:
                reader.extract_article(html)
            self.assertEqual(caught.exception.code, code)
        article = reader.extract_article(HTML, URL)
        d = article['diagnostics']
        self.assertEqual(d['code_block_count'], 1)
        self.assertEqual(d['table_count'], 1)
        self.assertEqual(d['unique_image_count'], 1)
        self.assertIn('最后一段不可截断', d['tail_excerpt'])
        self.assertEqual(d['text_characters'], len(article['text']))
        self.assertEqual(len(d['text_sha256']), 64)

    def test_browser_waits_for_body_and_keeps_status_classification(self):
        import types
        class BrowserError(Exception):
            pass
        playwright = MagicMock()
        browser = playwright.chromium.launch.return_value
        context = browser.new_context.return_value
        page = context.new_page.return_value
        page.goto.return_value.status = 200
        page.url = URL
        page.content.return_value = HTML
        manager = MagicMock()
        manager.__enter__.return_value = playwright
        fake = types.ModuleType('playwright.sync_api')
        fake.sync_playwright = lambda: manager
        fake.Error = BrowserError
        with patch.dict(sys.modules, {'playwright.sync_api': fake}):
            html, url = reader.fetch_browser(URL, 2)
            self.assertEqual(html, HTML)
            self.assertEqual(url, URL)
            page.wait_for_function.assert_called_once()
            self.assertEqual(page.wait_for_function.call_args.kwargs['timeout'], 2000)
            page.wait_for_function.side_effect = BrowserError('timeout')
            page.content.return_value = '<div class="weui-msg__title">环境异常，请验证</div>'
            with self.assertRaises(reader.ArticleError) as caught:
                reader.fetch_browser(URL, 2)
            self.assertEqual(caught.exception.code, 'verification_required')
            page.content.return_value = '<html></html>'
            with self.assertRaises(reader.ArticleError) as caught:
                reader.fetch_browser(URL, 2)
            self.assertEqual(caught.exception.code, 'render_timeout')

    def test_invalid_cli_timeout(self):
        for value in ['nan', '-1', '181']:
            result = subprocess.run([sys.executable, str(SCRIPT), URL, '--timeout', value], capture_output=True)
            self.assertEqual(result.returncode, 2)


if __name__ == '__main__':
    unittest.main()

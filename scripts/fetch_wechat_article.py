#!/usr/bin/env python3
"""Read public WeChat articles or saved HTML; export Markdown, text or JSON.

Python rewrite inspired by bulls-cows/skills yy-wechat-to-markdown (Apache-2.0).
2026-11-26: DOM parsing, validated URLs, bounded requests, offline input,
optional browser rendering, distinct dates and explicit failure handling.
"""
import argparse
import json
import math
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup
from markdownify import markdownify

MAX_BYTES = 12 * 1024 * 1024
AGENT = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131.0.0.0 Safari/537.36'


class ArticleError(ValueError):
    """Expected input, transport or extraction failure."""


def validate_url(url):
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError as exc:
        raise ArticleError('invalid article URL') from exc
    if (parsed.scheme != 'https' or parsed.hostname != 'mp.weixin.qq.com'
            or parsed.username or parsed.password or port not in (None, 443)
            or re.search(r'[\s\\\x00-\x1f]', url)):
        raise ArticleError('only HTTPS URLs on the exact host mp.weixin.qq.com are supported')
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, parsed.query, ''))


def fetch_http(url, timeout):
    url = validate_url(url)
    with requests.Session() as session:
        session.headers.update({'User-Agent': AGENT, 'Accept-Language': 'zh-CN,zh;q=0.9',
                                'Accept': 'text/html,application/xhtml+xml'})
        for _ in range(6):
            try:
                with session.get(url, timeout=timeout, allow_redirects=False, stream=True) as response:
                    if response.status_code in (301, 302, 303, 307, 308):
                        location = response.headers.get('Location')
                        if not location:
                            raise ArticleError('redirect has no Location header')
                        url = validate_url(urljoin(url, location))
                        continue
                    if response.status_code != 200:
                        raise ArticleError(f'HTTP {response.status_code}; article not retrieved')
                    kind = response.headers.get('Content-Type', '').lower()
                    if kind and 'html' not in kind:
                        raise ArticleError(f'expected HTML, received {kind}')
                    chunks, size = [], 0
                    for chunk in response.iter_content(65536):
                        size += len(chunk)
                        if size > MAX_BYTES:
                            raise ArticleError('HTML exceeds 12 MiB limit')
                        chunks.append(chunk)
                    # WeChat HTML is UTF-8; avoid requests' ISO-8859-1 default.
                    return b''.join(chunks).decode('utf-8-sig', errors='replace'), url
            except requests.RequestException as exc:
                # Do not print query strings, proxy credentials or cookies.
                raise ArticleError(f'HTTP request failed ({type(exc).__name__})') from exc
    raise ArticleError('too many redirects')


def fetch_browser(url, timeout):
    url = validate_url(url)
    try:
        from playwright.sync_api import sync_playwright, Error as BrowserError
    except ImportError as exc:
        raise ArticleError('browser mode requires requirements-browser.txt and playwright install chromium') from exc
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                context = browser.new_context(user_agent=AGENT, locale='zh-CN', accept_downloads=False)

                def route_request(route):
                    request = route.request
                    if request.is_navigation_request():
                        try:
                            validate_url(request.url)
                        except ArticleError:
                            return route.abort()
                    if request.resource_type in ('image', 'media', 'font'):
                        return route.abort()
                    return route.continue_()

                context.route('**/*', route_request)
                page = context.new_page()
                response = page.goto(url, wait_until='domcontentloaded', timeout=timeout * 1000)
                if response is not None and response.status >= 400:
                    raise ArticleError(f'HTTP {response.status}; article not retrieved')
                final_url = validate_url(page.url)
                html = page.content()
                if len(html.encode('utf-8')) > MAX_BYTES:
                    raise ArticleError('HTML exceeds 12 MiB limit')
                return html, final_url
            finally:
                browser.close()
    except BrowserError as exc:
        raise ArticleError(f'browser failed ({type(exc).__name__}); check Chromium installation or access restrictions') from exc


def text_of(soup, selector):
    node = soup.select_one(selector)
    return node.get_text(' ', strip=True) if node else ''


def meta_of(soup, key):
    node = soup.find('meta', attrs={'property': key}) or soup.find('meta', attrs={'name': key})
    return str(node.get('content', '')).strip() if node else ''


def js_string(html, name):
    match = re.search(r'\b(?:var|let|const)\s+' + re.escape(name)
                      + r'\s*=\s*("(?:\\.|[^"\\])*"|\x27(?:\\.|[^\x27\\])*\x27)', html)
    if not match:
        return ''
    raw = match.group(1)
    if raw.startswith('"'):
        try:
            return json.loads(raw)
        except ValueError:
            return ''
    return raw[1:-1].replace("\\'", "'")


def publication_date(soup, html):
    value = text_of(soup, '#publish_time') or meta_of(soup, 'article:published_time')
    if value:
        return value
    value = js_string(html, 'publish_time')
    if value:
        return value
    match = re.search(r'\b(?:var|let|const)\s+(?:ct|create_time)\s*=\s*[\x27"]?(\d{10})\b', html)
    if match:
        return datetime.fromtimestamp(int(match.group(1)), timezone.utc).isoformat()
    return None


def extract_article(html, source=None, retrieved_via='offline-html'):
    soup = BeautifulSoup(html, 'html.parser')
    body = soup.select_one('#js_content') or soup.select_one('.rich_media_content')
    # Explicit access indicators outside article text. Do not archive a preview as a full article.
    if soup.select_one('#js_pay_content, .pay_read_area, #js_pay_area, #js_verify, .weui-msg__title'):
        raise ArticleError('restricted, verification or status page; full article is not confirmed')
    if body is None:
        raise ArticleError('article body missing; page may be deleted, restricted, or require verification')
    title = (text_of(soup, '#activity-name') or text_of(soup, '.rich_media_title')
             or meta_of(soup, 'og:title') or js_string(html, 'msg_title'))
    account = text_of(soup, '#js_name') or js_string(html, 'nickname')
    author = text_of(soup, '#js_author_name') or meta_of(soup, 'author') or js_string(html, 'author')
    if not title:
        raise ArticleError('article title missing; page is not confirmed as a complete article')
    published_at = publication_date(soup, html)
    warnings = []
    if not published_at:
        warnings.append('Publication time was not present; fetched_at is retrieval time only.')
    if not author:
        warnings.append('Author was not identified; account is recorded separately.')
    if body.select('iframe, video, audio, mpvoice, mp-video, qqmusic'):
        warnings.append('Embedded audio/video is not transcribed or downloaded.')
    for node in body.select('script, style, noscript, iframe, video, audio, mpvoice, mp-video, qqmusic'):
        node.decompose()
    images = []
    for image in list(body.select('img')):
        src = image.get('data-src') or image.get('src') or ''
        src = urljoin(source or 'https://mp.weixin.qq.com/', src)
        if urlsplit(src).scheme not in ('http', 'https'):
            image.decompose()
            continue
        image['src'] = src
        image['alt'] = image.get('alt', '')
        image.attrs.pop('srcset', None)
        images.append(src)
    for link in body.select('a[href]'):
        href = urljoin(source or 'https://mp.weixin.qq.com/', link['href'])
        if urlsplit(href).scheme in ('http', 'https', 'mailto'):
            link['href'] = href
        else:
            del link['href']
    # WeChat often represents a fenced block as multiple code lines inside pre.
    for pre in body.select('pre'):
        codes = pre.find_all('code', recursive=False)
        if len(codes) > 1:
            combined = '\n'.join(code.get_text() for code in codes)
            pre.clear()
            pre.append(combined)
    markdown = markdownify(str(body), heading_style='ATX', bullets='-', wrap=False).strip()
    plain = body.get_text('\n', strip=True)
    if not markdown or (not plain and not images):
        raise ArticleError('article body is empty')
    if images:
        warnings.append('Images retain remote URLs; they are not downloaded and may expire.')
    return {'title': title, 'author': author or None, 'account': account or None,
            'source': source, 'published_at': published_at,
            'fetched_at': datetime.now(timezone.utc).isoformat(), 'retrieved_via': retrieved_via,
            'warnings': warnings, 'images': list(dict.fromkeys(images)),
            'markdown': markdown, 'text': plain}


def render(article, output_format):
    if output_format == 'json':
        return json.dumps(article, ensure_ascii=False, indent=2) + '\n'
    if output_format == 'text':
        return article['title'] + '\n\n' + article['text'] + '\n'
    # JSON scalars are valid YAML scalars and escape quotes, colons and newlines.
    fields = ['title', 'author', 'account', 'source', 'published_at', 'fetched_at', 'retrieved_via', 'warnings']
    frontmatter = '\n'.join(f'{key}: {json.dumps(article[key], ensure_ascii=False)}' for key in fields)
    title = ' '.join(article['title'].splitlines())
    return f'---\n{frontmatter}\n---\n\n# {title}\n\n{article["markdown"]}\n'


def positive_timeout(value):
    timeout = float(value)
    if not math.isfinite(timeout) or timeout <= 0 or timeout > 180:
        raise argparse.ArgumentTypeError('timeout must be in (0, 180] seconds')
    return timeout


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('url', nargs='?', help='Public HTTPS mp.weixin.qq.com article URL')
    parser.add_argument('--html', type=Path, help='Read saved UTF-8 HTML without network access')
    parser.add_argument('--source-url', help='Source article URL for saved HTML only')
    parser.add_argument('--mode', choices=('http', 'browser'), default='http')
    parser.add_argument('--format', choices=('markdown', 'text', 'json'), default='markdown')
    parser.add_argument('--output', type=Path, help='New output file; existing files are never overwritten')
    parser.add_argument('--timeout', type=positive_timeout, default=30.0)
    args = parser.parse_args(argv)
    if bool(args.url) == bool(args.html):
        parser.error('supply either URL or --html, exclusively')
    if args.source_url and not args.html:
        parser.error('--source-url requires --html')
    try:
        if args.output and (args.output.exists() or args.output.is_symlink()):
            raise ArticleError('output already exists; choose a new path')
        if args.html:
            if args.html.stat().st_size > MAX_BYTES:
                raise ArticleError('HTML exceeds 12 MiB limit')
            html = args.html.read_text(encoding='utf-8-sig')
            source = validate_url(args.source_url) if args.source_url else None
            via = 'offline-html'
        else:
            fetch = fetch_browser if args.mode == 'browser' else fetch_http
            html, source = fetch(args.url, args.timeout)
            via = args.mode
        article = extract_article(html, source, via)
        result = render(article, args.format)
        if args.output:
            # Parent must already exist; exclusive creation also protects against races.
            with args.output.open('x', encoding='utf-8', newline='\n') as handle:
                handle.write(result)
            print(json.dumps({'output': str(args.output.resolve()), 'title': article['title'],
                              'characters': len(article['text']), 'warnings': article['warnings']}, ensure_ascii=False), file=sys.stderr)
        else:
            sys.stdout.write(result)
        return 0
    except (ArticleError, OSError, UnicodeError, ValueError) as exc:
        print(f'ERROR: {exc}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
        sys.stderr.reconfigure(encoding='utf-8')
    sys.exit(main())

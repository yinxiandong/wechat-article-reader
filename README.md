# wechat-article-reader

微信公众号文章读取与 Markdown 归档技能。Python 3.10+，支持 URL 获取和离线 HTML，按需使用浏览器渲染；输出 Markdown、纯文本或 JSON。

## 安装

克隆本仓库后，整个仓库目录就是一个完整技能包，不需要其他技能或 AutoCode。将完整目录复制到 `~/.codex/skills/wechat-article-reader/` 或 `~/.claude/skills/wechat-article-reader/`，已有同名安装先检查并备份，避免混合覆盖。若设置了 `CODEX_HOME`，Codex 安装目标改为其下的 `skills/wechat-article-reader/`。

在自己的虚拟环境中安装依赖，新建会话使用 `$wechat-article-reader`：

```bash
python -m pip install -r requirements.txt
python scripts/fetch_wechat_article.py "https://mp.weixin.qq.com/s/ARTICLE_ID"
```

> 使用 $wechat-article-reader 读取这篇微信文章，保存到我指定目录，并根据正文总结要点。

## 用法

```bash
# 输出 Markdown 到已有工作目录；已存在文件会报错
python scripts/fetch_wechat_article.py "URL" --output article.md
# 纯文本 / 含元信息的 JSON
python scripts/fetch_wechat_article.py "URL" --format text
python scripts/fetch_wechat_article.py "URL" --format json --output article.json
# 离线：不访问网络
python scripts/fetch_wechat_article.py --html saved.html --source-url "URL" --output article.md
# 可选浏览器模式
python -m pip install -r requirements-browser.txt
python -m playwright install chromium
python scripts/fetch_wechat_article.py "URL" --mode browser --output article.md
```

不指定 `--output` 时内容写 stdout；诊断写 stderr。成功返回 0，输入、读取或转换失败返回 2。输出目录需事先存在。默认超时 30 秒，`--timeout` 最大 180 秒；HTTP 内容上限 12 MiB。离线模式同样限制输入大小。只接受 HTTPS 和精确微信文章域名，HTTP 每跳校验重定向目标。

## 改进及限制

参考 `yy-wechat-to-markdown` 重新实现 Python 版本，保留来源及 Apache-2.0 许可证。使用 DOM 处理嵌套正文；保留代码、列表、表格及懒加载图片；输出安全转义的 YAML 元信息，区分作者、公众号、发布时间和读取时间。不会将当天读取时间当作文章发表日期。

未识别到正文、识别到已知付费/验证提示或 HTTP 异常时明确失败，不自动重试绕过限制。可选浏览器模式不读取本机登录配置。未保证识别所有微信页面变体、截断预览或隐藏付费内容；需对目标文章核对结尾和重要图表。

图片保留远程地址，不下载；音视频不转录。不获取评论、阅读量或账号历史文章。不发布、不转发文章。文章内容不随技能公开提交。source 保留 URL 查询参数，分享归档前检查是否含临时访问标识。

## 目录与验证

```text
wechat-article-reader/
├── SKILL.md
├── scripts/fetch_wechat_article.py
├── requirements.txt
├── requirements-browser.txt
├── requirements-dev.txt
├── references/implementation.md
├── tests/test_reader.py
├── LICENSE
└── NOTICE
```

```bash
python -m pip install -r requirements-dev.txt
python -m unittest discover -s tests -v
```

测试以虚构 HTML 和模拟 HTTP 为主；不将单元测试通过视为任意微信链接可读。来源、具体改动和验证边界见 [implementation](references/implementation.md)。

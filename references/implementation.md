# 来源、实现及验证边界

## 来源

- 参考：bulls-cows/skills 的 `skills/yy-wechat-to-markdown`。
- 固定来源提交：`23aa3a0d86dd369e128c2e10e4e244bf1b393943`。
- 源码：`https://github.com/bulls-cows/skills/tree/23aa3a0d86dd369e128c2e10e4e244bf1b393943/skills/yy-wechat-to-markdown`。
- 上游根 LICENSE 为 Apache-2.0；本包保留完整 LICENSE，并在 NOTICE 中标注来源和改写范围。
- Hermes 的 scrapling 技能用于评估通用抓取方案，本包未复制其代码，也不依赖 Hermes、Scrapling 或 Parallel 服务。

## 改写范围

2026-11-26 将上游 TypeScript/正则转换流程改写为 Python/BeautifulSoup/markdownify：

- 精确解析 URL 主机，校验 HTTP 重定向；限制超时与响应大小。
- DOM 提取 `#js_content`，兼容 `.rich_media_content`，不使用嵌套 div 正则或 100 字符阈值。
- 标题依次使用可见标题、Open Graph、已声明的 JS 字符串；作者、公众号分别记录。
- 发布时间来自页面或 Unix 时间戳；缺失为 null，UTC 读取时间单独记录。
- 微信图片优先 `data-src`，移除脚本等非正文节点，保留标准 Markdown 结构。
- 显式支持离线 HTML、JSON、文本及可选 Playwright；不默认自动回退或安装依赖。
- 文件独占创建；转换失败时不创建归档。图片仅保留链接。

## 排错

- HTTP 状态错误：说明状态，检查文章是否仍公开可访问；不循环重试。
- 缺少正文/验证页：可尝试显式浏览器模式，仍受限则使用用户有权提供的 HTML。
- 浏览器缺失：在选定环境安装 requirements-browser.txt，然后 `python -m playwright install chromium`。
- 发布时间缺失：保留 null，不推断为当天。
- 输出已存在：使用新路径，不覆盖旧结果。

## 验证范围

自动化测试覆盖嵌套正文尾部、短文、纯图片正文、懒加载图、表格和代码、元信息转义、日期区分、域名与重定向限制、响应大小、错误页、离线执行及拒绝覆盖。所有测试素材均为虚构内容。

浏览器限制针对页面导航，未声称对页面全部子资源提供网络隔离。12 MiB 限制在 HTTP 流读取及浏览器 HTML 生成后分别检查；浏览器模式不是总网络流量限制。默认不支持登录、付费或验证码自动处理，也不保证识别所有预览页。

### 2026-11-26 本地验证记录

- Windows / Python 3.13.7；requests 2.32.4、beautifulsoup4 4.14.3、markdownify 1.2.2。
- 12 项行为测试通过；单独复制脚本后在临时目录完成离线 JSON 转换，未依赖仓库绝对路径。
- skill-creator 的 quick_validate.py 结构校验通过。
- Playwright 1.57.0 的 Chromium 启动及内存页面读取通过；未验证浏览器模式的真实微信文章端到端获取。
- 未取得用户目标文章，未进行真实微信公众号正文的成功读取验证。HTTP 测试使用模拟响应，不能证明线上反爬环境可用。
- GitHub Actions 定义了 Python 3.10 / 3.13 单元测试；实际远程执行状态以 Actions 运行结果为准。

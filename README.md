# wechat-article-reader

微信公众号文章读取、评价、五维评分与 Markdown 归档技能。Python 3.10+，支持 URL 获取和离线 HTML，按需使用浏览器渲染；输出 Markdown、纯文本或 JSON。

## 安装

克隆本仓库后，整个仓库目录就是一个完整技能包，不需要其他技能或 AutoCode。将完整目录复制到 `~/.codex/skills/wechat-article-reader/` 或 `~/.claude/skills/wechat-article-reader/`，已有同名安装先检查并备份，避免混合覆盖。若设置了 `CODEX_HOME`，Codex 安装目标改为其下的 `skills/wechat-article-reader/`。

在自己的虚拟环境中安装依赖，新建会话使用 `$wechat-article-reader`：

```bash
python -m pip install -r requirements.txt
python scripts/fetch_wechat_article.py "https://mp.weixin.qq.com/s/ARTICLE_ID"
```

> 使用 $wechat-article-reader 读取这篇微信文章，保存到我指定目录，并给出摘要、评价和五维评分。

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

不指定 `--output` 时内容写 stdout；诊断写 stderr。JSON/Markdown 元信息包含完整性诊断（正文长度、标题/代码/表格/图片数、结尾片段与正文哈希）；这些只帮助检查，不是全文完整或质量高的证明。错误有 verification_required、article_deleted、login_required、paid_or_restricted、body_missing 等可区分代码。成功返回 0，输入、读取或转换失败返回 2。输出目录需事先存在。默认超时 30 秒，`--timeout` 最大 180 秒；HTTP 内容上限 12 MiB。离线模式同样限制输入大小。只接受 HTTPS 和精确微信文章域名，HTTP 每跳校验重定向目标。

## 评分与评价（恢复旧体系）

默认使用原 wechat 2.8.0 的五个维度，各整数 0–5、等权满分 25：**信息密度、论证质量、原创性、可信度、实用价值**。先识别七类文章，再输出摘要、2–3 个分析角度、可借鉴点、一句话判断、分项依据及核验边界。用户只要求原文时跳过。

评分由当前助手结合全文完成，脚本负责依据校验、加总和记录，不靠关键词自动评分、不需要新增模型 API key。详见 [评分指南](references/scoring.md) 和 [结构化评分锚点](assets/scoring-rubric.json)。

```bash
python scripts/fetch_wechat_article.py "URL" --format json --output article.json
python scripts/review_workspace.py --workspace "用户点评工作区" init
# 助手读完整正文和评分指南，生成 review.json；格式见评分指南
python scripts/review_workspace.py --workspace "用户点评工作区" record --article article.json --review review.json
python scripts/review_workspace.py --workspace "用户点评工作区" status
python scripts/review_workspace.py --workspace "用户点评工作区" show --review-id "返回的ID"
```

点评工作区必须位于技能包外。评分绑定文章及 profile 哈希，每维需正文短摘录；同一篇/同一规则的重复记录不重复计数。部分内容明确标“暂评”；未核验的事实、代码和原创性均说明范围。

## 评分进化

旧版“每 5 篇 / 周期 / 手动 → Reflect → Bounded Edit → Gate”已整合，补上原来缺失的计数状态与回归种子。固定原五维评分锚点，仅追加有案例依据的校准说明；门槛为改善案例比例 **>50% 且零退步**，候选还需通过全部七类种子的预期范围和必要观察点。失败保留原 profile，历史评分不覆盖。

```bash
python scripts/review_workspace.py --workspace "用户点评工作区" reflect --trigger manual
# 按进化指南分析实际点评与反馈，准备、评估、校验和应用候选
```

详见 [进化指南](references/evolution.md)。未配置云端任务时，调用 status 检查满 7 天是否到期。普通阅读不调用模型 API；定时进化另用专用密钥，不注册本机后台任务、不上传私人记录。Gate 校验结构和数值，语义评价仍需审阅。

## GitHub 每周进化 PR

提供每周日北京时间 10:00 和手动触发工作流。配置 `EVOLUTION_OPENAI_API_KEY`、`EVOLUTION_MODEL` 和人工脱敏的 `EVOLUTION_FEEDBACK_JSON` 后，生成补充规则，分别评测基线与候选，通过 Gate 和测试才创建 PR。仅更新公共校准说明与合成报告，不自动合并、不修改技能代码。

缺配置会报告跳过。原文、点评状态、私有反馈和模型响应不公开；规则合并后，本机通过 `git pull --ff-only` 和 `review_workspace.py ... sync-profile` 同步，保留历史点评。

设置、脱敏导出、费用和同步命令见 [GitHub 定时进化与同步](references/github-evolution.md)。

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
├── assets/scoring-rubric.json
├── assets/eval-seed.json
├── assets/published-profiles.json
├── .github/workflows/scoring-evolution.yml
├── scripts/review_workspace.py
├── scripts/scheduled_evolution.py
├── references/scoring.md
├── references/evolution.md
├── references/github-evolution.md
├── references/implementation.md
├── tests/test_reader.py
├── tests/test_reviews.py
├── tests/test_cloud.py
├── LICENSE
└── NOTICE
```

```bash
python -m pip install -r requirements-dev.txt
python -m unittest discover -s tests -v
```

测试以虚构 HTML 和模拟 HTTP 为主；不将单元测试通过视为任意微信链接可读。来源、具体改动和验证边界见 [implementation](references/implementation.md)。

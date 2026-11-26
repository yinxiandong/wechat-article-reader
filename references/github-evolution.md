# GitHub 定时进化与版本同步

## 运行方式

每周日北京时间 10:00（UTC 02:00）运行，也可在 Actions → Weekly scoring evolution → Run workflow 手动启动。GitHub 调度可能延迟，不保证准点。没有配置、反馈版本过时、已有待审进化 PR、没有合理修改或未通过 Gate 时，不创建更新 PR。缺配置会在运行摘要列出名称，不伪装成完成模型评测。

自动任务只追加公共 profile 的 1–3 条校准说明和合成样例数值报告。不自动合并、不改五维评分锚点、不修改技能代码。SKILL.md 和 Python 代码仍通过普通开发、测试、PR 更新。本地每 5 篇复盘继续可用。

## 配置 GitHub

在仓库 Settings → Secrets and variables → Actions 设置：

| 类型 | 名称 | 内容 |
| --- | --- | --- |
| Secret | EVOLUTION_OPENAI_API_KEY | 专用于定时评测的 OpenAI API key |
| Variable | EVOLUTION_MODEL | 支持 Chat Completions JSON mode 的模型 ID |
| Secret | EVOLUTION_FEEDBACK_JSON | 下文导出的脱敏案例 JSON 全文 |

请求固定发往 OpenAI 官方 HTTPS API。不复用 Codex 登录态、不从本机上传已有密钥。每次最多 15 次请求（提案 1 次、七例基线和候选各一次），每次最多 4000 输出 token、60 秒超时，不自动重试。实际费用取决于模型和输入；使用专用项目密钥和预算监控。模型需支持 max_completion_tokens、JSON object 输出及 system/user 消息，不支持时失败，不自动换模型。

Settings → Actions → General → Workflow permissions 需要允许 GitHub Actions 创建 PR；组织策略也可能限制。工作流声明 contents: write 和 pull-requests: write，令牌仅运行时使用，不配置长期 GitHub PAT。默认 GITHUB_TOKEN 创建的 PR 不触发普通 pull_request 工作流，因此发布前已对候选运行全部单元测试；如分支保护要求额外检查，维护者应触发获准检查后合并。

## 准备脱敏反馈

不要上传 review-state.json。先在技能包外创建 lessons.json：对象的键是已有 review_id，值是人工编写的校准教训。例如“观点文的实践价值可以来自判断框架，但不要把概念示例称为真实实验”。不要包含原文、URL、姓名、账号、路径、身份信息、密钥或私人反馈全文。

```bash
python scripts/scheduled_evolution.py export-feedback --workspace "/用户点评工作区" --lessons "/私有目录/lessons.json" --output "/私有目录/evolution-feedback.json" --confirm-sanitized
```

只导出匿名案例 ID、文章类型、原五维分数和手工教训，绑定当前 profile 哈希；不含标题、作者、来源、正文、原点评或原始反馈。字段白名单和常见敏感信息检查不能替代人工脱敏。已有输出拒绝覆盖。最多 50 例，JSON 上限 48,000 字节，留出 Secret 储存余量。

将文件全文存入 EVOLUTION_FEEDBACK_JSON。如已安装 gh，可用 PowerShell：

```powershell
Get-Content -Raw -Encoding UTF8 "私有文件路径" | gh secret set EVOLUTION_FEEDBACK_JSON --repo yinxiandong/wechat-article-reader
```

脱敏反馈不进入 Git。合并新版 profile 后，同步本地再导出新反馈；旧反馈不会再次升级新版本。未修改配置的每周失败/不通过仍可能产生模型费用，排查后再运行。

## 评测与公开内容

提案模型只看到规则、当前 profile 和脱敏案例，不看回归答案。基线和候选分别请求，评测相同七类虚构样例；均不提供预期分数区间或其他请求结果。沿用 Gate：改善比例 >50%、零退步、候选全部通过。基线本来正确时不制造低分。

公共 PR 仅含 assets/published-profiles.json 和 assets/evolution-report.json。报告包含合成分数、误差、观察点键和哈希；不发布自由文本理由、用户案例或原始响应，不上传私有构建产物。哈希仅核对一致性，相同模型评测也不是独立审查。合并人需检查新说明的依据、隐私和评价边界。

## 本机同步

合并 PR 只更新 GitHub。本机推荐使用独立 Git clone 安装，确认工作区干净后执行：

```bash
git -C "/实际技能目录/wechat-article-reader" pull --ff-only origin main
python "/实际技能目录/wechat-article-reader/scripts/review_workspace.py" --workspace "/用户点评工作区" sync-profile
```

sync-profile 只追加已发布版本，保留历史点评；未结束复盘或本地 profile 分叉会拒绝，需核对迁移。新工作区 init 直接采用最新发布链。仅 pull 代码不改已有工作区规则，评分前先 sync-profile，再读取 status 的 active_profile 和哈希。

复制安装需先备份完整目录，再用审核后的仓库版本替换完整技能包，避免混合覆盖。普通文章点评仍由当前助手执行，无需 API 密钥。本流程不安装本机后台任务。

## 排查

运行摘要：not_configured 为缺配置；feedback_profile_stale 为需同步再导出；no_supported_changes 为无合理修改；regression_gate_rejected 为保留规则。网络、JSON、模型格式错误会失败，不创建 PR。

如果推送分支成功但 PR 权限失败，先检查 automation/scoring-evolution-* 分支并手动创建 PR，避免重复评测费用。已有待审规则 PR 会阻止后续评测。

## 官方依据

GitHub Actions 工作流语法、令牌权限和定时调度：docs.github.com 的 workflow-syntax-for-github-actions、automatic-token-authentication 和 events-that-trigger-workflows。模型请求参数：OpenAI API Reference 的 Chat Completions create。实施时以官方文档和实际仓库策略为准。

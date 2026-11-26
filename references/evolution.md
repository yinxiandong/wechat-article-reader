# 评分进化：Reflect → Bounded Edit → Gate

恢复旧 wechat 2.8.0 的三类触发和门槛，并补齐本地工具。旧目录只找回了规则，没有找到原计数器或 eval-seed，当前七类虚构种子为重新建立，不宣称继承历史评测结果。

## 触发与记录

- 计数：每次成功 record 自动计数；since_last_reflect >=5 时返回 reflect_due=true。助手看到后在本轮启动 Reflect。
- 周期：每 7 天触发检查；原约定为每周日 10:00。本仓库新增 GitHub 定时规则 PR 流程，配置见 [github-evolution.md](github-evolution.md)；本地复盘仍可由已有调度器调用 status 后 `reflect --trigger weekly`。不安装本机计划任务，未配置时只在下次调用发现到期。
- 手动：用户说“进化一下”“Reflect”“优化评分”时，`reflect --trigger manual` 立即建立快照。

```bash
python "技能目录/scripts/review_workspace.py" --workspace "工作区" status
python "技能目录/scripts/review_workspace.py" --workspace "工作区" feedback --review-id "ID" --note "用户纠正和理由"
python "技能目录/scripts/review_workspace.py" --workspace "工作区" reflect --trigger count
```

在本轮真正读完 snapshot 指定的点评和 feedback，归纳过度苛刻、类型错位、无证据批评、尺度漂移等反例。没有具体问题时用 `close-reflection --reflection-id ID --reason "核对结果"` 关闭，不强造改动。重复 reflect 返回同一个 open 快照。

## 有限修改

只增加 1–3 条有真实点评 ID 依据的补充解释，每条不超过 1000 字。五维、0–5 刻度、等权总分及原始锚点固定；需要修改这些时另做显式版本迁移和重标定，不走自动进化。历史 profile 和评分保留，不覆盖旧分数。本地校准 profile 存用户工作区。另行配置的 GitHub 流程可将通过门槛的公共补充规则提交 PR，见 [github-evolution.md](github-evolution.md)；不自动合并。

changes.json 是对象数组，每项含 guidance、reason、evidence_review_ids。使用 prepare 生成含前后 profile 哈希、每个 seed 哈希的待评估 proposal：

```bash
python "技能目录/scripts/review_workspace.py" --workspace "工作区" prepare --reflection-id "ID" --changes "changes.json" --output "proposal.json"
```

prepare 中空值只是待填写评测表，不可直接通过。读取 reflect 输出中的 evaluation_seed，以相同模型/上下文预算在基线和候选规则下分别评分，记录每条理由和检查项解释；不得为了通过 Gate 反推分数。每条 row 包含 case_id、case_sha256、scores（五维整数）、observations（required_observations 中实际识别出的键及解释）、rationale。

候选后评测使用 prepare 返回的 after_profile_sha256；before 必须标明当前 profile。填完两个结果数组后：

```bash
python "技能目录/scripts/review_workspace.py" --workspace "工作区" gate --proposal "proposal.json"
python "技能目录/scripts/review_workspace.py" --workspace "工作区" apply --proposal "proposal.json"
```

## Gate 定义及局限

每例误差=五维超出预期区间的距离之和 + 缺失必要观察点数量×2。候选需全部落在种子预期范围内、必要观察点齐全；改善案例数/全部案例数 >50%，且任何案例都不能退步。旧门槛“改善率>50%且0退回”在此具体化。

基线已经全部正确、或只改善少数案例时，不能声称达到门槛，保留原规则；可关闭反思并记录未采用理由。Gate 不等同语义真伪判定：脚本能校验哈希、数值和覆盖，不能证明模型真实独立重评或解释质量。必须审阅前后理由，必要时扩大种子/人工评审；不要用同一个自评结果冒充独立审查。

apply 会重新运行 Gate，并拒绝过时基线、修改过的种子或重复应用。通过后仅追加新 profile，保存提案和门禁结果，更新 last_reflect、reviewed_through；反思快照创建之后新增的点评仍计入下一轮。失败时不改 profile/计数。原子写与独占锁保护状态；锁残留需先核对上次进程和状态再清理，不盲删。

用户已授权接入定时规则 PR；仅配置好的 GitHub 工作流可推送规则候选分支并创建 PR，不自动合并。普通文章阅读和本地复盘不会自动推送。用户工作区记录不是 Codex memories，本技能不编辑全局记忆目录。不要把历史文章、用户反馈、source 参数或运行状态推到公开仓库。

prepare/show 的 `--output` 以 UTF-8 独占创建文件；在 Windows PowerShell 中也优先使用该参数，避免重定向生成 UTF-16 文本。

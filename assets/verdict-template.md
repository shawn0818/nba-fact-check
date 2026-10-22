# 核查输出模板

每次核查输出两部分：先是状态块，再是核查小结。状态块写好后用 `scripts/check_verdict.py` 检查一遍。

完整示例见 `assets/examples/`（七个真实案例）。

## 一、状态块（YAML）

```yaml
criteria_version: nba-fact-check/7   # 本 skill 的判据版本；判据改动时递增，缓存判定必须带上它
checked_at: 2026-10-17T15:30+08:00   # 截止时间，北京时间
supersedes: null                     # 复查时填上一份状态块的 checked_at，旧块保留，形成时间线
change: null                         # supersedes 不为空时必填：{type: 更正 / 更新, note: "改了什么、为什么"}
next_check: 2026-10-18               # 按 references/judging.md 第四节的复查节奏填写；不再复查时写 null
claim: "原始说法，逐字照录"
claim_source:                        # 这条说法是在哪看到的
  url: https://...
  publisher: 虎扑流言板
  published: 2026-10-17
  timestamp_verified: true           # 发布时刻是否核实过；只看到索引快照、核不了原页时写 false
english_source:                      # 说法是中文时必填（检查脚本会拦）；英文说法可省略
  found: true                        # 是否识别出中文稿**实际依据的**英文上游（直接读到原文，或通过可靠媒体逐字收录确认了内容和账号的社媒原帖）
  directly_accessed: true            # 是否读到了这份原始出处的原文本身（原站原文的完整索引快照也算）；认出了出处但读不到原文（社媒原帖常这样）写 false
  independent_primary: false         # 是否找到了能独立核事实的一手来源（官方数据、官方公告）；found 为 false 时必填，为 true 时可省略
  who: Shams Charania                # found 为 true 时：英文原文是谁说的
  url: https://...
  excerpt: "英文原文里对应的那一句，逐字"   # found 为 true 且读到了原文时必填；没读到原文（directly_accessed: false）可以留空，或者摘转述里的句子并写 excerpt_source（摘自哪篇）
  searched: []                       # found 为 false 时必填：搜过的英文检索词
  zh_earlier: false                  # 中文稿是否早于任何英文报道（是的话很可能起源于社媒猜测）；时间核不了写 null
earliest_source:                     # 内容本身首次公开（那次发言、那条帖子），不是第一篇报道（报道放进 evidence）；追不到就写 null 并在 notes 里说明。published 填它公开发生的时间，录像后来才上传的不算
  url: https://...
  who: Shams Charania
  tier: insider                      # official / insider / beat / outlet / aggregate / farm / none
  published: 2026-10-16
points:                              # 逐个核查点
  - question: "他原话是什么"
    asserts: "照原说法摘录被核查的那句断言"   # 不是自己改写的中性问题；标题暗示的写"（标题暗示）……"
    scope: attribution               # attribution（核归因：某人是否这样说过、报道过）/ fact（核事实：这件事是否属实）
    verdict: 措辞放大                # 准确 / 措辞放大 / 误导 / 虚假 / 查无出处 / 无法证实 / 不可核查
    # searched: []                   # 查无出处、无法证实的点：搜过的检索词和渠道
    # basis: reports                 # 事实点判准确时必填：primary（有一手证据）/ reports（两个以上独立报道一致、至少一个具名记者）
    # basis_note: "……"               # basis 为 reports 时必填：哪几家、谁具名、为什么互相独立（同一稿源的转载不算独立）
    finding: "原话是 X，中文转述写成 Y"
    evidence: [e1, e2]
  - question: "他是否在 Instagram 上发了这条否认"
    asserts: "詹姆斯社媒辟谣：恩比德训练从未迟到过"
    scope: attribution
    verdict: 准确
    finding: "……"
    evidence: [e3, e4]               # 只列直接支撑这一点的证据，不要挂无关证据凑层级
    l3_ok:                           # 只有转述（L3）支撑、需要正面证据的判定时必填，三条硬条件见 SKILL.md 的 L3 规则
      platform: Instagram            # X / Instagram / Threads，且实际试过打不开
      verbatim: [e3, e4]             # 至少两条用原文语言逐字收录帖子的证据（中文译文不算），须在本点 evidence 里
      handle: "e3 内嵌截图显示 @kingjames；e4 正文写明 James posted on his Instagram Story"   # 每一家怎么点明发帖人和平台；对不上具体账号就写"账号未直接核对"
      counter_evidence: "专门检索伪造、仿冒、更正，未见；帖子删除或 Story 过期不算反证"
overall: 措辞放大                    # 总标签：按核心事实断言给，"不可核查"的点不参与汇总
out_of_scope:                        # 没打算核的实质断言；不写的话复核者分不清是有意不核还是漏了
  - asserts: "稿中单程 6000-9000 美元的包机价格"   # 照原说法摘录
    reason: "与核心问题无关，本次未核"            # 必填
overall_scope: null                 # 核查点里既有准确、又有措辞放大/误导/虚假/查无出处/无法证实时必填：总标签管哪部分，其余是什么结论
event:                               # 只有涉及会推进的事件（交易、伤病、去留、调查）时填写
  modality: 有望                     # 在谈 / 有望 / 已达成 / 已完成 / 官宣；不是交易类、套不上这五格时写 none
  status: 传闻中                     # 传闻中 / 多方跟进 / 已证实 / 本人辟谣 / 已证伪 / 不了了之
  independent_sources: 1
  scope: null                        # status 管的是哪件事；一条说法里几件事状态不同时必写（例如"已证实只指实际缺阵"）
evidence:
  e1:
    url: https://...
    publisher: ...
    published: 2026-10-16            # 文章发布时间；比赛页、球员页这类没有发布时间的写 null
    event_at: null                   # 证据所记录事件的时间（例如开赛时间），和发布时间分开
    retrieved_at: null               # 我们取得这条证据的时间
    locator: null                    # 证据里支撑结论的位置：视频时间段（"21:04-21:22"）、段落、表格行；复核者靠它定位
    access_method: live              # 怎么拿到的：live（实时页面）/ raw_html（原始网页源码）/ index_snapshot（搜索索引快照）/ relay（读的是转述）
    kind: primary                    # primary / secondary
    access: full                     # full / L1 / L2 / L3：这条证据支撑的内容是在哪一级读到的；读的是转述就写 L3。见 references/paywalled-sources.md
    supports: true                   # true 支持 / false 反驳——对引用它的那个核查点而言；同一条证据支持一点、反驳另一点时，在 note 里写清
    note: "一句话说明它证明了什么"
  e2: { ... }
timeline:                            # 说法含先后顺序或因果时必填；所有时间换算到同一时区
  - at: 2026-07-06                   # 日期，需要时带时刻
    tz: ET                           # ET / PT / 北京 / UTC
    event: "热火 YouTube 总监招聘启事已挂出"
    evidence: e1                     # 对应 evidence 的编号
  - at: 2026-07-21
    tz: ET
    event: "热火误公开入队发布会视频"
    evidence: e2
claimed_order: "误发视频 → 解雇员工 → 次日招人"   # 说法宣称的顺序；和 timeline 还原的顺序对照
review:                              # 发布前的独立复核；publishable 为 true 时必须是 passed
  status: pending                    # passed / pending / failed
  by: null                           # 谁复核的，例如"另一个 agent（模型 X）"
  at: null
  note: null                         # 复核意见；failed 时写清分歧
missing: "还缺什么证据才能把结论往前推一步"
notes: "其他需要交代的情况"
display:                             # 给渲染器用的展示字段，规则见下文
  layout: quote                      # quote / chain / ladder / number / plain
  headline: "真相，一句话"
  claim_short: "被核查的说法，压缩版"
  stamp: 措辞放大
  why: "为什么是这个判定，一句话"
  quote_pair: { ... }                # layout 为 quote 时必填
  chain: [ ... ]                     # layout 为 chain 时必填
  ladder: { ... }                    # layout 为 ladder 时必填
  number: { ... }                    # layout 为 number 时必填
  sources_line: "依据：……"
  cutoff: "截至 10-17 15:30"
  publishable: true
  review_reason: null
```

## 二、display 段

`display` 是**表现层的原料**：渲染器（信息图、卡片、微博正文）只读这一段，按版式排版，不再调用模型写字。所以这里的每个字都要在写进来的时候就核对好——图片一旦发出去，改起来比改文字贵得多。

### 三条硬规则

1. **只搬运，不新增。** display 里出现的数字、人名、机构，必须来自**已核实的结果**：各核查点的 `finding`、`evidence`、英文源的 `who`/`excerpt`、时间线、事件。核查点的 `question`（待核的问题）和英文源的检索词不算。例外是展示**原说法**的三个字段——`claim_short`、`number.claimed`、`quote_pair.relayed`——它们本来就是在复述原说法，可以用原说法里的内容。`quote_pair.original` 要逐字出自证据。检查脚本会核对数字、外文专名和常见的中文机构名；中文人名和不带机构后缀的名字，仍要人工核。
2. **真相三明治：最大的字写真相。** `headline` 写的是核查后的事实，`claim_short` 才是被核查的说法，渲染时字号更小。不要把谣言印成大标题再盖一个"假"字的章——那等于帮它又传播了一遍。`headline` 不用问句。
3. **不放网址。** 网址都在 `evidence` 里；display 只写出处的名字。是否在图上放二维码、放哪个链接，由使用方决定。

### 写作细则

- **标题写具体事实，不写否定式。** "招聘早在误发前两周就挂出"优于"热火没有解雇员工"；"视频是预先准备的素材"优于"这不是泄露"。否定式标题会让读者先记住被否定的那个说法。
- **不用"假新闻"这个词**，判定只用标签表里的词。
- **没有传播数据，不写"广为流传""刷屏""热传"**；有数据就给约数（"超过 500 次转发"）。
- **普通网友不点名、不贴主页。** 涉及评论区、社媒上的普通用户时，写"有网友"即可；公众人物和机构可以点名。
- **只描述走样，不推断动机**：写"删掉了限定句"，不写"故意误导"。

### 通用字段

| 字段 | 必填 | 上限 | 说明 |
|---|---|---|---|
| `layout` | 是 | — | 选哪种版式，见下表 |
| `headline` | 是 | 20 字 | 真相。陈述句，措辞强度不得超过证据（遵守断言强度阶梯） |
| `claim_short` | 是 | 24 字 | 被核查的说法。保留原意，去掉"重磅""炸了"这类渲染词 |
| `stamp` | 是 | — | 判定标签，取值同 `points[].verdict`，通常等于 `overall` |
| `why` | 是 | 40 字 | 一句话说明判定依据 |
| `sources_line` | 是 | 60 字 | 例如"依据：ESPN 记者 Shams Charania；NBA 官方交易记录" |
| `cutoff` | 是 | — | "截至 MM-DD HH:MM"，和 `checked_at` 一致 |
| `publishable` | 是 | — | "可以对外发"：没有命中"交给人的情形"、并且 `review.status` 为 `passed` 时才能写 `true` |
| `review_reason` | `publishable: false` 时必填 | 40 字 | 卡在哪里、需要人做什么 |

字数按**显示宽度**算：汉字和全角标点算 1，英文字母、数字、半角符号算 0.5。所以"Maverick Carter"算 7.5，不算 15。`quote_pair.original` 的上限 220 也按这个算，大约是 440 个英文字符。

### 四种版式的专属字段

**`quote`：引语对照**（误译、断章取义、说话人张冠李戴）

```yaml
quote_pair:
  speaker: 勒布朗·詹姆斯             # 核实过的说话人
  original: "Yeah, he just read off the teleprompter. Ah, it wasn't good."   # 英文原话，逐字，≤220 字符
  faithful: "对，他就是照着提词器念的。嗯，不太行。"   # 我们的忠实译文，≤80 字
  relayed: "他只不过是照着提词器念台词罢了，那段广告拍得并不好"   # 被核查的中文转述，逐字，≤80 字
  drift: "原话没说什么不好，'广告拍得'是补出来的"   # 差在哪，≤30 字
  where: "Mind the Game 2026-27 赛季前瞻 1:27"     # 场合与时间点，≤40 字
```

- `original` 要包含判定所需的那一句**和它的下一句**（断章取义类尤其如此），超长时用"……"省略中间无关部分。
- `faithful` 是我们自己的译文，必须守断言强度阶梯和翻译陷阱清单；它是读者判断转述哪里走样的参照，自己不能走样。

**`chain`：溯源链**（"美媒称"、查无出处、出口转内销）

```yaml
chain:                               # 2 到 5 个节点，从看到说法的地方往源头排
  - who: 搜狐号                      # ≤12 字
    tier: aggregate                  # 同 earliest_source.tier
    note: "标题写成调查风波"          # ≤16 字，可省略
    drifted: true                    # 是否在这一环变形
  - who: FOS 会议发言
    tier: outlet
  - who: 托雷本人
    tier: outlet
```

至少要有一个节点 `drifted: true`，或者最后一个节点的 `who` 写"查无出处"。

**`ladder`：进度阶梯**（交易、伤病、去留、调查这类会推进的事件）

```yaml
ladder:
  subject: "某球员加盟某队"          # 这条阶梯描述的是什么事，≤16 字
  level: 有望                        # 当前所在格：在谈 / 有望 / 已达成 / 已完成 / 官宣
                                     # 不是交易类、套不上这五格的事件（例如"联盟是否调查"）写 none，只显示 status 和 history
  status: 多方跟进                   # 同 event.status
  history:                           # 历次复查，按时间排，最多显示最近 5 条
    - date: 2026-10-13
      note: "Shams 首报在谈"         # ≤16 字
    - date: 2026-10-15
      note: "Stein 跟进：接近达成"
```

**`number`：数字核对**（数据、纪录、合同金额）

```yaml
number:
  label: "首年薪资"                  # ≤12 字
  claimed: "388 万"                  # 说法里的数字，原样
  actual: "约 388 万美元"            # 核实后的数字；核不出来写"无法核实"
  unit_note: "美元，非人民币"        # ≤20 字，可省略
  basis: "Shams 报道总额与 Spotrac 第二年选项反推"   # 口径或算法，≤40 字
```

**`plain`：只有判定**（不可核查、证据太少不值得上图时）

只填通用字段即可。

### 选哪种版式

| 核查的主要问题出在 | layout |
|---|---|
| 原话被误译、删句、归错人 | `quote` |
| 出处被模糊（"美媒""据报道"）或根本没有出处 | `chain` |
| 事情进展到哪一步 | `ladder` |
| 数字、纪录、金额 | `number` |
| 不可核查，或只是一条简单的准确判定 | `plain` |

一条说法有多个问题时，选最能说明判定的那一个。其余的写进 `why` 或留在核查小结里，不要在一张图上塞多种版式。

## 三、核查小结（给人看）

```
【结论】一句话：这条说法是否成立，错在哪里。

【原话 vs 转述】（涉及引语或翻译时）
- 原文：……（出处、时间、场合）
- 转述：……
- 差别：……

【依据】
1. ……（链接）
2. ……（链接）

【事件进展】（涉及交易、伤病、去留时）
目前处于"……"阶段，依据是……。下一步要看到……才能确认。

【还缺什么】……

截至 YYYY-MM-DD HH:MM（北京时间）
```

写作要求：

- 先说结论，再说依据。
- 不写"据悉""网传"这类不指向任何人的说法；每条依据都要有链接或明确出处。
- 不复述虚假说法的煽动性部分，只交代核查所需的内容。
- 不表达立场。小结和 display 都是给使用者的原料，立场和语气由使用者在自己的内容里决定。

# 一手来源：地址与读法

下面的可达性是 2026-10-17 实测结果。"直连"指本机 curl / requests 直接请求，"Fetch"指 DeepSeek Fetch（见 `tool-limits.md`）。

## 交易、签约、裁员

**NBA 官方球员变动 JSON**（最适合区分"报道"和"官宣"）

- `https://stats.nba.com/js/data/playermovement/NBA_Player_Movement.json`
- 直连 200，约 4 MB，近万条，日粒度。字段包含日期、球员、球队、交易类型和描述。
- 只有正式完成的交易才会进来，所以"这里查得到"就是官宣级证据；"查不到"只说明还没正式完成，不说明是假的。
- 实测比 RealGM 晚约一天（勒布朗签 76人记在 07-26），核查"刚刚官宣"的消息时要考虑这个延迟。
- 2026-09 曾出现本机直连 TLS 握手失败、走代理才通的情况。连不上时先换网络路径再下结论。

**NBA.com 交易页** `https://www.nba.com/players/transactions`

- 由浏览器脚本渲染，Fetch 只拿到导航栏和 "Loading Transactions..."，**拿不到内容**。用上面的 JSON 代替。

**Hoops Rumors** `https://www.hoopsrumors.com/`

- Fetch 可读。每条都会写明依据（例如 "per a team press release"、"X of Y first reported"），是追溯首发者的好工具。它本身不是一手来源。

**HoopsHype 传闻页** `https://hoopshype.com/rumors/`

- Fetch 可读。逐条标注记者和原始出处，带时间戳。

## 伤病与出战状态

**NBA 官方伤病报告**

- 索引页：`https://official.nba.com/nba-injury-report-<赛季>-season/`，例如 `nba-injury-report-2025-26-season`。
  - 列表由脚本渲染，Fetch 只拿到标题。
  - 2026-27 赛季的索引页在 10-17 实测返回 404，可能还没上线，或者换了地址。
- PDF 直连可下载，文件名带发布时刻：
  - 2025-26 赛季起：`https://ak-static.cms.nba.com/referee/injury/Injury-Report_YYYY-MM-DD_HH_MMAM|PM.pdf`，例如 `Injury-Report_2026-04-12_05_00PM.pdf`。
  - 2024-25 赛季：`Injury-Report_YYYY-MM-DD_HHPM.pdf`，例如 `Injury-Report_2025-04-13_08PM.pdf`。
  - 不存在的时刻返回 403。比赛日一天有多份，取比赛开始前最近的一份。
- 报告只覆盖常规赛和季后赛，**季前赛没有官方伤病报告**，季前赛的出战信息只能依靠球队公告和跟队记者。
- 状态词含义见 `translation-traps.md`。

## 数据与纪录

**Basketball-Reference**

- 球员页：`https://www.basketball-reference.com/players/<首字母>/<id>.html`，勒布朗是 `j/jamesle01`。
- Fetch 可读（约 17 万字符，前面有一段 cookie 提示，跳过即可）。
- 适合核对生涯数据、逐场数据、历史排名。纪录类说法要按说法的口径（常规赛/季后赛、日期范围）自己复算。
- 它是可靠的二手整理，不是官方纪录。涉及"官方认证的纪录"时，以 NBA 官方或 Elias Sports Bureau 的口径为准。

**NBA.com 比赛页、球员页**（推荐，用 `scripts/nba_boxscore.py`）

- DeepSeek Fetch 把这些页面转成 Markdown 时，**球员姓名整列会丢**，有的只剩导航栏（2026-10-18 盲测实测），不能拿来对数字。
- 直接取同一公开页面的 HTML，解析里面的 `__NEXT_DATA__` JSON，能拿到带姓名和 personId 的逐人数据；`nba_boxscore.py` 已经封装好，并自动按"含并列、含替补"找全队最高。
- 比赛 ID 格式：季前赛 `001` 开头、常规赛 `002`、季后赛 `004`，例如 `0012600018`。只知道对阵和日期时，用 `nba_boxscore.py schedule <球队网址名> --date <日期>` 从球队赛程页查（日期用美东或北京都行）；用搜索找比赛页常常返回无关的历史页面。
- 输出会标出首发和替补：官方数据里没有 starter 字段，首发球员带位置（F/C/G），替补的位置为空。球员页的 gameLogs 是**最近若干场**，不保证是完整赛季。
- **半场、单节数据**：比赛页里内嵌了整场的逐回合记录（`playByPlay.actions`），`nba_boxscore.py game <ID> --periods 1-2` 按节复算。复算前先用同一份记录算全场，和官方全场表逐人逐项对、所选节次比分和官方分节比分对，对上了才用节次数据；上场时间靠换人事件推算，另有一行核对；正负值也靠在场推断，再单独对一遍全场官方正负值。2026-10-21 用 0012600034 实测：官方正负值把同一停表时刻的罚球算给换人之前的阵容，罚球中间换上来的人不算——照页面列表顺序算有 20 人对不上，按这条规则 30 人全部一致。复算的是**现在**的官方记录，赛后数据更正会让它和快讯发稿当时的数字不同，不能据此断定快讯当时就写错了。2026-10-19 实测，专用的半场数据接口（stats.nba.com 带节次参数、比赛页加 `?period=1stHalf`）返回的都不是数据，所以改走逐回合复算。
- 页面上的比赛时间是计划开赛时间，不是任何报道的发布时间。比赛页、球员页、官方接口是同一套统计来源，不算几个独立确认。

**NBA 官方数据接口**（stats.nba.com）

- 需要完整的浏览器请求头，只给一部分请求头时**不会报错，而是一直挂到超时**，很像被墙。
- 球员生涯逐季与总计：`nba_boxscore.py career <球员ID>`（`playercareerstats`，PerMode=Totals，常规赛和季后赛）。它会核对逐季合计（赛季中被交易时每队一行、另有一行 TOT，只算 TOT）和官方生涯总计，并算出投丢数。"30 分场次""连续 N 场"这类排他性纪录要逐场数据，生涯接口给不了，缺全量数据时按 SKILL.md 的规则进 `out_of_scope` 或判无法证实。
- 2026-10-20 实测：经本机代理出口时，整个 stats.nba.com 对任何请求都回 **200 加一张 Akamai 错误页**（"We are unable to process your request"），请求头给全也一样，urllib、requests、curl 都如此。状态码是 200，不看内容会当成成功；脚本遇到这种情况会直接报"被拦截"。同日又绕开代理、从国内家庭宽带直连测了一次（用 Cloudflare trace 确认确实没走代理），curl 拿到的还是这张页；在官网 www.nba.com/stats 页面里，生涯数据表也显示 NO DATA AVAILABLE。之后把 stats.nba.com 设成直连、再用真浏览器打开官网数据页，页面自己发出的 `playercareerstats`、`playerawards` 请求状态都是 0、拿不到数据。机房出口和国内家庭宽带、脚本和真浏览器四种组合全都不行，看起来这两类出口都被挡在外面（美国住宅网络大概率正常，但我们没有）。拿不到时，生涯和历史数据改用 Basketball-Reference（经搜索索引读快照）或别处保存下来的数据，用 `--from-raw` 读。

**本地数据库**（如果调用方有）

- 有本地 NBA 数据库时优先用它复算。数据类说法是唯一能**复算**判定的一类：不管用本地库、官方数据还是 Basketball-Reference，都是自己按口径重算，而不是采信谁的说法。

**StatMuse**

- 是数据查询工具，常被媒体引用（"per StatMuse"）。可以用来对照，但它的结果本身也需要按口径复核。

## 合同与金额

- **Spotrac**：`https://www.spotrac.com/nba/<球队>/`，Fetch 可读。实测抓回的球队页主要是未来年份的保障额与选项
  （例如"2027-28 Player Option $4,070,355"），当季逐人薪资不一定在抓到的内容里；需要当季精确金额时，换球员页或 HoopsHype 再查。
- **HoopsHype 薪资页**：可与 Spotrac 交叉对照。
- 两家数字不一致时，并列写出并注明来源，不要自己取平均或拼成一个区间。

## 言论与引语

- **找原视频**：中文引语稿很多转述自播客和发布会。`captions.py --find "节目名 嘉宾 日期"` 搜 YouTube 列出候选（只读搜索结果页，避免触发机器人验证；上传日期常拿不到）。不用为日期另发请求：`captions.py` 下载字幕时会一并打印并保存频道名、@账号、频道 ID 和上传时间，拿它核对是不是官方频道、哪天上传的。
- **采访原视频**：球队官方 YouTube 频道、NBA on ESPN 等。可以用 `yt-dlp` 下载英文字幕核对原话。
  只下字幕很快；要下视频或音频时，YouTube 需要 JS 运行时（`--js-runtimes node`），没有它会卡住不动或者只剩故事板格式，
  有 cookies 文件就一并带上。`--download-sections` 截片段在实测中经常卡住，直接下一个低码率的整段（360p 约 56 MB）再用 ffmpeg 本地切更快。
  自动字幕会有听写错误，关键词句要对照视频本身确认。
  **多语言视频**（有 AI 配音的采访常见）每种语言一条音轨，格式 ID 带后缀：2026-10-20 实测一条东契奇专访有 19 种语言，`140-0` 是阿拉伯语配音、`139-18` / `140-18` 才是英文原声（format_note 里写 original），裸的 `140` 不存在。`speakers.py` 会先列音轨、原声优先，并打印选中的是哪条；手动下载时先 `yt-dlp -F` 看清语言，**不要为了下载成功改用配音轨**。
- **球队官网视频**：媒体日、赛后采访常常只发在 NBA.com 球队官网（`https://www.nba.com/<球队>/videos/...`）。视频页 HTML 里内嵌的 `__NEXT_DATA__` JSON 中，`props.pageProps.pageObject.videoAssets.cc` 有 `srt` 和 `vtt` 两个字幕地址（形如 `https://cdn.nba.com/videos/teams/wordpress/media/sites/<球队>/<编号>_eng.srt`），`pageObject.date` 是发布时间、`pageObject.title` 是标题。`captions.py 视频页地址 "关键词"` 会自动找到并下载字幕；手动下载的用 `captions.py 文件.srt "关键词"`。这种 SRT 的毫秒有时用点号（`00:03:02.200`），脚本两种都认。页面请求和字幕下载各算一次检索。
- **说话人归属**（对谈、播客里"这句是谁说的"）。2026-10-17 实测过三种办法：
  1. **多模态模型听音频**（推荐，已做成 `scripts/speakers.py`）：截出包含争议段的 30 秒左右音频，里面要有一段**确定是谁在说**的话作参照，
     发给能听音频的模型（实测 OpenRouter 上的 `google/gemini-2.5-flash`，`input_audio` 传 mp3，一次约 0.002 美元），
     要求逐轮标注说话人。**至少跑两次**：一次给名字，一次只说"说了某句话的人记为 A"、不给任何声音描述，
     两次结论一致才采信。实测要点：
     - **参照用文字锚点**（此人说过的一句原话），不要只给时间。模型的时间感会漂，只给"0:11-0:14 说话的人"时，中立那遍几乎全标成 unknown。
     - **两遍按文字对齐，不按时间对齐**。模型返回的时间戳偏差可达 7 秒以上。
     - 模型自报的 high 置信度不能当证据；中立那遍可能在结尾陷入重复输出、给出超出音频长度的时间戳。
     - 两人笑着抢话的地方，两遍通常对不上，这正是该标"存疑"的地方。
  2. **看画面**：抽帧看镜头切给谁。特写通常是说话人，但播客常插听者的反应镜头；双人全景里人脸只有几十个像素，
     看不出嘴型，手势只能当旁证，实测还会误导。
  3. **测音高**：两人基频差得多时能粗分，但一有笑声、抢话就完全失效，只适合当旁证。
  - 实在判断不了，就写"归属存疑"，附上视频链接和时间点，请人听一下。
- **ASAP Sports**：`https://www.asapsports.com/`，有官方发布会文字实录，主要覆盖季后赛和总决赛等大型赛事，常规赛基本没有。
- **本人社媒**：搜索与抓取工具通常进不了 X 和 Instagram，见 `tool-limits.md` 里的间接方法。

## 网页存档

- archive.org 的 Fetch 在实测中失败（404），存档需要在浏览器里手动完成，或直接在报告里写明原始链接与访问时间。

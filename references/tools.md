# 工具详解

SKILL.md 的"工具速查"只列最常用的命令；这里是全部命令、参数和运行环境。

脚本都在 `scripts/` 下，只用 Python 标准库（`check_verdict.py` 读 YAML 时要 PyYAML）。需要 key 的脚本（`search.py`、`speakers.py`）都支持 `--env-file` 读含 key 的 `.env`；`nba_boxscore.py`、`domain_age.py` 不需要 key。

**运行环境**：Python 3.10+；`captions.py` 取 YouTube 字幕要 yt-dlp（球队官网视频页、字幕直链和本地 SRT/VTT 不用）；`nba_boxscore.py career` 要能访问 stats.nba.com（被拦截时会直接报出来）；`speakers.py` 要 yt-dlp 和 ffmpeg（没有系统 ffmpeg 时 `pip install imageio-ffmpeg`，脚本会自动用它自带的），下 YouTube 音视频还要 JS 运行时（node）。缺哪个就有哪类核查做不了，开始前先确认，做不了的在结论里写明。

| 要做的事 | 命令 |
|---|---|
| 把中文稿逐句拆出锚点（中文说法的第一步） | `python scripts/zh_anchors.py 文章.txt`，或 `--text "标题"` |
| 给检索记账（批量核查时必用） | `search.py`、`captions.py`、`speakers.py`、`nba_boxscore.py` 都加 `--log calls.jsonl --label <编号>`：每次搜索、抓取、页面请求、字幕或音频下载、列音频格式、模型调用各追加一行（含 `attempts`、`seconds`、`status`，不记 key） |
| 搜索（溯源、找反证） | `python scripts/search.py search "关键词 site:bbs.hupu.com after:2026-10-01"` |
| 搜索并带回索引里的正文（虎扑、Yahoo 这类抓不到的页面） | `python scripts/search.py search "..." --count 3 --content` |
| 抓一个网页 | `python scripts/search.py fetch URL --max-chars 4000` |
| 找原始播客、发布会视频（中文稿常转述它们） | `python scripts/captions.py --find "节目名 嘉宾 日期" --count 5` |
| 在采访视频字幕里定位原话 | `python scripts/captions.py 视频地址 "关键词" --out-dir evidence/caps`：YouTube 地址走 yt-dlp；NBA.com 球队官网视频页（`www.nba.com/<球队>/videos/...`）自动从页面数据里取 SRT 字幕；本地字幕直接 `captions.py 文件.srt "关键词"`（json3、SRT、VTT 按扩展名识别）。命中点离得近时合并成一段 |
| 核对视频来源（是不是官方频道、哪天上传） | 用 `captions.py` 下载字幕时会一并打印并保存（`<视频ID>.meta.json`）频道名、@账号、频道 ID、上传时间（UTC 和北京时间）；球队官网视频页给标题和发布时间。`--find` 列出的上传日期是"未知"的不必另查 |
| 判断对谈里某句是谁说的 | `python scripts/speakers.py 地址或文件 --from 66 --to 100 --ref "74-77=甲" --ref-text "甲说的一句原话" --other "乙" --lines "争议句" --save-raw 说话人.json`。重点句摘要分"一致 / 两遍说话人不一致 / 文字没匹配上（哪一遍）/ 匹配到多处 / 模型标了 unknown"，只有"一致"可采信；缩写（didn't = did not）和单个词（"Yes"）都能匹配。下载前先列出音轨，原声（original）优先，并打印选中的格式、语言、是不是原声；多语言视频的格式 ID 带后缀（`139-18`），要手动指定时用 `--audio-format`，**不要为了下载成功改用配音轨**片段里可能不止两个人（前半是另一名球员、旁白、解说），模型认不出的标"其他"；报告开头的"覆盖与参照检查"列出每遍轮次覆盖了多少时长、哪些时段没分到轮次、参照段和参照句对不对得上，有 ⚠ 时"一致"不代表覆盖完整，要去听列出的时段。 |
| 复核说话人判断（不联网、不花钱，可换重点句） | `python scripts/speakers.py --from-raw 说话人.json --lines "另一句"`；文件里有两遍的提示词、模型回复原文和解析出的轮次 |
| 官方交易记录 | 直连下载 `primary-sources.md` 里的球员变动 JSON，按球员筛 |
| 官方比赛数据、全队排名（含并列） | `python scripts/nba_boxscore.py game 0012600018 --team PHI --stat assists` |
| 一次看全部常用数据和各项最高 | `python scripts/nba_boxscore.py game 0012600018 --team PHI --all-stats`（得分、篮板、助攻、抢断、盖帽、失误、投篮、三分、罚球、时长） |
| 半场、单节数据（核半场快讯、单节表现） | `python scripts/nba_boxscore.py game 0012600034 --periods 1-2 --all-stats --save-raw evidence/0012600034.json`：用比赛页的逐回合记录按节复算，先拿全场复算和官方全场表逐人逐项对、所选节次比分和官方分节比分对；输出开头写"节次的计数数据可用"才能用，没通过时退出码 3。时长另有一行核对正负值（`--stat plusMinusPoints`，`--all-stats` 也带一列）按在场推断累计，单独一行核对全场正负值，过了才能用；同一停表时刻的罚球算给换人之前的阵容。 |
| 球员生涯逐季与总计（常规赛、季后赛；生涯得分、投丢数、季后赛场次这类） | `python scripts/nba_boxscore.py career 2544 --save-raw evidence/2544-career.json`：stats.nba.com playercareerstats，PerMode=Totals；自动核对逐季合计（转会赛季只算 TOT 行）和官方生涯总计，并算出投丢数（出手 − 命中）。"30 分场次""连续 N 场"这类要逐场数据，这个命令给不了。返回 200 却是网页时说明被拦截了：2026-10-20 实测，经代理出口、从国内家庭宽带直连都被拦，真浏览器打开官网数据页也拿不到数据，换出口不一定有用。拿不到时生涯和历史数据改用 Basketball-Reference 的搜索快照，或用别处存下来的数据 `--from-raw` |
| 保存官方数据作证据、离线复跑 | `nba_boxscore.py` 各子命令都支持 `--save-raw 路径`（页面内嵌的原始 JSON）和 `--from-raw 路径`（读回来，不联网） |
| 只知道对阵和日期，找比赛 ID | `python scripts/nba_boxscore.py schedule sixers --date 2026-10-05` |
| 球员生日、最近逐场数据 | `python scripts/nba_boxscore.py player https://www.nba.com/player/2544/lebron-james` |
| 查站点的域名注册时间（识别新注册的冒名站） | `python scripts/domain_age.py cntv-sports.com zhibo8.com` |
| 生成给复核者的盲审包（去掉作者判定） | `python scripts/review_packet.py 状态块.yaml > packet.md` |
| 比对复核结果，写入 review 字段 | `python scripts/review_compare.py 状态块.yaml 复核结果.yaml --write 复核后.yaml` |
| 改了规则或脚本后自检（离线，不花钱） | `python scripts/selftest.py` |
| 检查写好的状态块 | `python scripts/check_verdict.py 状态块.yaml` |

用到的 key：DeepSeek（搜索、抓取）、OpenRouter（说话人判断）。YouTube 下载音视频需要 `--js-runtimes node`。

补充：`search.py` 加 `--retries 0` 关掉自动重试（严格控制请求数时用），`--timeout 秒数` 设单次请求超时；账本里的 `attempts` 记了每次调用实际发出几次请求。

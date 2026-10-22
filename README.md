# nba-fact-check

一个给 AI agent 用的 NBA 新闻与传闻事实核查 skill。

An agent skill for fact-checking NBA news and rumors, with a focus on Chinese-language relays (Hupu, Zhibo8, Weibo) of English-language reporting. The skill itself is written in Chinese.

## 它做什么

给它一条说法——一条交易传闻、一句球员原话、一个数据纪录、一篇虎扑流言板或直播吧的中文稿——agent 按 skill 的流程：

1. 把中文稿逐句拆出数字、引语、信源这些"锚点"；
2. 追到英文原始出处（记者原稿、采访视频、官方公告），中文转述只当线索；
3. 按取证层级收集证据，找一次反证；
4. 逐点判定：准确 / 措辞放大 / 误导 / 虚假 / 查无出处 / 无法证实 / 不可核查；
5. 产出一份机器可读的状态块（YAML）和一份给人看的核查小结，并用检查脚本核对格式和"展示内容只搬运、不新增"。

它重点处理三类问题：英译中时情态和时态被抹平（"expected to sign"写成"将签约"）、传闻最终有没有成真、编造或张冠李戴的引语和数据。

## 安装

skill 就是这个目录。放到 agent 能找到 skill 的地方：

- **Claude Code**：克隆到 `~/.claude/skills/nba-fact-check`
- **Codex**：克隆到 `~/.agents/skills/nba-fact-check`
- **其他 agent**：让它先读 `SKILL.md`，再按需读 `references/` 下的文件

```bash
git clone https://github.com/shawn0818/nba-fact-check ~/.claude/skills/nba-fact-check
```

## 依赖

- Python 3.10+；脚本只用标准库，读写状态块的几个脚本要 `pip install pyyaml`
- 可选：`yt-dlp`（取 YouTube 字幕）、`ffmpeg` 或 `pip install imageio-ffmpeg`（判断说话人时截音频）、Node.js（yt-dlp 下载部分 YouTube 视频时要用）
- Key（只在用到对应脚本时需要，可以设环境变量，也可以用 `--env-file` 指向一个 `.env` 文件）：
  - `DEEPSEEK_API_KEY`：`scripts/search.py` 用 DeepSeek 的 Search / Fetch 接口检索和抓取网页
  - `OPENROUTER_API_KEY`：`scripts/speakers.py` 用能听音频的模型判断对谈里每句话是谁说的

agent 自带网页搜索时，不配 DeepSeek 也能用，只是检索记账、索引快照这些功能用不上。

## 目录

```
SKILL.md                  核心规则：铁律、流程、判定标签、输出格式
references/judging.md     判定与取证细则（拿不准时查）
references/tools.md       全部脚本命令和参数
references/*.md           信源分级、翻译陷阱、官方数据地址、付费墙取证、工具局限、复核流程、批量抽样
references/cases.md       实际核查和测试的完整记录（按需查）
assets/verdict-template.md  状态块模板
assets/examples/          7 个完整的状态块示例
scripts/                  锚点拆分、检索、字幕定位、说话人判断、官方数据、状态块检查、复核工具
tests/checker_cases.yaml  检查脚本的回归用例
```

## 自测

改了规则或脚本后跑一遍，离线、不花钱：

```bash
python scripts/selftest.py
```

## 判据版本

状态块里的 `criteria_version` 记录判定规则的版本（当前 `nba-fact-check/7`）。规则一改就递增；如果你缓存核查结论，版本号要放进缓存键，否则改了规则旧结论也不会失效。

## 已知局限

- **只做到"核查助手"**：结论要对外发布，先过独立复核（另一个 agent 或模型盲审，流程见 `references/review.md`），点名批评具体的人时再由人看一眼。实测两个模型逐点判定一致率约 92%。
- **社交媒体**：X、Instagram 原帖常读不到，要靠可靠媒体的逐字收录，有严格的使用条件。
- **生涯和历史数据**：stats.nba.com 会拦截一部分网络出口（实测机房 IP 和国内家庭宽带都被拦），拦截时生涯、历史排名类说法没法复算。
- **还没在真实案例里跑过的情形**：截图类说法、传闻在几周内的持续跟踪。

## 许可证

MIT，见 [LICENSE](LICENSE)。

# Design Morning Brief

每天自动搜索全球设计、咨询与战略资讯，使用 Gemini 完成分类、去重、价值评分和中文总结，再通过飞书群自定义机器人发送消息卡片。

## 搜索窗口

- 常规行业新闻：最近 48 小时
- 期刊、奖项、政策、展览及重大机构报告：最近 14 天

## GitHub Secrets

- `TAVILY_API_KEY`
- `GEMINI_API_KEY`
- `FEISHU_WEBHOOK_URL`
- `FEISHU_WEBHOOK_SECRET`

## 手动测试

进入 **Actions → Design Morning Brief → Run workflow**：

1. 先选 `test-feishu`，只验证飞书连接，不消耗 Tavily 或 Gemini 额度。
2. 测试成功后选 `full`，执行完整搜索、分析与发送。
3. `dry-run` 会完整生成早报，但不会发到飞书；可在 Action 的 Artifacts 下载输出。

## 自动时间

工作流默认每天 **08:07（Asia/Singapore）** 运行。选择 08:07 而不是整点，是为了减少 GitHub 定时队列在整点拥堵造成的延迟。

## 本地运行

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
# macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
python src/main.py --mode test-feishu
```

完整运行：

```bash
python src/main.py --mode full
```

# Design Intelligence Brief

每天自动生成一份面向设计、咨询与创新团队的全球设计情报与知识早报，并通过飞书群自定义机器人发送消息卡片。

## 核心变化：不再强行凑“最新新闻”

系统采用动态三层时间池：

1. **今日新讯**
   - 普通行业动态：最近 48 小时
   - 期刊、奖项、政策、学院、展览及重大机构发布：最近 14 天
2. **近期洞察**
   - 最近 6 个月的案例、报告、研究、组织实践与设计战略
3. **经典方法**
   - 默认最近 5 年的高质量方法、框架与研究
   - 若仍不足，可回溯更早的经典基础资料，但必须解释“为什么现在仍值得读”

程序先搜索新讯；若高质量内容不足，再自动搜索近期洞察和经典方法。旧资料会明确标记，不会伪装成今日新闻。

## 内容范围

- 工业设计
- 环境与空间设计
- 创新设计与系统设计
- 社会与服务设计
- 商业设计、设计咨询与设计战略
- 视觉设计、品牌、海报、字体与动态视觉
- 设计教育、研究、政策、奖项与专业机构

## 质量规则

- 只允许文章详情页、官方项目页或论文详情页进入正式报告
- 栏目页、标签页、搜索页、社交媒体和新闻稿转载只能作为线索
- 必须验证发布日期
- 公司公告必须保留“计划、目标、预计、拟议、最高可达”等限定词
- 每条旧资料必须包含 `why_now`，解释其当下价值
- 不再设置“至少 3 条才发送”的硬门槛；宁可少，也不补低质量内容
- 若三层检索后仍无合格信息，发送一张透明说明卡，而不是伪造趋势

## GitHub Secrets

在仓库 **Settings → Secrets and variables → Actions → Repository secrets** 添加：

- `TAVILY_API_KEY`
- `GEMINI_API_KEY`
- `FEISHU_WEBHOOK_URL`
- `FEISHU_WEBHOOK_SECRET`

## 手动运行

进入 **Actions → Design Intelligence Brief → Run workflow**：

1. `test-feishu`：只测试飞书连接，不调用 Tavily 或 Gemini
2. `dry-run`：完整检索和分析，但不发送飞书
3. `full`：完整检索、分析并发送飞书

## 自动时间

默认每天 **08:07（Asia/Singapore）** 运行。

## 输出文件

每次运行的 Artifact 包含：

- `brief.json`：最终报告
- `candidates.json`：已验证候选
- `rejected-candidates.json`：被拒绝的来源及原因
- `search-errors.json`：搜索错误
- `search-stages.json`：各时间池的搜索数量和 credits
- `card-*.json`：飞书卡片请求体

## 去重历史

`data/history.json` 保存已选来源。正式运行和定时运行后，工作流会尝试自动提交该文件，避免近期重复：

- 新讯和近期洞察：45 天内避免重复
- 经典方法：180 天内避免重复

若仓库分支保护阻止自动提交，早报仍能发送，但跨日去重不会持久化。

## 本地运行

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
# macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
pytest -q
python src/main.py --mode test-feishu
```

完整生成但不发送：

```bash
python src/main.py --mode dry-run
```

完整发送：

```bash
python src/main.py --mode full
```

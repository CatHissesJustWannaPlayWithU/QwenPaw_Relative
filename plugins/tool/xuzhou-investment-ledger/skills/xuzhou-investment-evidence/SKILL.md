---
name: xuzhou-investment-evidence
description: 在已获得公开信息后，规范登记徐州招商线索与证据；优先保留来源和待管理员复核状态，不做未经证实的投资结论。
---

# 徐州招商线索证据登记

当用户或研究流程已经取得企业、基金、创始团队、合作或融资的公开信息时，使用本 Skill 判断是否应登记为招商线索。

## 必须遵守

1. 先确认来源可以追溯：需要来源 URL、来源标题、公开日期和能支持判断的原文摘要。
2. 没有公开证据时，只能说明“尚待核验”，不得把传闻写成事实。
3. 使用 `record_xuzhou_investment_evidence` 登记；一条证据只记录它实际能支持的事实。
4. 需要查看已有记录时，使用 `list_xuzhou_investment_leads` 或 `get_xuzhou_investment_lead`，不能重新编造历史线索。
5. 高质量一手证据被标记为 `pending_admin_review` 时，说明需要管理员审核，不表示已经确定招商、确定投资或确定合作。

## 可登记的徐州关联标签

- `ancestry_or_birthplace`：公开资料可支持祖籍或出生地关联。
- `business_connection`：公开资料可支持主营业务、项目或布局与徐州的实质关联。
- `founder_or_team`：公开资料可支持创始人或核心团队与徐州关联。
- `university_alumni`：公开资料可支持高校校友关系。
- `organization_cooperation`：公开资料可支持机构合作关系。
- `industry_chain`：公开资料可支持本地上下游或产业链关系。

## 来源等级

- `primary`：企业、基金、政府、交易所、监管机构或当事方正式发布的信息。
- `secondary`：可信媒体、数据库或第三方报道，仍需后续核验。
- `lead_only`：线索入口，不能单独支撑高优先级判断。

## 交付原则

只报告已登记的事实、来源和待办事项。不要把“值得关注”写成“已经有投资意向”。

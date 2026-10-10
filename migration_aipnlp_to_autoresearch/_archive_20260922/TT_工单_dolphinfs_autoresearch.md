# TT 工单草稿 — 申请 hadoop-autoresearch dolphinfs 目录 + 配额

> 提交入口：tt.internal.example.com → 计算平台 / DolphinFS（按《DolphinFS 接入手册》对应队列）
> 参考：DolphinFS 接入手册 km.internal.example.com/collabpage/977279938 ｜ Auto-Research SOP km.internal.example.com/collabpage/2760391106

```
标题：【DolphinFS】申请创建 hadoop-autoresearch 项目组目录 + 配额

背景：
因高敏数据合规要求（policy.internal.example.com/policies/2780），本人 your-username 需退出
your-user 项目组，后续仅使用 autoresearch 项目组开展 AutoResearch 工作，需要在
autoresearch 下落地 dolphinfs 数据盘。当前 hadoop-autoresearch 目录尚不存在。

申请事项：
1. 为项目组 hadoop-autoresearch 创建 dolphinfs 目录
   期望路径：/mnt/your-fs
2. 若该租户尚无 DolphinFS 预算，请协助开通 / 告知预算申请入口
3. 初始配额：建议 1–2 TB（个人工作区 + 训练数据缓存；可按租户标准调整）
4. 个人目录：/mnt/your-fs
   属主：your-username

负责人 / 申请人 misId：your-username
访问项目组（租户）：hadoop-autoresearch
```

## 决策 ①：autoresearch 是否已有 dolphinfs 配额？
- **有** → 工单只保留第 1、4 条（建目录 + 个人目录），删掉第 2、3 条。
- **没有 / 不确定** → 全文照发，让平台一并处理预算。

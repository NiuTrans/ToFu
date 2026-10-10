# autoresearch 迁移材料包（your-username）

> 生成 2026-09-30。目标：工作负载从 aipnlp 迁到 autoresearch，名下 aipnlp 容器资产清零后退出 aipnlp。
> 已确认：① 项目组下拉确为 `autoresearch`；② 个人数据 GB 级 → rsync；③ tofu 整体随迁；④ 容器不逐个重建，只迁 `cpu_rjh` 的环境（启动文件/环境变量/工具链）。

## 关键结论（先看这个）

**`.bashrc` 的所有路径依赖都指向 aipnlp 个人空间自身**（miniforge3 / HF cache / rustup / cargo / hope / nltk），**不在 nlp-sh02 / nlp-hl02 / mlp-ckpt 上**。这三个共享盘只出现在**代码/训练脚本的硬编码数据路径**里（311+ 处引用）以及 hope 挂载配置（`afo.dolphinfs.otherusers`）。因此：

- **环境迁移** = rsync 个人空间 + 按文件 03 的查找替换表改 **7 类路径前缀**。
- **共享盘依赖** = 新容器/新 hope 任务的**挂载配置**（文件 02），与代码内路径解耦；退出 aipnlp 后能否继续读需平台确认（文件 05）。

## 迁移顺序

① 建 autoresearch 容器（文件 01+02）→ ② 迁环境（文件 03）→ ③ 旧容器逐个处理（文件 04）→ ④ 万象资产交接 → ⑤ 退出 aipnlp。

## 文件清单

| 文件 | 内容 |
|---|---|
| `00_README.md` | 本文件，总览 |
| `01_建容器_参数卡.md` | CodeLab 表单逐项填写 + 镜像/队列/资源 |
| `02_高级参数挂载方案.md` | dolphinfs 个人权限 + 共享盘只读挂载 + 依赖汇总 |
| `03_环境迁移_操作清单.md` | 启动文件映射 + 路径查找替换表 + rsync 命令 + 验证 |
| `04_容器对照表.csv` | 33 个 aipnlp 容器逐个处置建议 |
| `05_待确认与风险.md` | 待平台/你确认项 + 风险与回退 |

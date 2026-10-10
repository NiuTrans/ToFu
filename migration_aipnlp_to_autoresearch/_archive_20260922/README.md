# aipnlp → autoresearch 迁移包

因高敏数据合规（policy.internal.example.com/policies/2780），your-username 需退出 your-user，
仅保留 autoresearch。本目录是迁移用的三件交付物。

| 文件 | 用途 | 何时用 |
|---|---|---|
| `TT_工单_dolphinfs_autoresearch.md` | 申请 `hadoop-autoresearch` 目录/配额的工单草稿 | 第①步，先提 TT |
| `重建配置单_autoresearch_codelab.md` | 在 autoresearch 新建容器的逐项配置 + 旧容器处置 | 第②步建容器 |
| `migrate_rjh_to_autoresearch.sh` | 个人空间数据 rsync 迁移脚本（排除共享工具链） | 第③步迁数据 |

## 执行顺序
```
① 提 TT 建 hadoop-autoresearch        （工单草稿）
② 在 autoresearch 建容器、挂新盘        （配置单）
③ MIGRATE_TOFU=1 bash migrate_rjh_to_autoresearch.sh
④ 新容器验证环境/代码/数据
⑤ aipnlp 旧容器：有用的已重建→挂起/删除；无用→删除
⑥ 万象 assetTransfer 移交资产 → 退出 aipnlp
```

## 两个待拍板开关（影响工单/脚本细节）
1. **autoresearch 是否已有 dolphinfs 配额**？——决定工单是否含预算申请。
2. **tofu 整体搬(MIGRATE_TOFU=1) 还是 冷备(=0)**？——决定脚本是否排除 tofu 项目本体。

## 安全约定
- `.rustup` / `.cache` 是**多 agent 共享工具链**：不迁、不碰，新环境继续绝对路径调用。
- 大体积 / ckpt（如 `hadoop-mlp-ckpt`、TB 级）**别走 rsync**，用 distcp 提 TT（找 liuxun06）。
- 全程未在任何输出中写入容器 token / 登录密码。

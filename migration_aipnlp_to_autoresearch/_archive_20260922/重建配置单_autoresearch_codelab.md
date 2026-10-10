# autoresearch 新建 CodeLab 容器 — 配置单

> 用途：退出 aipnlp 后，在 autoresearch 重建工作容器。
> 旧容器（33 个 projectName=aipnlp）队列全在 `…your-user.*`、dolphinfs 挂 `your-user`，退出后全部失效，**只能重建，无法搬走**。

## 一、基础配置
| 配置项 | 值 |
|---|---|
| 项目组 | **autoresearch** |
| 队列 | autoresearch 下的队列（**没有** `…your-user.*`；算力按 autoresearch 配额，比 aipnlp 小很多，GPU 大容器基本放弃） |
| 使用权限 | 个人独享 |

## 二、镜像（沿用原 tag，跨组可用）
- 训练类（autoresearch SOP 指定）：
  `registry-offlinebiz.internal.example.com/custom_prod/com.example-corp.data.hadoop.gpu/aipnlp/training_cuda12.6_conda_python3.12_torch2.8_vllm0.11_verl0.6_fa3_ljx_c5690703:v1.0.0`
- CPU 开发类：沿用 `cpu_rjh` 原镜像 tag
- 环境固化类（environment/cuda12.x/glibc 系列）：直接选**相同镜像 tag** 重建即可

## 三、挂载 dolphinfs（二选一）
| 方案 | 配置 | 说明 |
|---|---|---|
| **A. 项目组盘（推荐，①完成后）** | 挂载 `hadoop-autoresearch` | 团队共享，标准做法 |
| **B. 个人盘（最干净）** | 「dolphinfs 权限 = **个人**」→ `/Users/your-username` | 读=组内全部、写=仅个人目录；恢复容器时组⇄个人可切换；**不依赖任何组** |

高级参数：
```
pc.use.dolphinfs=true
# 迁移过渡期如需临时读 aipnlp 旧数据，可临时加（退出后即失效）：
# pc.use.dolphinfs.users=your-user
```

## 四、对应旧容器 → 处置
- **cpu_rjh / shxs_cpu**（2 个运行中）：工作迁到新容器后挂起→删除
- **environment/cuda/glibc 系列**：按上面镜像 tag 重建
- **失败 + 闲置 + 临时**：直接删除（清单见对话/处置表）

## 五、迁移顺序
```
① 提 TT 建 hadoop-autoresearch（TT_工单_dolphinfs_autoresearch.md）
② 在 autoresearch 建容器（本配置单），挂新组盘/个人盘
③ 跑 migrate_rjh_to_autoresearch.sh（先 tofu 后数据）
④ 新容器验证环境/代码/数据
⑤ aipnlp 旧容器：有用的已重建→挂起/删除；无用→删除
⑥ 万象 assetTransfer 移交剩余资产 → 退出 aipnlp
```

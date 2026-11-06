# app/ —— MVP 工程架构（四层）

交付清单「工程架构」项的代码本体。四层结构：

```
入口层        main.py + api/v1/（REST）+ admin/（管理后台）
服务层        services/（业务逻辑，不碰大模型 SDK）
智能体编排层  agents/（唯一调用大模型的层）
数据模型层    models/（12 张表）+ alembic/versions/（迁移）
```

逐项对照见仓库 `src/MVP功能对照.md`。

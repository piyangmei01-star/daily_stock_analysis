# 早盘选股外部准点触发说明

适用工作流：`.github/workflows/01-morning-quality.yml`  
仓库：`piyangmei01-star/daily_stock_analysis`

## 1. 已实现的能力

- 保留原 `schedule`：北京时间 09:25、09:55。
- 保留 GitHub 网页 `workflow_dispatch` 手动触发。
- 新增 `repository_dispatch`，事件名为 `morning-quality-external`。
- 外部请求可传 `client_payload.no_push=true`，用于完整选股但不发微信的测试。
- 外部请求可传 `client_payload.force=true`，仅用于测试时绕过防重复检查。
- 外部触发与原 schedule 使用同一并发组串行执行，避免并发重复推送。
- 防重复以归档中 `push_ok=true` 的真实推送凭证为准，不再把 no-push 或跳过运行误算为已推送。

## 2. 创建专用 GitHub Token

建议使用新的 **fine-grained personal access token**，不要复用日常管理 Token：

1. GitHub → Settings → Developer settings → Personal access tokens → Fine-grained tokens。
2. Repository access 只选择 `daily_stock_analysis`。
3. Repository permissions → **Contents: Read and write**。这是 GitHub 官方对 repository dispatch API 的要求。
4. 不授予 Administration、Workflows 等无关权限；设置有效期并定期更换。
5. Token 只放在 cron-job.org 的请求 Header 中，不写进 URL、正文、截图或本文档。

> cron-job.org 会保存并发送这个 Token。若 Token 泄露，应立即在 GitHub 撤销并重新创建。

## 3. cron-job.org 设置（09:24 闹钟）

1. 注册并登录 https://cron-job.org/ ，新建 Cron job。
2. Title 可填：`A股早盘质量选股外部触发`。
3. URL 填：

   ```text
   https://api.github.com/repos/piyangmei01-star/daily_stock_analysis/dispatches
   ```

4. Schedule：每天 09:24；建议仅周一到周五。时区必须选择 **Asia/Shanghai**。
5. Request method 选择 **POST**，Timeout 建议 30 秒。
6. Request body 选择 JSON，正文填：

   ```json
   {"event_type":"morning-quality-external","client_payload":{"source":"cron-job.org"}}
   ```

7. Headers 添加：

   ```text
   Accept: application/vnd.github+json
   Authorization: Bearer <你的专用Token>
   Content-Type: application/json
   ```

8. 开启失败通知；HTTP **204 No Content** 表示 GitHub 已接受唤醒事件。

正式正文不要包含 `no_push` 或 `force`。

## 4. 安全测试（不发微信）

先在本地终端执行以下命令，或临时把同样正文放到 cron-job.org 的“Run now”测试中：

```bash
curl -i -X POST \
  -H "Accept: application/vnd.github+json" \
  -H "Authorization: Bearer <你的专用Token>" \
  -H "Content-Type: application/json" \
  https://api.github.com/repos/piyangmei01-star/daily_stock_analysis/dispatches \
  -d '{"event_type":"morning-quality-external","client_payload":{"no_push":true,"force":true,"source":"api-test"}}'
```

- 返回 `HTTP/2 204`：API 已接受事件。
- GitHub Actions 新运行的 `event` 应为 `repository_dispatch`。
- 日志应出现 `TRIGGER_RECEIVED`、`EXTERNAL_DISPATCH`、`NO_PUSH_OK`。
- 不应出现 `PUSH_OK`，也不会产生成功微信推送凭证。
- 测试会真实执行选股并新增 no-push 归档；`force=true` 仅用于测试，不要放进正式定时正文。
- 测试结束后，将正文恢复为第 3 节的正式版本。

## 5. 响应时间与 09:25 推送边界

- GitHub dispatch API 通常在数秒内返回 204；Actions 启动一般还需数秒到数十秒，排队时间不受本仓库控制。
- 当前完整质量选股实测约需 **4～5 分钟**。因此 **09:24 唤醒不等于 09:25 推送完成**，通常可能到 09:28～09:30。
- 如果目标是 09:25 左右收到，建议把外部闹钟提前到 **09:20**，再利用现有 09:25/09:55 schedule 作为补充保障。
- 真正的“09:24 唤醒后秒级推送”需要提前完成选股并落盘，再由独立推送任务在指定时间读取同一不可变 run_id；本次改造没有把完整选股伪称为秒级完成。

## 6. 常见问题

| 状态 | 含义与处理 |
| --- | --- |
| 204 | 唤醒事件已接受；继续检查 Actions，不代表微信已推送 |
| 401/403 | Token 无效、过期或权限不足；核对 Contents 写权限 |
| 404 | 仓库名错误、Token 无权访问该仓库，或私有仓库不可见 |
| 422 | JSON 正文或 event_type 不合法，或请求被限流 |
| 有运行但 skip=yes | 近 6 小时已有真实成功推送凭证，属于正常防重复 |
| 有运行但无微信 | 查是否传了 no_push；再查 PUSH_FAIL、凭证与 Actions 日志 |

完整推送是否成功仍以运行日志 `PUSH_OK`、企业微信 `errcode=0` 和 `push_receipt_quality_*.json` 为准，不能仅凭 cron 网站显示成功判定。

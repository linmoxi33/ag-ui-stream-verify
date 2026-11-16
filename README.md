# ag-ui-stream-verify

**离线校验录下来的 AG-UI 事件流：既按 1.0 规范判一次，也按 TypeScript SDK 的 `enforceEvents` 视图判一次，把“只被严格判定拒绝、客户端却照单全收”的事件逐条点名。** 它存在的理由很具体：真机上最难查的兼容性问题不是报错，而是静默放行。

Upstream: https://github.com/ag-ui-protocol/ag-ui

## 上游是什么，缺什么

[AG-UI](https://github.com/ag-ui-protocol/ag-ui) 是 agent 与前端的流式事件协议：一次 run 用 `RUN_STARTED` 打开、`RUN_FINISHED` / `RUN_ERROR` 关闭，中间是 text / tool / state / reasoning 等 31 种事件。规范把顺序写成了 MUST：

- [Runs and Steps](https://github.com/ag-ui-protocol/ag-ui/blob/main/docs/spec/1.0/events/lifecycle.mdx)：流必须以 `RUN_STARTED` 或 `RUN_ERROR` 开头；`RUN_FINISHED.outcome` 只能是 schema 描述的值。
- 但 TypeScript SDK 的 enforcement 阶段会剥掉不认识的成员——[issue #3391](https://github.com/ag-ui-protocol/ag-ui/issues/3391)（2026-11-13，open，带回复）报告：`enforceEvents` 接受 malformed 的 `RUN_FINISHED` outcome，`outcome.type` 缺失或非字符串时被当成 success。规范说这叫 unrecognised material、应被剥掉；客户端因此**看不到那个 run 是以什么方式结束的**。
- 上游确实有 conformance 目录（`spec/1.0/conformance/streams/`）和 `verifyEvents`，但它们跑在 SDK 的 TypeScript 测试链路里；没有独立工具能对**一份录下来的 SSE/JSONL 文件**同时给出“规范判定”和“enforce 后客户端实际看到的判定”。

这个工具补的就是这一步：输入是字节，输出是每条 finding 的事件下标，以及 `silent_accept` 标记。

## 它做什么 / 不做什么

做：

- 解析 SSE（`data:` 多行拼接）和 JSONL 两种录制格式。
- 严格检查 run 生命周期、text/tool/step 开关配对、`STATE_DELTA.delta` 必须是 JSON Patch 数组、`RUN_FINISHED.outcome.type` 必须是 `success|interrupt|cancelled`。
- 模拟 upstream enforcement 的两个行为：未知成员被剥掉、malformed outcome 被当 success。严格判定失败而模拟判定放行的事件标 `silent_accept: true`。
- `--json` 输出可直接喂给 CI；退出码 0/1/2。

不做：

- 不执行 TypeScript SDK，`--no-enforce-sim` 关闭的只是这个简化模型；`silent_accept` 只在 outcome 类错误上标，不代表完整复刻 zod 行为。
- 不发请求、不起 server、不连接任何 agent；不校验业务语义（工具参数是否合法、状态是否合理）。
- 不替代上游 conformance 测试，只做一个可离线、可断言的外围检查器。

## 安装 / 用法

零依赖，Python ≥ 3.9：

```bash
git clone https://github.com/linmoxi33/ag-ui-stream-verify.git
cd ag-ui-stream-verify
python3 tests/run.py                     # 12 passed
python3 agui_stream_verify.py examples/in/clean.sse
```

```bash
$ python3 agui_stream_verify.py examples/in/silent-outcome.sse --json
{
  "ok": false,
  "failed": true,
  "events": 2,
  "counts": {"error": 1, "warning": 0, "silent_accept": 1},
  "silent_accepts": [{"code": "E011", "event_index": 1, ...}],
  ...
}
$ echo $?
1
```

| 退出码 | 含义 |
| --- | --- |
| 0 | `--fail-on` 阈值内没有命中 |
| 1 | 检出 error（默认 `--fail-on error`）或 warning（`--fail-on warning`） |
| 2 | 用法错误 / 文件读不了 |

常用参数：`--format auto|sse|jsonl`、`--json`、`--no-enforce-sim`、`--fail-on error|warning|never`。

## 本轮创新

B 档：把 AG-UI 的兼容性从“静态 schema 是否通过”推进到“**同一份录制字节，在规范视图与 enforce 视图下结论是否分叉**”。和上游 `verifyEvents`（TS 测试内）与 conformance fixtures 的差别是：独立进程、离线文件、逐事件下标、显式 `silent_accept`。与台账里的 mcp 系列（工具名/参数类型/manifest）不重合。

## 局限

- `enforce-sim` 是按 issue #3391 描述的剥除行为写的近似；未跑真实 zod schema，未知字段的剥除范围可能比真实更宽。
- 只覆盖规范里可离线判定的顺序/必需字段；chunk 展开（`TEXT_MESSAGE_CHUNK`）与跨版本会话协商未实现。
- `E011/E012` 之外的错误不会被标 `silent_accept`，宁可少标不可错标。
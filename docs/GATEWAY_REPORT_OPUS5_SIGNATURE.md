# 给 sankuai 网关团队的报告:yuju-claude-opus-5-evaDaily 在 OpenAI 兼容线不返回 thinking 签名

> 日期:2026-07-28
> 联系人:(Tofu 项目)
> 影响面:Claude extended-thinking 的多轮工具连续性(tool_use continuity)

## 一句话结论

`yuju-claude-opus-5-evaDaily` 经 `/v1/openai/native/chat/completions` 流式返回时,
thinking 以 `reasoning_content` 增量到达,**从不携带 `reasoning_details` 签名块**;
而同一网关、同一条线上的 `aws.claude-opus-4.7 / 4.8` 后端**稳定携带**该签名。
这导致客户端无法按 Anthropic 合同回放「已签名的 thinking 块」。

## 实测数据(2026-07-28,可复算)

| 请求 | 结果 |
|---|---|
| `POST /v1/openai/native/chat/completions`,model=yuju-claude-opus-5-evaDaily,`thinking:{type:'adaptive'}` + effort | 111 个 SSE chunk:33 个 `reasoning_content` 增量、**0 个 `reasoning_details`、0 个 signature** |
| 同形状,model=aws.claude-opus-4.7 / 4.8(历史 raw SSE 留档) | 每个 thinking 块末尾携带 `reasoning_details:[{"type":"thinking","signature":"…"}]` |
| `POST /v1/anthropic/v1/messages`,同 yuju opus-5 部署 | `signature_delta` 正常下发;`tool_use` 流式正常;prompt caching(5m + 1h ext-TTL)正常 |

## 为什么这对我们重要

Anthropic extended-thinking 的合同:多轮工具调用中回放历史 thinking 块时**必须携带原签名**,
否则上游硬拒(`invalid_request_error: … signature: Field required`,HTTP 400,不可重试)。
由于兼容线拿不到签名,我们只能剥离无签名的 thinking 块让模型重新推理 ——
每个受影响回合损失推理上下文连续性,并付出重复推理的 token 成本。

## 请求

1. **首选**:在 OpenAI 兼容线上为 yuju opus-5 透传 `reasoning_details` 签名块(与 aws.* 后端行为对齐);
2. **备选**:确认 `/v1/anthropic/v1/messages` 为官方支持路径,并告知其限流/配额参数是否与兼容线一致
   (我们已实测该面全功能 parity,可整体切换 Claude 流量)。

## 附:偶发包装错误(顺带报告)

2026-07-28 12:47 前后,anthropic 原生面间歇返回 HTTP 400,错误体为包装形态、不含错误分类:

```json
{"type":"error","error":{"type":"<nil>","message":"bad response status code 400 (request id: toio20260728044734160442739SC7sFnGZ)"}}
```

同一请求体在数分钟后 8/8 成功 —— 为上游瞬时抖动的包装转发,建议网关侧在包装时保留上游错误类型
(`error.type` 不应为 `<nil>`),便于客户端区分「确定性拒绝」与「瞬时抖动」。

## M-TraceId / request id 样本

- 兼容线 opus-5 正常流量(无签名):`M-TraceId=b1e9c756ebab45d6b31cd9edce099fd5`、`7289d958dcf2407db193be89fdf61488`
- 包装 400 样本:`toio20260728044734160442739SC7sFnGZ`、`toio202607280447373699259904B7ccBzv`

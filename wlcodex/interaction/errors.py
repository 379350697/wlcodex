from __future__ import annotations


def classify_user_error(error: object) -> str:
    raw = str(error)
    lowered = raw.lower()

    if "401" in lowered or "unauthorized" in lowered or "token" in lowered:
        return "认证看起来失效了，需要先检查登录状态。"
    if "429" in lowered or "rate limit" in lowered or "too many requests" in lowered:
        return "现在被限流了，等一会儿再继续会更稳。"
    if "context" in lowered and ("long" in lowered or "length" in lowered):
        return "上下文太长了。我建议开新对话，或者先让我压缩范围。"
    if "codex" in lowered and ("启动失败" in raw or "failed" in lowered):
        return "Codex 没启动起来。我保留了这次请求，可以稍后重试。"
    if "network" in lowered or "timed out" in lowered or "timeout" in lowered:
        return "网络这下没接稳，我会尽量保留当前状态。"
    return "这次运行失败了。详细错误已写入日志，可以用状态或诊断命令查看。"

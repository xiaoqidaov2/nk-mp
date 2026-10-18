# AWS 出口探针（一次性，用来对比"从 AWS 出网"到底可不可行）

## 它回答三个问题（一次调用，10 秒内出结果）

1. **AWS 的出口 IP 是什么**（`api.ipify.org` / `checkip.amazonaws.com`）——给 AWS 那段"不可枚举"的说法一个具体样本
2. **到 Nike 的延迟**：DNS / TCP / TLS 建连耗时（对照我们生产机：TCP 47–490ms、TTFB 175–634ms）
3. **拦截率**：对 `api.nike.com.cn` 的**免鉴权**端点发 N 次请求，统计 200 / 403 / **405(挑战)** 的比例
   —— 对照我们生产机（香港机房、中国优化线路）实测的 **405 挑战率 57–60%**

再加一个**控制臂**：测试域尺码表端点（我们文档实测 0% WAF），用来确认"探针本身没问题"。

## 前置

```bash
# AWS CLI 已装在本机（无需 sudo）
~/.local/bin/aws --version
# 配好凭据（建议 region = ap-southeast-1 新加坡）
~/.local/bin/aws configure        # 需要 lambda:* 与 iam:CreateRole/AttachRolePolicy/PassRole
~/.local/bin/aws sts get-caller-identity
```

不想把 IAM 权限给这个脚本的话：**先在控制台建一个角色**（信任 lambda.amazonaws.com，挂
`AWSLambdaBasicExecutionRole`），然后：

```bash
ROLE=你的角色名-or-ARN ./qa/aws_egress_probe/deploy.sh
```

## 三步

```bash
# 1) 部署 + 调用（默认新加坡、每个臂 5 次请求）
./qa/aws_egress_probe/deploy.sh

# 2) 可选：加测"带 token 的门店列表"这一臂（会发少量真实鉴权请求到 Nike）
PAYLOAD='{"requests":5,"token":"<Nike access_token>"}' ./qa/aws_egress_probe/deploy.sh

# 3) 用完删干净（不留函数/角色）
./qa/aws_egress_probe/deploy.sh cleanup
```

## 输出怎么读

```json
{
  "egress_ips": ["13.x.x.x", "13.x.x.x"],
  "latency_api_nike": {"dns_ms": 1.2, "tcp_ms": 35.0, "tls_ms": 70.0},
  "arms": [
    {"label": "免鉴权·门店列表", "statuses": {"200": 3, "405(challenge)": 2}, "challenged_pct": 40.0, "avg_ms": 480.0},
    {"label": "免鉴权·测试域尺码表(对照臂)", "statuses": {"200": 5}, "challenged_pct": 0.0, "avg_ms": 120.0}
  ]
}
```

判定口径（和我们生产基线比）：
- **挑战率**：`challenged_pct` 明显高于 57–60%（例如 ≥80%）= 出口不划算，别搬；
  基本持平（±5pp）= 出口中性，可按"算力/突发"的需求决定搬不搬；
  明显更低 = 白赚，值得进一步验证。
- **延迟**：`tcp_ms` / `tls_ms` 若比 47–490ms 高很多，会直接放大单轮墙钟（16 路并发下每轮多几秒）。
- **对照臂**若也出现 405，说明探针/上游当时整体异常，样本不可用，重跑一次。

## 成本与安全

- 成本 ≈ **$0**：每次调用 256MB × ~10s ≈ 0.0025 GB-s（远在每月 40 万 GB-s 免费额度内），
  请求费也在 100 万次免费额度内；IAM 角色与 Lambda 函数本身不收费。
- 默认**只打免鉴权端点**，每个臂默认 5 次请求；只有显式传 `token` 才会发带鉴权的请求。
- 探针是只读的：不做写入、不下单、不改任何状态。
- 用完请 `./deploy.sh cleanup`，避免账号里留残留资源（脚本会删函数 + 角色）。

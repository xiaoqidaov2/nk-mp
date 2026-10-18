#!/usr/bin/env bash
# 部署 → 调用 → （可选）清理一个一次性探针 Lambda。
#
#   ./deploy.sh                 # 部署并调用（默认新加坡，每臂 5 次请求）
#   PAYLOAD='{"requests":5,"token":"..."}' ./deploy.sh    # 额外测"带 token"这一臂
#   ./deploy.sh cleanup         # 删函数 + 删角色（不留残留）
#
# 前置：`aws configure` 已配好（region 建议 ap-southeast-1），且有
#       lambda:Create/Update/Invoke + iam:CreateRole/AttachRolePolicy/PassRole 权限。
set -euo pipefail

AWS="${AWS:-$HOME/.local/bin/aws}"
REGION="${REGION:-ap-southeast-1}"
FUNC="${FUNC:-nkmp-egress-probe}"
ROLE="${ROLE:-nkmp-egress-probe-role}"
DIR="$(cd "$(dirname "$0")" && pwd)"

echo "== 账号 =="
"$AWS" --region "$REGION" sts get-caller-identity --output json

if [ "${1:-probe}" = "cleanup" ]; then
  "$AWS" --region "$REGION" lambda delete-function --function-name "$FUNC" 2>/dev/null && echo "deleted function $FUNC" || echo "function 不存在"
  "$AWS" iam detach-role-policy --role-name "$ROLE" \
    --policy-arn arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole 2>/dev/null || true
  "$AWS" iam delete-role --role-name "$ROLE" 2>/dev/null && echo "deleted role $ROLE" || echo "role 不存在"
  exit 0
fi

if ! "$AWS" iam get-role --role-name "$ROLE" >/dev/null 2>&1; then
  echo "== 创建执行角色 $ROLE =="
  "$AWS" iam create-role --role-name "$ROLE" --assume-role-policy-document \
    '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"lambda.amazonaws.com"},"Action":"sts:AssumeRole"}]}' \
    >/dev/null
  "$AWS" iam attach-role-policy --role-name "$ROLE" \
    --policy-arn arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole
  echo "等角色传播 10s…"; sleep 10
fi
ROLE_ARN="$("$AWS" iam get-role --role-name "$ROLE" --query 'Role.Arn' --output text)"

echo "== 打包 =="
rm -f /tmp/nkmp_probe.zip
(cd "$DIR" && zip -q /tmp/nkmp_probe.zip lambda_probe.py)

if "$AWS" --region "$REGION" lambda get-function --function-name "$FUNC" >/dev/null 2>&1; then
  echo "== 更新函数代码 =="
  "$AWS" --region "$REGION" lambda update-function-code --function-name "$FUNC" \
    --zip-file fileb:///tmp/nkmp_probe.zip --query 'LastUpdateStatus' --output text
  sleep 5
else
  echo "== 创建函数 =="
  "$AWS" --region "$REGION" lambda create-function --function-name "$FUNC" --runtime python3.12 \
    --role "$ROLE_ARN" --handler lambda_probe.handler \
    --zip-file fileb:///tmp/nkmp_probe.zip --timeout 60 --memory-size 256 --architectures x86_64 \
    --query 'FunctionArn' --output text
fi

echo "== 调用 =="
PAYLOAD="${PAYLOAD:-{\"requests\":5}}"
"$AWS" --region "$REGION" lambda invoke --function-name "$FUNC" \
  --payload "$PAYLOAD" --cli-binary-format raw-in-base64-out \
  /tmp/nkmp_probe_out.json >/dev/null
python3 -m json.tool /tmp/nkmp_probe_out.json

echo
echo "提示：看完就跑 ./deploy.sh cleanup 删掉（不删也不收费，只是留着函数）。"

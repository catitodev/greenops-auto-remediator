# Guia de Deploy — GreenOps Auto-Remediador

Este guia cobre tudo que você precisa para colocar o GreenOps em produção, desde os pré-requisitos até a verificação pós-deploy.

---

## Índice

1. [Pré-requisitos](#1-pré-requisitos)
2. [Instalação Rápida](#2-instalação-rápida)
3. [Deploy Manual](#3-deploy-manual)
4. [Configuração Pós-Deploy](#4-configuração-pós-deploy)
5. [Verificação](#5-verificação)
6. [Troubleshooting](#6-troubleshooting)

---

## 1. Pré-requisitos

### Conta AWS

| Requisito | Detalhe |
|---|---|
| Conta AWS ativa | Com acesso ao console e CLI |
| Região suportada | Qualquer região com Lambda, DynamoDB e CloudWatch (recomendado: `us-east-1` ou `sa-east-1`) |
| Cost Explorer habilitado | Necessário para métricas de custo no relatório. Habilite em **Billing → Cost Explorer → Enable** |
| Compute Optimizer habilitado | Necessário para recomendações de rightsizing. Habilite em **Compute Optimizer → Get started** |

### Permissões IAM necessárias para o deploy

O usuário ou role que executa o deploy precisa das seguintes permissões:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "cloudformation:*",
        "lambda:*",
        "dynamodb:*",
        "s3:*",
        "sns:*",
        "iam:CreateRole",
        "iam:DeleteRole",
        "iam:AttachRolePolicy",
        "iam:DetachRolePolicy",
        "iam:PutRolePolicy",
        "iam:DeleteRolePolicy",
        "iam:PassRole",
        "iam:GetRole",
        "iam:ListRolePolicies",
        "events:*",
        "cloudwatch:*",
        "kms:CreateKey",
        "kms:CreateAlias",
        "kms:DescribeKey",
        "kms:PutKeyPolicy"
      ],
      "Resource": "*"
    }
  ]
}
```

> **Dica:** Em ambientes corporativos, use uma role de deploy dedicada com essas permissões em vez de credenciais de usuário IAM.

### Ferramentas locais

| Ferramenta | Versão mínima | Instalação |
|---|---|---|
| Python | 3.11+ | [python.org](https://www.python.org/downloads/) |
| AWS CLI | v2.x | `pip install awscli` ou [docs AWS](https://docs.aws.amazon.com/cli/latest/userguide/install-cliv2.html) |
| Git | 2.x | [git-scm.com](https://git-scm.com/) |
| Make | 3.x | Nativo no Linux/macOS; Windows: `choco install make` |

### Verificar pré-requisitos

```bash
# Verificar versões
python3 --version        # Python 3.11.x ou 3.12.x
aws --version            # aws-cli/2.x.x
git --version            # git version 2.x.x
make --version           # GNU Make 3.x ou 4.x

# Verificar credenciais AWS configuradas
aws sts get-caller-identity
# Deve retornar: Account, UserId, Arn
```

### Serviços AWS que serão criados

O deploy cria os seguintes recursos na sua conta:

| Serviço | Recursos criados | Custo estimado |
|---|---|---|
| Lambda | 3 funções + 1 layer | ~$0/mês (free tier cobre) |
| DynamoDB | 4 tabelas (PAY_PER_REQUEST) | ~$0-2/mês |
| S3 | 1 bucket | ~$0.02/GB/mês |
| SNS | 3 tópicos | ~$0/mês (free tier) |
| CloudWatch | Log groups + dashboard + alarms | ~$3/mês |
| KMS | 1 chave CMK | $1/mês por chave |
| EventBridge | 3 rules | ~$0/mês |
| **Total estimado** | | **~$5-10/mês** |

---

## 2. Instalação Rápida

Para a maioria dos casos, esses 4 comandos são suficientes:

```bash
# 1. Clone o repositório
git clone https://github.com/seu-usuario/greenops-auto-remediator.git
cd greenops-auto-remediator

# 2. Configure o ambiente local
make setup
# Cria .venv/, instala dependências, copia .env.example → .env

# 3. Edite as variáveis obrigatórias
nano .env
# Preencha: APPROVER_EMAIL, AWS_REGION, GREENOPS_ENVIRONMENT

# 4. Faça o deploy
make deploy ENV=dev AWS_REGION=us-east-1
```

Após o deploy, confirme o email de subscrição SNS que chegará na caixa de `APPROVER_EMAIL`.

### Variáveis obrigatórias no `.env`

Abra o arquivo `.env` (criado automaticamente pelo `make setup`) e preencha:

```bash
# Região onde os recursos serão gerenciados
AWS_REGION=us-east-1

# Ambiente: dev | staging | prod
GREENOPS_ENVIRONMENT=dev

# IMPORTANTE: manter true até validar o comportamento
GREENOPS_DRY_RUN=true

# Email para receber notificações e pedidos de aprovação
APPROVER_EMAIL=seu-email@empresa.com
```

As demais variáveis têm valores padrão seguros e podem ser ajustadas depois.

---

## 3. Deploy Manual

### Opção A — AWS CLI (recomendado)

```bash
# Valida o template antes de fazer deploy
aws cloudformation validate-template \
  --template-body file://infrastructure/cloudformation/main.yaml \
  --region us-east-1

# Deploy com parâmetros obrigatórios
aws cloudformation deploy \
  --template-file infrastructure/cloudformation/main.yaml \
  --stack-name greenops-auto-remediator-dev \
  --region us-east-1 \
  --capabilities CAPABILITY_NAMED_IAM \
  --parameter-overrides \
    Environment=dev \
    NotificationEmail=ops@empresa.com \
    ApprovalEmail=aprovador@empresa.com \
  --tags \
    GreenOpsManaged=true \
    Environment=dev \
    Project=greenops-auto-remediator
```

### Parâmetros do CloudFormation

| Parâmetro | Obrigatório | Padrão | Descrição |
|---|---|---|---|
| `Environment` | ✅ | `dev` | Ambiente: `dev`, `staging` ou `prod` |
| `NotificationEmail` | ✅ | — | Email para notificações gerais |
| `ApprovalEmail` | ✅ | — | Email para aprovações de remediação |
| `DiscoverySchedule` | ❌ | `rate(6 hours)` | Frequência do discovery |
| `RemediationSchedule` | ❌ | `rate(24 hours)` | Frequência da remediação agendada |
| `ReportingSchedule` | ❌ | `cron(0 8 * * ? *)` | Horário do relatório semanal |
| `LambdaMemorySize` | ❌ | `256` | Memória das Lambdas em MB |
| `LambdaTimeout` | ❌ | `300` | Timeout das Lambdas em segundos |
| `FindingsTTLDays` | ❌ | `90` | Dias de retenção dos findings |
| `RollbackTTLDays` | ❌ | `30` | Dias de retenção dos rollbacks |
| `ReportRetentionDays` | ❌ | `365` | Dias antes de mover relatórios para Glacier |
| `LogRetentionDays` | ❌ | `30` | Dias de retenção dos logs CloudWatch |

### Opção B — Console AWS

1. Acesse **CloudFormation → Stacks → Create stack → With new resources**
2. Em **Template source**, selecione **Upload a template file**
3. Faça upload de `infrastructure/cloudformation/main.yaml`
4. Preencha os parâmetros na tela seguinte
5. Em **Capabilities**, marque **I acknowledge that AWS CloudFormation might create IAM resources with custom names**
6. Clique em **Create stack**

### Opção C — AWS CloudShell

Para deploy direto do console sem instalar nada localmente:

```bash
# No AWS CloudShell (console.aws.amazon.com → CloudShell)
git clone https://github.com/seu-usuario/greenops-auto-remediator.git
cd greenops-auto-remediator

aws cloudformation deploy \
  --template-file infrastructure/cloudformation/main.yaml \
  --stack-name greenops-auto-remediator-dev \
  --capabilities CAPABILITY_NAMED_IAM \
  --parameter-overrides \
    Environment=dev \
    NotificationEmail=ops@empresa.com \
    ApprovalEmail=aprovador@empresa.com
```

### Deploy em múltiplos ambientes

```bash
# Dev
make deploy ENV=dev AWS_REGION=us-east-1

# Staging
make deploy ENV=staging AWS_REGION=us-east-1

# Produção (requer confirmação explícita)
make deploy ENV=prod AWS_REGION=us-east-1
```

Cada ambiente cria uma stack independente com nome `greenops-auto-remediator-{env}`.

---

## 4. Configuração Pós-Deploy

### 4.1 Confirmar subscrição SNS

Após o deploy, dois emails serão enviados para `NotificationEmail` e `ApprovalEmail` com assunto:

```
AWS Notification - Subscription Confirmation
```

**Clique no link "Confirm subscription"** em cada email. Sem isso, nenhuma notificação será entregue.

### 4.2 Adicionar tags nos recursos AWS

Para que o GreenOps gerencie um recurso, ele precisa ter a tag:

```
GreenOpsManaged = true
```

**Via console AWS:**
1. Acesse o recurso (ex: EC2 → Instances)
2. Selecione a instância → **Tags → Manage tags**
3. Adicione `GreenOpsManaged = true`

**Via AWS CLI:**
```bash
# EC2 instance
aws ec2 create-tags \
  --resources i-1234567890abcdef0 \
  --tags Key=GreenOpsManaged,Value=true

# RDS instance
aws rds add-tags-to-resource \
  --resource-name arn:aws:rds:us-east-1:123456789012:db:mydb \
  --tags Key=GreenOpsManaged,Value=true

# Lambda function
aws lambda tag-resource \
  --resource arn:aws:lambda:us-east-1:123456789012:function:my-function \
  --tags GreenOpsManaged=true
```

**Para proteger um recurso de qualquer ação automática:**
```bash
aws ec2 create-tags \
  --resources i-1234567890abcdef0 \
  --tags Key=GreenOpsProtected,Value=true
```

### 4.3 Configurar dry-run

O sistema inicia com `GREENOPS_DRY_RUN=true` por padrão — nenhuma ação é executada, apenas simulada.

Para habilitar remediações reais após validar o comportamento:

```bash
# Atualiza a variável de ambiente da Lambda de Remediation
aws lambda update-function-configuration \
  --function-name greenops-remediation-dev \
  --environment "Variables={GREENOPS_DRY_RUN=false,ENVIRONMENT=dev,...}"
```

> **Recomendação:** Mantenha `dry-run=true` por pelo menos 7 dias em um novo ambiente. Revise os findings gerados antes de habilitar remediações reais.

### 4.4 Configurar aprovadores

Por padrão, ações com `riskLevel=MEDIUM` ou superior requerem aprovação via email. Para aprovar uma remediação:

1. Receba o email de aprovação no endereço configurado em `ApprovalEmail`
2. Acesse a tabela `greenops-approvals-{env}` no DynamoDB
3. Localize o item pelo `approvalId` do email
4. Atualize o campo `status` de `PENDING` para `APPROVED`

```bash
# Aprovar via CLI
aws dynamodb update-item \
  --table-name greenops-approvals-dev \
  --key '{"approvalId": {"S": "uuid-do-email"}}' \
  --update-expression "SET #s = :approved" \
  --expression-attribute-names '{"#s": "status"}' \
  --expression-attribute-values '{":approved": {"S": "APPROVED"}}'
```

### 4.5 Ajustar limites de rate limiting

Os limites padrão são conservadores. Ajuste conforme o tamanho da sua conta:

```bash
# Aumentar limite para contas maiores
aws lambda update-function-configuration \
  --function-name greenops-remediation-dev \
  --environment "Variables={
    MAX_ACTIONS_PER_HOUR=50,
    MAX_DESTRUCTIVE_ACTIONS_PER_DAY=10,
    ...
  }"
```

---

## 5. Verificação

### 5.1 Verificar stack CloudFormation

```bash
# Status da stack
aws cloudformation describe-stacks \
  --stack-name greenops-auto-remediator-dev \
  --query 'Stacks[0].StackStatus'
# Esperado: "CREATE_COMPLETE" ou "UPDATE_COMPLETE"

# Listar recursos criados
aws cloudformation list-stack-resources \
  --stack-name greenops-auto-remediator-dev \
  --query 'StackResourceSummaries[*].[ResourceType,LogicalResourceId,ResourceStatus]' \
  --output table
```

### 5.2 Invocar Discovery manualmente

```bash
# Invoca a Lambda de Discovery em modo dry-run
aws lambda invoke \
  --function-name greenops-discovery-dev \
  --payload '{"source": "manual-test", "action": "dry-run"}' \
  --cli-binary-format raw-in-base64-out \
  response.json

# Verifica a resposta
cat response.json
# Esperado: {"statusCode": 200, "body": "{\"findingsCount\": N, ...}"}
```

### 5.3 Verificar findings no DynamoDB

```bash
# Conta quantos findings foram gerados
aws dynamodb scan \
  --table-name greenops-findings-dev \
  --select COUNT \
  --query 'Count'

# Lista os 5 findings com maior priorityScore
aws dynamodb scan \
  --table-name greenops-findings-dev \
  --projection-expression "resourceId, resourceType, wasteType, severity, priorityScore" \
  --query 'Items[:5]'
```

### 5.4 Verificar métricas no CloudWatch

```bash
# Verifica se métricas foram publicadas
aws cloudwatch get-metric-statistics \
  --namespace GreenOps/Discovery \
  --metric-name FindingsCount \
  --dimensions Name=Environment,Value=dev \
  --start-time $(date -u -d '1 hour ago' +%Y-%m-%dT%H:%M:%S) \
  --end-time $(date -u +%Y-%m-%dT%H:%M:%S) \
  --period 3600 \
  --statistics Sum
```

### 5.5 Verificar logs da Lambda

```bash
# Últimas 50 linhas de log do Discovery
aws logs tail /aws/lambda/greenops-discovery-dev \
  --since 1h \
  --format short

# Filtrar apenas erros
aws logs filter-log-events \
  --log-group-name /aws/lambda/greenops-discovery-dev \
  --filter-pattern "[ERROR]" \
  --start-time $(date -d '1 hour ago' +%s000)
```

### 5.6 Invocar Reporting manualmente

```bash
# Gera relatório imediatamente (sem esperar o schedule semanal)
aws lambda invoke \
  --function-name greenops-reporting-dev \
  --payload '{"source": "manual-test", "action": "generate-daily-report"}' \
  --cli-binary-format raw-in-base64-out \
  report-response.json

cat report-response.json
# Esperado: {"statusCode": 200, "body": "{\"reportDate\": \"...\", ...}"}
```

### 5.7 Checklist de verificação completa

```
[ ] Stack CloudFormation com status CREATE_COMPLETE
[ ] Email de confirmação SNS recebido e confirmado
[ ] Lambda Discovery invocada com sucesso (statusCode 200)
[ ] Findings aparecendo na tabela DynamoDB greenops-findings-dev
[ ] Métricas visíveis no CloudWatch namespace GreenOps/Discovery
[ ] Dashboard GreenOps-Executive-Dashboard criado no CloudWatch
[ ] Relatório JSON salvo no S3 (reports/YYYY/MM/DD/report.json)
[ ] Email executivo recebido no APPROVER_EMAIL
[ ] Logs sem erros críticos no CloudWatch Logs
```

---

## 6. Troubleshooting

### Erro: `ROLLBACK_COMPLETE` na stack CloudFormation

**Sintoma:** A stack falha durante o deploy e fica em estado `ROLLBACK_COMPLETE`.

**Causa mais comum:** Email inválido nos parâmetros `NotificationEmail` ou `ApprovalEmail`.

**Solução:**
```bash
# 1. Veja os eventos de erro
aws cloudformation describe-stack-events \
  --stack-name greenops-auto-remediator-dev \
  --query 'StackEvents[?ResourceStatus==`CREATE_FAILED`].[LogicalResourceId,ResourceStatusReason]' \
  --output table

# 2. Delete a stack com falha
aws cloudformation delete-stack \
  --stack-name greenops-auto-remediator-dev

# 3. Aguarde a deleção
aws cloudformation wait stack-delete-complete \
  --stack-name greenops-auto-remediator-dev

# 4. Corrija os parâmetros e faça novo deploy
make deploy ENV=dev
```

---

### Erro: `CAPABILITY_NAMED_IAM` não informado

**Sintoma:**
```
An error occurred (InsufficientCapabilitiesException): Requires capabilities: [CAPABILITY_NAMED_IAM]
```

**Solução:** Adicione `--capabilities CAPABILITY_NAMED_IAM` ao comando de deploy. O `make deploy` já inclui essa flag automaticamente.

---

### Erro: Lambda com timeout

**Sintoma:** Logs mostram `Task timed out after 300.00 seconds` para contas com muitos recursos.

**Solução:**
```bash
# Aumenta o timeout para 15 minutos (máximo Lambda)
aws cloudformation deploy \
  --template-file infrastructure/cloudformation/main.yaml \
  --stack-name greenops-auto-remediator-dev \
  --capabilities CAPABILITY_NAMED_IAM \
  --parameter-overrides \
    Environment=dev \
    NotificationEmail=ops@empresa.com \
    ApprovalEmail=aprovador@empresa.com \
    LambdaTimeout=900 \
    LambdaMemorySize=512
```

---

### Erro: `AccessDeniedException` nos logs da Lambda

**Sintoma:** Logs mostram `AccessDeniedException` ao chamar APIs AWS.

**Causa:** A IAM Role da Lambda não tem permissão para o serviço específico.

**Diagnóstico:**
```bash
# Identifica qual chamada falhou
aws logs filter-log-events \
  --log-group-name /aws/lambda/greenops-discovery-dev \
  --filter-pattern "AccessDeniedException"

# Verifica a policy da role
aws iam get-role-policy \
  --role-name greenops-discovery-role-dev \
  --policy-name DiscoveryPolicy
```

**Solução:** Atualize o template CloudFormation adicionando a permissão faltante na role correspondente e faça redeploy.

---

### Erro: Findings não aparecem no DynamoDB

**Sintoma:** Lambda Discovery retorna `findingsCount: 0` mesmo com recursos que deveriam ser detectados.

**Causas possíveis:**

| Causa | Verificação | Solução |
|---|---|---|
| Recursos sem tag `GreenOpsManaged=true` | `aws ec2 describe-instances --filters Name=tag:GreenOpsManaged,Values=true` | Adicionar tag nos recursos |
| CloudWatch sem dados (instância nova) | Verificar se há datapoints nos últimos 7 dias | Aguardar 7 dias de métricas |
| Compute Optimizer não habilitado | Console → Compute Optimizer | Habilitar e aguardar 24h |
| Dry-run ativo (findings não salvos) | Verificar `GREENOPS_DRY_RUN` na Lambda | Esperado — findings são simulados |

---

### Erro: Email de aprovação não chega

**Sintoma:** Remediações ficam em `PENDING_APPROVAL` mas nenhum email é recebido.

**Verificações:**
```bash
# 1. Confirma se a subscrição SNS está confirmada
aws sns list-subscriptions-by-topic \
  --topic-arn $(aws sns list-topics --query 'Topics[?contains(TopicArn,`approvals`)].TopicArn' --output text) \
  --query 'Subscriptions[*].[Protocol,Endpoint,SubscriptionArn]' \
  --output table
# SubscriptionArn deve ser um ARN real, não "PendingConfirmation"

# 2. Verifica se o tópico SNS existe
aws sns list-topics --query 'Topics[?contains(TopicArn,`greenops`)]'

# 3. Testa publicação manual
aws sns publish \
  --topic-arn arn:aws:sns:us-east-1:123456789012:greenops-approvals-dev \
  --subject "Teste GreenOps" \
  --message "Mensagem de teste"
```

**Solução:** Reconfirme a subscrição SNS acessando o link no email original ou reinscreva o email via console SNS.

---

### Erro: `NoSuchBucket` ao salvar relatório

**Sintoma:** Logs do Reporting mostram `NoSuchBucket` ao tentar salvar em S3.

**Causa:** O bucket S3 não foi criado (falha silenciosa no deploy) ou o nome está incorreto.

```bash
# Verifica se o bucket existe
aws s3 ls | grep greenops-reports

# Verifica o nome correto do bucket (inclui account ID)
aws cloudformation describe-stack-resource \
  --stack-name greenops-auto-remediator-dev \
  --logical-resource-id ReportsBucket \
  --query 'StackResourceDetail.PhysicalResourceId'

# Atualiza a variável de ambiente da Lambda com o nome correto
BUCKET_NAME=$(aws cloudformation describe-stack-resource \
  --stack-name greenops-auto-remediator-dev \
  --logical-resource-id ReportsBucket \
  --query 'StackResourceDetail.PhysicalResourceId' \
  --output text)

aws lambda update-function-configuration \
  --function-name greenops-reporting-dev \
  --environment "Variables={REPORTS_BUCKET=$BUCKET_NAME,...}"
```

---

### Remover o GreenOps completamente

```bash
# 1. Esvazia o bucket S3 (obrigatório antes de deletar a stack)
BUCKET=$(aws cloudformation describe-stack-resource \
  --stack-name greenops-auto-remediator-dev \
  --logical-resource-id ReportsBucket \
  --query 'StackResourceDetail.PhysicalResourceId' \
  --output text)

aws s3 rm s3://$BUCKET --recursive

# 2. Deleta a stack (DynamoDB e S3 têm DeletionPolicy=Retain — dados preservados)
aws cloudformation delete-stack \
  --stack-name greenops-auto-remediator-dev

# 3. Aguarda a deleção
aws cloudformation wait stack-delete-complete \
  --stack-name greenops-auto-remediator-dev

echo "Stack removida. Tabelas DynamoDB e bucket S3 foram preservados (DeletionPolicy=Retain)."
echo "Para remover os dados também, delete manualmente as tabelas e o bucket."
```

> **Nota:** As tabelas DynamoDB e o bucket S3 têm `DeletionPolicy: Retain` — eles **não são deletados** junto com a stack. Isso protege dados históricos de findings e relatórios em caso de remoção acidental da stack.
